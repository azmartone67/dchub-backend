"""
routes/funnel_health.py — Single-pane /admin/funnel-health dashboard (2026-06-06).

ONE operator-friendly page that surfaces ALL the telemetry shipped today:
  • Hero KPIs (MRR / 30d conversions / active dev keys / 7d tool calls)
  • MCP funnel waterfall (calls → distinct → signals → codes → views →
    Stripe clicks → conversions) with per-stage drop %.
  • Per-tool signal breakdown (validates Fix C — mcp_upgrade_signals.tool_requested
    write was restored from the trial_preview path).
  • mcp_session_upgrades card (Fix E — session-bound conversion-loop closure).
  • renewal_nudge_log card (day-330 nudge cron).
  • source_plan='pro_annual_onetime' cohort (expiring next 60d + already-expired-
    not-yet-demoted).
  • Per-AI-platform breakdown (Claude / ChatGPT / Meta AI / Gemini / Copilot /
    Perplexity / DeepSeek / Cursor / Cline / Continue) — joined off
    mcp_call_log.platform + mcp_upgrade_signals.mcp_client.
  • Pricing A/B status (Arm A $199 vs Arm B $99) — pulls the same numbers as
    /api/v1/admin/pricing/ab-stats and adds a Kill A/B button (uses the
    existing PRICING_AB_DISABLE env var).
  • Recent events stream (Stripe webhooks, demotes, renewal nudges, A/B cohort
    assignments) — last 20 across all sources.

Routes:
  GET  /admin/funnel-health                main URL (CF Pages serves)
  GET  /api/v1/admin/funnel-health         CF zone-worker bypass alias
       (mirrors the dual-route pattern used by feedback_forum.admin_feedback_dashboard
       so a CF allowlist edge-case never hides the dashboard from the operator)
  POST /api/v1/admin/funnel-health/kill-ab kill-switch (sets the in-memory
       PRICING_AB_DISABLE flag for THIS Railway replica; persistent via Railway
       env-var UI). Pop-up shows operator the exact env-var change to persist.

Auth:
  - X-Admin-Key header OR ?admin_key= query param.
  - Match DCHUB_ADMIN_KEY (falls back to ADMIN_KEY env-var).
  - Same gate as routes/feedback_forum.admin_feedback_dashboard.

Cache:
  - Module-level dict, TTL 60s, single key. The dashboard fans out ~12
    queries; without the cache an aggressive operator refresh would spike
    DB load on the single Railway replica.

Defensive query design:
  - Every probe is wrapped in its own try/except + savepoint-style rollback,
    so a missing brand-new table (mcp_session_upgrades, renewal_nudge_log,
    pricing_ab_events) NEVER blanks the rest of the page. Each missing-table
    error is surfaced as a faint warning chip at the top so the operator can
    immediately spot a schema_repair gap.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from html import escape as _esc
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

funnel_health_bp = Blueprint("funnel_health", __name__)


# ── env ───────────────────────────────────────────────────────────────

_ADMIN_KEY = (os.environ.get("DCHUB_ADMIN_KEY")
              or os.environ.get("ADMIN_KEY") or "").strip()

# Same plan→USD/mo map the Stripe webhook in main.py uses (line ~10403).
# Used to compute MRR. pro_annual is a one-time charge; we count it at the
# equivalent monthly rate ($1188 / 12 = $99) so the dashboard MRR reflects
# the cash run-rate, not just recurring-subscription MRR.
_PLAN_MONTHLY_USD = {
    "starter":            9,
    "developer":          49,
    "pro":                99,   # r-price-collapse 2026-09-05: $99 list. Checked
                                # against live Stripe the same day — no active
                                # sub sits above $99 except the NLR annual seed,
                                # which is not plan 'pro'. So 99 is both the
                                # conservative floor AND the list. (Was 199.)
                                # at $199; new list is $299 (r-reprice 2026-06-19)
    "pro_annual":         99,   # $1188/yr ≈ $99/mo equivalent
    "pro_annual_onetime": 99,   # source_plan value matches above
    # brain-ascension #28 (2026-07-25): team + founding were MISSING — a $699
    # Team or $99 Founding subscriber contributed $0 to MRR (and the users
    # query below filtered them out of the probe entirely).
    "team":               699,  # $699/mo, 5 seats
    "founding":           99,   # $99/mo r-founder99 link (conservative floor)
    "enterprise":         500,
    "enterprise_annual":  500,
    "research_seed_nlr":  250,  # $3,000/yr ≈ $250/mo
    "metered":            0,    # PAYG — counted via mcp_call_log if needed
}

# Canonical AI platform → match keys (mcp_client + UA prefix). Order matters —
# the dashboard renders the table in this order so the operator sees the most
# important platforms first.
_AI_PLATFORMS = [
    ("claude",     "Claude",     ["claude", "anthropic"]),
    ("chatgpt",    "ChatGPT",    ["chatgpt", "openai", "gpt"]),
    ("meta",       "Meta AI",    ["meta", "llama", "meta-ai"]),
    ("gemini",     "Gemini",     ["gemini", "bard", "google-ai"]),
    ("copilot",    "Copilot",    ["copilot", "github-copilot"]),
    ("perplexity", "Perplexity", ["perplexity"]),
    ("deepseek",   "DeepSeek",   ["deepseek"]),
    ("cursor",     "Cursor",     ["cursor"]),
    ("cline",      "Cline",      ["cline"]),
    ("continue",   "Continue",   ["continue", "continue-dev"]),
]


# ── De-loop definition (SINGLE SOURCE) ─────────────────────────────────
# THE honest "real external tool calls" definition NOW LIVES IN ONE PLACE:
# top-level mcp_calls_deloop.py. Both this dashboard's tool_calls_7d_real KPI
# AND the /api/v1/mcp/funnel endpoint (flask_mcp_endpoints.mcp_funnel) import
# the SAME PLATFORM_CASE classifier + PROBE_PLATFORMS list from there, so the
# two `tool_calls_7d_real` counts are byte-identical by construction.
#
# Why a shared module: this clause used to be hand-rolled here (a broad
# client_name NOT-IN list + NOT-LIKE families + self-UA patterns) while the
# endpoint counted over a `_platform_case` classifier + a narrow 6-item probe
# list. Both claimed to be "the honest number" but they diverged. The shared
# classifier folds every self-UA (dchub-/dchubhealer/brain-radar/...) into the
# 'internal-dchub' bucket and excludes it, so null-client_name self-traffic is
# still caught — same intent as the old UA NOT-LIKE block, now expressed once.
#
# DO NOT duplicate this clause inline — both the 7d/30d KPI and the weekly
# trend below reuse _deloop_calls_where(). Honest-numbers fence: one filter,
# many sinks. tests/test_funnel_health_deloop.py asserts this stays identical
# to the endpoint's definition.
try:
    from mcp_calls_deloop import (
        deloop_calls_where as _shared_deloop_where,
        PROBE_PLATFORMS as _PROBE_CLIENT_NAMES,  # re-exported for back-compat
    )
except Exception:  # pragma: no cover - defensive: never let an import blank the page
    _shared_deloop_where = None
    _PROBE_CLIENT_NAMES = ()

# r-canonical-funnel (2026-06-27): ONE source of truth for funnel KPIs (active
# keys / MRR / conversions), so /admin/funnel-health, /api/v1/mcp/funnel,
# /api/v1/site/stats and mcp_funnel_diag stop disagreeing (the
# cross_surface_metric_divergence finding). Defensive import — a module error
# must never blank this page.
try:
    from canonical_funnel import get_canonical_funnel as _canonical_funnel
except Exception:  # pragma: no cover
    def _canonical_funnel():
        return {}


def _deloop_calls_where() -> str:
    """Return the SQL boolean fragment (no leading AND) that keeps ONLY real
    external tool calls in mcp_tool_calls — i.e. excludes loop/selfheal/probe/
    sweep traffic. Columns referenced: client_name, user_agent. Identical to
    the /api/v1/mcp/funnel endpoint's `tool_calls_7d_real` filter because both
    import mcp_calls_deloop.deloop_calls_where(). Trusted hardcoded constants
    only — inlined as SQL literals (no bound params, so the literal % in the
    ILIKE patterns is left alone)."""
    if _shared_deloop_where is not None:
        return _shared_deloop_where()
    # Fallback (shared module unavailable): keep the dashboard alive with a
    # conservative client_name-only exclude so a missing import never 500s the
    # page. This path is exercised only if mcp_calls_deloop fails to import.
    return " COALESCE(LOWER(client_name),'') NOT LIKE 'dchub-%' "


# ── High-intent claim funnel: real-prospect exclusion ─────────────────────
# 2026-06-23: the high-intent headline ("N claims minted, 0 converted") was
# inflated by self-test + scripting traffic, not real prospects. Live audit:
# of 123 claims minted/30d, ~93 were raw python-httpx/Python-urllib/curl
# scripts (+ this-session gating/funnel tests) and only ~24 were real,
# browser-bearing agents (claude/cursor/opencode + anonymous mcp-remote).
# Counting the noise made the funnel read like a ~99% leak.
#
# We DELEGATE to the ONE predicate the mint gate and the step-drop endpoint
# already use — routes.mcp_high_intent_claim._hi_real_sql() — so the mint
# decision, that endpoint, and this dashboard agree by construction (no second
# list to drift). It reads mcp_client + user_agent directly and KEEPS bare
# 'node' (mcp-remote, a real transport) while dropping our automation + raw
# scripting UAs. (An earlier draft here reused the tool-call de-loop classifier
# instead; that wrongly dropped the mcp-remote 'node' agents — 16 vs the correct
# 24 — which is exactly why we share the high-intent-specific predicate.)
def _high_intent_real_where(prefix: str = "") -> str:
    """Real-prospect SQL boolean (no leading AND) for mcp_high_intent_sessions.
    Lazily delegates to the shared _hi_real_sql() so the dashboard never drifts
    from the mint gate; the import is lazy (per-call) to avoid any route-module
    circular import at load. Minimal script-UA fallback keeps the page alive if
    the import is unavailable. `prefix` = optional alias incl. trailing dot."""
    try:
        from routes.mcp_high_intent_claim import _hi_real_sql
        return _hi_real_sql(prefix)
    except Exception:  # pragma: no cover - defensive: never blank the page
        p = prefix
        return (f"COALESCE({p}user_agent,'') !~* "
                f"'(python-httpx|python-urllib|urllib|curl/|wget|node-fetch|undici|axios)'")


def _hi_real_from() -> str:
    """A real-prospect-FILTERED derived table over mcp_high_intent_sessions,
    aliased `h`, exposing every column via SELECT *. The exclusion is applied
    INSIDE over `_z`, so callers may JOIN `h` to users / auto_trial_keys /
    mcp_upgrade_signals without the predicate's columns ever going ambiguous.
    Use as:
        SELECT ... FROM {_hi_real_from()} WHERE <business conditions> …
    or  SELECT ... FROM {_hi_real_from()} JOIN users u ON … WHERE …
    """
    return ("(SELECT * FROM mcp_high_intent_sessions _z "
            " WHERE " + _high_intent_real_where("_z.") + ") h")


# ── DB ────────────────────────────────────────────────────────────────

def _conn():
    """Open a raw psycopg2 connection. Returns None on failure — callers
    must handle None defensively (this dashboard intentionally degrades
    gracefully when the DB is down so the operator still gets a page)."""
    try:
        import psycopg2 as _pg
        dsn = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL") or "")
        if not dsn:
            return None
        # 502-fix (2026-07-01): per-probe statement_timeout lives in
        # _scalar/_rows (SET LOCAL inside an explicit transaction). It can
        # NOT go here: Neon's POOLED endpoint (pgbouncer, transaction mode)
        # rejects options="-c statement_timeout" at connect ("unsupported
        # startup parameter"), and a plain post-connect SET lands on one
        # backend connection while later probes run on others (verified
        # live 2026-07-01: SET then SHOW returned the untouched default).
        c = _pg.connect(dsn, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("funnel_health: db connect failed: %s", e)
        return None


def _scalar(cur, sql: str, params: tuple = ()) -> Optional[int]:
    """Run a COUNT(*)/SUM(...)/scalar SELECT, return int or None on error.
    Each probe is isolated so one missing table never blanks the page.

    ★ When there are NO bind params, execute WITHOUT a params arg. Passing an
    empty tuple makes psycopg2 run %-substitution over the SQL, which trips on
    the 91 literal % in the de-loop's ILIKE predicates (IndexError: tuple index
    out of range) — that silently blanked tool_calls_*_real EVERY render and was
    mislabeled as a transient 'de-loop timeout' (the query is really 0.1-0.5s;
    statement_timeout is 30s). See reference_psycopg2_empty_tuple_percent_trap."""
    try:
        r = _bounded(cur, sql, params, fetch="one")
        if not r:
            return 0
        v = r[0]
        if v is None:
            return 0
        try:
            return int(v)
        except (TypeError, ValueError):
            try:
                return int(float(v))
            except Exception:
                return 0
    except Exception as e:
        logger.debug("funnel_health _scalar failed: %s -- %s", sql[:60], e)
        return None


def _rows(cur, sql: str, params: tuple = ()) -> list[tuple]:
    """Run a SELECT, return rows or [] on error.
    Same empty-tuple %-substitution trap guard as _scalar (above)."""
    try:
        return list(_bounded(cur, sql, params, fetch="all") or [])
    except Exception as e:
        logger.debug("funnel_health _rows failed: %s -- %s", sql[:60], e)
        return []


_PROBE_TIMEOUT_MS = 8000


def _bounded(cur, sql: str, params: tuple, fetch: str):
    """Execute ONE probe inside its own explicit transaction with
    SET LOCAL statement_timeout — the only form that sticks on Neon's
    POOLED endpoint (pgbouncer transaction mode: startup options are
    rejected at connect, and a plain session SET lands on a different
    backend connection than the queries; verified live 2026-07-01).
    The connection is autocommit, so BEGIN/COMMIT here are explicit and
    cheap; ROLLBACK on any error so a timed-out probe never poisons the
    next one ("current transaction is aborted"). 502-fix: bounds every
    probe at 8s so a slow-Neon window degrades to warning chips instead
    of pushing _build_data() past Railway's 30s gunicorn timeout."""
    cur.execute("BEGIN")
    try:
        cur.execute("SET LOCAL statement_timeout = %d" % _PROBE_TIMEOUT_MS)
        if params:
            cur.execute(sql, params)
        else:
            cur.execute(sql)
        result = cur.fetchone() if fetch == "one" else cur.fetchall()
        cur.execute("COMMIT")
        return result
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise


# ── Auth ──────────────────────────────────────────────────────────────

def _admin_ok(req) -> bool:
    """Same gate as feedback_forum.admin_feedback_dashboard."""
    sent = (req.headers.get("X-Admin-Key")
            or req.args.get("admin_key") or "").strip()
    if _ADMIN_KEY and sent == _ADMIN_KEY:
        return True
    return False


# ── 60s in-memory cache ───────────────────────────────────────────────

_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}
_CACHE_TTL_S = 60


def _build_data() -> dict:
    """Run every probe and assemble the dashboard data blob.

    Returns a dict with: kpis, funnel, signals_per_tool, session_upgrades,
    renewal_nudge, source_plan_cohort, platforms, ab_status, events,
    missing_tables, generated_at, db_ok.

    All numeric values default to 0 on probe failure. missing_tables lists
    every brand-new table we couldn't read (so the operator sees them as
    chips at the top → schema_repair gap is instantly visible).
    """
    out: dict[str, Any] = {
        "kpis": {"mrr_usd": 0, "conversions_30d": 0, "active_dev_keys": 0,
                 "tool_calls_7d": 0, "tool_calls_7d_incl_loops": 0,
                 "tool_calls_7d_real": 0, "tool_calls_30d_real": 0,
                 "tool_calls_30d_total": 0,
                 "reach_total_served": 0, "reach_external_ai": 0,
                 "reach_external_ai_7d": None,
                 "real_conversions_30d": 0,
                 "dev_keys_by_tier": {}},
        "calls_by_week": [],
        "calls_week_trend": {},
        "funnel": {"calls_30d": 0, "distinct_paid_users_30d": 0,
                   "signals_30d": 0, "codes_minted_30d": 0,
                   "pages_viewed_30d": 0, "stripe_clicked_30d": 0,
                   "conversions_30d": 0, "stage_drops_pct": {}},
        "signals_per_tool": [],
        "session_upgrades": {"total": 0, "last_7d": 0, "top_platforms": []},
        "renewal_nudge": {"total": 0, "eligible_next_30d": 0,
                          "sent_last_7d": 0},
        "signals_with_tool_tagged": {"total_30d": 0, "untagged_30d": 0,
                                     "pct_tagged_30d": 0.0,
                                     "excluded_view_stamps_30d": 0},
        "source_plan_cohort": {"total": 0, "expiring_next_60d": 0,
                               "expired_not_demoted": 0},
        # 2026-06-07: session-bound high-intent claim KPIs.
        # r-two-branch (2026-07-03): sessions_30d = REAL paywall-hit rows in
        # the last 30d (the honest mint-rate denominator — see the query
        # block for why the old paid_call_count_24h >= 3 filter was wrong).
        # claims_minted = signed URLs emitted into paywall responses.
        # r-used-is-human (2026-07-27): claims_used = HUMAN opened the claim page
        # (claim_page_opened_at). claims_redeemed = agent auto-redeem OR human
        # form-submit (the pre-07-27 claims_used).
        # claim_to_paid = email-path (users.plan) UNION key-path (Stripe
        # pack/top-up on the minted key).
        "high_intent": {"sessions_30d": 0, "claims_minted_30d": 0,
                        "claims_used_30d": 0, "claims_redeemed_30d": 0,
                        "claims_used_human_30d": 0, "claim_to_paid_30d": 0,
                        "minted_rate_pct": 0.0, "claim_to_paid_rate_pct": 0.0,
                        # Round 2 (2026-06-07): threshold is env-driven via
                        # DCHUB_HIGH_INTENT_THRESHOLD (default 2 since 3→2 drop).
                        # We pull the LIVE value from the module so the
                        # dashboard reflects what the endpoint is actually
                        # using (not a hardcoded duplicate).
                        "threshold": 2,
                        # Round 2: per-variant A/B breakdown — minted/used/paid
                        # per claim_variant ('claude', 'cursor', 'cline',
                        # 'chatgpt', 'generic'). Populated by the dedicated
                        # query block below.
                        "variant_breakdown": [],
                        # r-two-branch (2026-07-03): trunk + two-branch drop
                        # monitor (see mcp_high_intent_claim.
                        # build_step_waterfall). alarm only on MECHANICAL
                        # breakage with prev >= 5 — never on intent outcomes.
                        "step_drop": []},
        "step_drop_alarm": False,
        "step_drop_killer": "",
        "step_drop_killer_pct": 0.0,
        "platforms": [],
        "ab_status": {"ab_active": False, "kill_switch": False,
                      "cohorts": {"A": {}, "B": {}}, "z": 0.0, "sig_pct": 0.0,
                      "winner": "no_data"},
        "events": [],
        "missing_tables": [],
        # writer-path canaries (2026-07-03): per-table INSERT-then-ROLLBACK
        # probe result ("ok" / "FAILED: <err>") so an all-zero card is
        # distinguishable from a silently broken writer.
        "writer_canary": {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_ok": False,
    }

    conn = _conn()
    if conn is None:
        return out
    out["db_ok"] = True

    try:
        cur = conn.cursor()

        # ── KPI 1: MRR ────────────────────────────────────────────────
        # Sum of active subscriptions × per-plan price. We avoid hitting
        # Stripe — the canonical source for "is this customer paying" is
        # users.plan + users.subscription_status.
        try:
            # 2026-06-28 honest-MRR split: the headline mrr_usd was inflated
            # ~60% by COMP subscriptions — the NLR research grant (3× enterprise
            # @nlr.gov), internal @dchub.cloud keys, and AI-lab onboarding comp
            # (developer keys @coreweave/deepmind/groq/nvidia/… with no Stripe).
            # A REAL recurring payer has a stripe_customer_id (went through
            # checkout); comp grants don't. Split so the brief can headline real
            # recurring and show comp separately instead of one inflated number.
            cur.execute(
                "SELECT plan, "
                "  COUNT(*) FILTER (WHERE stripe_customer_id IS NOT NULL "
                "                     AND stripe_customer_id <> '') AS paid, "
                "  COUNT(*) FILTER (WHERE stripe_customer_id IS NULL "
                "                     OR  stripe_customer_id = '')  AS comp "
                " FROM users "
                " WHERE COALESCE(subscription_status, 'active') = 'active' "
                "   AND plan IN ('starter','developer','pro','pro_annual',"
                "                'pro_annual_onetime','team','founding',"
                "                'enterprise','enterprise_annual',"
                "                'research_seed_nlr') "
                " GROUP BY plan"
            )
            mrr_real = mrr_comp = 0
            for plan, paid_n, comp_n in cur.fetchall() or []:
                price = _PLAN_MONTHLY_USD.get(str(plan) or "", 0)
                mrr_real += int(paid_n or 0) * price
                mrr_comp += int(comp_n or 0) * price
            out["kpis"]["mrr_usd"]      = mrr_real + mrr_comp  # back-compat (gross)
            out["kpis"]["mrr_real_usd"] = mrr_real             # stripe-linked recurring
            out["kpis"]["mrr_comp_usd"] = mrr_comp             # comp/internal/seed (not revenue)
        except Exception as e:
            logger.debug("MRR probe failed: %s", e)
            out["missing_tables"].append("users (MRR probe)")

        # ── KPI 2: Conversions 30d ────────────────────────────────────
        # Prefer Stripe-webhook signal (mcp_conversions table). Fall back
        # to mcp_pair_codes.redeemed_at + users.plan_updated_at.
        # r-honest-conv (2026-07-03): this gross number is NO LONGER the
        # headline — the hero card leads with real_conversions_30d (KPI 4f,
        # Stripe-backed + comp-stripped) and shows this as context. The
        # source is recorded so the pair-code fallback renders as
        # 'redemptions', never as conversions.
        conv_source = "mcp_conversions"
        conv_30d = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_conversions "
            " WHERE created_at >= NOW() - INTERVAL '30 days'")
        if conv_30d is None:
            out["missing_tables"].append("mcp_conversions")
            # Fall back to pair-code redemptions.
            conv_source = "pair_code_redemptions"
            conv_30d = _scalar(cur,
                "SELECT COUNT(*) FROM mcp_pair_codes "
                " WHERE redeemed_at >= NOW() - INTERVAL '30 days'")
            if conv_30d is None:
                conv_30d = 0
        out["kpis"]["conversions_30d"] = int(conv_30d or 0)
        out["kpis"]["conversions_30d_source"] = conv_source

        # ── KPI 2b: Emails captured 30d (the AGENT-funnel lead signal) ──
        # 2026-06-28: ~0 here while signals/high-intent claims are high = the
        # agent funnel is leaking (the brain's recurring "conversion dead"
        # narrative). Web subs converting doesn't mean the agent funnel is
        # healthy. Sum the explicit capture table + auto-trial email binds
        # (the new email-gate-the-tail path). Best-effort; missing table → 0.
        emails_cap = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_email_capture "
            " WHERE created_at >= NOW() - INTERVAL '30 days'") or 0
        trial_binds = _scalar(cur,
            "SELECT COUNT(*) FROM auto_trial_keys "
            " WHERE signed_up_email IS NOT NULL AND signed_up_email <> '' "
            "   AND minted_at >= NOW() - INTERVAL '30 days'") or 0
        # #1551 fix (2026-07-13): the REAL bind_email sink is mcp_dev_keys.email
        # (flask_mcp_endpoints ~1310 UPDATE mcp_dev_keys SET email=…), which this KPI
        # omitted → it printed ~0 while the identify flow was actually binding operators.
        # Add distinct in-window dev-key emails. No bind-timestamp col exists, so
        # created_at is the safe, non-erroring window (may miss late binds of old keys).
        devkey_binds = _scalar(cur,
            "SELECT COUNT(DISTINCT LOWER(email)) FROM mcp_dev_keys "
            " WHERE email IS NOT NULL AND email <> '' "
            "   AND created_at >= NOW() - INTERVAL '30 days'") or 0
        out["kpis"]["emails_captured_30d"] = (
            int(emails_cap) + int(trial_binds) + int(devkey_binds))
        out["kpis"]["emails_captured_30d_devkey_binds"] = int(devkey_binds)

        # Agent demand (denominator for the agent-funnel-leak verdict): how many
        # high-intent agent sessions minted a claim in 30d, and active trials.
        out["kpis"]["high_intent_claims_30d"] = int(_scalar(cur,
            "SELECT COUNT(*) FROM mcp_high_intent_sessions "
            " WHERE claim_minted_at >= NOW() - INTERVAL '30 days'") or 0)
        out["kpis"]["trial_keys_active"] = int(_scalar(cur,
            "SELECT COUNT(*) FROM auto_trial_keys WHERE expires_at > NOW()") or 0)

        # ── KPI 3: Active dev keys + per-tier breakdown ───────────────
        # r-canonical-funnel (2026-06-27): read from canonical_funnel (the ONE
        # SoT) instead of an inline COALESCE(status,'active')='active' query. The
        # old COALESCE counted NULL-status rows as active (inflation); canonical
        # is status='active' (NULL excluded), matching flask_mcp_endpoints +
        # site_stats so the three surfaces finally agree. NOTE: this will move the
        # admin number DOWN from the COALESCE-inflated count to the honest active
        # count — that is the intended correction, not a regression.
        try:
            _cf = _canonical_funnel()
            out["kpis"]["active_dev_keys"]  = _cf.get("active_dev_keys", 0)
            out["kpis"]["dev_keys_by_tier"] = _cf.get("dev_keys_by_tier", {})
            out["kpis"]["paid_keys"]        = _cf.get("paid_keys", 0)
            out["kpis"]["mcp_dev_keys_registered"] = _cf.get("mcp_dev_keys_registered", 0)
            out["kpis"]["mrr_invoiced_usd"] = _cf.get("mrr_invoiced_usd", 0.0)
        except Exception as e:
            logger.debug("canonical dev_keys read failed: %s", e)
            out["missing_tables"].append("mcp_dev_keys")

        # ── KPI 4: Tool calls 7d ──────────────────────────────────────
        # tool_calls_7d = GROSS mcp_call_log row count — INCLUDES our own
        # loop/selfheal/probe/sweep traffic (~35-41k). Kept for backward
        # compat ONLY (brain detectors / mcp_growth.py read it). It is NOT the
        # honest external number — see tool_calls_7d_real below.
        v = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_call_log "
            " WHERE timestamp >= NOW() - INTERVAL '7 days'")
        if v is None:
            out["missing_tables"].append("mcp_call_log")
            v = 0
        out["kpis"]["tool_calls_7d"] = int(v)
        out["kpis"]["tool_calls_7d_incl_loops"] = int(v)  # explicit honest label

        # ── KPI 4b: HONEST de-looped tool calls (7d + 30d) ────────────
        # Single source of truth, identical definition to /api/v1/mcp/funnel's
        # tool_calls_7d_real: mcp_tool_calls minus loop/selfheal/probe/sweep
        # (see _deloop_calls_where). This is the number every surface should
        # headline — it does NOT collapse when the selfheal loop changes cadence.
        _where = _deloop_calls_where()
        v_real7 = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_tool_calls "
            " WHERE created_at >= NOW() - INTERVAL '7 days' "
            "   AND " + _where)
        v_real30 = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_tool_calls "
            " WHERE created_at >= NOW() - INTERVAL '30 days' "
            "   AND " + _where)
        # HARDENING (2026-06-20): the de-loop COUNT (19 LIKE predicates over
        # mcp_tool_calls) is the slowest probe on this page and times out under
        # transient DB load (crawler connection churn) → _scalar None.
        # mcp_tool_calls is a CORE table many live surfaces read, so a None here
        # is ~always a transient query timeout, NOT a missing table. Rendering it
        # as 'Schema gaps: mcp_tool_calls' + 'external: 0' twice scared a health
        # spot-check into thinking MCP died. Surface it as DEGRADED (self-recovers
        # next 60s refresh); never emit a false schema-gap or a false 0.
        if v_real7 is None or v_real30 is None:
            out.setdefault("degraded", []).append(
                "tool_calls_*_real de-loop query timed out (transient DB load, NOT a schema gap)")
        out["kpis"]["tool_calls_7d_real"]  = int(v_real7) if v_real7 is not None else None
        out["kpis"]["tool_calls_30d_real"] = int(v_real30) if v_real30 is not None else None

        # ── KPI 4d: gross 30d tool-call denominator (no de-loop) ──────
        # The USAGE bifurcation needs total-vs-real. tool_calls_30d_real
        # (above) is the de-looped numerator; this is the gross denominator.
        # Plain COUNT over an indexed created_at — fast, unlike the de-loop.
        v_total30 = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_tool_calls "
            " WHERE created_at >= NOW() - INTERVAL '30 days'")
        out["kpis"]["tool_calls_30d_total"] = (
            int(v_total30) if v_total30 is not None else None)

        # ── KPI 4e: REACH (AI-platform discovery) — live, zero-drift ──
        # Mirrors the /ai page: "Total Requests Served" (all sources) and
        # "from external AI platforms" (the AI_PLATFORMS allowlist subset),
        # both summed from the SAME ai_cumulative table /ai reads, so this
        # header can never drift from /ai. DEFENSIVE local import: any
        # ai_tracking failure degrades only this one card — never the whole
        # dashboard (a module-level import error would make the blueprint
        # register try/except 404 BOTH routes silently).
        try:
            from ai_tracking import _execute as _ai_execute, AI_PLATFORMS
            _ai_rows = _ai_execute(
                "SELECT platform, total_requests FROM ai_cumulative",
                fetchall=True) or []
            _reach_total = sum(int(r.get("total_requests") or 0)
                               for r in _ai_rows)
            _reach_external = sum(
                int(r.get("total_requests") or 0) for r in _ai_rows
                if str(r.get("platform") or "").strip().lower() in AI_PLATFORMS)
            out["kpis"]["reach_total_served"] = int(_reach_total)
            out["kpis"]["reach_external_ai"] = int(_reach_external)
            # SAME-WINDOW 7d external-AI reach (ai_daily_stats) — pairs with
            # tool_calls_7d_real for an honest reach→usage %: the cumulative
            # number above divides a 30d numerator by a since-Feb denominator.
            # Watched weekly as flywheel lane 6. Own try: a daily-stats gap
            # must not blank the cumulative reach card.
            try:
                _r7 = _ai_execute(
                    "SELECT COALESCE(SUM(request_count),0) AS n "
                    "FROM ai_daily_stats WHERE date >= CURRENT_DATE - 7 "
                    "AND platform IN %s",
                    (tuple(AI_PLATFORMS.keys()),), fetch=True)
                out["kpis"]["reach_external_ai_7d"] = (
                    int(_r7["n"]) if _r7 else None)
            except Exception as e:
                logger.debug("7d reach probe failed: %s", e)
                out["kpis"]["reach_external_ai_7d"] = None
        except Exception as e:
            logger.debug("reach probe failed: %s", e)
            out["kpis"]["reach_total_served"] = None
            out["kpis"]["reach_external_ai"] = None

        # ── KPI 4f: HONEST conversion FLOW — real Stripe payments (30d) ──
        # Count of real Stripe-backed conversions (subs + $10 packs) in the
        # last 30d, with seed/comp/NLR rows stripped. We deliberately do NOT
        # synthesize an MRR here: mcp_conversions has no active/churned status
        # and users.plan 'active' is comp-inflated (prior ★finding: real cash-
        # recurring is a fraction of the plan run-rate), so any single MRR
        # number would be contestable. The existing mrr_usd card already shows
        # the plan run-rate WITH its honest caveat. %-free SQL (no LIKE) to
        # dodge the _scalar empty-params %-substitution trap.
        # ★2026-07-28: + refunded_at IS NULL. Must stay byte-identical in intent
        # to canonical_funnel.conversions_30d_real — these two definitions are
        # deliberately kept in lock-step, so a filter added to one belongs in the
        # other. Refunds were never reversed anywhere until today.
        real_conv30 = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_conversions "
            " WHERE created_at >= NOW() - INTERVAL '30 days' "
            "   AND stripe_customer_id IS NOT NULL "
            "   AND LOWER(COALESCE(plan_to,'')) NOT IN "
            "       ('comp','complimentary','research_seed_nlr','seed') "
            # ★2026-08-01: seed labels are free text (live NLR rows say
            # 'Year 1 Research Seed — FY2026 calibration'); POSITION not LIKE
            # keeps this %-free per the _scalar trap note above.
            "   AND POSITION('seed' IN LOWER(COALESCE(plan_to,''))) = 0 "
            "   AND LOWER(COALESCE(source,'')) <> 'seed' "
            "   AND refunded_at IS NULL")
        out["kpis"]["real_conversions_30d"] = (
            int(real_conv30) if real_conv30 is not None else None)

        # ── KPI 4c: WEEKLY TREND (de-looped) so a real decline is visible ──
        # Last ~8 ISO weeks of de-looped calls + DISTINCT external callers
        # (by ip_address). A genuine drop shows here; a loop-cadence change
        # does NOT (it's already excluded). Current (partial) week is dropped
        # so the latest bucket isn't a misleading mid-week dip.
        try:
            cur.execute(
                "SELECT DATE_TRUNC('week', created_at)::DATE AS week_start, "
                "       COUNT(*) AS calls, "
                "       COUNT(DISTINCT ip_address) AS distinct_callers "
                "  FROM mcp_tool_calls "
                " WHERE created_at >= DATE_TRUNC('week', NOW()) "
                "                    - INTERVAL '8 weeks' "
                "   AND created_at <  DATE_TRUNC('week', NOW()) "
                "   AND " + _where +
                " GROUP BY 1 ORDER BY 1")
            weeks = [
                {"week_start": (r[0].isoformat() if r[0] is not None else None),
                 "calls": int(r[1] or 0),
                 "distinct_callers": int(r[2] or 0)}
                for r in (cur.fetchall() or [])
            ]
            out["calls_by_week"] = weeks
            # Pre-compute the "this week vs trailing 4-week avg" delta so any
            # consumer (brain, dashboard) reads ONE honest judgement, not raw
            # rows. Uses the last COMPLETE week vs the 4 weeks before it.
            if len(weeks) >= 2:
                last = weeks[-1]["calls"]
                prior = [w["calls"] for w in weeks[:-1]][-4:]
                avg_prior = (sum(prior) / len(prior)) if prior else 0
                out["calls_week_trend"] = {
                    "last_week_calls": last,
                    "trailing_4wk_avg_calls": round(avg_prior, 1),
                    "delta_pct": (round((last - avg_prior) / avg_prior * 100.0, 1)
                                  if avg_prior > 0 else None),
                    "last_week_distinct_callers": weeks[-1]["distinct_callers"],
                }
        except Exception as e:
            logger.debug("[funnel_health] calls_by_week probe failed: %s", e)
            try: conn.rollback()
            except Exception: pass

        # ── MCP funnel waterfall (30d) ────────────────────────────────
        v = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_call_log "
            " WHERE timestamp >= NOW() - INTERVAL '30 days'")
        out["funnel"]["calls_30d"] = int(v or 0)

        v = _scalar(cur,
            "SELECT COUNT(DISTINCT api_key) FROM mcp_call_log "
            " WHERE timestamp >= NOW() - INTERVAL '30 days' "
            "   AND api_key IS NOT NULL "
            "   AND api_key NOT LIKE 'dchub-%%' "
            "   AND api_key NOT LIKE '%%-probe' "
            "   AND api_key NOT LIKE '%%-health'")
        out["funnel"]["distinct_paid_users_30d"] = int(v or 0)

        v = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_upgrade_signals "
            " WHERE created_at >= NOW() - INTERVAL '30 days'")
        if v is None:
            out["missing_tables"].append("mcp_upgrade_signals")
            v = 0
        out["funnel"]["signals_30d"] = int(v)

        v = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_pair_codes "
            " WHERE created_at >= NOW() - INTERVAL '30 days'")
        if v is None:
            out["missing_tables"].append("mcp_pair_codes")
            v = 0
        out["funnel"]["codes_minted_30d"] = int(v)

        v = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_pair_codes "
            " WHERE redeem_viewed_at IS NOT NULL "
            "   AND created_at >= NOW() - INTERVAL '30 days'")
        out["funnel"]["pages_viewed_30d"] = int(v or 0)

        v = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_pair_codes "
            " WHERE stripe_clicked_at IS NOT NULL "
            "   AND created_at >= NOW() - INTERVAL '30 days'")
        out["funnel"]["stripe_clicked_30d"] = int(v or 0)

        out["funnel"]["conversions_30d"] = int(conv_30d or 0)

        # Per-stage drop %.
        def _drop(a: int, b: int) -> Optional[float]:
            if a <= 0:
                return None
            # Negative drop means stage grew — show 0% rather than nonsense.
            return round(max(0.0, (1 - b / a) * 100.0), 1)

        f = out["funnel"]
        out["funnel"]["stage_drops_pct"] = {
            "calls→distinct":  _drop(f["calls_30d"],
                                     f["distinct_paid_users_30d"]),
            "distinct→signals": _drop(f["distinct_paid_users_30d"],
                                      f["signals_30d"]),
            "signals→codes":   _drop(f["signals_30d"], f["codes_minted_30d"]),
            "codes→viewed":    _drop(f["codes_minted_30d"],
                                     f["pages_viewed_30d"]),
            "viewed→clicked":  _drop(f["pages_viewed_30d"],
                                     f["stripe_clicked_30d"]),
            "clicked→converted": _drop(f["stripe_clicked_30d"],
                                       f["conversions_30d"]),
        }

        # ── Per-tool signal breakdown (validates Fix C) ───────────────
        try:
            cur.execute(
                "SELECT COALESCE(tool_requested, '(untagged)') AS tool, "
                "       COUNT(*) AS n "
                "  FROM mcp_upgrade_signals "
                " WHERE created_at >= NOW() - INTERVAL '30 days' "
                " GROUP BY 1 ORDER BY n DESC LIMIT 10")
            out["signals_per_tool"] = [
                {"tool": r[0], "count": int(r[1] or 0)}
                for r in cur.fetchall() or []
            ]
        except Exception:
            pass

        # Fix-C validation: total signals 30d split by tool_tagged vs not.
        # 'unknown' (historic session-level mints, pair_code.py) and its
        # 2026-07-02 replacement 'session_cta' are placeholders, not real
        # tool tags — counting them read as a false 100% tagged.
        # r-structural-untagged (2026-07-03): signal_type='redeem_url_viewed'
        # is EXCLUDED from the denominator — those rows are page-view stamps,
        # not tool-gated paywall signals, so they structurally carry no tool
        # (the 07-03 deep dive found ALL 708 untagged rows were this type;
        # the resulting 69.5% "tagged" read as a Fix-C regression when the
        # taggable population was actually ~100%). The excluded count is
        # surfaced separately so the view-stamp volume stays visible.
        try:
            cur.execute(
                "SELECT "
                "  COUNT(*) FILTER (WHERE COALESCE(signal_type,'') "
                "                         <> 'redeem_url_viewed' "
                "                     AND tool_requested IS NOT NULL "
                "                     AND tool_requested NOT IN "
                "                         ('', 'unknown', 'session_cta')), "
                "  COUNT(*) FILTER (WHERE COALESCE(signal_type,'') "
                "                         <> 'redeem_url_viewed'), "
                "  COUNT(*) FILTER (WHERE COALESCE(signal_type,'') "
                "                         = 'redeem_url_viewed') "
                "  FROM mcp_upgrade_signals "
                " WHERE created_at >= NOW() - INTERVAL '30 days'")
            r = cur.fetchone() or (0, 0, 0)
            tagged = int(r[0] or 0)
            total = int(r[1] or 0)
            excluded = int(r[2] or 0)
            untagged = max(0, total - tagged)
            pct = round(100.0 * tagged / total, 1) if total else 0.0
            out["signals_with_tool_tagged"] = {
                "total_30d": total,
                "untagged_30d": untagged,
                "tagged_30d": tagged,
                "pct_tagged_30d": pct,
                "excluded_view_stamps_30d": excluded,
            }
        except Exception:
            pass

        # ── mcp_session_upgrades (Fix E) ──────────────────────────────
        v = _scalar(cur, "SELECT COUNT(*) FROM mcp_session_upgrades")
        if v is None:
            out["missing_tables"].append("mcp_session_upgrades")
        else:
            out["session_upgrades"]["total"] = int(v)
            out["session_upgrades"]["last_7d"] = int(_scalar(cur,
                "SELECT COUNT(*) FROM mcp_session_upgrades "
                " WHERE upgraded_at >= NOW() - INTERVAL '7 days'") or 0)
            try:
                cur.execute(
                    "SELECT COALESCE(plan, '(unknown)'), COUNT(*) "
                    "  FROM mcp_session_upgrades "
                    " GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
                out["session_upgrades"]["top_platforms"] = [
                    {"plan": r[0], "count": int(r[1] or 0)}
                    for r in cur.fetchall() or []
                ]
            except Exception:
                pass

        # ── renewal_nudge_log ─────────────────────────────────────────
        v = _scalar(cur, "SELECT COUNT(*) FROM renewal_nudge_log")
        if v is None:
            out["missing_tables"].append("renewal_nudge_log")
        else:
            out["renewal_nudge"]["total"] = int(v)
            out["renewal_nudge"]["sent_last_7d"] = int(_scalar(cur,
                "SELECT COUNT(*) FROM renewal_nudge_log "
                " WHERE sent_at >= NOW() - INTERVAL '7 days'") or 0)
            # Eligible-next-30d: users.tier_expires_at between NOW() and
            # NOW()+30d AND source_plan='pro_annual_onetime' AND no nudge
            # in last 7d.
            v2 = _scalar(cur,
                "SELECT COUNT(*) FROM users u "
                " WHERE u.source_plan = 'pro_annual_onetime' "
                "   AND u.tier_expires_at IS NOT NULL "
                "   AND u.tier_expires_at BETWEEN NOW() "
                "                          AND NOW() + INTERVAL '30 days' "
                "   AND NOT EXISTS ( "
                "         SELECT 1 FROM renewal_nudge_log r "
                "          WHERE r.user_id = u.id "
                "            AND r.sent_at > NOW() - INTERVAL '7 days')")
            out["renewal_nudge"]["eligible_next_30d"] = int(v2 or 0)

        # ── Writer-path canaries (2026-07-03) ─────────────────────────
        # mcp_session_upgrades and renewal_nudge_log both read 0 — which is
        # consistent with zero eligible events, but indistinguishable from a
        # silently broken writer (a wrong-column INSERT dies inside the
        # writer's own try/except — the brain_findings class of bug). Prove
        # the write PATH (table exists, columns match, INSERT accepted) with
        # a synthetic row inside a rolled-back transaction: nothing persists,
        # counts stay honest, and any schema drift flips the chip to FAILED.
        out["writer_canary"] = {}
        for _tbl, _ins in (
            ("mcp_session_upgrades",
             "INSERT INTO mcp_session_upgrades "
             "(mcp_session_id, user_email, plan, amount_cents) "
             "VALUES ('qa_canary_writer_probe', 'qa-canary@dchub.cloud', "
             "'qa_canary', 0)"),
            ("renewal_nudge_log",
             "INSERT INTO renewal_nudge_log "
             "(user_id, email, days_remaining_at_send, status) "
             "VALUES ('qa_canary_writer_probe', 'qa-canary@dchub.cloud', "
             "0, 'qa_canary')"),
        ):
            try:
                cur.execute("BEGIN")
                cur.execute(_ins)
                cur.execute("ROLLBACK")
                out["writer_canary"][_tbl] = "ok"
            except Exception as _wc_e:
                try: cur.execute("ROLLBACK")
                except Exception: pass
                out["writer_canary"][_tbl] = f"FAILED: {type(_wc_e).__name__}"

        # ── source_plan='pro_annual_onetime' cohort ───────────────────
        try:
            v = _scalar(cur,
                "SELECT COUNT(*) FROM users "
                " WHERE source_plan = 'pro_annual_onetime'")
            if v is None:
                out["missing_tables"].append("users.source_plan column")
            else:
                out["source_plan_cohort"]["total"] = int(v)
                out["source_plan_cohort"]["expiring_next_60d"] = int(_scalar(cur,
                    "SELECT COUNT(*) FROM users "
                    " WHERE source_plan = 'pro_annual_onetime' "
                    "   AND tier_expires_at IS NOT NULL "
                    "   AND tier_expires_at BETWEEN NOW() "
                    "                        AND NOW() + INTERVAL '60 days'") or 0)
                # Already-expired-not-demoted: source_plan tag still present,
                # tier_expires_at in the past, plan still != 'free'.
                out["source_plan_cohort"]["expired_not_demoted"] = int(_scalar(cur,
                    "SELECT COUNT(*) FROM users "
                    " WHERE source_plan = 'pro_annual_onetime' "
                    "   AND tier_expires_at IS NOT NULL "
                    "   AND tier_expires_at < NOW() "
                    "   AND COALESCE(plan, 'free') NOT IN ('free','')") or 0)
        except Exception:
            pass

        # ── 2026-06-07: High-intent claim KPIs (env-driven hit gate) ──
        # The new conversion-funnel surface — see
        # routes/mcp_high_intent_claim.py. We probe directly here (not via
        # the public stats endpoint) so the dashboard works during the
        # rollout window before the endpoint is necessarily deployed.
        try:
            # All high-intent counts run over the REAL-traffic derived table
            # (probe/self-test excluded) — see _hi_real_from / _high_intent_real_where.
            # r-two-branch (2026-07-03): sessions_30d = ALL real paywall-hit rows
            # in-window. The old filter (paid_call_count_24h >= 3) hardcoded a 3
            # while the LIVE mint threshold is env-driven (DCHUB_HIGH_INTENT_
            # THRESHOLD, currently 1) AND read a self-resetting 24h counter — so
            # the denominator undercounted and the mint rate read >100%.
            v = _scalar(cur,
                "SELECT COUNT(*) FROM " + _hi_real_from() +
                " WHERE last_hit_at >= NOW() - INTERVAL '30 days'")
            if v is None:
                out["missing_tables"].append("mcp_high_intent_sessions")
            else:
                out["high_intent"]["sessions_30d"] = int(v or 0)
                out["high_intent"]["claims_minted_30d"] = int(_scalar(cur,
                    "SELECT COUNT(*) FROM " + _hi_real_from() +
                    " WHERE claim_minted_at IS NOT NULL "
                    "   AND claim_minted_at >= NOW() - INTERVAL '30 days'") or 0)
                # r-used-is-human (2026-07-27): mirror of the public
                # /high-intent/stats change — claim_used_at is ~99% server-side
                # machine auto-redeem (server.mjs _autoRedeemClaim, ~0-25s after
                # mint), so it measured the gateway, not adoption. The human
                # instrument is claim_page_opened_at (stamped only by the GET of
                # the HTML claim form). Old number kept as claims_redeemed_30d.
                # Both surfaces must move together or the dashboard and the
                # public route publish different "claims used" numbers.
                out["high_intent"]["claims_used_30d"] = int(_scalar(cur,
                    "SELECT COUNT(*) FROM " + _hi_real_from() +
                    " WHERE claim_page_opened_at IS NOT NULL "
                    "   AND claim_page_opened_at >= NOW() - INTERVAL '30 days'") or 0)
                out["high_intent"]["claims_redeemed_30d"] = int(_scalar(cur,
                    "SELECT COUNT(*) FROM " + _hi_real_from() +
                    " WHERE claim_used_at IS NOT NULL "
                    "   AND claim_used_at >= NOW() - INTERVAL '30 days'") or 0)
                out["high_intent"]["claims_used_human_30d"] = int(_scalar(cur,
                    "SELECT COUNT(*) FROM " + _hi_real_from() +
                    " WHERE claim_used_at IS NOT NULL "
                    "   AND claim_used_at >= NOW() - INTERVAL '30 days' "
                    "   AND claim_email IS NOT NULL AND claim_email <> ''") or 0)
                # claim_to_paid — r-two-branch (2026-07-03): email-path (users.plan
                # via claim_email) UNION key-path (Stripe pack/top-up on the minted
                # key). 11 of 12 live redemptions have claim_email NULL (agents
                # auto-redeem), so the old email-only join made the agent path
                # structurally uncountable. mcp_topups.api_key_hash is
                # sha256(key).hexdigest()[:32] (mcp_conversion_plays._hash_key).
                try:
                    cur.execute(
                        "SELECT COUNT(*) FROM ("
                        " SELECT h.id FROM " + _hi_real_from() +
                        "   JOIN users u ON LOWER(u.email) = LOWER(h.claim_email)"
                        "  WHERE h.claim_email IS NOT NULL"
                        "    AND h.claim_used_at >= NOW() - INTERVAL '30 days'"
                        "    AND COALESCE(u.plan, 'free') NOT IN ('free','')"
                        " UNION "
                        " SELECT h.id FROM " + _hi_real_from() +
                        # r-attr-sid (2026-07-06): join on the Mcp-Session-Id
                        # (survives end-to-end via client_reference_id) — the old
                        # sha256(dch_trial_ key) vs api_key_hash(dch_live_) leg can
                        # never match. Mirrors routes/mcp_high_intent_claim.py.
                        "   JOIN mcp_topups t ON ("
                        "        t.mcp_session_id = h.mcp_session_id"
                        "     OR t.api_key_hash = "
                        "        LEFT(ENCODE(SHA256(CONVERT_TO(h.minted_api_key,'UTF8')),'hex'),32))"
                        "  WHERE h.mcp_session_id IS NOT NULL"
                        "    AND h.claim_used_at >= NOW() - INTERVAL '30 days'"
                        "    AND t.paid_at IS NOT NULL"
                        ") q")
                    out["high_intent"]["claim_to_paid_30d"] = int(
                        (cur.fetchone() or (0,))[0] or 0)
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                if out["high_intent"]["sessions_30d"] > 0:
                    out["high_intent"]["minted_rate_pct"] = min(100.0, round(
                        100.0 * out["high_intent"]["claims_minted_30d"]
                        / out["high_intent"]["sessions_30d"], 1))
                # r-used-is-human (2026-07-27): this claim_to_paid_30d has TWO legs
                # (email-path AND key-path), so unlike the public route's email-only
                # numerator it can legitimately count agent redemptions — keep the
                # any-channel denominator here, just under its honest name.
                if out["high_intent"]["claims_redeemed_30d"] > 0:
                    out["high_intent"]["claim_to_paid_rate_pct"] = min(100.0, round(
                        100.0 * out["high_intent"]["claim_to_paid_30d"]
                        / out["high_intent"]["claims_redeemed_30d"], 1))
                # Round 2: pull the LIVE threshold so the dashboard reflects
                # what the endpoint is using (env-driven via
                # DCHUB_HIGH_INTENT_THRESHOLD).
                try:
                    from routes.mcp_high_intent_claim import HIGH_INTENT_THRESHOLD as _T
                    out["high_intent"]["threshold"] = int(_T)
                except Exception:
                    pass
        except Exception:
            try: conn.rollback()
            except Exception: pass

        # ── Round 2 (2026-06-07): per-variant claim breakdown ─────────
        # claim_variant tells us which platform-specific copy the human saw.
        # We compute minted/used/paid per variant + per-variant use_rate and
        # paid_rate so the dashboard surfaces the A/B winner directly.
        # Defensive: claim_variant might be absent on a still-deploying box;
        # the try/except + per-row column-present check keeps the page green.
        #
        # r-variant-honest-split (2026-07-11): `used` split into the two
        # redemption channels (Branch-A/B convention: claim_email IS NULL =
        # server-side machine auto-redeem, IS NOT NULL = human form-submit)
        # plus `opened` (claim_page_opened_at). Raw `used` compared machine-
        # capable cohorts (gateway agents, auto-redeemed 0-25s after mint
        # since 07-04) against human-click-only cohorts (Claude.ai/desktop,
        # header-less) — generic 99.3% vs claude 0% was that artifact, not a
        # copy verdict. The A/B signal is used_human/opened.
        try:
            cur.execute(
                "SELECT "
                "    COALESCE(NULLIF(claim_variant,''), 'generic') AS variant, "
                "    COUNT(*) FILTER (WHERE claim_minted_at IS NOT NULL "
                "                     AND claim_minted_at >= NOW() - INTERVAL '30 days') AS minted, "
                "    COUNT(*) FILTER (WHERE claim_used_at IS NOT NULL "
                "                     AND claim_used_at   >= NOW() - INTERVAL '30 days' "
                "                     AND claim_email IS NULL) AS used_agent, "
                "    COUNT(*) FILTER (WHERE claim_used_at IS NOT NULL "
                "                     AND claim_used_at   >= NOW() - INTERVAL '30 days' "
                "                     AND claim_email IS NOT NULL) AS used_human, "
                "    COUNT(*) FILTER (WHERE claim_page_opened_at IS NOT NULL "
                "                     AND claim_page_opened_at >= NOW() - INTERVAL '30 days') AS opened "
                "  FROM " + _hi_real_from() +
                " GROUP BY 1 "
                # ordinals: PG forbids expressions over output aliases in
                # ORDER BY (used_agent + used_human would error).
                " ORDER BY 2 DESC, 3 DESC, 4 DESC")
            v_rows = cur.fetchall() or []
            # Paid join is a SEPARATE query so a missing 'users' table or
            # email-column mismatch doesn't blow away the minted/used numbers.
            paid_by_variant = {}
            try:
                cur.execute(
                    "SELECT COALESCE(NULLIF(h.claim_variant,''), 'generic'), "
                    "       COUNT(DISTINCT LOWER(h.claim_email)) "
                    "  FROM " + _hi_real_from() +
                    "  JOIN users u "
                    "    ON LOWER(u.email) = LOWER(h.claim_email) "
                    " WHERE h.claim_used_at IS NOT NULL "
                    "   AND h.claim_used_at >= NOW() - INTERVAL '30 days' "
                    "   AND COALESCE(u.plan, 'free') NOT IN ('free','') "
                    " GROUP BY 1")
                for v_name, paid_n in (cur.fetchall() or []):
                    paid_by_variant[v_name or "generic"] = int(paid_n or 0)
            except Exception:
                try: conn.rollback()
                except Exception: pass

            variant_breakdown = []
            for v_name, minted, used_agent, used_human, opened in v_rows:
                minted     = int(minted or 0)
                used_agent = int(used_agent or 0)
                used_human = int(used_human or 0)
                opened     = int(opened or 0)
                used       = used_agent + used_human
                paid       = int(paid_by_variant.get(v_name or "generic", 0))
                use_rate       = round(100.0 * used / minted, 1) if minted else 0.0
                human_use_rate = round(100.0 * used_human / minted, 1) if minted else 0.0
                paid_rate      = round(100.0 * paid / minted, 1) if minted else 0.0
                variant_breakdown.append({
                    "variant": v_name or "generic",
                    "minted": minted,
                    "used": used,
                    "used_agent": used_agent,
                    "used_human": used_human,
                    "opened": opened,
                    "paid": paid,
                    "use_rate_pct": use_rate,
                    "human_use_rate_pct": human_use_rate,
                    "paid_rate_pct": paid_rate,
                })
            # Add zero-rows for variants that haven't fired yet (signal-to-
            # noise: easier to spot UA-detection gaps).
            seen = {r["variant"] for r in variant_breakdown}
            for v in ("claude", "cursor", "cline", "chatgpt", "generic"):
                if v not in seen:
                    variant_breakdown.append({
                        "variant": v, "minted": 0, "used": 0,
                        "used_agent": 0, "used_human": 0, "opened": 0,
                        "paid": 0, "use_rate_pct": 0.0,
                        "human_use_rate_pct": 0.0, "paid_rate_pct": 0.0})
            out["high_intent"]["variant_breakdown"] = variant_breakdown
        except Exception:
            try: conn.rollback()
            except Exception: pass

        # ── Step-by-step drop monitor ─────────────────────────────────
        # r-two-branch (2026-07-03): the trunk + two-branch waterfall is now
        # computed by routes.mcp_high_intent_claim.build_step_waterfall — the
        # SAME builder the step-drop endpoint uses, so the card and the
        # endpoint agree by construction (the 06-27 r-claim-honest fix had to
        # be applied twice because each surface had its own copy). Agents
        # auto-redeem with NO email, humans form-submit WITH one; modeling
        # them as one linear chain produced the "Email 1 → Trial key 12 =
        # -1100% drop" artifact that pinned step_drop_alarm True (07-03
        # flywheel-truth QA). Fail-soft: a bad query CANNOT blank the page.
        try:
            from routes.mcp_high_intent_claim import build_step_waterfall
            # r-cursor-shadow (2026-07-02): private cursor name — `with
            # conn.cursor() as cur:` here would shadow and CLOSE the
            # function-wide `cur`, silently zeroing every query below.
            with conn.cursor() as _sd_cur:
                def _ds(sql):
                    try:
                        _sd_cur.execute(sql)
                        return int((_sd_cur.fetchone() or [0])[0] or 0)
                    except Exception:
                        try: conn.rollback()
                        except Exception: pass
                        return 0
                wf = build_step_waterfall(_ds, days=30)
            out["high_intent"]["step_drop"] = wf["steps"]
            out["high_intent"]["branch_agent"] = wf["branch_agent"]
            out["high_intent"]["branch_human"] = wf["branch_human"]
            out["high_intent"]["paid_total_30d"] = wf["paid_total"]
            # r-claim-honest (2026-06-27): genuine human browser opens of /claim,
            # kept as a diagnostic (agents redeem server-side — ~0 by design).
            out["high_intent"]["human_page_opens"] = wf["human_page_opens"]
            out["step_drop_alarm"] = wf["alarm"]
            out["step_drop_killer"] = wf["killer_step"]
            out["step_drop_killer_pct"] = wf["killer_drop_pct"]
        except Exception as e:
            logger.debug("[funnel_health] step_drop probe failed: %s", e)
            try: conn.rollback()
            except Exception: pass

        # ── Mint → first-call cliff (r-mint-cliff 2026-08-12) ─────────
        # 41.3% of minted keys (309/748 in 30d) never make ONE call — the
        # largest absolute loss in this funnel, and until now visible ONLY as
        # the key_issued→first_call count above. A count cannot distinguish a
        # re-mint ARTIFACT (one agent minting 19 keys and using the last) from
        # a delivery bug from an agent that genuinely left, and those imply
        # completely different fixes. Published HERE, beside the waterfall it
        # explains, rather than in a corner nobody opens.
        #
        # Same builder as GET /api/v1/admin/mcp/mint-cliff, so the card and the
        # endpoint agree by construction — see the r-two-branch note above for
        # what happens when each surface keeps its own copy.
        #
        # ★ FAIL-SOFT BUT NEVER FAIL-FLATTERING: on any error this sets the
        # block to an explicit UNMEASURED marker, never to zeros. A 0 here
        # would read as "no keys died", the exact opposite of "we could not
        # look" — and a flattering zero is a bug.
        try:
            from routes.mcp_mint_cliff import build_mint_cliff
            # r-cursor-shadow (2026-07-02): private cursor name — a bare `cur`
            # here would shadow and CLOSE the function-wide one, silently
            # zeroing every query below it.
            with conn.cursor() as _mc_cur:
                out["mint_cliff"] = build_mint_cliff(_mc_cur, days=30)
        except Exception as e:
            logger.debug("[funnel_health] mint_cliff probe failed: %s", e)
            try: conn.rollback()
            except Exception: pass
            out["mint_cliff"] = {
                "ok": False,
                "unmeasured": [f"probe failed: {type(e).__name__}"],
                "population": None,
                "cohorts": None,
                "note": ("UNMEASURED — the cliff probe failed. This is NOT "
                         "'no keys died'; it is 'we could not look'."),
            }

        # ── Per-AI-platform breakdown ─────────────────────────────────
        # Match on mcp_call_log.platform (LIKE) + mcp_upgrade_signals.mcp_client.
        # conversions_30d here = pair_codes REDEMPTIONS (user_agent_at_view
        # match) — labeled 'Redemptions' on the card since 2026-07-03; real
        # Stripe conversions are the hero real_conversions_30d. JSON key kept
        # for back-compat.
        for key, label, patterns in _AI_PLATFORMS:
            row = {"key": key, "label": label, "requests_30d": 0,
                   "distinct_sessions_30d": 0, "signals_30d": 0,
                   "conversions_30d": 0, "conv_rate_pct": 0.0}
            try:
                # FIX (2026-06-22): this read mcp_call_log.platform, but mcp_call_log is
                # logged WITHOUT a platform column (_bulk_log_call + onboarding inserts omit
                # it) → every per-platform match was 0. The CANONICAL, correctly-attributed
                # r-perplatform-source (2026-06-23): read mcp_tool_calls, NOT
                # mcp_connections. mcp_connections was effectively empty (~3 rows/7d)
                # because ai_tracking.log_mcp_connection's ON CONFLICT bug swallowed
                # every insert (now fixed, but it refills over ~30d). mcp_tool_calls
                # is the populated, correctly-attributed table (platform + client_name
                # + user_agent per row, ~1.2k/7d) the working /mcp/funnel endpoint reads.
                # Match platform OR client_name OR user_agent; distinct = callers by IP.
                # NOTE: the signals + conversions per-platform sub-queries below are a
                # SEPARATE, harder gap (need a session→platform join) — left as-is.
                like_clauses = " OR ".join(
                    "LOWER(COALESCE(platform,'')) LIKE %s OR LOWER(COALESCE(client_name,'')) LIKE %s "
                    "OR LOWER(COALESCE(user_agent,'')) LIKE %s"
                    for _ in patterns)
                params = tuple(x for p in patterns for x in (f"%{p}%", f"%{p}%", f"%{p}%"))
                cur.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT ip_address) "
                    "  FROM mcp_tool_calls "
                    " WHERE created_at >= NOW() - INTERVAL '30 days' "
                    "   AND (" + like_clauses + ")",
                    params)
                r = cur.fetchone() or (0, 0)
                row["requests_30d"] = int(r[0] or 0)
                row["distinct_sessions_30d"] = int(r[1] or 0)
            except Exception:
                try: conn.rollback()
                except Exception: pass
            try:
                like_clauses = " OR ".join(
                    "LOWER(COALESCE(mcp_client,'')) LIKE %s" for _ in patterns)
                params = tuple(f"%{p}%" for p in patterns)
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_upgrade_signals "
                    " WHERE created_at >= NOW() - INTERVAL '30 days' "
                    "   AND (" + like_clauses + ")",
                    params)
                row["signals_30d"] = int((cur.fetchone() or (0,))[0] or 0)
            except Exception:
                try: conn.rollback()
                except Exception: pass
            try:
                like_clauses = " OR ".join(
                    "LOWER(COALESCE(user_agent_at_view,'')) LIKE %s"
                    for _ in patterns)
                params = tuple(f"%{p}%" for p in patterns)
                cur.execute(
                    "SELECT COUNT(*) FROM mcp_pair_codes "
                    " WHERE redeemed_at IS NOT NULL "
                    "   AND redeemed_at >= NOW() - INTERVAL '30 days' "
                    "   AND (" + like_clauses + ")",
                    params)
                row["conversions_30d"] = int((cur.fetchone() or (0,))[0] or 0)
            except Exception:
                try: conn.rollback()
                except Exception: pass
            if row["distinct_sessions_30d"] > 0:
                row["conv_rate_pct"] = round(
                    100.0 * row["conversions_30d"]
                    / row["distinct_sessions_30d"], 2)
            out["platforms"].append(row)

        # r-attribution (2026-06-30; CORRECTED 2026-07-02): the zeros were
        # blamed on mcp_tool_calls being ~85% platform='unknown' — a
        # MISDIAGNOSIS. The per-platform view also matches client_name and
        # user_agent, so it returns real rows (Claude ≈194 req / 50 callers
        # in the 30d window when the query is run by hand). The actual cause
        # of the zeros was the cursor-shadowing bug fixed above
        # (r-cursor-shadow). ai_cumulative stays as a COMPLEMENT — lifetime
        # reach-by-engine alongside the 30d window — not a replacement.
        try:
            _eng_keys = {k for k, _, _ in _AI_PLATFORMS}
            cur.execute("SELECT platform, name, total_requests FROM ai_cumulative "
                        "ORDER BY total_requests DESC")
            out["ai_platforms_reach"] = [
                {"key": str(r[0]).strip().lower(),
                 "label": r[1] or r[0],
                 "requests_lifetime": int(r[2] or 0)}
                for r in cur.fetchall()
                if str(r[0] or "").strip().lower() in _eng_keys
            ]
        except Exception:
            try: conn.rollback()
            except Exception: pass
            out["ai_platforms_reach"] = []

        # ── Pricing A/B status ────────────────────────────────────────
        try:
            from routes.pricing_ab import (_ab_active, _ab_disabled,
                                            _two_proportion_z, _z_to_pct,
                                            _arm_b_price_id)
            ab_active = bool(_ab_active())
            kill = bool(_ab_disabled())
            arm_b_ok = bool(_arm_b_price_id())
        except Exception:
            ab_active = False
            kill = False
            arm_b_ok = False
            _two_proportion_z = None
            _z_to_pct = None
        out["ab_status"]["ab_active"] = ab_active
        out["ab_status"]["kill_switch"] = kill
        out["ab_status"]["arm_b_configured"] = arm_b_ok

        # 2026-06-12: the REAL failure (exposed via the named-exception probe):
        # InterfaceError: cursor already closed. This section ran after the
        # step-drop `with` block that used to shadow-and-close `cur`, so
        # every read here died and rendered "0 impressions / missing table"
        # while pricing_ab_events held live rows. Open a fresh cursor.
        # (2026-07-02: the shadowing itself is fixed — r-cursor-shadow above —
        # the fresh cursor stays as a cheap guard.)
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            cur = conn.cursor()
        except Exception:
            cur = None

        for arm in ("A", "B"):
            try:
                cur.execute(
                    "SELECT event_type, COUNT(*) "
                    "  FROM pricing_ab_events "
                    " WHERE cohort = %s "
                    "   AND event_at >= NOW() - INTERVAL '30 days' "
                    " GROUP BY event_type", (arm,))
                imp = 0
                clicks = 0
                checkouts = 0
                for et, n in cur.fetchall() or []:
                    n = int(n or 0)
                    if et == "impression":
                        imp = n
                    elif et == "click_upgrade":
                        clicks = n
                    elif et == "stripe_checkout_complete":
                        checkouts = n
                conv = round(100.0 * checkouts / imp, 3) if imp else 0.0
                out["ab_status"]["cohorts"][arm] = {
                    "impressions": imp,
                    "upgrade_clicks": clicks,
                    "checkouts": checkouts,
                    "conv_rate_pct": conv,
                }
            except Exception as _ab_e:
                # carry the REAL exception out — this read failed for weeks as
                # an anonymous "missing table" while the table sat there with
                # live impressions. Name the actual error so it can't hide.
                out["missing_tables"].append(
                    f"pricing_ab_events ({type(_ab_e).__name__}: {str(_ab_e)[:80]})")
                try:
                    conn.rollback()
                except Exception:
                    pass
                out["ab_status"]["cohorts"][arm] = {
                    "impressions": 0, "upgrade_clicks": 0,
                    "checkouts": 0, "conv_rate_pct": 0.0}

        cA = out["ab_status"]["cohorts"]["A"]
        cB = out["ab_status"]["cohorts"]["B"]
        if _two_proportion_z and _z_to_pct:
            try:
                nA, nB = cA["impressions"], cB["impressions"]
                pA = cA["checkouts"] / nA if nA else 0
                pB = cB["checkouts"] / nB if nB else 0
                z = _two_proportion_z(pA, nA, pB, nB)
                out["ab_status"]["z"] = round(z, 2)
                out["ab_status"]["sig_pct"] = _z_to_pct(z)
                if nA and nB:
                    if pB > pA:
                        out["ab_status"]["winner"] = "B"
                    elif pA > pB:
                        out["ab_status"]["winner"] = "A"
                    else:
                        out["ab_status"]["winner"] = "tie"
            except Exception:
                pass

        # ── Recent events stream ──────────────────────────────────────
        # Pull last 20 across 4 sources, then merge + sort by timestamp DESC.
        evs: list[dict] = []

        # 1) Stripe webhook (checkout.session.completed) — proxy via
        #    mcp_session_upgrades (which we just write on the webhook).
        try:
            cur.execute(
                "SELECT 'stripe_checkout', upgraded_at, plan, user_email "
                "  FROM mcp_session_upgrades "
                " ORDER BY upgraded_at DESC LIMIT 20")
            for r in cur.fetchall() or []:
                evs.append({
                    "kind": "stripe_checkout",
                    "ts": r[1].isoformat() if r[1] else "",
                    "detail": f"plan={r[2] or '?'} email={r[3] or '?'}"
                })
        except Exception:
            pass

        # 2) Expired demote (users.demoted_at).
        try:
            cur.execute(
                "SELECT 'expired_demote', demoted_at, email, plan "
                "  FROM users "
                " WHERE demoted_at IS NOT NULL "
                " ORDER BY demoted_at DESC LIMIT 20")
            for r in cur.fetchall() or []:
                evs.append({
                    "kind": "expired_demote",
                    "ts": r[1].isoformat() if r[1] else "",
                    "detail": f"{r[2] or '?'} → plan={r[3] or '?'}"
                })
        except Exception:
            pass

        # 3) Renewal nudge sends.
        try:
            cur.execute(
                "SELECT 'renewal_nudge', sent_at, email, days_remaining_at_send "
                "  FROM renewal_nudge_log "
                " ORDER BY sent_at DESC LIMIT 20")
            for r in cur.fetchall() or []:
                evs.append({
                    "kind": "renewal_nudge",
                    "ts": r[1].isoformat() if r[1] else "",
                    "detail": f"{r[2] or '?'} ({r[3] or '?'}d left)"
                })
        except Exception:
            pass

        # 4) Pricing A/B cohort assignments (impressions).
        try:
            try:
                conn.rollback()  # same dead-cursor class as the cohort reader
            except Exception:
                pass
            cur = conn.cursor()
            cur.execute(
                "SELECT 'ab_event', event_at, cohort, event_type "
                "  FROM pricing_ab_events "
                " ORDER BY event_at DESC LIMIT 20")
            for r in cur.fetchall() or []:
                evs.append({
                    "kind": "ab_event",
                    "ts": r[1].isoformat() if r[1] else "",
                    "detail": f"arm={r[2]} event={r[3]}"
                })
        except Exception:
            pass

        # Sort + truncate.
        evs.sort(key=lambda e: e.get("ts") or "", reverse=True)
        out["events"] = evs[:20]

        # Dedup the missing_tables list — keep first-seen order.
        seen: set = set()
        uniq: list = []
        for t in out["missing_tables"]:
            if t in seen:
                continue
            seen.add(t)
            uniq.append(t)
        out["missing_tables"] = uniq

        try: cur.close()
        except Exception: pass
    except Exception as e:
        logger.warning("funnel_health probe loop failed: %s", e)
    finally:
        try: conn.close()
        except Exception: pass

    return out


_REFRESH_LOCK = threading.Lock()   # single-flight guard for the bg rebuild
_REFRESH_RUNNING = False


def _refresh_cache() -> None:
    """Rebuild the blob and publish it. Runs in a daemon thread."""
    global _REFRESH_RUNNING
    try:
        data = _build_data()
        _CACHE["data"] = data
        _CACHE["ts"] = time.time()
    except Exception as e:  # _build_data is defensive, but never kill the flag
        logger.warning("funnel_health background refresh failed: %s", e)
    finally:
        with _REFRESH_LOCK:
            _REFRESH_RUNNING = False


def _data_cached() -> dict:
    """Return cached data; refresh in the background when stale.

    502-fix (2026-07-01): the old version re-ran the full ~30-query
    _build_data() SYNCHRONOUSLY on every cache miss (TTL 60s → sentinel's
    hourly page scan ALWAYS hit a cold cache). Cold build is ~5s on a good
    day but unbounded when Neon is slow — past Railway's 30s gunicorn
    timeout the worker gets killed and the edge returns 502 (the recurring
    red page). Now: stale data is served instantly and ONE daemon thread
    (single-flight) rebuilds off the request path — same
    stale-while-revalidate shape as routes/surface_brain._SURFACES_TTL_S.
    Only the first request after process boot still builds inline (bounded
    by the 8s per-probe statement_timeout in _conn)."""
    global _REFRESH_RUNNING
    now = time.time()
    data = _CACHE["data"]
    if data is not None and (now - _CACHE["ts"]) < _CACHE_TTL_S:
        return data
    if data is not None:
        # Stale: serve it now, rebuild in the background (single-flight).
        with _REFRESH_LOCK:
            if not _REFRESH_RUNNING:
                _REFRESH_RUNNING = True
                threading.Thread(target=_refresh_cache,
                                 name="funnel-health-refresh",
                                 daemon=True).start()
        return data
    # First build since boot — no stale copy to serve, run inline.
    data = _build_data()
    _CACHE["data"] = data
    _CACHE["ts"] = time.time()
    return data


# ── HTML page ─────────────────────────────────────────────────────────

def _fmt_n(n: Any) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return "0"


def _fmt_pct(p: Any) -> str:
    if p is None:
        return "—"
    try:
        return f"{float(p):.1f}%"
    except Exception:
        return "—"


def _render_html(data: dict, admin_key: str) -> str:
    """Render the dashboard as a single self-contained HTML page.
    Matches the visual style of /admin/feedback (palette + spacing tokens)."""

    k = data["kpis"]
    f = data["funnel"]
    s = data["signals_with_tool_tagged"]
    sess = data["session_upgrades"]
    rn = data["renewal_nudge"]
    coh = data["source_plan_cohort"]
    hi = data.get("high_intent", {"sessions_30d": 0, "claims_minted_30d": 0,
                                   "claims_used_30d": 0, "claims_redeemed_30d": 0,
                                   "claim_to_paid_30d": 0,
                                   "minted_rate_pct": 0.0,
                                   "claim_to_paid_rate_pct": 0.0,
                                   "threshold": 2})
    # r-two-branch (2026-07-03): ONE live threshold value + a correct ordinal
    # for the card copy. The old markup defaulted to 3 in the title and 2 in
    # the copy, and its ordinal only knew 'nd'/'rd' — env threshold=1
    # rendered as "1rd".
    _hi_t = int(hi.get("threshold") or 2)
    _hi_ord = {1: "st", 2: "nd", 3: "rd"}.get(_hi_t, "th")
    ab = data["ab_status"]
    plats = data["platforms"]
    evs = data["events"]
    missing = data["missing_tables"]
    wc = data.get("writer_canary", {})

    def _canary_chip(tbl: str) -> str:
        """Writer-canary sub-stat markup: green 'ok' means a synthetic INSERT
        was accepted (then rolled back), so a 0 total = no eligible events,
        not a dead writer. Red = the write path itself is broken."""
        v = str(wc.get(tbl) or "—")
        color = ("#22c55e" if v == "ok"
                 else "var(--muted)" if v == "—" else "#ef4444")
        return (f'<div class="sub-stat"><div class="ss-l">Writer path</div>'
                f'<div class="ss-v" style="font-size:13px;color:{color}">'
                f'{_esc(v)}</div></div>')

    admin_key_safe = _esc(admin_key, quote=True)

    # Hero KPIs.
    keys_by_tier = k.get("dev_keys_by_tier") or {}
    tier_strs = " · ".join(
        f"{_fmt_n(v)} {_esc(t)}" for t, v in sorted(keys_by_tier.items(),
                                                   key=lambda x: -int(x[1] or 0))
    ) or "no keys"

    # ── Unified funnel header: reach → usage → conversion, bifurcated ──
    # One honest strip connecting the /ai reach number to real tool-call
    # usage to real Stripe cash, so reach (mostly crawlers) is never read
    # as usage. Every value computed live in _build_data; None → '—'.
    def _vn(x):
        return _fmt_n(x) if x is not None else "—"
    _reach_ext = k.get("reach_external_ai")
    _reach_tot = k.get("reach_total_served")
    _use_real  = k.get("tool_calls_30d_real")
    _use_tot   = k.get("tool_calls_30d_total")
    _real_conv = k.get("real_conversions_30d")
    _paid_keys = int(keys_by_tier.get("paid") or 0) + int(keys_by_tier.get("enterprise") or 0)
    _use_int_pct = (round(100 * (1 - (_use_real / _use_tot)))
                    if (_use_real is not None and _use_tot) else None)
    _r2u = (round(100 * _use_real / _reach_ext, 1)
            if (_use_real and _reach_ext) else None)
    # Same-window variant: 7d real calls / 7d external-AI reach — the honest
    # weekly rate (the % above divides 30d usage by cumulative-since-Feb
    # reach). Watched weekly + WoW-floored as flywheel lane 6.
    _use7 = k.get("tool_calls_7d_real")
    _reach7 = k.get("reach_external_ai_7d")
    _r2u7 = (round(100 * _use7 / _reach7, 1)
             if (_use7 and _reach7) else None)
    unified_funnel_html = f"""
  <div style="display:flex;align-items:baseline;gap:10px;margin:4px 0 10px;flex-wrap:wrap">
    <span style="font-size:13px;font-weight:600;letter-spacing:0.04em;color:var(--ink)">UNIFIED FUNNEL — reach → usage → conversion</span>
    <span style="font-size:11px;color:var(--muted)">honest · internal noise stripped · 60s cache</span>
  </div>
  <div class="heros">
    <div class="hero">
      <div class="hero-l">1 · Reach (AI platforms)</div>
      <div class="hero-v">{_vn(_reach_ext)}</div>
      <div class="hero-d">external AI · of {_vn(_reach_tot)} total served — mostly crawlers indexing you for citations</div>
    </div>
    <div class="hero">
      <div class="hero-l">2 · Usage (tools invoked)</div>
      <div class="hero-v">{_vn(_use_real)}</div>
      <div class="hero-d">real external · 30d · of {_vn(_use_tot)} calls{(' · ' + str(_use_int_pct) + '% internal self-heal stripped') if _use_int_pct is not None else ''}</div>
    </div>
    <div class="hero">
      <div class="hero-l">3 · Conversion (real paid · 30d)</div>
      <div class="hero-v">{_vn(_real_conv)}</div>
      <div class="hero-d">real Stripe payments · {_fmt_n(_paid_keys)} active paid keys · the $10 pack converts (MRR card below = plan run-rate)</div>
    </div>
    <div class="hero">
      <div class="hero-l">⟂ Bottleneck: reach → usage</div>
      <div class="hero-v">{(str(_r2u) + '%') if _r2u is not None else '—'}</div>
      <div class="hero-d">of AI platforms that find you actually invoke a tool — the leak is here, not the offer{(' · same-window 7d: ' + str(_r2u7) + '% (' + _vn(_use7) + '/' + _vn(_reach7) + ' — flywheel lane 6)') if _r2u7 is not None else ''}</div>
    </div>
  </div>"""

    # Funnel waterfall (vertical).
    stages = [
        ("Tool calls (30d)",          f["calls_30d"],
            None),
        ("Distinct active callers (30d)",  f["distinct_paid_users_30d"],
            f["stage_drops_pct"].get("calls→distinct")),
        ("Upgrade signals",           f["signals_30d"],
            f["stage_drops_pct"].get("distinct→signals")),
        ("Pair codes minted",         f["codes_minted_30d"],
            f["stage_drops_pct"].get("signals→codes")),
        ("Pages viewed (/redeem)",    f["pages_viewed_30d"],
            f["stage_drops_pct"].get("codes→viewed")),
        ("Stripe clicks",             f["stripe_clicked_30d"],
            f["stage_drops_pct"].get("viewed→clicked")),
        ("Redemptions (pair codes)"
             if k.get("conversions_30d_source") == "pair_code_redemptions"
             else "Conversions (gross)",
                                      f["conversions_30d"],
            f["stage_drops_pct"].get("clicked→converted")),
    ]
    funnel_rows = []
    for label, val, drop in stages:
        drop_html = ""
        if drop is not None:
            color = ("#ef4444" if drop >= 80
                     else "#f59e0b" if drop >= 50
                     else "#22c55e")
            drop_html = (f'<div class="drop" style="color:{color}">'
                         f'↓ {drop:.1f}% drop</div>')
        funnel_rows.append(
            f'<div class="stage">'
            f'  <div class="stage-label">{_esc(label)}</div>'
            f'  <div class="stage-val">{_fmt_n(val)}</div>'
            f'  {drop_html}'
            f'</div>'
        )
    funnel_html = "\n".join(funnel_rows)

    # Per-tool signals.
    sig_rows_html = "".join(
        f'<tr><td>{_esc(r["tool"])}</td>'
        f'<td class="num">{_fmt_n(r["count"])}</td></tr>'
        for r in (data.get("signals_per_tool") or [])
    ) or '<tr><td colspan="2" class="empty">No signals in last 30d.</td></tr>'

    # Per-AI-platform table.
    plat_rows_html = "".join(
        f'<tr>'
        f'  <td>{_esc(p["label"])}</td>'
        f'  <td class="num">{_fmt_n(p["requests_30d"])}</td>'
        f'  <td class="num">{_fmt_n(p["distinct_sessions_30d"])}</td>'
        f'  <td class="num">{_fmt_n(p["signals_30d"])}</td>'
        f'  <td class="num">{_fmt_n(p["conversions_30d"])}</td>'
        f'  <td class="num">{p["conv_rate_pct"]:.2f}%</td>'
        f'</tr>'
        for p in plats
    ) or '<tr><td colspan="6" class="empty">No platform data.</td></tr>'

    # Two-branch step-drop card rows (r-two-branch 2026-07-03). Branch bases
    # are SPLITS of claim_redeemed, not drops — drop_from_prev is None there
    # and renders as '—'. Only mechanical steps go red (they alone can alarm).
    _BR_HEAD = {
        "trunk": "TRUNK — every real paywall-hit session",
        "agent": "BRANCH A · AGENT — auto-redeem, key-attributed (claim_email IS NULL)",
        "human": "BRANCH B · HUMAN — email form-submit (claim_email IS NOT NULL)",
    }
    _sd_rows = []
    _sd_last_branch = None
    for s_ in (hi.get("step_drop") or []):
        _br = s_.get("branch") or "trunk"
        if _br != _sd_last_branch:
            _sd_rows.append(
                '<tr><td colspan="4" style="font-size:10px;letter-spacing:0.06em;'
                'text-transform:uppercase;color:var(--accent);padding-top:14px;'
                f'border-bottom:none">{_esc(_BR_HEAD.get(_br, _br))}</td></tr>')
            _sd_last_branch = _br
        _dfp = s_.get("drop_from_prev")
        _mech = bool(s_.get("mechanical"))
        if _dfp is None:
            _dfp_html = '<span style="color:var(--muted)">—</span>'
        else:
            _dc = ("#ef4444" if (_mech and _dfp > 95)
                   else "#f59e0b" if _dfp > 50 else "var(--muted)")
            _dfp_html = f'<span style="color:{_dc}">{_dfp:.1f}%</span>'
        _cum = s_.get("drop_pct")
        _cum_html = "—" if _cum is None else f"{_cum:.1f}%"
        _pad = "10px" if _br == "trunk" else "22px"
        _sd_rows.append(
            f'<tr><td style="padding-left:{_pad}">{_esc(s_.get("label", "?"))}</td>'
            f'<td style="text-align:right">{_fmt_n(s_.get("count", 0))}</td>'
            f'<td style="text-align:right">{_dfp_html}</td>'
            f'<td style="text-align:right;color:var(--muted)">{_cum_html}</td></tr>')
    step_rows_html = "".join(_sd_rows) or (
        '<tr><td colspan="4" style="color:var(--muted);text-align:center;'
        'padding:18px">No data yet</td></tr>')

    # ── Mint→first-call cliff rows (r-mint-cliff 2026-08-12) ──────────
    # The step above says N keys never called. This says WHY, split into
    # mutually exclusive causes. Rendered HERE rather than JSON-only: a
    # diagnosis nobody opens is not published. ★An UNMEASURED block must render
    # as UNMEASURED, never as an empty table that reads like "nothing to see".
    _mc = data.get("mint_cliff") or {}
    _mc_pop = _mc.get("population")
    if not _mc.get("ok") or _mc_pop is None:
        _mc_note = _esc(str(_mc.get("note") or "UNMEASURED — no reading taken."))
        cliff_rows_html = (
            '<tr><td colspan="3" style="color:#f59e0b;text-align:center;'
            f'padding:18px">UNMEASURED · {_mc_note}</td></tr>')
        cliff_head_html = "UNMEASURED"
    else:
        _never = _mc_pop.get("never_called") or 0
        _pct = _mc_pop.get("never_called_pct")
        cliff_head_html = (
            f"{_fmt_n(_never)} of {_fmt_n(_mc_pop.get('minted') or 0)} minted keys "
            f"never called" + (f" · {_pct:.1f}%" if _pct is not None else ""))
        _cr = []
        for _c in (_mc.get("cohorts") or []):
            _n = _c.get("n") or 0
            _cp = _c.get("pct_of_never_called")
            # The artifact bucket is the one that changes how the headline
            # number may be read at all — colour it so it cannot be skimmed past.
            _col = "#f59e0b" if _c.get("code") == "superseded_by_remint" else "var(--fg)"
            _cr.append(
                f'<tr><td style="padding-left:10px;color:{_col}">'
                f'{_esc(_c.get("label", "?"))}</td>'
                f'<td style="text-align:right;color:{_col}">{_fmt_n(_n)}</td>'
                f'<td style="text-align:right;color:var(--muted)">'
                + ("—" if _cp is None else f"{_cp:.1f}%") + "</td></tr>"
                + f'<tr><td colspan="3" style="padding-left:10px;font-size:10px;'
                  f'color:var(--muted);border-bottom:none">'
                  f'{_esc(_c.get("means", ""))}</td></tr>')
        if not _mc.get("sums_ok", True):
            _cr.insert(0, '<tr><td colspan="3" style="color:#ef4444;padding:8px">'
                          f'{_esc(str(_mc.get("sums_note", "")))}</td></tr>')
        cliff_rows_html = "".join(_cr) or (
            '<tr><td colspan="3" style="color:var(--muted);text-align:center;'
            'padding:18px">No never-called keys in window</td></tr>')

    # A/B table.
    cA = ab["cohorts"].get("A") or {}
    cB = ab["cohorts"].get("B") or {}
    sig_pct = ab.get("sig_pct") or 0.0
    z = ab.get("z") or 0.0
    winner = ab.get("winner") or "no_data"
    ab_active = ab.get("ab_active")
    ab_kill = ab.get("kill_switch")
    # r72: a configured-but-empty A/B was rendering a green "ACTIVE" with
    # winner:no_data — misleading. If it's active yet recording zero
    # impressions (frontend not posting, or pricing_ab_events table
    # missing/mismatched — see missing_tables), say so in amber so the
    # operator knows to check, instead of implying a running experiment.
    _abc = ab.get("cohorts") or {}
    ab_total_imp = int((_abc.get("A") or {}).get("impressions", 0) or 0) + \
                   int((_abc.get("B") or {}).get("impressions", 0) or 0)
    ab_no_data = bool(ab_active) and ab_total_imp == 0
    ab_state = ("KILL SWITCH ON" if ab_kill
                else "ACTIVE · NO DATA (events not recording)" if ab_no_data
                else "ACTIVE" if ab_active
                else "OFF (arm A only)")
    ab_state_color = ("#ef4444" if ab_kill
                      else "#f59e0b" if ab_no_data
                      else "#22c55e" if ab_active
                      else "#94a3b8")
    sig_color = ("#22c55e" if sig_pct >= 95
                 else "#f59e0b" if sig_pct >= 80
                 else "#94a3b8")

    # Events stream.
    ev_html_parts = []
    for e in evs:
        kind = e.get("kind") or "event"
        color = {
            "stripe_checkout": "#22c55e",
            "expired_demote":  "#f59e0b",
            "renewal_nudge":   "#3da9fc",
            "ab_event":        "#a78bfa",
        }.get(kind, "#94a3b8")
        ts = (e.get("ts") or "")[:19].replace("T", " ")
        ev_html_parts.append(
            f'<div class="ev">'
            f'  <span class="ev-kind" style="background:{color}">{_esc(kind)}</span>'
            f'  <span class="ev-ts">{_esc(ts)}</span>'
            f'  <span class="ev-detail">{_esc(e.get("detail") or "")}</span>'
            f'</div>'
        )
    ev_html = "\n".join(ev_html_parts) or (
        '<div class="empty">No recent events.</div>')

    # Missing-tables chips.
    miss_html = ""
    if missing:
        miss_html = (
            '<div class="missing-strip">'
            '<strong>Schema gaps:</strong> '
            + " ".join(f'<span class="miss-chip">{_esc(t)}</span>'
                       for t in missing)
            + '</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Funnel Health — DC Hub Admin</title>
<meta name="robots" content="noindex,nofollow">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/dchub-brand.css">
<style>
  :root {{ --bg:#0a0e1a; --panel:#111726; --panel2:#0d1322; --border:#1f2940;
          --ink:#e6ecf5; --muted:#94a3b8; --accent:#3da9fc; --ok:#22c55e;
          --warn:#f59e0b; --bad:#ef4444; --shipped:#10b981; }}
  *{{box-sizing:border-box}}
  body {{ background:var(--bg); color:var(--ink);
         font-family:'Inter',system-ui,sans-serif; margin:0; padding:0; }}
  .wrap {{ max-width:1500px; margin:0 auto; padding:24px 20px 80px; }}
  h1 {{ font-size:24px; margin:0 0 4px; letter-spacing:-0.01em; }}
  h2 {{ font-size:14px; margin:0 0 12px; color:var(--muted);
        text-transform:uppercase; letter-spacing:0.06em; font-weight:600; }}
  .sub {{ color:var(--muted); margin:0 0 18px; font-size:13px; }}
  .refresh {{ float:right; font-size:12px; color:var(--muted);
              text-decoration:none; padding:6px 12px;
              background:var(--panel); border:1px solid var(--border);
              border-radius:6px; }}
  .refresh:hover {{ color:var(--ink); }}
  .missing-strip {{ background:#3a1f0f; border:1px solid #7c4a1f; border-radius:8px;
                    padding:10px 14px; font-size:13px; color:#fbbf24; margin:0 0 16px; }}
  .miss-chip {{ display:inline-block; background:#7c4a1f; color:#fef3c7;
                padding:2px 8px; border-radius:4px; font-size:11px;
                font-family:'JetBrains Mono',monospace; margin:0 4px 0 0; }}
  .heros {{ display:grid; grid-template-columns:repeat(4,1fr); gap:14px;
            margin:0 0 24px; }}
  @media (max-width:980px) {{ .heros {{ grid-template-columns:repeat(2,1fr); }} }}
  .hero {{ background:var(--panel); border:1px solid var(--border);
          border-radius:12px; padding:18px 20px; }}
  .hero-l {{ font-size:11px; color:var(--muted); text-transform:uppercase;
            letter-spacing:0.07em; font-weight:600; }}
  .hero-v {{ font-size:32px; font-weight:700; margin:6px 0 4px;
            letter-spacing:-0.02em; }}
  .hero-d {{ font-size:11px; color:var(--muted);
            font-family:'JetBrains Mono',monospace; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:16px;
          margin:0 0 18px; }}
  @media (max-width:1100px) {{ .grid {{ grid-template-columns:1fr; }} }}
  .card {{ background:var(--panel); border:1px solid var(--border);
          border-radius:12px; padding:18px 20px; }}
  .card-row {{ display:grid; grid-template-columns:repeat(3,1fr); gap:14px;
              margin-top:8px; }}
  .card-row .sub-stat {{ background:var(--panel2); border-radius:8px;
                          padding:10px 12px; }}
  .ss-l {{ font-size:10px; color:var(--muted); text-transform:uppercase;
          letter-spacing:0.06em; }}
  .ss-v {{ font-size:18px; font-weight:600; }}
  /* Funnel waterfall */
  .funnel {{ display:flex; flex-direction:column; gap:6px; margin-top:8px; }}
  .stage {{ background:var(--panel2); border:1px solid var(--border);
           border-radius:10px; padding:12px 16px; display:flex;
           align-items:center; gap:18px; }}
  .stage-label {{ font-size:12px; color:var(--muted);
                 text-transform:uppercase; letter-spacing:0.05em;
                 width:200px; flex-shrink:0; }}
  .stage-val {{ font-size:22px; font-weight:600; flex-grow:1; }}
  .stage .drop {{ font-size:11px; font-weight:600;
                 font-family:'JetBrains Mono',monospace; }}
  /* Tables */
  table {{ width:100%; border-collapse:collapse; font-size:13px;
          margin-top:8px; }}
  th, td {{ padding:8px 10px; text-align:left;
           border-bottom:1px solid var(--border); }}
  th {{ background:var(--panel2); font-size:10px; color:var(--muted);
        text-transform:uppercase; letter-spacing:0.06em; font-weight:600; }}
  tr:last-child td {{ border-bottom:none; }}
  td.num {{ font-family:'JetBrains Mono',monospace;
           text-align:right; white-space:nowrap; }}
  .empty {{ text-align:center; color:var(--muted); padding:20px 0;
           font-style:italic; }}
  /* A/B card */
  .ab-state {{ display:inline-block; padding:3px 10px; border-radius:999px;
              font-size:11px; font-weight:700; letter-spacing:0.05em;
              text-transform:uppercase; }}
  .kill-btn {{ background:var(--bad); color:#fff; border:none; cursor:pointer;
              padding:7px 14px; border-radius:6px; font-size:12px;
              font-weight:600; font-family:inherit; margin-top:10px; }}
  .kill-btn:hover {{ opacity:0.85; }}
  .kill-btn:disabled {{ opacity:0.4; cursor:not-allowed; }}
  /* Events stream */
  .ev {{ display:grid; grid-template-columns:140px 160px 1fr; gap:12px;
        padding:8px 0; border-bottom:1px solid var(--border);
        font-size:12px; align-items:center; }}
  .ev:last-child {{ border-bottom:none; }}
  .ev-kind {{ display:inline-block; padding:2px 8px; border-radius:4px;
            color:#0a0e1a; font-weight:700; font-size:10px;
            text-transform:uppercase; letter-spacing:0.05em; }}
  .ev-ts {{ color:var(--muted); font-family:'JetBrains Mono',monospace; }}
  .ev-detail {{ color:var(--ink); font-family:'JetBrains Mono',monospace; }}
  .pulse {{ background:var(--ok); color:#062a1d; padding:1px 6px; border-radius:4px;
           font-size:10px; font-weight:700; letter-spacing:0.04em; }}
  .footer {{ color:var(--muted); font-size:11px; margin-top:30px;
            text-align:center; font-family:'JetBrains Mono',monospace; }}
</style>
</head>
<body>
<div class="wrap">
  <a class="refresh" href="?admin_key={admin_key_safe}">↻ Refresh (60s TTL)</a>
  <a class="refresh" style="right:170px;background:#fbbf24;color:#000"
     href="/admin/qa/state-of-2026?admin_key={admin_key_safe}">▶ State of 2026 QA</a>
  <h1>Funnel Health <span class="pulse">LIVE</span></h1>
  <p class="sub">Single-pane view of every conversion signal — MCP funnel,
     Stripe MRR, new lifecycle events, per-platform breakdown.
     Cached 60s · DB {'OK' if data['db_ok'] else 'DOWN'} ·
     Generated {_esc(data['generated_at'])}</p>

  {miss_html}

  {unified_funnel_html}

  <!-- HERO KPIS -->
  <div class="heros">
    <div class="hero">
      <div class="hero-l">MRR</div>
      <div class="hero-v">${_fmt_n(k['mrr_usd'])}</div>
      <div class="hero-d">/mo plan-based run-rate · counts annual/comped as monthly — not Stripe cash-recurring</div>
    </div>
    <div class="hero">
      <div class="hero-l">Real conversions 30d</div>
      <div class="hero-v">{_vn(_real_conv)}</div>
      <div class="hero-d">Stripe-backed, comp-stripped · {_fmt_n(k['conversions_30d'])} {('redemptions (pair codes — NOT conversions)' if k.get('conversions_30d_source') == 'pair_code_redemptions' else 'gross incl. comp (mcp_conversions)')}</div>
    </div>
    <div class="hero">
      <div class="hero-l">Active dev keys</div>
      <div class="hero-v">{_fmt_n(k['active_dev_keys'])}</div>
      <div class="hero-d">{_esc(tier_strs)}</div>
    </div>
    <div class="hero">
      <div class="hero-l">Tool calls 7d (external)</div>
      <div class="hero-v">{(_fmt_n(k['tool_calls_7d_real']) if k.get('tool_calls_7d_real') is not None else '—')}</div>
      <div class="hero-d">{('de-looped · ' + _fmt_n(k.get('tool_calls_7d_incl_loops', k.get('tool_calls_7d', 0))) + ' incl-loops') if k.get('tool_calls_7d_real') is not None else 'refreshing — de-loop query timed out (transient DB load); auto-retries in 60s'}</div>
    </div>
  </div>

  <!-- FUNNEL + AB STATUS row -->
  <div class="grid">
    <div class="card">
      <h2>MCP Funnel Waterfall — 30d</h2>
      <div class="funnel">
        {funnel_html}
      </div>
    </div>
    <div class="card">
      <h2>Pricing A/B Status — 30d</h2>
      <p style="margin:6px 0 8px;">
        <span class="ab-state" style="background:{ab_state_color};color:#0a0e1a;">{_esc(ab_state)}</span>
        &nbsp;
        <span style="color:var(--muted);font-size:12px;">
          z={z:.2f} · <span style="color:{sig_color};font-weight:600">{sig_pct:.1f}% confidence</span> · winner: <strong>{_esc(winner)}</strong>
        </span>
      </p>
      <table>
        <thead><tr><th>Arm</th><th>Price</th><th>Impressions</th>
          <th>Upgrade clicks</th><th>Checkouts</th><th>Conv %</th></tr></thead>
        <tbody>
          <tr><td><strong>A</strong></td><td>$199</td>
            <td class="num">{_fmt_n(cA.get('impressions',0))}</td>
            <td class="num">{_fmt_n(cA.get('upgrade_clicks',0))}</td>
            <td class="num">{_fmt_n(cA.get('checkouts',0))}</td>
            <td class="num">{cA.get('conv_rate_pct',0):.3f}%</td></tr>
          <tr><td><strong>B</strong></td><td>$99</td>
            <td class="num">{_fmt_n(cB.get('impressions',0))}</td>
            <td class="num">{_fmt_n(cB.get('upgrade_clicks',0))}</td>
            <td class="num">{_fmt_n(cB.get('checkouts',0))}</td>
            <td class="num">{cB.get('conv_rate_pct',0):.3f}%</td></tr>
        </tbody>
      </table>
      <button id="killbtn" class="kill-btn">Kill A/B (force everyone to Arm A)</button>
      <div id="killmsg" style="font-size:11px;color:var(--muted);margin-top:8px;"></div>
    </div>
  </div>

  <!-- NEW TABLES row 1 -->
  <div class="grid">
    <div class="card">
      <h2>mcp_session_upgrades <span style="font-size:10px;color:var(--ok);">FIX E</span></h2>
      <div class="card-row">
        <div class="sub-stat">
          <div class="ss-l">Total</div>
          <div class="ss-v">{_fmt_n(sess['total'])}</div>
        </div>
        <div class="sub-stat">
          <div class="ss-l">Last 7d</div>
          <div class="ss-v">{_fmt_n(sess['last_7d'])}</div>
        </div>
        <div class="sub-stat">
          <div class="ss-l">Top plan</div>
          <div class="ss-v" style="font-size:14px;">{_esc((sess['top_platforms'][0]['plan'] if sess['top_platforms'] else '—'))}</div>
        </div>
        {_canary_chip('mcp_session_upgrades')}
      </div>
    </div>
    <div class="card">
      <h2>renewal_nudge_log <span style="font-size:10px;color:var(--accent);">DAY-330</span></h2>
      <div class="card-row">
        <div class="sub-stat">
          <div class="ss-l">Total sent</div>
          <div class="ss-v">{_fmt_n(rn['total'])}</div>
        </div>
        <div class="sub-stat">
          <div class="ss-l">Sent last 7d</div>
          <div class="ss-v">{_fmt_n(rn['sent_last_7d'])}</div>
        </div>
        <div class="sub-stat">
          <div class="ss-l">Eligible next 30d</div>
          <div class="ss-v">{_fmt_n(rn['eligible_next_30d'])}</div>
        </div>
        {_canary_chip('renewal_nudge_log')}
      </div>
    </div>
  </div>

  <!-- NEW TABLES row 2 -->
  <div class="grid">
    <div class="card">
      <h2>Signals with tool_requested tagged <span style="font-size:10px;color:var(--ok);">FIX C</span></h2>
      <div class="card-row">
        <div class="sub-stat">
          <div class="ss-l">Taggable 30d</div>
          <div class="ss-v">{_fmt_n(s.get('total_30d',0))}</div>
        </div>
        <div class="sub-stat">
          <div class="ss-l">% tagged</div>
          <div class="ss-v" style="color:{('#22c55e' if s.get('pct_tagged_30d',0)>=80 else '#f59e0b' if s.get('pct_tagged_30d',0)>=40 else '#ef4444')}">{s.get('pct_tagged_30d',0):.1f}%</div>
        </div>
        <div class="sub-stat">
          <div class="ss-l">Untagged</div>
          <div class="ss-v">{_fmt_n(s.get('untagged_30d',0))}</div>
        </div>
        <div class="sub-stat">
          <div class="ss-l">View-stamps excl.</div>
          <div class="ss-v" style="color:var(--muted)">{_fmt_n(s.get('excluded_view_stamps_30d',0))}</div>
        </div>
      </div>
      <div style="font-size:10px;color:var(--muted);margin-top:6px;">redeem_url_viewed rows are page-view stamps with no tool by design — excluded from the denominator (r-structural-untagged 2026-07-03).</div>
    </div>
    <div class="card">
      <h2>Pro-annual-onetime cohort <span style="font-size:10px;color:var(--warn);">DEMOTE-WATCH</span></h2>
      <div class="card-row">
        <div class="sub-stat">
          <div class="ss-l">Total</div>
          <div class="ss-v">{_fmt_n(coh['total'])}</div>
        </div>
        <div class="sub-stat">
          <div class="ss-l">Expiring 60d</div>
          <div class="ss-v">{_fmt_n(coh['expiring_next_60d'])}</div>
        </div>
        <div class="sub-stat">
          <div class="ss-l">Expired+stuck</div>
          <div class="ss-v" style="color:{'#ef4444' if coh['expired_not_demoted']>0 else 'var(--ink)'}">{_fmt_n(coh['expired_not_demoted'])}</div>
        </div>
      </div>
    </div>
  </div>

  <!-- 2026-06-07: HIGH-INTENT CLAIM (closes 0% MCP-conversion gap).
       r-two-branch 2026-07-03: title/copy render the LIVE env-driven hit
       gate instead of a hardcoded strike count. -->
  <div class="card" style="margin-bottom:18px;">
    <h2>High-intent claim funnel <span style="font-size:10px;color:var(--accent);">session-bound · mint threshold={_hi_t}</span></h2>
    <div class="card-row">
      <div class="sub-stat">
        <div class="ss-l">Paywall-hit sessions 30d</div>
        <div class="ss-v">{_fmt_n(hi.get('sessions_30d',0))}</div>
      </div>
      <div class="sub-stat">
        <div class="ss-l">Claims minted 30d</div>
        <div class="ss-v">{_fmt_n(hi.get('claims_minted_30d',0))}</div>
      </div>
      <div class="sub-stat">
        <div class="ss-l">Mint rate</div>
        <div class="ss-v" style="color:{('#22c55e' if hi.get('minted_rate_pct',0)>=80 else '#f59e0b' if hi.get('minted_rate_pct',0)>=40 else '#94a3b8')}">{hi.get('minted_rate_pct',0):.1f}%</div>
      </div>
      <div class="sub-stat">
        <div class="ss-l">Claims opened 30d (human)</div>
        <div class="ss-v">{_fmt_n(hi.get('claims_used_30d',0))}</div>
      </div>
      <div class="sub-stat">
        <div class="ss-l">Claims redeemed 30d (any channel)</div>
        <div class="ss-v" style="color:var(--muted)">{_fmt_n(hi.get('claims_redeemed_30d',0))}</div>
      </div>
      <div class="sub-stat">
        <div class="ss-l">Claim → paid 30d</div>
        <div class="ss-v" style="color:{('#22c55e' if hi.get('claim_to_paid_30d',0)>0 else 'var(--muted)')}">{_fmt_n(hi.get('claim_to_paid_30d',0))}</div>
      </div>
      <div class="sub-stat">
        <div class="ss-l">Claim-to-paid rate</div>
        <div class="ss-v" style="color:{('#22c55e' if hi.get('claim_to_paid_rate_pct',0)>=10 else '#f59e0b' if hi.get('claim_to_paid_rate_pct',0)>0 else 'var(--muted)')}">{hi.get('claim_to_paid_rate_pct',0):.1f}%</div>
      </div>
    </div>
    <div style="font-size:11px;color:var(--muted);margin-top:10px;">
      mcp-server tracks every paid-tool hit per (session_id, tool); on the
      <b>{_hi_t}{_hi_ord}</b> hit in 24h the paywall response embeds a signed
      <code>https://dchub.cloud/claim/&lt;token&gt;</code> URL. Agents auto-redeem
      it server-side (no email); humans enter an email → trial key via Resend.
      Threshold env-driven via <code>DCHUB_HIGH_INTENT_THRESHOLD</code> — the
      live value ({_hi_t}) is shown in the title. Mint rate = claims minted /
      real paywall-hit sessions, same 30d window. Claim → paid counts BOTH
      paths: email → users.plan, and key → Stripe pack/top-up.
    </div>
  </div>

  <!-- r-mint-cliff 2026-08-12: WHY the key_issued→first_call step drops.
       The waterfall below can say N keys never called; this says why, in
       mutually exclusive buckets that sum. superseded_by_remint is amber
       because it is an ARTIFACT — one agent minting many keys — and until it
       is sized the headline rate is not a loss rate. Endpoint:
       GET /api/v1/admin/mcp/mint-cliff -->
  <div class="card" style="margin-bottom:18px;">
    <h2>Mint → first-call cliff
      <span style="font-size:10px;color:var(--accent);">{cliff_head_html}</span>
    </h2>
    <table>
      <thead><tr>
        <th>Why this key never called</th>
        <th style="text-align:right">Keys (30d)</th>
        <th style="text-align:right">Share of never-called</th>
      </tr></thead>
      <tbody>
      {cliff_rows_html}
      </tbody>
    </table>
    <div style="font-size:11px;color:var(--muted);margin-top:10px;">
      Buckets are mutually exclusive and evaluated most-specific-cause first, so
      they sum to never-called. <b>Read the amber row first:</b> the funnel has
      measured ~15–19 re-mints per distinct agent, so keys whose sibling from the
      same IP <i>did</i> call are duplicate keys belonging to one working agent,
      not lost agents — deduct them before quoting the headline as a loss rate.
      Only <i>silent_no_return</i> supports “the agent left”;
      <i>unattributable_no_session</i> is UNKNOWN and is kept separate so rows we
      cannot see never inflate a behavioural story. A failed probe renders as
      UNMEASURED, never as zeros.
    </div>
  </div>

  <!-- r-two-branch 2026-07-03: trunk + two-branch drop monitor.
       TRUNK paywall_sessions → claims_minted → claim_redeemed, then
       BRANCH A (agent auto-redeem, key-attributed) and BRANCH B (human
       email). Drops are within-branch only, clamped [0,100]; the alarm
       fires ONLY on mechanical breakage with prev >= 5 — never on the
       upsell/paid intent steps. Endpoint:
       GET /api/v1/admin/mcp/high-intent/step-drop -->
  <div class="card" style="margin-bottom:18px;border:{(
      '2px solid #ef4444' if data.get('step_drop_alarm') else
      '1px solid var(--border)')};">
    <h2>High-intent funnel · two-branch step drop
      <span style="font-size:10px;color:{('#ef4444' if data.get('step_drop_alarm') else 'var(--accent)')};">
        {('ALARM · killer=' + str(data.get('step_drop_killer',''))) if data.get('step_drop_alarm') else 'monitoring'}
      </span>
    </h2>
    <table>
      <thead><tr>
        <th>Step</th>
        <th style="text-align:right">Count (30d)</th>
        <th style="text-align:right">Drop from prev</th>
        <th style="text-align:right">Cumulative drop vs minted</th>
      </tr></thead>
      <tbody>
      {step_rows_html}
      </tbody>
    </table>
    <div style="font-size:11px;color:var(--muted);margin-top:10px;">
      Agents auto-redeem the claim token server-side with no email, so Branch A
      is attributed by the minted API key (unlock_more_data calls via
      mcp_call_log, payments via mcp_topups); Branch B by claim_email. '—' =
      branch base (a split of claim_redeemed, not a drop). Killer step = the
      biggest MECHANICAL drop with prev ≥ 5 — a mint/redeem/key-issuance/first-call
      breakage screams within 60s (cache TTL); 0 on upsell/paid is a growth
      problem, not an incident.
      Endpoint: <code>/api/v1/admin/mcp/high-intent/step-drop</code>.
    </div>
  </div>

  <!-- Round 2 (2026-06-07): per-variant A/B breakdown
       The claim copy is platform-specific (claude/cursor/cline/chatgpt/generic).
       This card surfaces minted → used → paid per variant so we can pick a
       winner. Endpoint: GET /api/v1/admin/mcp/claim-variant-conversion
       r-variant-honest-split (2026-07-11): Used split into agent-auto vs human
       so machine auto-redeem can't masquerade as a copy win. -->
  <div class="card" style="margin-bottom:18px;">
    <h2>Claim copy A/B by platform <span style="font-size:10px;color:var(--accent);">5 variants · {len(hi.get('variant_breakdown', []) or [])} rows · used split agent/human</span></h2>
    <table>
      <thead><tr>
        <th>Variant</th>
        <th style="text-align:right">Minted</th>
        <th style="text-align:right" title="Machine auto-redeem by server.mjs (X-Internal-Key, ~1s after mint) — NOT a copy signal">Used (agent auto)</th>
        <th style="text-align:right" title="Human email form-submit on /claim/&lt;token&gt; — THE copy signal">Used (human)</th>
        <th style="text-align:right" title="Human loaded the /claim page">Opened</th>
        <th style="text-align:right">Paid</th>
        <th style="text-align:right" title="used_human / minted — the only rate comparable across variants">Human use rate</th>
        <th style="text-align:right">Paid rate</th>
      </tr></thead>
      <tbody>
      {''.join(
        f'<tr><td>{_esc(vb.get("variant","generic"))}</td>'
        f'<td style="text-align:right">{_fmt_n(vb.get("minted",0))}</td>'
        f'<td style="text-align:right;color:var(--muted)">{_fmt_n(vb.get("used_agent", vb.get("used",0)))}</td>'
        f'<td style="text-align:right;color:{("#22c55e" if vb.get("used_human",0)>0 else "var(--muted)")}">{_fmt_n(vb.get("used_human",0))}</td>'
        f'<td style="text-align:right">{_fmt_n(vb.get("opened",0))}</td>'
        f'<td style="text-align:right;color:{("#22c55e" if vb.get("paid",0)>0 else "var(--muted)")}">{_fmt_n(vb.get("paid",0))}</td>'
        f'<td style="text-align:right">{vb.get("human_use_rate_pct",0):.1f}%</td>'
        f'<td style="text-align:right;color:{("#22c55e" if vb.get("paid_rate_pct",0)>=10 else "#f59e0b" if vb.get("paid_rate_pct",0)>0 else "var(--muted)")}">{vb.get("paid_rate_pct",0):.1f}%</td>'
        f'</tr>'
        for vb in (hi.get('variant_breakdown', []) or []))}
      </tbody>
    </table>
    <div style="font-size:11px;color:var(--muted);margin-top:10px;">
      Each minted claim records the variant the human saw (locked on first observation).
      The mcp-server picks the variant from the inbound MCP <code>clientInfo.name</code>
      (Claude.ai / Cursor / Cline / ChatGPT) and falls back to UA matching.
      <strong>Used (agent auto)</strong> is the server-side machine auto-redeem
      (<code>claim_email IS NULL</code>, live since 07-04) — it fires ~1s after mint for
      any variant and measures redeem-path uptime, not copy. Cross-variant copy verdicts
      must use <strong>Used (human)</strong>/<strong>Opened</strong>: header-less hosts
      (Claude.ai / desktop) can only convert via a human click, and gateway-fronted
      sessions (<code>clientInfo.name='mcp'</code>, UA <code>node</code>) hide their real
      platform under <em>generic</em>.
      JSON: <a href="/api/v1/admin/mcp/claim-variant-conversion?admin_key={admin_key_safe}&amp;days=30"
            style="color:var(--accent)">/api/v1/admin/mcp/claim-variant-conversion</a>
    </div>
  </div>

  <!-- PER-AI-PLATFORM table -->
  <div class="card" style="margin-bottom:18px;">
    <h2>Per-AI-platform conversion (30d)</h2>
    <table>
      <thead><tr>
        <th>Platform</th>
        <th style="text-align:right">Requests</th>
        <th style="text-align:right">Distinct callers (IP)</th>
        <th style="text-align:right">Signals</th>
        <th style="text-align:right">Redemptions (pair codes)</th>
        <th style="text-align:right">Redeem %</th>
      </tr></thead>
      <tbody>{plat_rows_html}</tbody>
    </table>
  </div>

  <!-- PER-TOOL signal breakdown + EVENTS stream -->
  <div class="grid">
    <div class="card">
      <h2>Upgrade signals by tool (30d) — top 10</h2>
      <table>
        <thead><tr><th>Tool</th><th style="text-align:right">Signals</th></tr></thead>
        <tbody>{sig_rows_html}</tbody>
      </table>
    </div>
    <div class="card">
      <h2>Recent events stream <span style="font-size:10px;color:var(--accent);">LAST 20</span></h2>
      {ev_html}
    </div>
  </div>

  <!-- R3 Unlock 1 (2026-06-07): per-tool conversion ranking — top 3 + bottom 3.
       Compact inline view; full 38-tool table at /admin/per-tool-conversion.
       Cron at 02:00 UTC writes LOW (≥100 callers + 0 paid) and HIGH (top 3
       by rate) findings to brain_findings. Card content is hydrated by JS
       from /api/v1/admin/per-tool-conversion/top so this dashboard doesn't
       have to re-probe 48 tools on every refresh. -->
  <div class="card" style="margin-bottom:18px;">
    <h2>Per-tool conversion ranking
      <span style="font-size:10px;color:var(--accent);">R3 NEW · 48 tools · daily</span>
    </h2>
    <div id="ptc-inline" style="display:grid;grid-template-columns:1fr 1fr;gap:16px;font-size:12px;">
      <div>
        <div style="font-size:10px;color:#22c55e;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px;">Top 3 by conv rate</div>
        <ul id="ptc-top" style="margin:0;padding-left:18px;line-height:1.7;color:var(--fg);">
          <li style="color:var(--muted);list-style:none;margin-left:-18px;">Loading…</li>
        </ul>
      </div>
      <div>
        <div style="font-size:10px;color:#ef4444;text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px;">Bottom 3 (≥1 caller)</div>
        <ul id="ptc-bot" style="margin:0;padding-left:18px;line-height:1.7;color:var(--fg);">
          <li style="color:var(--muted);list-style:none;margin-left:-18px;">Loading…</li>
        </ul>
      </div>
    </div>
    <div style="font-size:11px;color:var(--muted);margin-top:12px;">
      Full table: <a href="/admin/per-tool-conversion?admin_key={admin_key_safe}" style="color:var(--accent);">/admin/per-tool-conversion</a>
      · JSON: <a href="/api/v1/admin/per-tool-conversion?admin_key={admin_key_safe}&amp;format=json" style="color:var(--accent);">/api/v1/admin/per-tool-conversion?format=json</a>
    </div>
  </div>

  <div class="footer">
    cache_ttl={_CACHE_TTL_S}s ·
    routes/funnel_health.py ·
    /admin/funnel-health · /api/v1/admin/funnel-health
  </div>
</div>

<script>
(function(){{
  var ADMIN_KEY = new URLSearchParams(window.location.search).get('admin_key')
                  || "{admin_key_safe}" || "";
  document.getElementById('killbtn').addEventListener('click', async function(){{
    if (!confirm('Kill the A/B test? Everyone routes to Arm A ($199) for THIS Railway replica until the env var change is persisted in Railway UI.')) {{
      return;
    }}
    this.disabled = true;
    var msg = document.getElementById('killmsg');
    try {{
      var r = await fetch('/api/v1/admin/funnel-health/kill-ab', {{
        method: 'POST',
        headers: {{ 'X-Admin-Key': ADMIN_KEY }}
      }});
      var j = await r.json().catch(function(){{ return {{}}; }});
      if (r.ok && j.ok) {{
        msg.innerHTML = 'Kill switch flipped (in-memory). ' +
          '<strong style="color:#fbbf24">Now set <code>PRICING_AB_DISABLE=1</code> in Railway env vars</strong> ' +
          'to make persistent.';
      }} else {{
        msg.innerHTML = '<span style="color:#ef4444">Kill failed: ' +
          (j.error || r.status) + '</span>';
        this.disabled = false;
      }}
    }} catch (e) {{
      msg.innerHTML = '<span style="color:#ef4444">Network error.</span>';
      this.disabled = false;
    }}
  }});

  // R3 Unlock 1 (2026-06-07): hydrate the per-tool conversion card from
  // /api/v1/admin/per-tool-conversion/top. The endpoint reuses its own
  // 10-min cache so the fan-out from funnel-health to ptc never spikes
  // the DB.
  function esc(s){{ var d=document.createElement('div'); d.textContent = s||''; return d.innerHTML; }}
  function fmtRate(r){{ return (r>=0.01 ? r.toFixed(2) : '0.00') + '%'; }}
  function ptcRow(r){{
    var color = r.conv_rate_pct >= 1.0 ? '#22c55e'
              : r.conv_rate_pct > 0 ? '#f59e0b'
              : (r.distinct_callers_30d >= 100 ? '#ef4444' : '#94a3b8');
    return '<li><strong>' + esc(r.tool) + '</strong> · ' +
           '<span style="color:' + color + ';">' + fmtRate(r.conv_rate_pct) + '</span> · ' +
           (r.distinct_callers_30d||0).toLocaleString() + ' callers · ' +
           (r.paid_conversions_30d||0) + ' paid</li>';
  }}
  (async function(){{
    try {{
      var r = await fetch('/api/v1/admin/per-tool-conversion/top', {{
        headers: {{ 'X-Admin-Key': ADMIN_KEY }}
      }});
      if (!r.ok) throw new Error(r.status);
      var j = await r.json();
      var topEl = document.getElementById('ptc-top');
      var botEl = document.getElementById('ptc-bot');
      if ((j.top_3||[]).length) {{
        topEl.innerHTML = j.top_3.map(ptcRow).join('');
      }} else {{
        topEl.innerHTML = '<li style="color:var(--muted);list-style:none;margin-left:-18px;">No data yet</li>';
      }}
      if ((j.bottom_3||[]).length) {{
        botEl.innerHTML = j.bottom_3.map(ptcRow).join('');
      }} else {{
        botEl.innerHTML = '<li style="color:var(--muted);list-style:none;margin-left:-18px;">No data yet</li>';
      }}
    }} catch (e) {{
      var topEl = document.getElementById('ptc-top');
      var botEl = document.getElementById('ptc-bot');
      if (topEl) topEl.innerHTML = '<li style="color:#ef4444;list-style:none;margin-left:-18px;">Load failed: ' + esc(String(e)) + '</li>';
      if (botEl) botEl.innerHTML = '';
    }}
  }})();
}})();
</script>
</body>
</html>"""


# ── Endpoints ─────────────────────────────────────────────────────────


@funnel_health_bp.route("/admin/funnel-health", methods=["GET"])
@funnel_health_bp.route("/api/v1/admin/funnel-health", methods=["GET"])
def admin_funnel_health():
    """Dual-route: /admin/funnel-health (canonical) + /api/v1/admin/funnel-health
    (CF zone-worker bypass — mirrors the dual-route pattern in
    routes/feedback_forum.admin_feedback_dashboard).

    JSON shape when ?format=json — operator can scrape this for headless
    smoke tests / brain detectors. Default is the rendered HTML page.
    """
    if not _admin_ok(request):
        return Response(
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>Admin Only · DC Hub</title>"
            '<link rel="stylesheet" href="/dchub-brand.css"></head>'
            '<body style="background:#0a0e1a;color:#e6ecf5;'
            "font-family:'Inter',system-ui,sans-serif;max-width:560px;"
            'margin:80px auto;padding:32px;">'
            '<h1 style="margin:0 0 12px;font-size:22px;">Admin only</h1>'
            '<p style="color:#94a3b8;line-height:1.6;">'
            "Pass <code>X-Admin-Key: …</code> as a header or "
            "<code>?admin_key=…</code> as a query param. "
            "The key value is the <code>DCHUB_ADMIN_KEY</code> env var "
            "(falls back to <code>ADMIN_KEY</code>)."
            "</p></body></html>",
            status=401, mimetype="text/html")

    data = _data_cached()

    if (request.args.get("format") or "").lower() == "json":
        return jsonify(data), 200

    admin_key = (request.headers.get("X-Admin-Key")
                 or request.args.get("admin_key") or "").strip()
    html = _render_html(data, admin_key)
    return Response(html, mimetype="text/html")


@funnel_health_bp.route("/api/v1/admin/funnel-health/kill-ab",
                         methods=["POST"])
def admin_funnel_health_kill_ab():
    """In-memory kill switch for the pricing A/B test.

    Flips os.environ['PRICING_AB_DISABLE']='1' for THIS Railway replica,
    which the pricing_ab._ab_disabled() helper re-reads on every request.
    The operator MUST also set the env var in the Railway dashboard to
    make it persist across replica restarts — the JSON response says so
    + the UI toast shows the instruction.
    """
    if not _admin_ok(request):
        return jsonify(error="unauthorized"), 401
    os.environ["PRICING_AB_DISABLE"] = "1"
    # Bust the dashboard cache so the next refresh shows the new state.
    _CACHE["data"] = None
    _CACHE["ts"] = 0.0
    return jsonify(
        ok=True,
        message="Kill switch flipped in-memory for this replica.",
        next_step=("Set PRICING_AB_DISABLE=1 in Railway env vars to "
                   "persist across replica restarts."),
    ), 200


@funnel_health_bp.route("/api/v1/admin/funnel-health/per-platform-joined",
                         methods=["GET"])
def admin_funnel_health_per_platform_joined():
    """Honest per-platform funnel via a session→platform JOIN.

    Kills the 0%-everywhere artifact in the main panel's per-platform block (which
    matched platform names by raw LIKE on UA strings that header-less agents never
    populate, so signals/conversions read ~0 across ALL platforms). Builds a
    modal-platform-per-session map from mcp_tool_calls via the canonical PLATFORM_CASE
    classifier, then keys requests / upgrade-signals / conversions onto that SAME
    session identity:
      requests:    mcp_tool_calls       (session_id → platform)
      signals:     mcp_upgrade_signals  (session_id → platform)
      conversions: mcp_conversions → mcp_upgrade_signals(id) → session_id → platform
    Read-only + additive (a NEW endpoint — the live panel is untouched). Reports an
    explicit 'unknown' bucket: the header-less majority is genuinely ~unknown, a true
    finding, not dropped. See reference_dchub_funnel_redesign_0628.
    """
    if not _admin_ok(request):
        return jsonify(error="unauthorized"), 401
    out = {
        "window_days": 30, "platforms": [],
        "attribution_coverage_pct": None,
        "note": ("session-keyed join (mcp_tool_calls PLATFORM_CASE) — supersedes the "
                 "raw-LIKE per-platform block in /api/v1/admin/funnel-health"),
    }
    conn = _conn()
    if conn is None:
        out["error"] = "no_db"
        return jsonify(out), 200
    try:
        from mcp_calls_deloop import PLATFORM_CASE as _pcase
        # NB: execute() is called with NO params arg so psycopg2 does NOT run
        # %-substitution over the literal % in PLATFORM_CASE's ILIKE patterns
        # (the empty-tuple %-trap — see reference_psycopg2_empty_tuple_percent_trap).
        sql = ("""
        WITH sess_platform AS (
          SELECT session_id, client_platform FROM (
            SELECT session_id,
                   %PCASE% AS client_platform,
                   ROW_NUMBER() OVER (
                     PARTITION BY session_id
                     ORDER BY (CASE WHEN %PCASE% = 'unknown' THEN 0 ELSE 1 END) DESC,
                              COUNT(*) DESC) AS rn
            FROM mcp_tool_calls
            WHERE created_at >= NOW() - INTERVAL '30 days'
              AND session_id IS NOT NULL
            GROUP BY session_id, %PCASE%
          ) t WHERE rn = 1
        ),
        req AS (
          SELECT sp.client_platform AS platform,
                 COUNT(*)                       AS requests,
                 COUNT(DISTINCT tc.session_id)  AS sessions
          FROM mcp_tool_calls tc
          JOIN sess_platform sp USING (session_id)
          WHERE tc.created_at >= NOW() - INTERVAL '30 days'
          GROUP BY sp.client_platform
        ),
        sig AS (
          SELECT sp.client_platform AS platform, COUNT(*) AS signals
          FROM mcp_upgrade_signals s
          JOIN sess_platform sp ON sp.session_id = s.session_id
          WHERE s.created_at >= NOW() - INTERVAL '30 days'
          GROUP BY sp.client_platform
        ),
        conv AS (
          SELECT sp.client_platform AS platform, COUNT(*) AS conversions
          FROM mcp_conversions c
          JOIN mcp_upgrade_signals s ON s.id = c.attribution_signal_id
          JOIN sess_platform sp ON sp.session_id = s.session_id
          WHERE c.created_at >= NOW() - INTERVAL '30 days'
          GROUP BY sp.client_platform
        )
        SELECT COALESCE(req.platform, sig.platform, conv.platform) AS platform,
               COALESCE(req.requests, 0)     AS requests,
               COALESCE(req.sessions, 0)     AS sessions,
               COALESCE(sig.signals, 0)      AS signals,
               COALESCE(conv.conversions, 0) AS conversions
        FROM req
        FULL OUTER JOIN sig  ON sig.platform  = req.platform
        FULL OUTER JOIN conv ON conv.platform = COALESCE(req.platform, sig.platform)
        ORDER BY 2 DESC NULLS LAST
        """).replace("%PCASE%", _pcase)
        with conn.cursor() as cur:  # lint-ok: cursor-shadow (no function-wide cur in this handler)
            cur.execute(sql)
            rows = cur.fetchall() or []

        def _c(r, idx, key):
            return (r.get(key) if hasattr(r, "get") else r[idx])

        tot_sig = kn_sig = tot_conv = kn_conv = 0
        for r in rows:
            plat = (_c(r, 0, "platform") or "unknown")
            rec = {
                "platform": plat,
                "requests_30d": int(_c(r, 1, "requests") or 0),
                "sessions_30d": int(_c(r, 2, "sessions") or 0),
                "signals_30d": int(_c(r, 3, "signals") or 0),
                "conversions_30d": int(_c(r, 4, "conversions") or 0),
            }
            rec["conv_rate_pct"] = (round(100.0 * rec["conversions_30d"]
                                          / rec["sessions_30d"], 2)
                                    if rec["sessions_30d"] else 0.0)
            tot_sig += rec["signals_30d"]
            tot_conv += rec["conversions_30d"]
            if plat not in ("unknown", "internal-dchub"):
                kn_sig += rec["signals_30d"]
                kn_conv += rec["conversions_30d"]
            out["platforms"].append(rec)
        # Attribution coverage = share of signals+conversions that resolve to a NAMED
        # (non-unknown, non-internal) platform. The honest headline: how much of the
        # funnel we can actually attribute, vs the raw-LIKE block's ~0%.
        _den = tot_sig + tot_conv
        out["attribution_coverage_pct"] = (round(100.0 * (kn_sig + kn_conv) / _den, 1)
                                           if _den else None)
        out["totals"] = {"signals_30d": tot_sig, "conversions_30d": tot_conv,
                         "named_signals_30d": kn_sig,
                         "named_conversions_30d": kn_conv}
    except Exception as e:
        out["error"] = str(e)[:200]
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass
    return jsonify(out), 200


# ── AGENT-PAY FUNNEL SPLIT (r-mpp-abandonment, 2026-08-12) ────────────────
#
# THE DEFECT. `mpp_challenge` records "a signed quote was ISSUED to the caller".
# Every reader — including DC Hub's own agent brief, which said "13 agents
# reached the pay step" — took it to mean "the agent attempted to pay". With no
# counter between issuance and return, 13 issued quotes plus 1 verify failure
# was indistinguishable from 13 attempts that mostly broke. Those two readings
# demand opposite fixes (repair the settlement path vs. fix price/framing), so
# the funnel could not tell anyone which to work on.
#
# THE FIX. The gateway now stamps `mpp_credential_returned` the instant a caller
# comes back holding a credential (server.mjs, MPP_FUNNEL_STATUS in
# mpp-hook.mjs). The stage that was missing is now its own event, and the gap
# between issued and returned is PUBLISHED AS A NUMBER named `abandonment`
# rather than left for a reader to subtract.
#
# BACK-COMPAT. `mpp_challenge` keeps its wire name — it is published here and
# quoted externally, and a rename would silently zero every existing reader. It
# is ALIASED to the honest label `quote_issued` in the funnel block below.
# `totals` and `by_status_tool` are untouched.
_FUNNEL_RETURNED_ST = 'mpp_credential_returned'
# ★2026-08-15: the compact under-cap offer's own status. Defined HERE, above
# every use, for the same reason _FUNNEL_RETURNED_ST is: the watcher's status
# list (_ST) reads it long before the pay-shell constants block further down.
_UNDERCAP_ST = 'mpp_offer_undercap'

# The instrumentation go-live. The returned-credential counter CANNOT predate
# this, so any window reaching back further is a lower bound, not a measurement.
# This is provenance, not a count — nothing downstream derives a number from it.
_FUNNEL_RETURNED_LIVE_UTC = '2026-08-12'

# One line per counter naming EXACTLY which rows increment it. The whole defect
# was a counter whose NAME implied an event it does not record, so a counter
# published without a basis here is a regression.
_FUNNEL_BASIS = {
    "offer_prewall_shown":
        "COUNT(status='mpp_offer_prewall') — a passive pay offer rode along with a "
        "tool call that SUCCEEDED. The caller was not gated and paid nothing. NOT pay-intent.",
    "offer_undercap_shown":
        "COUNT(status='mpp_offer_undercap') — the COMPACT under-cap offer rode along with a "
        "tool call that SUCCEEDED, once per (session, tool). Sibling of offer_prewall_shown, "
        "counted separately because it fires on ORDINARY under-cap calls while the pre-wall "
        "offer fires only at the last free call. The caller was not gated and paid nothing. "
        "Records that an offer was ISSUED, never that one was accepted. NOT pay-intent.",
    "quote_issued":
        "COUNT(status='mpp_challenge') — the gateway MINTED and RETURNED a signed price "
        "quote to a gated caller that asked for one. Records ISSUANCE ONLY. Nothing came "
        "back at this point. Wire name kept for back-compat; 'quote_issued' is the honest label.",
    "credential_returned":
        "COUNT(status IN ('mpp_credential_returned','mpp_verify_failed','mpp_paid')) — the "
        "caller CAME BACK and presented a credential. The union is the counter: the two "
        "terminal statuses each imply a return, and 'mpp_credential_returned' is stamped "
        "before verify so a return that never reaches a terminal state is still counted.",
    "verify_failed":
        "COUNT(status='mpp_verify_failed') — a credential WAS presented and verify/settle "
        "returned not-ok. Strictly a subset of credential_returned.",
    "paid":
        "COUNT(status='mpp_paid') — a credential WAS presented, verified and settled. Real "
        "money. Strictly a subset of credential_returned.",
    "abandonment.quotes_never_returned":
        "quote_issued MINUS credential_returned over the SAME window. Named, not left to "
        "arithmetic: this is the population that saw a price and never came back.",
}

# Deliberately NOT folded into any counter above. A zero in this rail is not a
# measurement of these; three-valued reporting requires saying so out loud.
_FUNNEL_UNMEASURED = [
    "A credential presented on a call the gate ALLOWED is never counted — the gateway's MPP "
    "block only runs on a gated call, so no status is stamped and the return is invisible.",
    "No correlation id links a returned credential to the quote that produced it "
    "(challenge.id is not persisted on the call row). abandonment.quotes_never_returned is "
    "therefore a POPULATION difference over a window, not a per-quote attribution.",
    "The verify_failed rows carry no error text on the call row, so WHY a settle failed is "
    "not readable from this endpoint.",
]

# r-selftraffic-funnel (2026-08-17): the canonical real-traffic UA verdict,
# imported rather than re-spelled — a second hand-written UA list here is
# exactly the drift mcp_calls_deloop centralises to prevent. Regex form: no
# literal %, so it is safe beside the bound params these queries use.
from mcp_calls_deloop import real_ua_predicate as _deloop_real_ua_predicate  # noqa: E402

_REAL_UA = _deloop_real_ua_predicate("user_agent")

_FUNNEL_SQL = (
    "SELECT "
    "  COUNT(*) FILTER (WHERE status = 'mpp_offer_prewall') AS prewall, "
    "  COUNT(*) FILTER (WHERE status = 'mpp_offer_undercap') AS undercap, "
    "  COUNT(*) FILTER (WHERE status = 'mpp_challenge') AS quoted, "
    "  COUNT(*) FILTER (WHERE status IN "
    "        ('mpp_credential_returned','mpp_verify_failed','mpp_paid')) AS returned, "
    "  COUNT(*) FILTER (WHERE status = 'mpp_credential_returned') AS returned_no_terminal, "
    "  COUNT(*) FILTER (WHERE status = 'mpp_verify_failed') AS failed, "
    "  COUNT(*) FILTER (WHERE status = 'mpp_paid') AS paid "
    " FROM mcp_call_log "
    " WHERE timestamp > NOW() - make_interval(days => %s) "
    # r-selftraffic-funnel (2026-08-17): the split funnel must apply the SAME
    # UA exclusion as totals above, or the two blocks in one payload disagree
    # about how many quotes exist — and the split funnel is the one that
    # publishes the abandonment gap.
    "   AND " + _REAL_UA + " "
)


def _funnel_window_predates_split(win):
    """True when the requested window reaches back before the returned-credential
    counter existed — i.e. credential_returned is a lower bound, not a measurement."""
    from datetime import datetime, timedelta, timezone
    live = datetime.strptime(_FUNNEL_RETURNED_LIVE_UTC, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - timedelta(days=int(win))) < live


def _funnel_block(prewall, quoted, returned, returned_no_terminal, failed, paid, win,
                  undercap=0):
    """Assemble the published pay funnel. Pure — no DB, so it is unit-testable.

    Stage counts in, one named block out: every stage carries its basis, the
    issued→returned gap is published as `abandonment` rather than left for the
    reader to subtract, and the pre-instrumentation history is declared
    UNMEASURED instead of being invented.
    """
    gap = quoted - returned
    return {
        "stages": {
            "offer_prewall_shown": prewall,
            "offer_undercap_shown": undercap,
            "quote_issued": quoted,
            "credential_returned": returned,
            "verify_failed": failed,
            "paid": paid,
        },
        # ★ THE NUMBER THIS SPLIT EXISTS TO PUBLISH. Issued minus returned,
        # named as abandonment. Before this, a reader had to subtract two
        # counters that nobody knew measured different events.
        "abandonment": {
            "quotes_never_returned": gap,
            "meaning": "quotes issued in this window for which NO credential ever came back "
                       "— the caller saw a price and left. Distinct from a caller who came "
                       "back and failed (verify_failed).",
            "rate": (round(gap / quoted, 4) if quoted else None),
            "sign_note": "CAN GO NEGATIVE, and a negative is meaningful, not a bug: the "
                         "one-step pre-wall offer ships a payable challenge WITHOUT stamping "
                         "mpp_challenge, so a credential can be returned against a quote that "
                         "was never counted as issued.",
        },
        "returned_without_terminal_outcome": returned_no_terminal,
        "aliases": {
            "quote_issued": "mpp_challenge",
            "_note": "mpp_challenge keeps its wire name (published + externally quoted); "
                     "quote_issued is the same rows under the name that matches the event.",
        },
        "basis": _FUNNEL_BASIS,
        "unmeasured": _FUNNEL_UNMEASURED,
        "history": {
            "credential_returned_exact_since": _FUNNEL_RETURNED_LIVE_UTC,
            "window_predates_instrumentation": _funnel_window_predates_split(win),
            "credential_returned_measurement":
                ("LOWER_BOUND (window predates the split)"
                 if _funnel_window_predates_split(win) else "EXACT"),
            "pre_instrumentation_basis":
                "NOT BACKFILLED. Before " + _FUNNEL_RETURNED_LIVE_UTC + " no returned-credential "
                "event existed. For any earlier span credential_returned is a LOWER BOUND equal "
                "to verify_failed + paid — each of those implies a credential was presented — "
                "and a return that crashed before a terminal outcome left no recoverable row. "
                "The series is honest from " + _FUNNEL_RETURNED_LIVE_UTC + " forward; it was not "
                "reconstructed backwards.",
        },
        "window_days": win,
    }


@funnel_health_bp.route("/api/v1/admin/agent-pay-events", methods=["GET"])
def admin_agent_pay_events():
    """WATCHER endpoint for autonomous agent-native payment events (Wave-1 MPP rail).

    Reads mcp_call_log.status — the column the MCP gateway's /api/v1/mcp/track callback
    writes (flask_mcp_endpoints.py) — for the agent-pay statuses server.mjs sets on the
    MPP/x402 settle+challenge paths: mpp_challenge (an agent OPTED IN to pay, leading
    indicator), mpp_paid (settled — real $, the conversion), mpp_verify_failed, x402_paid,
    x402_failed. Lets a cron watcher detect the FIRST real agent-pay with no DB access.
    Read-only; ?days=N (default 30, capped 120). first_paid_at = the all-time first
    settled agent-pay (null until it happens — that's the headline the watcher waits on).
    """
    if not _admin_ok(request):
        return jsonify(error="unauthorized"), 401
    try:
        win = max(1, min(120, int(request.args.get("days", "30"))))
    except Exception:
        win = 30
    out = {"window_days": win, "by_status_tool": {},
           "by_status_tool_including_self_traffic": {},
           "totals": {"challenges": 0, "paid": 0, "failed": 0},
           # r-selftraffic-funnel (2026-08-17): the pre-filter figures stay
           # published so the exclusion can be audited and added back. See the
           # comment on the query below — every quote/verify event all-time was
           # ours, so `totals` reading 0 is the measurement, not a breakage.
           "totals_including_self_traffic": {"challenges": 0, "paid": 0, "failed": 0},
           "excluded": {"rows_internal_ua": 0,
                        "basis": "Rows whose user_agent matches the canonical "
                                 "internal/raw-scripting families "
                                 "(mcp_calls_deloop.real_ua_predicate) are kept out of "
                                 "totals/by_status_tool and counted here. platform is "
                                 "deliberately NOT used: the gateway's own /track "
                                 "callback stamps platform='dchub-internal' on ~95% of "
                                 "MPP rows regardless of caller, so filtering on it "
                                 "would fail closed."},
           "recent": [], "first_paid_at": None,
           # ★ 2026-08-12: `totals.challenges` counts QUOTES ISSUED, not payment
           # attempts. It is left as-is so existing readers keep working, but the
           # note no longer says "opted in" without saying what that records.
           "note": "totals.challenges = mpp_challenge = a signed quote was ISSUED to the "
                   "caller (NOT an attempt to pay). mpp_paid/x402_paid = settled real "
                   "payment. Read pay_funnel for the split with abandonment."}
    # ★ 2026-07-28: + mpp_offer_prewall (PASSIVE pre-wall offer). Without it
    # this watcher reported the surface as nonexistent rather than as zero.
    # Visibility only — it is NOT pay-intent (see _CHAL_ST note below).
    # ★ 2026-08-12: + mpp_credential_returned — the caller came back and presented
    # something. Without it, `recent` could not show a return that never reached a
    # terminal outcome, and the whole event class stayed invisible.
    # ★ 2026-08-15: + mpp_offer_undercap — the compact under-cap offer, same
    # visibility-only class as mpp_offer_prewall and far more frequent.
    _ST = ['mpp_challenge', 'mpp_paid', 'mpp_verify_failed', 'x402_paid',
           'x402_failed', 'mpp_offer_prewall', _UNDERCAP_ST, _FUNNEL_RETURNED_ST]
    conn = _conn()
    if conn is None:
        out["error"] = "no_db"
        return jsonify(out), 200
    try:
        with conn.cursor() as cur:  # lint-ok: cursor-shadow (no function-wide cur in this handler)
            # ── r-selftraffic-funnel (2026-08-17) ──────────────────────────
            # EVERY mpp_challenge / mpp_verify_failed row in this table's
            # all-time history (19 of them, 2026-06-21 → 2026-08-17) carries an
            # internal UA: curl/8.7.1, Python-urllib/3.14, DCHubProbe/1.0.
            # Not one external caller has ever requested a quote or presented a
            # credential. The counters were reporting our own probes as agent
            # demand, and MPP_FUNNEL_BASIS's abandonment question ("13 quotes,
            # 1 verify failure — did the other 12 try and break, or see a price
            # and leave?") was being asked of a population of zero.
            #
            # `platform` is NOT usable as the discriminator here: 851 of the
            # ~890 mpp rows carry platform='dchub-internal' because the gateway's
            # own /track callback stamps it, so an external-platform filter would
            # fail closed and zero the surface for the wrong reason. UA is the
            # honest key, and it is the same canonical verdict every other
            # real-traffic read uses.
            #
            # Published, never silent: totals_including_self_traffic keeps the
            # old figure and `excluded` names what came out.
            cur.execute(
                "SELECT status, tool, COUNT(*) AS n, "
                "       COUNT(*) FILTER (WHERE " + _REAL_UA + ") AS n_real "
                "  FROM mcp_call_log "
                " WHERE status = ANY(%s) AND timestamp > NOW() - make_interval(days => %s) "
                " GROUP BY status, tool ORDER BY n DESC",
                (_ST, win))
            for r in (cur.fetchall() or []):
                st = r.get("status") if hasattr(r, "get") else r[0]
                tool = r.get("tool") if hasattr(r, "get") else r[1]
                n_all = int((r.get("n") if hasattr(r, "get") else r[2]) or 0)
                n = int((r.get("n_real") if hasattr(r, "get") else r[3]) or 0)
                out["by_status_tool"].setdefault(st, {})[tool or "?"] = n
                out["by_status_tool_including_self_traffic"].setdefault(
                    st, {})[tool or "?"] = n_all
                out["excluded"]["rows_internal_ua"] += (n_all - n)
                for bucket, keys in (("paid", ("mpp_paid", "x402_paid")),
                                     ("challenges", ("mpp_challenge",)),
                                     ("failed", ("mpp_verify_failed", "x402_failed"))):
                    if st in keys:
                        out["totals"][bucket] += n
                        out["totals_including_self_traffic"][bucket] += n_all
            cur.execute(
                "SELECT timestamp, status, tool, tier, platform FROM mcp_call_log "
                " WHERE status = ANY(%s) AND timestamp > NOW() - make_interval(days => %s) "
                " ORDER BY timestamp DESC LIMIT 25",
                (_ST, win))
            for r in (cur.fetchall() or []):
                g = (lambda k, i: r.get(k) if hasattr(r, "get") else r[i])
                out["recent"].append({"at": str(g("timestamp", 0)), "status": g("status", 1),
                                      "tool": g("tool", 2), "tier": g("tier", 3),
                                      "platform": g("platform", 4)})
            cur.execute("SELECT MIN(timestamp) FROM mcp_call_log "
                        " WHERE status IN ('mpp_paid', 'x402_paid')")
            r = cur.fetchone()
            v = (r.get("min") if hasattr(r, "get") else r[0]) if r else None
            out["first_paid_at"] = str(v) if v else None
            # ★ 2026-08-12: the SPLIT funnel — one counter per distinct event,
            # each with its basis, and the issued→returned gap published as
            # `abandonment` instead of left for the reader to subtract. Added
            # ALONGSIDE totals/by_status_tool, which are untouched.
            cur.execute(_FUNNEL_SQL, (win,))
            fr = cur.fetchone()
            if fr is not None:
                _fg = (lambda k, i: fr.get(k) if hasattr(fr, "get") else fr[i])
                # ★2026-08-15: `undercap` was inserted as column 1 of _FUNNEL_SQL,
                # so every POSITIONAL index below it shifted by one. The name is
                # used when the driver returns a mapping; the index is the tuple
                # fallback, and a stale index there would silently mis-wire every
                # stage to its neighbour on tuple-returning cursors.
                out["pay_funnel"] = _funnel_block(
                    int(_fg("prewall", 0) or 0), int(_fg("quoted", 2) or 0),
                    int(_fg("returned", 3) or 0), int(_fg("returned_no_terminal", 4) or 0),
                    int(_fg("failed", 5) or 0), int(_fg("paid", 6) or 0), win,
                    undercap=int(_fg("undercap", 1) or 0))
    except Exception as e:
        out["error"] = str(e)[:200]
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass
    return jsonify(out), 200


# ── Agent-Pay Master Shell ────────────────────────────────────────────────
# 2026-07-04: the raw watcher (agent-pay-events, above) answers ONE binary —
# "has anyone settled?" — and hides the two facts that actually matter while
# volume is near-zero: (1) every challenge so far is SYNTHETIC test traffic
# (scout-rail-test / acp-test-agent / junk 'v' clientInfo tags), so the raw
# "challenges" total is a vanity number and REAL agent pay-intent is 0; and
# (2) not one challenge has converted to a settle, and we can't see whether
# that's expected (test agents that never meant to pay) or a broken settle
# path (a real agent that tried and errored). This master-tick orchestrates
# the levers into a scoreboard + verdict + next-action so the operator reads
# "does the rail actually work E2E when a REAL agent tries?" not just a light.

# Synthetic/non-real platform exclusion. Trusted hardcoded constants inlined
# as SQL literals — but EVERY query that embeds this also carries bound %s
# params (status list, window), so psycopg2 runs %-substitution over the whole
# string: the literal % in the LIKE patterns MUST be doubled to %% here or it
# trips the empty-tuple/format trap (see _scalar docstring + the
# reference_psycopg2_empty_tuple_percent_trap memo). Keep the %% doubled.
_SYNTH_PLATFORM_SQL = (
    " ( platform IS NULL "
    # r-synth-align (2026-07-25): '%%dchub%%' was MISSING and it mattered. The MCP
    # gateway collapses every harness/QA/self-ID clientInfo (test, verify, probe,
    # audit, smoke, regression, selfheal, 'clawith', 'value-harness', …) to the single
    # tag 'dchub-internal' (server.mjs _KNOWN_PLATFORM_FROM_NAME, r-junk-platform),
    # on the documented assumption that "every backend read predicate already excludes
    # %%dchub%%". THIS predicate did not — so all internal traffic counted as REAL
    # agent pay-intent and the master-tick reported our own probes as customer demand.
    # Mirrors the canonical exclusion set in
    # migrations/2026-07-01_mcp_calls_identity_platform_tag_exclusions.sql.
    "   OR COALESCE(LOWER(platform),'') LIKE '%%dchub%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%test%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%canary%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%probe%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%staging%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%verify%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%audit%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%harness%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%check%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%diag%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%sweep%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%smoke%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%regression%%' "
    "   OR COALESCE(LOWER(platform),'') LIKE '%%selfheal%%' "
    "   OR COALESCE(LOWER(platform),'') IN ('clawith','value-harness','dbg','raw','full',"
    "        'f5r','fv','rev','final','vinline','qa','qa-mozilla','fix2-v2','mcp-vouch',"
    "        'capwall2','pipeline_mcp','curl','insomnia','postman','node-script','python-script') "
    "   OR LENGTH(COALESCE(platform,'')) <= 2 ) "  # junk 1-2 char clientInfo tags ('v', …)
)
_REAL_PLATFORM_SQL = " NOT " + _SYNTH_PLATFORM_SQL
_PAID_ST = ('mpp_paid', 'x402_paid')
_FAIL_ST = ('mpp_verify_failed', 'x402_failed')
# ★ 2026-08-12: RENAMED IN MEANING, NOT ON THE WIRE. This status records that a
# signed price QUOTE WAS ISSUED to the caller. It was read for months as "the
# agent attempted to pay" — it never recorded that, and the reading drove a wrong
# diagnosis. The wire value stays 'mpp_challenge' (published + externally quoted);
# the honest label is `quote_issued`, and the event the old reading meant is now
# its own counter, _FUNNEL_RETURNED_ST. See _FUNNEL_BASIS.
_CHAL_ST = 'mpp_challenge'
# ★ 2026-07-28: `mpp_offer_prewall` (the PASSIVE pre-wall offer) was missing
# from this universe, so both admin surfaces reported the whole surface as
# NONEXISTENT rather than as zero — the flattering-zero pattern, one level up:
# not a failed read rendering 0, but a real event class no query could see.
# It is included here for VISIBILITY only.
# ★★ It is deliberately NOT in _CHAL_ST: `mpp_challenge` means "an agent ASKED
# to pay" and is the pay-intent metric. A passively-shipped offer is not
# intent; counting it would turn pay-intent into "every call near the cap" and
# destroy the signal. The success measure for this surface stays
# mpp_paid / mpp_verify_failed.
_PREWALL_ST = 'mpp_offer_prewall'
# ★★2026-08-15: `mpp_offer_undercap` (_UNDERCAP_ST, defined above) joins the
# universe on exactly the same terms — VISIBILITY only, never pay-intent, never
# folded into _CHAL_ST. Note it is the offer surface's REACH number in practice:
# the pre-wall stamp fires only on the last free call, so a prewall count of 0
# is normal and says nothing about whether agents are being offered a price.
_ALL_PAY_ST = ['mpp_challenge', 'mpp_paid', 'mpp_verify_failed', 'x402_paid',
               'x402_failed', _PREWALL_ST, _UNDERCAP_ST, _FUNNEL_RETURNED_ST]


@funnel_health_bp.route("/api/v1/admin/agent-pay/master-tick", methods=["GET"])
def admin_agent_pay_master_tick():
    """SELF-DRIVING agent-pay orchestrator over mcp_call_log — the master shell
    on top of the raw agent-pay-events watcher.

    Levers (each isolated; one failing probe never blanks the tick):
      • split     — challenges/paid/failed for ALL traffic AND real-only
                    (synthetic test platforms excluded). Real pay-intent is the
                    number that matters; the raw watcher couldn't see it.
      • funnel    — real challenge→settle conversion: settle_rate, abandon
                    (real challenges that never settled), failed. Tells expected
                    -no-pay apart from broken-settle-path.
      • by_tool   — real pay-intent per flagship tool, ranked → where demand
                    concentrates (point conversion effort there).
      • trend     — WoW: real challenges/paid this 7d vs prior 7d + direction.
      • milestones— first_paid_at (all-time settle, THE headline) and
                    first_real_challenge_at (all-time first NON-test opt-in — the
                    leading indicator you hit long before first settle).
      • verdict   — status + one-line headline + next_action (the self-driving
                    recommendation for which lever to pull next).

    Read-only. ?days=N (default 30, cap 120) scopes split/funnel/by_tool;
    trend is fixed 7d WoW; milestones are all-time.
    """
    if not _admin_ok(request):
        return jsonify(error="unauthorized"), 401
    try:
        win = max(1, min(120, int(request.args.get("days", "30"))))
    except Exception:
        win = 30

    out = {
        "window_days": win,
        # ★ 2026-07-28: `prewall` added. `_ALL_PAY_ST` already carried
        # mpp_offer_prewall, so those rows were INSIDE the query window — but no
        # projection ever counted them, so every surface reported the pre-wall
        # offer as nonexistent rather than as a number. Being in the universe is
        # not the same as being reported; extend the SELECT list, not just the
        # status filter.
        # ★ 2026-08-12: `returned` added — a credential was actually PRESENTED
        # back to the gateway. `challenges` never recorded that (it records
        # quote ISSUANCE), so until now the two were indistinguishable and the
        # gap between them could not be read at all.
        # ★ 2026-08-15: `undercap` added at the same time as the status itself,
        # rather than months later like `prewall` — the 07-28 lesson applied
        # forward instead of repeated.
        "split": {"all": {"challenges": 0, "returned": 0, "paid": 0, "failed": 0,
                          "prewall": 0, "undercap": 0},
                  "real": {"challenges": 0, "returned": 0, "paid": 0, "failed": 0,
                           "prewall": 0, "undercap": 0},
                  "test": {"challenges": 0, "returned": 0, "paid": 0, "failed": 0,
                           "prewall": 0, "undercap": 0}},
        "funnel": {"real_challenges": 0, "real_paid": 0, "real_failed": 0,
                   "settle_rate": None, "abandoned": 0,
                   "real_returned": 0, "abandonment_quotes_never_returned": 0},
        "by_tool": [],
        "trend": {"real_challenges_7d": 0, "real_challenges_prev7d": 0,
                  "real_paid_7d": 0, "real_paid_prev7d": 0, "direction": "flat"},
        "milestones": {"first_paid_at": None, "first_real_challenge_at": None},
        "real_platforms": [],
        "verdict": {"status": "UNKNOWN", "headline": "", "next_action": ""},
        # ★ 2026-08-12: every counter on this board now states which event
        # increments it. The defect that forced this was a counter whose NAME
        # implied an event it does not record, so a number published here
        # without a basis is the regression.
        "basis": _FUNNEL_BASIS,
        "unmeasured": _FUNNEL_UNMEASURED,
        "history": {"credential_returned_exact_since": _FUNNEL_RETURNED_LIVE_UTC,
                    "backfilled": False,
                    "note": "The returned-credential series starts at the date above and was "
                            "NOT reconstructed backwards. For earlier spans it reads as a "
                            "lower bound (verify_failed + paid)."},
        "note": "real=synthetic test platforms excluded. `challenges` counts QUOTES ISSUED, "
                "NOT attempts to pay — read `funnel.real_returned` for callers that actually "
                "came back and `funnel.abandonment_quotes_never_returned` for those that did not.",
    }

    conn = _conn()
    if conn is None:
        out["error"] = "no_db"
        out["verdict"] = {"status": "NO_DB", "headline": "DB unavailable — cannot tick.",
                          "next_action": "Check DATABASE_URL / Neon pooler health."}
        return jsonify(out), 200

    def _run(cur, label, sql, params, fetch="one"):
        """Isolated probe: return rows/scalar or None on error (never raise)."""
        try:
            cur.execute(sql, params)
            if fetch == "one":
                r = cur.fetchone()
                return r
            return cur.fetchall() or []
        except Exception as e:
            logger.debug("agent_pay master-tick lever %s failed: %s", label, e)
            try: conn.rollback()
            except Exception: pass
            return None

    def _n(r, i=0):
        if not r:
            return 0
        v = (r.get(list(r.keys())[i]) if hasattr(r, "get") else r[i])
        try: return int(v or 0)
        except Exception: return 0

    try:
        with conn.cursor() as cur:  # lint-ok: cursor-shadow (handler-local)
            # ── split: all vs real vs test, over the window ──────────────
            base = (" FROM mcp_call_log WHERE status = ANY(%s) "
                    " AND timestamp > NOW() - make_interval(days => %s) ")
            _sel = ("SELECT "
                    "  COUNT(*) FILTER (WHERE status = 'mpp_challenge') AS chal, "
                    "  COUNT(*) FILTER (WHERE status IN ('mpp_paid','x402_paid')) AS paid, "
                    "  COUNT(*) FILTER (WHERE status IN ('mpp_verify_failed','x402_failed')) AS fail, "
                    # ★ PASSIVE offer shipped to an agent — NOT pay-intent. Kept
                    # in its own column and deliberately never folded into
                    # `challenges`: mpp_challenge means "an agent ASKED to pay",
                    # and mixing a passively-attached offer in would turn that
                    # signal into "every call near the cap" and destroy it.
                    "  COUNT(*) FILTER (WHERE status = 'mpp_offer_prewall') AS prewall, "
                    # ★ 2026-08-12: THE MISSING EVENT. A credential was actually
                    # PRESENTED back to the gateway. `chal` above never recorded
                    # this — it records that a quote went OUT — so every reader
                    # that treated a challenge as an attempt to pay was reading a
                    # counter for a different event. Union of the pre-verify stamp
                    # and the two terminal statuses, each of which implies a
                    # credential was in hand. See _FUNNEL_BASIS.credential_returned.
                    "  COUNT(*) FILTER (WHERE status IN "
                    "        ('mpp_credential_returned','mpp_verify_failed','mpp_paid')) AS ret, "
                    # ★ 2026-08-15: the COMPACT under-cap offer, its own column
                    # for the same reason prewall has one. Appended LAST so no
                    # existing positional _n(r, i) index moves.
                    "  COUNT(*) FILTER (WHERE status = 'mpp_offer_undercap') AS undercap")
            r = _run(cur, "split_all", _sel + base, (list(_ALL_PAY_ST), win))
            if r:
                out["split"]["all"] = {"challenges": _n(r, 0), "paid": _n(r, 1),
                                       "failed": _n(r, 2), "prewall": _n(r, 3),
                                       "returned": _n(r, 4), "undercap": _n(r, 5)}
            r = _run(cur, "split_real",
                     _sel + base + " AND " + _REAL_PLATFORM_SQL,
                     (list(_ALL_PAY_ST), win))
            if r:
                out["split"]["real"] = {"challenges": _n(r, 0), "paid": _n(r, 1),
                                        "failed": _n(r, 2), "prewall": _n(r, 3),
                                        "returned": _n(r, 4), "undercap": _n(r, 5)}
            # test = all − real (derived; avoids a third round-trip)
            for k in ("challenges", "paid", "failed", "prewall", "returned", "undercap"):
                out["split"]["test"][k] = max(0, out["split"]["all"][k] - out["split"]["real"][k])

            # ── funnel: real challenge→settle ───────────────────────────
            rc = out["split"]["real"]["challenges"]
            rp = out["split"]["real"]["paid"]
            rf = out["split"]["real"]["failed"]
            out["funnel"] = {
                "real_challenges": rc, "real_paid": rp, "real_failed": rf,
                "settle_rate": (round(rp / rc, 4) if rc else None),
                "abandoned": max(0, rc - rp - rf),
                # ★ Offers DELIVERED to real agents. This is the pre-wall
                # surface's own reach number — the thing lane 2's gating-based
                # reachability metric structurally cannot see, because the offer
                # attaches to calls that SUCCEED (`gate.allowed`) while that lane
                # only counts calls that were GATED. A healthy pre-wall surface
                # can therefore run with reachability pinned at 0.1% forever;
                # read THIS instead. Success is still mpp_paid/mpp_verify_failed.
                "real_prewall_offers": out["split"]["real"]["prewall"],
                # ★ 2026-08-15: the under-cap offer's reach, and in practice THE
                # reach number for the offer surface. real_prewall_offers above
                # can sit at 0 forever without meaning anything is broken (the
                # pre-wall stamp fires only at the last free call); this one
                # fires on ordinary under-cap calls, once per (session, tool).
                # Read them as siblings, never summed — a single call carries
                # exactly one of the two statuses.
                "real_undercap_offers": out["split"]["real"]["undercap"],
                # ★ 2026-08-12: THE ABANDONMENT GAP, named rather than inferred.
                # `abandoned` above is quotes that never SETTLED, which silently
                # merges two populations with opposite fixes: agents that came
                # back and broke (fix the settlement path) and agents that saw a
                # price and left (fix price/framing/retry ergonomics). Splitting
                # the return event apart from the issue event is what makes the
                # second one countable at all.
                "real_returned": out["split"]["real"]["returned"],
                "abandonment_quotes_never_returned":
                    rc - out["split"]["real"]["returned"],
                "abandonment_basis":
                    _FUNNEL_BASIS["abandonment.quotes_never_returned"],
            }

            # ── by_tool: real pay-intent per flagship tool ──────────────
            rows = _run(cur, "by_tool",
                        "SELECT tool, "
                        "  COUNT(*) FILTER (WHERE status = 'mpp_challenge') AS chal, "
                        "  COUNT(*) FILTER (WHERE status IN ('mpp_paid','x402_paid')) AS paid, "
                        "  COUNT(*) FILTER (WHERE status = 'mpp_offer_prewall') AS prewall, "
                        "  COUNT(*) FILTER (WHERE status = 'mpp_offer_undercap') AS undercap "
                        + base + " AND " + _REAL_PLATFORM_SQL +
                        # ★ order by prewall too: with 0 real challenges (the
                        # steady state) the old ordering left every row tied at
                        # 0 and the tools actually receiving offers sorted
                        # arbitrarily — i.e. the only non-zero signal on this
                        # board had no influence on what you were shown.
                        # ★ 2026-08-15: undercap joins the ORDER BY for exactly
                        # that reason — it is now the most frequently non-zero
                        # signal on the board, so leaving it out of the sort
                        # re-creates the arbitrary ordering this line fixed.
                        " GROUP BY tool ORDER BY chal DESC, paid DESC, prewall DESC, "
                        " undercap DESC LIMIT 20",
                        (list(_ALL_PAY_ST), win), fetch="all")
            for r in (rows or []):
                g = (lambda i: r.get(list(r.keys())[i]) if hasattr(r, "get") else r[i])
                out["by_tool"].append({"tool": g(0) or "?",
                                       "real_challenges": int(g(1) or 0),
                                       "real_paid": int(g(2) or 0),
                                       "real_prewall_offers": int(g(3) or 0),
                                       "real_undercap_offers": int(g(4) or 0)})

            # ── trend: WoW real challenges/paid ─────────────────────────
            def _wow(status_pred, params):
                cur_r = _run(cur, "wow_cur",
                             "SELECT COUNT(*) FROM mcp_call_log WHERE " + status_pred +
                             " AND timestamp > NOW() - INTERVAL '7 days' AND " + _REAL_PLATFORM_SQL,
                             params)
                prev_r = _run(cur, "wow_prev",
                              "SELECT COUNT(*) FROM mcp_call_log WHERE " + status_pred +
                              " AND timestamp <= NOW() - INTERVAL '7 days' "
                              " AND timestamp > NOW() - INTERVAL '14 days' AND " + _REAL_PLATFORM_SQL,
                              params)
                return _n(cur_r), _n(prev_r)
            c7, cp = _wow("status = %s", (_CHAL_ST,))
            p7, pp = _wow("status = ANY(%s)", (list(_PAID_ST),))
            direction = "flat"
            if c7 > cp: direction = "up"
            elif c7 < cp: direction = "down"
            out["trend"] = {"real_challenges_7d": c7, "real_challenges_prev7d": cp,
                            "real_paid_7d": p7, "real_paid_prev7d": pp, "direction": direction}

            # ── milestones (all-time) ───────────────────────────────────
            r = _run(cur, "first_paid",
                     "SELECT MIN(timestamp) FROM mcp_call_log "
                     " WHERE status IN ('mpp_paid','x402_paid')", None)
            v = (r[0] if r else None)
            out["milestones"]["first_paid_at"] = str(v) if v else None
            r = _run(cur, "first_real_chal",
                     "SELECT MIN(timestamp) FROM mcp_call_log "
                     " WHERE status = %s AND " + _REAL_PLATFORM_SQL, (_CHAL_ST,))
            v = (r[0] if r else None)
            out["milestones"]["first_real_challenge_at"] = str(v) if v else None

            # ── who: distinct real platforms that opted in (window) ─────
            rows = _run(cur, "real_platforms",
                        "SELECT platform, COUNT(*) AS n FROM mcp_call_log "
                        " WHERE status = %s "
                        " AND timestamp > NOW() - make_interval(days => %s) "
                        " AND " + _REAL_PLATFORM_SQL +
                        " GROUP BY platform ORDER BY n DESC LIMIT 15",
                        (_CHAL_ST, win), fetch="all")
            for r in (rows or []):
                g = (lambda i: r.get(list(r.keys())[i]) if hasattr(r, "get") else r[i])
                out["real_platforms"].append({"platform": g(0), "challenges": int(g(1) or 0)})
    except Exception as e:
        out["error"] = str(e)[:200]
        try: conn.rollback()
        except Exception: pass
    finally:
        try: conn.close()
        except Exception: pass

    # ── verdict: the self-driving read + next action ────────────────────
    fp = out["milestones"]["first_paid_at"]
    all_paid = out["split"]["all"]["paid"]
    real_chal = out["funnel"]["real_challenges"]
    real_fail = out["funnel"]["real_failed"]
    all_chal = out["split"]["all"]["challenges"]
    if fp or all_paid > 0:
        top = (out["by_tool"][0]["tool"] if out["by_tool"] else "the flagship tools")
        out["verdict"] = {
            "status": "SETTLED",
            "headline": f"🎉 FIRST AUTONOMOUS AGENT PAYMENT settled on the MPP rail "
                        f"(first_paid_at={fp}). The rail works E2E with real money.",
            "next_action": f"Double down: instrument what {top} did right, then widen the "
                           f"same challenge→pay path to the other flagship tools + all agent platforms.",
        }
    elif real_chal > 0:
        # ★ 2026-08-12: these two branches used to BOTH describe quote issuance as
        # "opted in to pay", which is the misreading that made the funnel useless.
        # With the return event split out, the board can now say which of the two
        # opposite fixes applies — and say UNKNOWN when it still cannot tell.
        real_ret = out["funnel"]["real_returned"]
        never_ret = out["funnel"]["abandonment_quotes_never_returned"]
        if real_fail > 0:
            out["verdict"] = {
                "status": "RETURNED_AND_FAILED",
                "headline": f"{real_chal} quote(s) ISSUED to real agents; {real_ret} came back "
                            f"with a credential and {real_fail} of those FAILED verification. "
                            f"{never_ret} never returned at all.",
                "next_action": "Two different fixes, and the split says how much of each: audit "
                               "the server.mjs settle → Stripe MPP verify handshake for the "
                               f"{real_fail} that returned and broke, and treat the {never_ret} "
                               "that never came back as a price/framing problem, not a bug.",
            }
        elif real_ret > 0:
            out["verdict"] = {
                "status": "RETURNED_NO_SETTLE",
                "headline": f"{real_ret} real agent(s) came back with a credential and none "
                            f"settled, with no verify failure logged — the return is being lost "
                            f"between presentation and settlement.",
                "next_action": "Trace one returned credential end-to-end; a return with no "
                               "terminal outcome is now visible as mpp_credential_returned.",
            }
        else:
            out["verdict"] = {
                "status": "QUOTED_NEVER_RETURNED",
                "headline": f"{real_chal} quote(s) were ISSUED to real agents and NOT ONE came "
                            f"back with a credential. Nothing failed — nobody tried.",
                "next_action": "This is not a settlement bug: no settlement was ever attempted. "
                               "Work the price, the framing and the retry ergonomics of the "
                               "offer itself. Auditing the verify handshake would find nothing.",
            }
    elif all_chal > 0:
        out["verdict"] = {
            "status": "LIVE_TEST_ONLY",
            "headline": f"Rail proven E2E on synthetic agents ({all_chal} test challenges) but "
                        f"ZERO real agent pay-intent yet.",
            "next_action": "Demand problem, not a plumbing problem. Drive real agent traffic to the "
                           "gated flagship tools (get_grid_intelligence / get_fiber_intel / get_market_intel) "
                           "and make the paywall challenge legible to autonomous callers.",
        }
    else:
        # r-mpp-onestep (2026-07-25): the gateway now ships a ready-to-pay challenge
        # INSIDE the paywall preview (structuredContent.agent_payment.challenges[0]),
        # so an agent no longer has to request one via _meta.mpp_pay=true. A passively
        # offered challenge is NOT pay-intent and is deliberately not recorded as
        # 'mpp_challenge' — that status still means "an agent explicitly asked to pay".
        # Consequence: challenges≈0 is now the EXPECTED steady state, not evidence the
        # challenge is missing. The live signal moved downstream to settle attempts
        # (mpp_paid / mpp_verify_failed), so point the operator there instead of at the
        # old "is it even surfaced?" check, which was verified surfaced on 2026-07-25.
        out["verdict"] = {
            "status": "NO_INTENT",
            "headline": "Rail live, challenge shipped inline with every gated preview — "
                        "no agent has attempted a settle yet.",
            "next_action": "Challenges are now offered passively, so a 0 here is expected — "
                           "watch mpp_paid/mpp_verify_failed (settle attempts), not challenges. "
                           "To move it: drive real agent traffic to the gated flagship tools and "
                           "confirm agent_payment.challenges[0] is present on a live anon preview.",
        }
    return jsonify(out), 200
