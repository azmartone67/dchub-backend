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
import logging
import datetime
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
schema_repair_bp = Blueprint("schema_repair", __name__)


_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
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
