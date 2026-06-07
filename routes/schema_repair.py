"""Phase FF+25-followup-r25 (2026-05-20) — schema repair + geocoding + funnel diag.
==========================================================================

Brief #2 from the Inspector surfaced three concrete issues:

  1. `brain_findings` table doesn't exist — consistency radar can't
     persist findings, so the brain can't learn from history.
  2. `worker_versions` table doesn't exist — worker drift detector blind.
  3. `press_releases.published_at` column missing on some deploys.
  4. 25% of facilities (3,187 rows) have country='?' — geocoding gap.
  5. MCP funnel shows 99.8% drop-off from upgrade signals to final
     paid conversion — needs diagnostic surface.

This module ships three endpoints to address each:

  POST /api/v1/admin/schema/repair         create missing tables + cols
  POST /api/v1/admin/geocoding/backfill    fill country='?' rows
  GET  /api/v1/admin/funnel/leakage        per-stage drop-off detail
"""
import os
from internal_auth import accepted_internal_keys
import logging
import datetime
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
schema_repair_bp = Blueprint("schema_repair", __name__)


_INTERNAL_KEYS = accepted_internal_keys()
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.headers.get("X-Admin-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


def _get_db():
    try:
        from main import get_db
        return get_db()
    except Exception:
        return None


# ── Schema repair ────────────────────────────────────────────────────
# Each entry: (label, list of idempotent SQL statements to run).
# All wrapped in try/except so one bad statement doesn't block the rest.
SCHEMA_STATEMENTS = [
    ("brain_findings table", [
        """CREATE TABLE IF NOT EXISTS brain_findings (
            id           SERIAL PRIMARY KEY,
            issue        TEXT NOT NULL,
            url          TEXT,
            count        INTEGER NOT NULL DEFAULT 1,
            detail       TEXT,
            detector     TEXT,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at  TIMESTAMPTZ,
            status       TEXT NOT NULL DEFAULT 'open'
        )""",
        "CREATE INDEX IF NOT EXISTS ix_brain_findings_issue ON brain_findings(issue)",
        "CREATE INDEX IF NOT EXISTS ix_brain_findings_created ON brain_findings(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_brain_findings_status ON brain_findings(status)",
    ]),
    ("worker_versions table", [
        """CREATE TABLE IF NOT EXISTS worker_versions (
            id           SERIAL PRIMARY KEY,
            version      TEXT NOT NULL,
            observed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            source       TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS ix_worker_versions_observed ON worker_versions(observed_at DESC)",
    ]),
    ("observability_metrics table", [
        # r32-mt-fix (2026-05-21): Railway logs showed
        #   PG query failed: INSERT INTO observability_metrics
        #   error: relation "observability_metrics" does not exist
        # firing on every brain cycle. Created the table so the metric
        # writes succeed and the brain's L20 durability surface has a
        # place to land.
        """CREATE TABLE IF NOT EXISTS observability_metrics (
            id          SERIAL PRIMARY KEY,
            metric      TEXT NOT NULL,
            value       NUMERIC,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS ix_observability_metrics_metric ON observability_metrics(metric)",
        "CREATE INDEX IF NOT EXISTS ix_observability_metrics_recorded ON observability_metrics(recorded_at DESC)",
    ]),
    ("substations.UNIQUE constraint", [
        # r32-mt-fix (2026-05-21): Railway logs showed
        #   error: there is no unique or exclusion constraint matching
        #   the ON CONFLICT specification
        # firing on every infrastructure sync because the INSERT uses
        # ON CONFLICT (name, lat, lng) but no such unique index exists.
        # CREATE UNIQUE INDEX IF NOT EXISTS is idempotent — safe to
        # apply even if a partial duplicate exists in the table.
        # We exclude rows where lat/lng are NULL since those can't
        # have a stable identity. If duplicates exist, the index
        # creation will FAIL — that's expected and the migration
        # statement is wrapped in try/except so the rest continue.
        """CREATE UNIQUE INDEX IF NOT EXISTS ix_substations_name_lat_lng
           ON substations(name, lat, lng)
           WHERE lat IS NOT NULL AND lng IS NOT NULL""",
    ]),
    ("mcp_upgrade_signals.outreach_sent_at column", [
        # r32-conv (2026-05-20): outreach campaign tracking. Records
        # when the upgrade-pool email actually landed (not just that
        # it was queued). Lets us correlate send timing → conversion
        # in the funnel analytics.
        "ALTER TABLE mcp_upgrade_signals ADD COLUMN IF NOT EXISTS outreach_sent_at TIMESTAMPTZ",
    ]),
    ("users.pocket_preferences column", [
        # r33 (2026-05-20): single JSONB column for profile-driven
        # /api/v1/pockets/for-me re-ranking. Stores target_mw,
        # preferred_iso, preferred_state, within_ttp, workload_type.
        # JSONB lets us add new dimensions (carbon target, latency
        # threshold, water risk tolerance) without re-migrating.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS pocket_preferences JSONB DEFAULT '{}'::jsonb",
    ]),
    ("users.dunning_counters columns", [
        # Phase r26 (2026-05-20): tracks dunning state per customer so
        # handle_payment_failed can decide whether to demote API rate
        # limits without yanking the key entirely. Real customers (with
        # at least one successful invoice) ride out Stripe's full
        # retry cycle untouched; first-charge-never-succeeded freeloaders
        # get demoted to 'free' rate limit on the 2nd consecutive failure
        # while keeping their key alive so dunning emails still work.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS invoices_paid_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_failed_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS demoted_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS demoted_reason TEXT",
    ]),
    ("users.tier_expiry + source_plan columns", [
        # r65-annual-onetime (2026-06-06): one-time Pro Annual link
        # (price_1TecqhJ9ey2ATcQl4Hmp99OU, $1,188) fires
        # checkout.session.completed with mode='payment' — there is NO
        # follow-up customer.subscription.* event with current_period_end,
        # so without a stored expiry the user would keep tier='pro'
        # FOREVER. tier_expires_at gets stamped to NOW()+365d on the
        # one-time path; subscription-mode buyers leave it NULL (their
        # expiry is implicit in subscription_status / Stripe's renewal).
        # source_plan lets renewal-nudge crons target one-time annual
        # buyers ~330 days in without hitting recurring subscribers.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS tier_expires_at TIMESTAMPTZ",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS source_plan TEXT",
        "CREATE INDEX IF NOT EXISTS ix_users_tier_expires_at ON users(tier_expires_at) WHERE tier_expires_at IS NOT NULL",
    ]),
    ("renewal_nudge_log table", [
        # r65-renewal-nudge (2026-06-06): per-send log for the day-330
        # renewal nudge cron (routes/renewal_nudge.py). Powers the 7-day
        # cooldown ("NOT EXISTS in last 7 days") AND a public history
        # endpoint. user_id is TEXT to match the existing users.id
        # convention (Stripe-prefixed strings, not BIGSERIAL).
        # routes/renewal_nudge._ensure_schema also runs this at request
        # time as a belt-and-suspenders for cold deploys.
        """CREATE TABLE IF NOT EXISTS renewal_nudge_log (
            id                       BIGSERIAL PRIMARY KEY,
            user_id                  TEXT NOT NULL,
            email                    TEXT NOT NULL,
            sent_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            days_remaining_at_send   INT,
            resend_message_id        TEXT,
            status                   TEXT NOT NULL DEFAULT 'sent',
            delivery_info            TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS ix_renewal_nudge_log_user_sent ON renewal_nudge_log(user_id, sent_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_renewal_nudge_log_email_sent ON renewal_nudge_log(email, sent_at DESC)",
    ]),
    ("campaign_log table", [
        # r66-halfprice-annual (2026-06-06): per-campaign send log for
        # the half-price annual upgrade outreach (routes/campaign_halfprice_annual.py).
        # UNIQUE(campaign_name, email) makes re-firing the same campaign
        # a no-op for already-sent rows — idempotent by construction.
        # Used by ANY future campaign endpoint that needs "never email the
        # same person twice for the same blast" semantics, not just the
        # halfprice_annual_2026_06 campaign.
        """CREATE TABLE IF NOT EXISTS campaign_log (
            id              SERIAL PRIMARY KEY,
            campaign_name   TEXT NOT NULL,
            user_id         TEXT,
            email           TEXT NOT NULL,
            sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resend_message_id TEXT,
            payload         JSONB,
            UNIQUE(campaign_name, email)
        )""",
        "CREATE INDEX IF NOT EXISTS ix_campaign_log_campaign ON campaign_log(campaign_name)",
        "CREATE INDEX IF NOT EXISTS ix_campaign_log_sent_at ON campaign_log(sent_at DESC)",
    ]),
    ("press_releases.published_at column", [
        # If the column doesn't exist, ALTER TABLE ADD COLUMN IF NOT
        # EXISTS is idempotent. Some deploys may have it as
        # published_date instead — we add published_at unconditionally
        # so the column the brain expects always exists.
        "ALTER TABLE press_releases ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ",
        # Backfill from published_date if both exist
        """DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'press_releases'
                   AND column_name = 'published_date'
            ) THEN
                UPDATE press_releases
                   SET published_at = published_date::timestamptz
                 WHERE published_at IS NULL
                   AND published_date IS NOT NULL;
            END IF;
        END $$""",
    ]),
    ("discovered_facilities operators index", [
        # slow_pages quick-win: /operators groups by provider over
        # active rows. Partial index on (provider, status) makes the
        # filter cheap. IF NOT EXISTS is idempotent.
        """CREATE INDEX IF NOT EXISTS idx_disc_provider_active
           ON discovered_facilities(provider, status)
           WHERE status IN ('active','Operational')""",
    ]),
    ("url_emissions table (media_no_404 chokepoint)", [
        # Phase media_no_404 (2026-06-02): single registry for every
        # public URL emitted by DC Hub Media (linkedin/x/email/press/rss).
        # Pre-post HEAD lives in routes/url_registry.emit; the 5-min cron
        # /api/v1/cron/url-smoke recomputes HEAD on every published row
        # in the last 7d and auto-revokes LinkedIn on dead_404.
        """CREATE TABLE IF NOT EXISTS url_emissions (
            id              SERIAL PRIMARY KEY,
            channel         TEXT NOT NULL,
            kind            TEXT NOT NULL,
            slug            TEXT NOT NULL,
            url             TEXT NOT NULL,
            state           TEXT NOT NULL DEFAULT 'planned',
            pre_head_status INT,
            ref_id          TEXT,
            payload         JSONB,
            last_smoke_at   TIMESTAMPTZ,
            smoke_status    INT,
            revoked_at      TIMESTAMPTZ,
            revoke_reason   TEXT,
            republished_at  TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            published_at    TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS ix_url_emissions_state ON url_emissions(state)",
        "CREATE INDEX IF NOT EXISTS ix_url_emissions_url ON url_emissions(url)",
        "CREATE INDEX IF NOT EXISTS ix_url_emissions_published ON url_emissions(published_at DESC)",
        """CREATE UNIQUE INDEX IF NOT EXISTS ix_url_emissions_channel_url_active
           ON url_emissions(channel, url)
           WHERE state IN ('planned','published')""",
    ]),
    ("mcp_funnel canonical identity + view", [
        # r68-canonical (2026-06-02): collapse the per-file synthetic-client
        # filter (5 Python copies that drifted across releases) into a SINGLE
        # SQL view definition. Every reader (funnel diag, visitor intel,
        # outreach, brain consistency radar) queries the view, not the raw
        # table. Every writer (mcp_signal_canonical.record_signal) inserts
        # everything; reads gate via is_synthetic.
        "ALTER TABLE mcp_upgrade_signals ADD COLUMN IF NOT EXISTS caller_id TEXT",
        """CREATE INDEX IF NOT EXISTS idx_mcp_signals_caller_id
            ON mcp_upgrade_signals(caller_id) WHERE caller_id IS NOT NULL""",
        """CREATE INDEX IF NOT EXISTS idx_mcp_signals_session_id
            ON mcp_upgrade_signals(session_id)
            WHERE session_id IS NOT NULL AND session_id <> ''""",
        """CREATE INDEX IF NOT EXISTS idx_mcp_signals_converted_at
            ON mcp_upgrade_signals(converted_at) WHERE converted_at IS NOT NULL""",
        # Backfill caller_id for existing rows so retroactive attribution works.
        """UPDATE mcp_upgrade_signals
             SET caller_id = COALESCE(
                    NULLIF(LOWER(TRIM(user_email)), ''),
                    NULLIF(session_id, ''),
                    'anon:' || md5(COALESCE(LOWER(mcp_client),'') || '|'
                                  || SUBSTRING(COALESCE(user_agent,'') FROM 1 FOR 120) || '|'
                                  || COALESCE(ip_address,''))
                 )
           WHERE caller_id IS NULL""",
        "ALTER TABLE mcp_conversions ADD COLUMN IF NOT EXISTS caller_id TEXT",
        """UPDATE mcp_conversions SET caller_id = LOWER(TRIM(user_email))
            WHERE caller_id IS NULL AND user_email IS NOT NULL""",
        "CREATE INDEX IF NOT EXISTS idx_mcp_conv_caller_id ON mcp_conversions(caller_id)",
        # The CANONICAL view: every reader queries this, not the raw table.
        "DROP VIEW IF EXISTS mcp_funnel_callers CASCADE",
        "DROP VIEW IF EXISTS mcp_funnel_real CASCADE",
        "DROP VIEW IF EXISTS mcp_funnel_canonical CASCADE",
        """CREATE OR REPLACE VIEW mcp_funnel_canonical AS
            SELECT
              s.id, s.created_at, s.signal_type, s.tool_requested,
              s.tier_current, s.tier_required,
              s.session_id, s.user_email, s.ip_address,
              s.mcp_client, s.user_agent, s.caller_id,
              s.converted, s.converted_at, s.outreach_sent, s.outreach_sent_at,
              CASE
                WHEN COALESCE(LOWER(s.mcp_client),'') IN (
                  'node','dchub-selfheal','dchub-mcp-test','mcp-probe','mcp-test',
                  'pipeline_mcp','canary','mcp-remote-fallback-test',
                  'registry-health-checker','mcp-shield-scanner','yellowmcp-health',
                  'glama-health','chiark-prober','fabrique-noauth-probe',
                  'agentpulse','mcpscoringengine','mcp-extractor',
                  'curl','python-script','node-script','postman','insomnia','verify'
                ) THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'loop%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'dchub-%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'local-agent-mode%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'leakaudit%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'trial-leak%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE '%-probe' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE '%-health' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE '%-scanner' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE '%-checker' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'step2_%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'qa-%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'r5_%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'r6_%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'hn-prepost%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'paywall-probe%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'funnel-test%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'e2e-%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'recheck%' THEN TRUE
                WHEN COALESCE(LOWER(s.mcp_client),'') LIKE 'healthcheck%' THEN TRUE
                ELSE FALSE
              END AS is_synthetic
            FROM mcp_upgrade_signals s""",
        """CREATE OR REPLACE VIEW mcp_funnel_real AS
            SELECT * FROM mcp_funnel_canonical WHERE is_synthetic = FALSE""",
        """CREATE OR REPLACE VIEW mcp_funnel_callers AS
            SELECT
              caller_id,
              MIN(created_at) AS first_signal_at,
              MAX(created_at) AS last_signal_at,
              COUNT(*) AS signal_count,
              BOOL_OR(converted) AS converted,
              MIN(converted_at) AS converted_at,
              BOOL_OR(outreach_sent) AS outreached,
              MAX(user_email) FILTER (WHERE user_email IS NOT NULL AND user_email <> '') AS email,
              MAX(mcp_client) AS mcp_client,
              array_agg(DISTINCT tool_requested) FILTER (WHERE tool_requested IS NOT NULL) AS tools
            FROM mcp_funnel_real
            WHERE caller_id IS NOT NULL
            GROUP BY caller_id""",
    ]),
    ("brain_pending_html_fixes (Item D: dry-run HTML insertion queue)", [
        # Item D (2026-06-02): brain L4 now stages HTML insertion fixes in
        # this table at status='dry_run' once two fuzzy-matched proposals
        # cross the 2-cycle gate. NOTHING auto-applies — surface via
        # /api/v1/brain/pending-html-fixes so a human (or a later
        # explicit approval cycle) can promote dry_run -> applied.
        """CREATE TABLE IF NOT EXISTS brain_pending_html_fixes (
            id              BIGSERIAL PRIMARY KEY,
            page_url        TEXT,
            find_text       TEXT,
            replace_text    TEXT,
            rationale       TEXT,
            status          TEXT DEFAULT 'dry_run',
            approval_count  INT  DEFAULT 0,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            applied_at      TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS ix_brain_pending_html_status ON brain_pending_html_fixes(status)",
        "CREATE INDEX IF NOT EXISTS ix_brain_pending_html_created ON brain_pending_html_fixes(created_at DESC)",
    ]),
    ("market_gas_pricing (Powered Land — Gas Pricing PRO surface, Phase 1)", [
        # Powered Land Gas Economics (2026-06-04, Phase 1 spine):
        # Per-DCPI-market gas pricing stack — Henry Hub spot, regional
        # basis differential at the nearest pricing hub (Algonquin/
        # Transco Z6/Tetco M3/Chicago Citygate/SoCal Border/Waha/etc.),
        # delivered industrial $/MMBtu, delivered electric-power $/MMBtu,
        # and the heat-rate-derived gas-to-grid $/MWh for 5 scenarios
        # (new CCGT 6,400 Btu/kWh → old peaker 12,000 Btu/kWh).
        # Refreshed nightly by /api/v1/markets/gas-pricing/cron (admin-gated).
        # The eia_gas_prices table (already populated by eia_gas_bulk_loader)
        # supplies the state-level delivered tariffs; this table caches
        # the per-market roll-up + the derived $/MWh so the public
        # /api/v1/markets/<slug>/gas-pricing endpoint is one SELECT.
        """CREATE TABLE IF NOT EXISTS market_gas_pricing (
            market_slug              TEXT PRIMARY KEY,
            market_name              TEXT NOT NULL,
            state                    TEXT,
            pricing_hub_key          TEXT,
            pricing_hub_name         TEXT,
            henry_hub_spot_usd_mmbtu NUMERIC(8,3),
            basis_diff_usd_mmbtu     NUMERIC(8,3),
            hub_spot_usd_mmbtu       NUMERIC(8,3),
            delivered_industrial_usd_mmbtu NUMERIC(8,3),
            delivered_electric_usd_mmbtu   NUMERIC(8,3),
            usd_per_mwh_cc_new       NUMERIC(8,2),
            usd_per_mwh_cc_avg       NUMERIC(8,2),
            usd_per_mwh_cc_old       NUMERIC(8,2),
            usd_per_mwh_peaker       NUMERIC(8,2),
            usd_per_mwh_peaker_old   NUMERIC(8,2),
            hh_source                TEXT,
            basis_source             TEXT,
            delivered_source         TEXT,
            data_basis               TEXT,
            fetched_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            period                   TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS ix_mgp_state ON market_gas_pricing(state)",
        "CREATE INDEX IF NOT EXISTS ix_mgp_hub ON market_gas_pricing(pricing_hub_key)",
        "CREATE INDEX IF NOT EXISTS ix_mgp_usdmwh_cc_avg ON market_gas_pricing(usd_per_mwh_cc_avg)",
        "CREATE INDEX IF NOT EXISTS ix_mgp_fetched ON market_gas_pricing(fetched_at DESC)",
    ]),
    ("mcp_session_upgrades (Fix E — Stripe client_reference_id → MCP session_id binding)", [
        # Fix E (2026-06-06): close the conversion loop end-to-end inside the
        # caller's in-flight MCP session. The MCP server now appends
        # ?client_reference_id=<Mcp-Session-Id> to every Stripe Checkout URL it
        # emits in a paywall response (server.mjs/_stripeWithSession). When the
        # human completes Stripe Checkout, the checkout.session.completed
        # webhook (handle_checkout_completed in main.py) writes a row here.
        # The /api/v1/mcp/trial-check route (flask_mcp_endpoints.py) then
        # returns tier_upgrade=<plan> on the very next tool call from the same
        # Mcp-Session-Id — server.mjs flips the in-memory tier in-place,
        # re-runs the gate, and serves full data immediately. No key swap,
        # no reconnect, no waiting for the api_key table to propagate.
        # consumed_at is stamped on the first successful read so we don't
        # re-flip tier_upgrade on every subsequent call in the same session.
        """CREATE TABLE IF NOT EXISTS mcp_session_upgrades (
            mcp_session_id     TEXT PRIMARY KEY,
            user_id            TEXT,
            user_email         TEXT,
            plan               TEXT,
            stripe_session_id  TEXT,
            stripe_customer_id TEXT,
            amount_cents       INTEGER,
            upgraded_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            consumed_at        TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS ix_mcp_session_upgrades_upgraded ON mcp_session_upgrades(upgraded_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_mcp_session_upgrades_email ON mcp_session_upgrades(user_email)",
        "CREATE INDEX IF NOT EXISTS ix_mcp_session_upgrades_stripe ON mcp_session_upgrades(stripe_session_id)",
    ]),
    ("auto_interconnect_findings (3-stage pipeline notification queue)", [
        # Auto-interconnect (2026-06-04): novel-UA + pending-bucket findings
        # surfaced for admin approval. Single-use approve_token; TTL purge
        # at 30d (unapproved) / 90d (approved). status in: pending, approved,
        # dismissed, expired. NEVER auto-promoted past stage (b) — outbound
        # email / dev-key mint require status='approved'.
        """CREATE TABLE IF NOT EXISTS auto_interconnect_findings (
            id              BIGSERIAL PRIMARY KEY,
            user_agent      VARCHAR(500) NOT NULL,
            vendor          TEXT,
            version         TEXT,
            inferred_slug   TEXT,
            company         TEXT,
            color           TEXT,
            source          TEXT NOT NULL DEFAULT 'novel_ua',
            hits_14d        INTEGER NOT NULL DEFAULT 0,
            distinct_ips    INTEGER NOT NULL DEFAULT 0,
            first_seen      TIMESTAMPTZ,
            last_seen       TIMESTAMPTZ,
            approve_token   TEXT UNIQUE,
            status          TEXT NOT NULL DEFAULT 'pending',
            approved_by_ip  TEXT,
            approved_by_ua  TEXT,
            approved_at     TIMESTAMPTZ,
            promoted_steps  TEXT,
            ttl_at          TIMESTAMPTZ,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_aif_ua_unique ON auto_interconnect_findings(user_agent) WHERE status IN ('pending','approved')",
        "CREATE INDEX IF NOT EXISTS ix_aif_status ON auto_interconnect_findings(status)",
        "CREATE INDEX IF NOT EXISTS ix_aif_token ON auto_interconnect_findings(approve_token)",
        "CREATE INDEX IF NOT EXISTS ix_aif_created ON auto_interconnect_findings(created_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_aif_ttl ON auto_interconnect_findings(ttl_at) WHERE status='pending'",
    ]),
    ("connect_landing_views table", [
        # Phase r80-mcp-connect (2026-06-06): per-MCP-client landing-page
        # telemetry — routes/mcp_connect.py serves /connect/<cursor|cline|
        # continue|claude-desktop>. One row per page view. The minted trial
        # key is patched in via POST /api/v1/connect/mint-update so we can
        # attribute "this conversion came from /connect/cursor's mint".
        """CREATE TABLE IF NOT EXISTS connect_landing_views (
            id              BIGSERIAL PRIMARY KEY,
            client          TEXT NOT NULL,
            viewed_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            user_agent      TEXT,
            referer         TEXT,
            ip              TEXT,
            key_minted_for  TEXT,
            key_minted_at   TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS ix_connect_lv_client ON connect_landing_views(client)",
        "CREATE INDEX IF NOT EXISTS ix_connect_lv_viewed ON connect_landing_views(viewed_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_connect_lv_keyfor ON connect_landing_views(key_minted_for) WHERE key_minted_for IS NOT NULL",
    ]),
    ("r68-funnel-stamping columns", [
        # r68-funnel-stamping (2026-06-06): close the two measurement leaks
        # the /admin/funnel-health dashboard surfaced — 18 codes minted but
        # 0 redeem-page views and 11 conversions but 0 stripe-click stamps.
        # The redeem_viewed_at stamp was already fired by pair_code.redeem
        # _landing but no per-view counter existed. The stripe_clicked_at
        # stamp died at sendBeacon races (browser navigation to Stripe ran
        # in the same tick and dropped the beacon). Both fixes need new
        # columns:
        #   • mcp_pair_codes.redeem_view_count  — counter for repeat views
        #     (the canonical landing keeps was_first_view semantics; this
        #     adds a "this human looked at it N times" signal for the
        #     conversion-funnel dashboard).
        #   • mcp_pair_codes.stripe_click_count — same idea for the new
        #     server-side click proxy (/api/v1/redeem/<code>/click).
        #   • connect_landing_views.stripe_clicked_at + stripe_clicked_plan
        #     — added by the /api/v1/connect/click proxy that stamps
        #     before 302ing to Stripe.
        # All ADD COLUMN IF NOT EXISTS so re-runs are idempotent.
        "ALTER TABLE mcp_pair_codes ADD COLUMN IF NOT EXISTS redeem_view_count INTEGER DEFAULT 0",
        "ALTER TABLE mcp_pair_codes ADD COLUMN IF NOT EXISTS stripe_click_count INTEGER DEFAULT 0",
        "ALTER TABLE connect_landing_views ADD COLUMN IF NOT EXISTS stripe_clicked_at TIMESTAMPTZ",
        "ALTER TABLE connect_landing_views ADD COLUMN IF NOT EXISTS stripe_clicked_plan TEXT",
        "CREATE INDEX IF NOT EXISTS ix_connect_lv_clicked ON connect_landing_views(stripe_clicked_at) WHERE stripe_clicked_at IS NOT NULL",
    ]),
    ("state_visitor_intent table (2026-06-07 R2 — state-of-2026 web-visitor claim)", [
        # 2026-06-07 Round 2: state-of-2026 web visitors. Page-side JS bumps
        # brief_clicks / time_on_page_seconds via /api/v1/state-of-2026/track-event;
        # when EITHER crosses threshold (2 clicks OR 60s), the modal fires
        # /api/v1/state-of-2026/claim-email which mints a trial key + emails
        # it. visitor_session_id UNIQUE so retries can't double-mint.
        # Mirrors _SCHEMA_SQL in routes/state_visitor_claim.py so a fresh
        # deploy that hasn't called the endpoint yet still has the table.
        """CREATE TABLE IF NOT EXISTS state_visitor_intent (
            id                       BIGSERIAL PRIMARY KEY,
            visitor_session_id       TEXT NOT NULL UNIQUE,
            brief_clicks             INTEGER NOT NULL DEFAULT 0,
            time_on_page_seconds     INTEGER NOT NULL DEFAULT 0,
            brief_slugs              TEXT,
            hi_threshold_hit_at      TIMESTAMPTZ,
            claim_token              TEXT,
            claim_minted_at          TIMESTAMPTZ,
            claim_used_at            TIMESTAMPTZ,
            email                    TEXT,
            minted_api_key           TEXT,
            ua                       TEXT,
            referer                  TEXT,
            ip_hash                  TEXT,
            first_seen_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_event_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS ix_svi_hi_hit ON state_visitor_intent(hi_threshold_hit_at DESC) WHERE hi_threshold_hit_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_svi_used ON state_visitor_intent(claim_used_at DESC) WHERE claim_used_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_svi_email ON state_visitor_intent(LOWER(email)) WHERE email IS NOT NULL",
    ]),
    ("mcp_high_intent_sessions table (2026-06-07 session-bound 3-strike claim)", [
        # 2026-06-07: session-bound HIGH-INTENT capture. When an MCP session
        # hits the SAME paid tool 3+ times in 24h, the mcp-server fires a
        # claim_token via /api/v1/mcp/should-mint-claim. The token is HMAC-
        # signed (DCHUB_HMAC_SECRET) — DB row exists for idempotency
        # (claim_minted_at, claim_used_at) + funnel attribution, not for
        # token validity. UNIQUE(mcp_session_id, tool_name) guarantees a
        # retrying agent gets the SAME token (not N different links).
        # Mirrors the _SCHEMA_SQL block in routes/mcp_high_intent_claim.py
        # so a fresh deploy that hasn't called the endpoint yet still has
        # the table (the public stats route doesn't trigger _ensure_schema).
        """CREATE TABLE IF NOT EXISTS mcp_high_intent_sessions (
            id                   BIGSERIAL PRIMARY KEY,
            mcp_session_id       TEXT NOT NULL,
            tool_name            TEXT NOT NULL,
            paid_call_count_24h  INTEGER NOT NULL DEFAULT 0,
            first_hit_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_hit_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            claim_token          TEXT,
            claim_minted_at      TIMESTAMPTZ,
            claim_used_at        TIMESTAMPTZ,
            claim_email          TEXT,
            minted_api_key       TEXT,
            user_agent           TEXT,
            mcp_client           TEXT,
            claim_variant        TEXT
        )""",
        # Round 2 (2026-06-07): idempotent ALTER for rows that pre-date variant
        # tracking. Existing rows backfill to 'generic' so the per-variant
        # breakdown isn't NULL-poisoned.
        "ALTER TABLE mcp_high_intent_sessions ADD COLUMN IF NOT EXISTS claim_variant TEXT",
        "UPDATE mcp_high_intent_sessions SET claim_variant = 'generic' WHERE claim_variant IS NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_mhis_sid_tool ON mcp_high_intent_sessions(mcp_session_id, tool_name)",
        "CREATE INDEX IF NOT EXISTS ix_mhis_minted_at ON mcp_high_intent_sessions(claim_minted_at DESC) WHERE claim_minted_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_mhis_used_at ON mcp_high_intent_sessions(claim_used_at DESC) WHERE claim_used_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_mhis_token ON mcp_high_intent_sessions(claim_token) WHERE claim_token IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_mhis_variant ON mcp_high_intent_sessions(claim_variant) WHERE claim_variant IS NOT NULL",
    ]),
    ("state_of_2026 living-doc tables (clicks/pageviews/claim_proposals)", [
        # 2026-06-07 Round-1 cleanup: the boot-init hook
        # state_of_2026_live.init_state_of_2026_tables() creates these on
        # first boot, but /admin/state-of-2026-pulse 500s on any deploy
        # that booted before the routes/state_of_2026_live.py module
        # landed (the table CREATE only runs through the live module's
        # init hook). Mirroring the DDL here so POST /schema/repair is
        # the single recovery lever for cold-deploy schema gaps.
        # Idempotent CREATE TABLE IF NOT EXISTS.
        """CREATE TABLE IF NOT EXISTS state_of_2026_pageviews (
            id            BIGSERIAL PRIMARY KEY,
            ip_hash       TEXT,
            referer       TEXT,
            ua            TEXT,
            session_id    TEXT,
            ts            TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS state_of_2026_pageviews_ts_idx ON state_of_2026_pageviews(ts)",
        """CREATE TABLE IF NOT EXISTS state_of_2026_clicks (
            id            BIGSERIAL PRIMARY KEY,
            token         TEXT NOT NULL,
            destination   TEXT NOT NULL,
            referer       TEXT,
            ip_hash       TEXT,
            ua            TEXT,
            session_id    TEXT,
            ts            TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS state_of_2026_clicks_token_ts_idx ON state_of_2026_clicks(token, ts)",
        """CREATE TABLE IF NOT EXISTS state_of_2026_claim_proposals (
            id              BIGSERIAL PRIMARY KEY,
            claim_lineno    INTEGER,
            current_text    TEXT,
            proposed_text   TEXT,
            current_min     DOUBLE PRECISION,
            current_max     DOUBLE PRECISION,
            proposed_min    DOUBLE PRECISION,
            proposed_max    DOUBLE PRECISION,
            live_value      DOUBLE PRECISION,
            reason          TEXT,
            status          TEXT NOT NULL DEFAULT 'pending',
            ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            applied_ts      TIMESTAMPTZ
        )""",
        "CREATE INDEX IF NOT EXISTS state_of_2026_claim_props_status_idx ON state_of_2026_claim_proposals(status, ts)",
    ]),
    ("brain_strategic_recommendations table", [
        # Brain Layer-6 Strategic Synthesis (2026-06-06): weekly Claude-
        # backed pass that reads funnel + page health + competitor signal +
        # customer feedback + stuck-issue queue, then writes 3-7 strategic
        # recommendations for the human to review. Each row maps 1:1 to one
        # bullet in the Monday digest email. Status starts 'new'; flips to
        # 'pr_drafted' when brain_strategic_planner opens the scaffold PR;
        # 'shipped' is operator-set after merge. The strategy_payload column
        # holds the full Claude response so we can re-render the digest
        # without re-paying the API call.
        """CREATE TABLE IF NOT EXISTS brain_strategic_recommendations (
            id                BIGSERIAL PRIMARY KEY,
            run_id            TEXT NOT NULL,
            week_of           DATE NOT NULL,
            kind              TEXT NOT NULL,
            title             TEXT NOT NULL,
            spec_md           TEXT,
            file_scaffold     TEXT,
            dollar_lift_est   NUMERIC,
            confidence        NUMERIC,
            evidence_keys     TEXT,
            pr_url            TEXT,
            pr_number         INTEGER,
            status            TEXT NOT NULL DEFAULT 'new',
            strategy_payload  JSONB,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS ix_bsr_week_of ON brain_strategic_recommendations(week_of DESC)",
        "CREATE INDEX IF NOT EXISTS ix_bsr_run_id ON brain_strategic_recommendations(run_id)",
        "CREATE INDEX IF NOT EXISTS ix_bsr_kind ON brain_strategic_recommendations(kind)",
        "CREATE INDEX IF NOT EXISTS ix_bsr_status ON brain_strategic_recommendations(status)",
    ]),
    ("brain_pr_outcomes table", [
        # Brain ROUND 2 (2026-06-07): closes the loop on Layer-5 draft PRs.
        # When the brain opens a draft PR it currently STOPS there — the
        # operator clicks Merge, but the brain never finds out whether the
        # patch actually fixed anything or regressed sentinel. This table
        # records the outcome: on every brain-authored merged PR we wait
        # for Railway deploy, re-probe the touched endpoint via sentinel,
        # and write a success/regression row. The strategic synthesis then
        # reads the last 30d of outcomes so the brain LEARNS from its own
        # track record ("last time I proposed X for finding Y, sentinel
        # regressed B → A — try Z this time").
        #
        # outcome values:
        #   - 'draft'      → PR opened, never merged (still in flight)
        #   - 'success'    → merged + deploy ok + sentinel grade equal/better
        #   - 'regression' → merged + sentinel grade worse OR endpoint 5xx
        #   - 'deploy_fail'→ merge ok but Railway deploy failed/timed out
        #   - 'unknown'    → merged but sentinel had no baseline / couldn't probe
        """CREATE TABLE IF NOT EXISTS brain_pr_outcomes (
            id                       BIGSERIAL PRIMARY KEY,
            pr_number                INTEGER NOT NULL,
            pr_url                   TEXT,
            pr_title                 TEXT,
            branch                   TEXT,
            merged_at                TIMESTAMPTZ,
            commit_sha               TEXT,
            deploy_at                TIMESTAMPTZ,
            deploy_status            TEXT,
            sentinel_endpoint        TEXT,
            sentinel_before_grade    NUMERIC,
            sentinel_after_grade     NUMERIC,
            outcome                  TEXT NOT NULL DEFAULT 'draft',
            regression_details       TEXT,
            files_changed            TEXT,
            brain_authored           BOOLEAN NOT NULL DEFAULT FALSE,
            learned_at               TIMESTAMPTZ,
            created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_bpo_pr_number ON brain_pr_outcomes(pr_number)",
        "CREATE INDEX IF NOT EXISTS ix_bpo_outcome ON brain_pr_outcomes(outcome)",
        "CREATE INDEX IF NOT EXISTS ix_bpo_merged_at ON brain_pr_outcomes(merged_at DESC) WHERE merged_at IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_bpo_brain_authored ON brain_pr_outcomes(brain_authored) WHERE brain_authored = TRUE",
    ]),
    ("brain_self_perception table", [
        # Task #161 (2026-06-07): brain reads its own dashboards.
        # Closes the verification loop. Once a day at 04:00 UTC the brain
        # fans out to every admin dashboard, asks Claude for an honest
        # wins/losses/adjustments self-assessment. Persists ONE row per
        # UTC date (UNIQUE on ran_on) so the daily cap is enforced at the
        # DB layer. Strategic synthesis L6 reads back via
        # gather_self_perception_context() so the next weekly prompt
        # sees "here's how you self-assessed last week".
        #
        # skipped_reason values:
        #   - NULL        → ran successfully + Claude responded
        #   - low_activity → <2 brain-authored PRs+recs in last 7d; no
        #                    Claude call, sentinel row only
        #   - (others can be added later — kept TEXT for flexibility)
        """CREATE TABLE IF NOT EXISTS brain_self_perception (
            id                    BIGSERIAL PRIMARY KEY,
            ran_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            ran_on                DATE NOT NULL DEFAULT CURRENT_DATE,
            summary               TEXT,
            wins                  JSONB NOT NULL DEFAULT '[]'::jsonb,
            losses                JSONB NOT NULL DEFAULT '[]'::jsonb,
            adjustments           JSONB NOT NULL DEFAULT '[]'::jsonb,
            raw_context_size_kb   NUMERIC,
            claude_cost_cents     NUMERIC,
            model_used            TEXT,
            activity_window_days  INTEGER DEFAULT 7,
            pr_count_in_window    INTEGER DEFAULT 0,
            skipped_reason        TEXT,
            raw_payload           JSONB
        )""",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_brain_self_perception_day ON brain_self_perception(ran_on)",
        "CREATE INDEX IF NOT EXISTS ix_brain_self_perception_ran_at ON brain_self_perception(ran_at DESC)",
    ]),
    ("brain_findings cross_session columns", [
        # Brain ROUND 2 (2026-06-07): cross-session finding pollination.
        # Today the brain processes findings PER session (loop_run), so the
        # same shadowed_route or stale_cron pattern might surface 3 weeks
        # in a row across 3 different brain sessions but never escalate.
        # After every brain_findings INSERT we check if (detector,issue_kind)
        # has appeared in N+ DISTINCT brain sessions in the last 7d. If yes,
        # we set cross_session_escalated_at + bump status='escalated'. The
        # backlog dashboard surfaces these as a separate "Cross-session
        # learnings" card so the operator sees recurring patterns the brain
        # has independently rediscovered.
        "ALTER TABLE brain_findings ADD COLUMN IF NOT EXISTS session_id TEXT",
        "ALTER TABLE brain_findings ADD COLUMN IF NOT EXISTS cross_session_escalated_at TIMESTAMPTZ",
        "ALTER TABLE brain_findings ADD COLUMN IF NOT EXISTS cross_session_count INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS ix_brain_findings_session_id ON brain_findings(session_id) WHERE session_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_brain_findings_cross_escalated ON brain_findings(cross_session_escalated_at DESC) WHERE cross_session_escalated_at IS NOT NULL",
    ]),
    ("sentinel_auto_merge_log + block tables (Round 2)", [
        # Sentinel Round 2 (2026-06-07): per-decision log for the brain-L5
        # auto-merge runner. EVERY decision (allow/reject + dry-run) lands
        # here so the operator can audit the gate that fired. The block
        # table powers the dashboard's one-click 24h kill.
        # Source of truth lives in routes/sentinel_auto_merge.py:_LOG_SCHEMA;
        # this entry is for the schema-repair sweep so a cold deploy has
        # the tables on first cron run, not first request.
        """CREATE TABLE IF NOT EXISTS sentinel_auto_merge_log (
            id                          BIGSERIAL PRIMARY KEY,
            pr_number                   INT NOT NULL,
            decision                    TEXT NOT NULL,
            reason                      TEXT,
            issue                       TEXT,
            sentinel_path               TEXT,
            confidence                  REAL,
            files_changed               JSONB,
            merged_at                   TIMESTAMPTZ,
            merge_sha                   TEXT,
            dry_run                     BOOLEAN NOT NULL DEFAULT FALSE,
            follow_up_at                TIMESTAMPTZ,
            follow_up_sentinel_grade    TEXT,
            follow_up_status_code       INT,
            follow_up_outcome           TEXT,
            decided_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS ix_sam_pr           ON sentinel_auto_merge_log(pr_number)",
        "CREATE INDEX IF NOT EXISTS ix_sam_decided      ON sentinel_auto_merge_log(decided_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_sam_decision     ON sentinel_auto_merge_log(decision)",
        "CREATE INDEX IF NOT EXISTS ix_sam_merged       ON sentinel_auto_merge_log(merged_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_sam_followup_due ON sentinel_auto_merge_log(follow_up_at) WHERE follow_up_at IS NOT NULL AND follow_up_outcome IS NULL",
        """CREATE TABLE IF NOT EXISTS sentinel_auto_merge_block (
            id              SERIAL PRIMARY KEY,
            blocked_until   TIMESTAMPTZ NOT NULL,
            blocked_by      TEXT,
            note            TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS ix_sam_block_until ON sentinel_auto_merge_block(blocked_until DESC)",
    ]),
    ("media_comment_engagement_log table", [
        # 2026-06-07 — LinkedIn comment engagement loop (routes/
        # media_comment_engagement.py). Detects new comments on DC Hub
        # org posts, drafts replies via Claude, posts as the org URN.
        # Ships DRY-RUN by default; daily cap=10; 4-7min human cadence
        # delay; per-comment-urn unique so the same comment can never
        # be re-evaluated. Critical for the State of 2026 Monday
        # launch comment storm.
        """CREATE TABLE IF NOT EXISTS media_comment_engagement_log (
            id                       BIGSERIAL PRIMARY KEY,
            source_post_urn          TEXT NOT NULL,
            source_post_id           TEXT,
            comment_urn              TEXT UNIQUE NOT NULL,
            comment_author_urn       TEXT,
            comment_author_name      TEXT,
            comment_text             TEXT,
            comment_detected_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            reply_generated          TEXT,
            reply_posted_at          TIMESTAMPTZ,
            reply_urn                TEXT,
            scheduled_for            TIMESTAMPTZ,
            decision                 TEXT,
            decision_reason          TEXT,
            attributed_visit         BOOLEAN,
            attributed_signup        BOOLEAN
        )""",
        "CREATE INDEX IF NOT EXISTS ix_mcel_decided ON media_comment_engagement_log(comment_detected_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_mcel_pending ON media_comment_engagement_log(scheduled_for) WHERE decision = 'queued' AND reply_posted_at IS NULL",
        "CREATE INDEX IF NOT EXISTS ix_mcel_decision ON media_comment_engagement_log(decision)",
    ]),
    ("media_style_outcomes table", [
        # 2026-06-07: DC Hub Media style A/B learner
        # (routes/media_style_ab.py). One row per LinkedIn post's
        # (topic, style) decision + later-filled engagement metrics.
        # The picker reads this to compute epsilon-greedy choices per
        # topic; the linkedin_engagement_sync cron backfills the
        # metrics 24h after publish.
        """CREATE TABLE IF NOT EXISTS media_style_outcomes (
            id              BIGSERIAL PRIMARY KEY,
            post_id         BIGINT,
            post_urn        TEXT,
            topic           TEXT NOT NULL,
            style           TEXT NOT NULL,
            published_at    TIMESTAMPTZ NOT NULL,
            impressions     INTEGER,
            reactions       INTEGER,
            comments        INTEGER,
            clicks          INTEGER,
            score           REAL,
            measured_at     TIMESTAMPTZ,
            decision_phase  TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS ix_mso_topic_style ON media_style_outcomes(topic, style)",
        "CREATE INDEX IF NOT EXISTS ix_mso_published ON media_style_outcomes(published_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_mso_post_urn ON media_style_outcomes(post_urn) WHERE post_urn IS NOT NULL",
    ]),
]


@schema_repair_bp.route("/api/v1/admin/schema/repair", methods=["POST"])
def schema_repair():
    """Run idempotent schema-repair statements. Safe to call multiple
    times — each statement uses IF NOT EXISTS or equivalent."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    results = []
    try:
        for label, stmts in SCHEMA_STATEMENTS:
            for sql in stmts:
                try:
                    with c.cursor() as cur:
                        cur.execute(sql)
                    try: c.commit()
                    except Exception: pass
                    results.append({"label": label, "ok": True,
                                    "stmt": sql.split("\n")[0][:80]})
                except Exception as e:
                    try: c.rollback()
                    except Exception: pass
                    results.append({"label": label, "ok": False,
                                    "stmt": sql.split("\n")[0][:80],
                                    "error": str(e)[:200]})
        return jsonify(
            ok=True,
            statements_run=len(results),
            successes=sum(1 for r in results if r["ok"]),
            failures=sum(1 for r in results if not r["ok"]),
            results=results,
        )
    finally:
        try: c.close()
        except Exception: pass


# ── Geocoding backfill ───────────────────────────────────────────────
# Static state/province → country mapping. Covers US, CA, AU, UK, BR,
# MX which together account for 95%+ of the gap. Anything not in this
# table stays as country='?' for a later (Nominatim-based) pass.
STATE_TO_COUNTRY = {
    # US states + territories
    **{s: "US" for s in (
        "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD "
        "MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC "
        "SD TN TX UT VT VA WA WV WI WY DC PR VI GU AS MP"
    ).split()},
    # Canadian provinces + territories
    **{p: "CA" for p in
        "AB BC MB NB NL NS NT NU ON PE QC SK YT".split()},
    # Australian states + territories
    **{s: "AU" for s in
        "ACT NSW NT QLD SA TAS VIC WA".split()},
    # UK home nations (sometimes appear as state)
    "ENG": "GB", "SCT": "GB", "WLS": "GB", "NIR": "GB",
    "England": "GB", "Scotland": "GB", "Wales": "GB",
    # Major Mexican states (Spanish)
    "CMX": "MX", "CDMX": "MX", "JAL": "MX", "NLE": "MX",
    # Major Brazilian states
    "SP": "BR", "RJ": "BR",   # note collision: SP also South Africa
}


@schema_repair_bp.route("/api/v1/admin/geocoding/backfill", methods=["POST"])
def geocoding_backfill():
    """For facilities with country IS NULL OR country = '?', infer
    the country from the state/province code. Reports per-country
    additions + leftover unresolved count."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    dry_run = (request.args.get("dry_run") or "").lower() in ("1","true","yes")
    out = {"dry_run": dry_run, "scanned": 0, "updated": 0,
           "by_country": {}, "unresolved": 0, "examples": []}
    try:
        with c.cursor() as cur:
            cur.execute("""
                SELECT id, name, state, city, address
                  FROM facilities
                 WHERE (country IS NULL
                        OR country = ''
                        OR country = '?'
                        OR LOWER(country) = 'unknown')
                 LIMIT 5000
            """)
            rows = cur.fetchall()
        out["scanned"] = len(rows)

        # Batch UPDATEs
        for r in rows:
            fid = r[0]
            state = (r[2] or "").strip()
            city = (r[3] or "").strip()
            address = (r[4] or "")
            inferred = None
            # Priority 1: state code → country
            if state:
                # Uppercase + canonicalize
                key = state.upper()
                # 2-letter and 3-letter codes
                inferred = STATE_TO_COUNTRY.get(key) or STATE_TO_COUNTRY.get(state)
                # Some "state" fields hold full names
                if not inferred and len(state) > 2:
                    inferred = STATE_TO_COUNTRY.get(state.title())
            # Priority 2: address contains a country name (final 30 chars)
            if not inferred and address:
                tail = address.upper()[-60:]
                if " USA" in tail or ", US" in tail or " UNITED STATES" in tail:
                    inferred = "US"
                elif " CANADA" in tail or ", CA " in tail:
                    inferred = "CA"
                elif " UK" in tail or "UNITED KINGDOM" in tail:
                    inferred = "GB"
                elif " GERMANY" in tail:
                    inferred = "DE"
                elif " AUSTRALIA" in tail:
                    inferred = "AU"
                elif " BRAZIL" in tail or " BRASIL" in tail:
                    inferred = "BR"

            if inferred:
                if dry_run:
                    out["updated"] += 1
                    out["by_country"][inferred] = out["by_country"].get(inferred, 0) + 1
                    if len(out["examples"]) < 12:
                        out["examples"].append({
                            "name": r[1], "state": state,
                            "city": city, "inferred": inferred,
                        })
                else:
                    try:
                        with c.cursor() as cur:
                            cur.execute(
                                "UPDATE facilities SET country = %s "
                                "WHERE id = %s",
                                (inferred, fid),
                            )
                        try: c.commit()
                        except Exception: pass
                        out["updated"] += 1
                        out["by_country"][inferred] = out["by_country"].get(inferred, 0) + 1
                        if len(out["examples"]) < 12:
                            out["examples"].append({
                                "name": r[1], "state": state,
                                "city": city, "inferred": inferred,
                            })
                    except Exception:
                        try: c.rollback()
                        except Exception: pass
            else:
                out["unresolved"] += 1
        return jsonify(ok=True, **out)
    finally:
        try: c.close()
        except Exception: pass


# ── Funnel leakage diagnostic ───────────────────────────────────────
@schema_repair_bp.route("/api/v1/admin/funnel/leakage", methods=["GET"])
def funnel_leakage():
    """Per-tool, per-stage drop-off in the MCP conversion funnel.
    Reads mcp_call_log + mcp_upgrade_signals + mcp_pair_codes +
    api_keys to surface where users actually leave.

    Inspector Brief #2 reported: 25,405 calls → 4,452 signals → 9
    conversions. This breaks that down per-stage."""
    if not _admin_ok():
        return jsonify(ok=False, error="forbidden"), 403
    c = _get_db()
    if c is None: return jsonify(ok=False, error="no_db"), 503
    days = int(request.args.get("days") or "30")
    out = {"days": days, "stages": {}, "top_leak_tools": []}
    try:
        with c.cursor() as cur:
            # Stage 1: total tool calls
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_tool_calls "
                    "WHERE created_at >= NOW() - INTERVAL %s",
                    (f"{days} days",),
                )
                out["stages"]["1_tool_calls"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            # Stage 2: paywall-hit signals
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_upgrade_signals "
                    "WHERE created_at >= NOW() - INTERVAL %s",
                    (f"{days} days",),
                )
                out["stages"]["2_paywall_signals"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            # Stage 3: pair codes minted (offers shown)
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_pair_codes "
                    "WHERE created_at >= NOW() - INTERVAL %s",
                    (f"{days} days",),
                )
                out["stages"]["3_codes_minted"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            # Stage 4: codes redeemed
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_pair_codes "
                    "WHERE redeemed_at IS NOT NULL "
                    "AND redeemed_at >= NOW() - INTERVAL %s",
                    (f"{days} days",),
                )
                out["stages"]["4_codes_redeemed"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass
            # Stage 5: final paid keys created
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM api_keys "
                    "WHERE plan IN ('developer','pro','enterprise') "
                    "AND created_at >= NOW() - INTERVAL %s",
                    (f"{days} days",),
                )
                out["stages"]["5_paid_keys"] = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                try: c.rollback()
                except Exception: pass

            # Per-tool drop-off — tools with highest paywall-signal
            # volume that have NEAR-ZERO conversions
            try:
                cur.execute("""
                    SELECT tool_requested,
                           COUNT(*) AS signals,
                           COUNT(DISTINCT session_id) AS distinct_sessions
                      FROM mcp_upgrade_signals
                     WHERE created_at >= NOW() - INTERVAL %s
                     GROUP BY tool_requested
                     ORDER BY signals DESC
                     LIMIT 15
                """, (f"{days} days",))
                out["top_leak_tools"] = [
                    {"tool": r[0], "signals": r[1], "sessions": r[2]}
                    for r in cur.fetchall()
                ]
            except Exception:
                try: c.rollback()
                except Exception: pass

            # Compute drop rates
            stages = out["stages"]
            if stages.get("1_tool_calls", 0) > 0 and stages.get("2_paywall_signals", 0) > 0:
                out["drop_calls_to_signals_pct"] = round(
                    100 * (1 - stages["2_paywall_signals"] / stages["1_tool_calls"]), 2)
            if stages.get("2_paywall_signals", 0) > 0 and stages.get("3_codes_minted", 0) is not None:
                out["drop_signals_to_codes_pct"] = round(
                    100 * (1 - stages["3_codes_minted"] / max(1, stages["2_paywall_signals"])), 2)
            if stages.get("3_codes_minted", 0) > 0 and stages.get("4_codes_redeemed", 0) is not None:
                out["drop_codes_to_redeemed_pct"] = round(
                    100 * (1 - stages["4_codes_redeemed"] / max(1, stages["3_codes_minted"])), 2)
            if stages.get("4_codes_redeemed", 0) is not None and stages.get("5_paid_keys", 0) is not None:
                out["drop_redeemed_to_paid_pct"] = round(
                    100 * (1 - stages["5_paid_keys"] / max(1, stages.get("4_codes_redeemed", 1))), 2)

        return jsonify(ok=True, **out)
    finally:
        try: c.close()
        except Exception: pass


def _smoke():
    logger.info("[schema-repair] ready · POST /schema/repair · "
                 "/geocoding/backfill · GET /funnel/leakage")

_smoke()
