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
    "pro":                199,
    "pro_annual":         99,   # $1188/yr ≈ $99/mo equivalent
    "pro_annual_onetime": 99,   # source_plan value matches above
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
        c = _pg.connect(dsn, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception as e:
        logger.warning("funnel_health: db connect failed: %s", e)
        return None


def _scalar(cur, sql: str, params: tuple = ()) -> Optional[int]:
    """Run a COUNT(*)/SUM(...)/scalar SELECT, return int or None on error.
    Each probe is isolated so one missing table never blanks the page."""
    try:
        cur.execute(sql, params)
        r = cur.fetchone()
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
    """Run a SELECT, return rows or [] on error."""
    try:
        cur.execute(sql, params)
        return list(cur.fetchall() or [])
    except Exception as e:
        logger.debug("funnel_health _rows failed: %s -- %s", sql[:60], e)
        return []


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
                 "tool_calls_7d": 0, "dev_keys_by_tier": {}},
        "funnel": {"calls_30d": 0, "distinct_paid_users_30d": 0,
                   "signals_30d": 0, "codes_minted_30d": 0,
                   "pages_viewed_30d": 0, "stripe_clicked_30d": 0,
                   "conversions_30d": 0, "stage_drops_pct": {}},
        "signals_per_tool": [],
        "session_upgrades": {"total": 0, "last_7d": 0, "top_platforms": []},
        "renewal_nudge": {"total": 0, "eligible_next_30d": 0,
                          "sent_last_7d": 0},
        "signals_with_tool_tagged": {"total_30d": 0, "untagged_30d": 0,
                                     "pct_tagged_30d": 0.0},
        "source_plan_cohort": {"total": 0, "expiring_next_60d": 0,
                               "expired_not_demoted": 0},
        # 2026-06-07: session-bound 3-strike high-intent claim KPIs.
        # high_intent_sessions_30d = rows that crossed the 3-paid-hits
        # threshold in the last 30d. claims_minted = how many of those got
        # a signed URL emitted into the paywall response. claims_used =
        # how many humans clicked the URL + entered an email. claim_to_paid
        # = how many of THOSE eventually paid (joined to users.email).
        "high_intent": {"sessions_30d": 0, "claims_minted_30d": 0,
                        "claims_used_30d": 0, "claim_to_paid_30d": 0,
                        "minted_rate_pct": 0.0, "claim_to_paid_rate_pct": 0.0,
                        "threshold": 3},
        "platforms": [],
        "ab_status": {"ab_active": False, "kill_switch": False,
                      "cohorts": {"A": {}, "B": {}}, "z": 0.0, "sig_pct": 0.0,
                      "winner": "no_data"},
        "events": [],
        "missing_tables": [],
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
            cur.execute(
                "SELECT plan, COUNT(*) FROM users "
                " WHERE COALESCE(subscription_status, 'active') = 'active' "
                "   AND plan IN ('starter','developer','pro','pro_annual',"
                "                'enterprise','enterprise_annual',"
                "                'research_seed_nlr') "
                " GROUP BY plan"
            )
            mrr = 0
            for plan, n in cur.fetchall() or []:
                mrr += int(n or 0) * _PLAN_MONTHLY_USD.get(str(plan) or "", 0)
            out["kpis"]["mrr_usd"] = mrr
        except Exception as e:
            logger.debug("MRR probe failed: %s", e)
            out["missing_tables"].append("users (MRR probe)")

        # ── KPI 2: Conversions 30d ────────────────────────────────────
        # Prefer Stripe-webhook signal (mcp_conversions table). Fall back
        # to mcp_pair_codes.redeemed_at + users.plan_updated_at.
        conv_30d = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_conversions "
            " WHERE created_at >= NOW() - INTERVAL '30 days'")
        if conv_30d is None:
            out["missing_tables"].append("mcp_conversions")
            # Fall back to pair-code redemptions.
            conv_30d = _scalar(cur,
                "SELECT COUNT(*) FROM mcp_pair_codes "
                " WHERE redeemed_at >= NOW() - INTERVAL '30 days'")
            if conv_30d is None:
                conv_30d = 0
        out["kpis"]["conversions_30d"] = int(conv_30d or 0)

        # ── KPI 3: Active dev keys + per-tier breakdown ───────────────
        try:
            cur.execute(
                "SELECT tier, COUNT(*) FROM mcp_dev_keys "
                " WHERE COALESCE(status,'active') = 'active' "
                " GROUP BY tier")
            total = 0
            tiers: dict = {}
            for tier, n in cur.fetchall() or []:
                n = int(n or 0)
                tiers[str(tier or "unknown")] = n
                total += n
            out["kpis"]["active_dev_keys"] = total
            out["kpis"]["dev_keys_by_tier"] = tiers
        except Exception as e:
            logger.debug("dev_keys probe failed: %s", e)
            out["missing_tables"].append("mcp_dev_keys")

        # ── KPI 4: Tool calls 7d ──────────────────────────────────────
        v = _scalar(cur,
            "SELECT COUNT(*) FROM mcp_call_log "
            " WHERE timestamp >= NOW() - INTERVAL '7 days'")
        if v is None:
            out["missing_tables"].append("mcp_call_log")
            v = 0
        out["kpis"]["tool_calls_7d"] = int(v)

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
        try:
            cur.execute(
                "SELECT "
                "  COUNT(*) FILTER (WHERE tool_requested IS NOT NULL "
                "                     AND tool_requested <> ''), "
                "  COUNT(*) "
                "  FROM mcp_upgrade_signals "
                " WHERE created_at >= NOW() - INTERVAL '30 days'")
            r = cur.fetchone() or (0, 0)
            tagged = int(r[0] or 0)
            total = int(r[1] or 0)
            untagged = max(0, total - tagged)
            pct = round(100.0 * tagged / total, 1) if total else 0.0
            out["signals_with_tool_tagged"] = {
                "total_30d": total,
                "untagged_30d": untagged,
                "tagged_30d": tagged,
                "pct_tagged_30d": pct,
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

        # ── 2026-06-07: High-intent 3-strike claim KPIs ───────────────
        # The new conversion-funnel surface — see
        # routes/mcp_high_intent_claim.py. We probe directly here (not via
        # the public stats endpoint) so the dashboard works during the
        # rollout window before the endpoint is necessarily deployed.
        try:
            v = _scalar(cur,
                "SELECT COUNT(*) FROM mcp_high_intent_sessions "
                " WHERE last_hit_at >= NOW() - INTERVAL '30 days' "
                "   AND paid_call_count_24h >= 3")
            if v is None:
                out["missing_tables"].append("mcp_high_intent_sessions")
            else:
                out["high_intent"]["sessions_30d"] = int(v or 0)
                out["high_intent"]["claims_minted_30d"] = int(_scalar(cur,
                    "SELECT COUNT(*) FROM mcp_high_intent_sessions "
                    " WHERE claim_minted_at IS NOT NULL "
                    "   AND claim_minted_at >= NOW() - INTERVAL '30 days'") or 0)
                out["high_intent"]["claims_used_30d"] = int(_scalar(cur,
                    "SELECT COUNT(*) FROM mcp_high_intent_sessions "
                    " WHERE claim_used_at IS NOT NULL "
                    "   AND claim_used_at >= NOW() - INTERVAL '30 days'") or 0)
                # claim_to_paid: joined to users.email. Skips silently if
                # users.email column missing (rollout-time defensiveness).
                try:
                    cur.execute(
                        "SELECT COUNT(DISTINCT h.claim_email) "
                        "  FROM mcp_high_intent_sessions h "
                        "  JOIN users u "
                        "    ON LOWER(u.email) = LOWER(h.claim_email) "
                        " WHERE h.claim_used_at IS NOT NULL "
                        "   AND h.claim_used_at >= NOW() - INTERVAL '30 days' "
                        "   AND COALESCE(u.plan, 'free') NOT IN ('free','')")
                    out["high_intent"]["claim_to_paid_30d"] = int(
                        (cur.fetchone() or (0,))[0] or 0)
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                if out["high_intent"]["sessions_30d"] > 0:
                    out["high_intent"]["minted_rate_pct"] = round(
                        100.0 * out["high_intent"]["claims_minted_30d"]
                        / out["high_intent"]["sessions_30d"], 1)
                if out["high_intent"]["claims_used_30d"] > 0:
                    out["high_intent"]["claim_to_paid_rate_pct"] = round(
                        100.0 * out["high_intent"]["claim_to_paid_30d"]
                        / out["high_intent"]["claims_used_30d"], 1)
        except Exception:
            try: conn.rollback()
            except Exception: pass

        # ── Per-AI-platform breakdown ─────────────────────────────────
        # Match on mcp_call_log.platform (LIKE) + mcp_upgrade_signals.mcp_client.
        # Conversions = pair_codes redeemed where user_agent_at_view matches.
        for key, label, patterns in _AI_PLATFORMS:
            row = {"key": key, "label": label, "requests_30d": 0,
                   "distinct_sessions_30d": 0, "signals_30d": 0,
                   "conversions_30d": 0, "conv_rate_pct": 0.0}
            try:
                like_clauses = " OR ".join(
                    "LOWER(COALESCE(platform,'')) LIKE %s" for _ in patterns)
                params = tuple(f"%{p}%" for p in patterns)
                cur.execute(
                    "SELECT COUNT(*), COUNT(DISTINCT session_id) "
                    "  FROM mcp_call_log "
                    " WHERE timestamp >= NOW() - INTERVAL '30 days' "
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
            except Exception:
                out["missing_tables"].append("pricing_ab_events")
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


def _data_cached() -> dict:
    """Return cached data if fresh (<60s); else re-build + cache."""
    now = time.time()
    if _CACHE["data"] is not None and (now - _CACHE["ts"]) < _CACHE_TTL_S:
        return _CACHE["data"]
    data = _build_data()
    _CACHE["data"] = data
    _CACHE["ts"] = now
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
                                   "claims_used_30d": 0, "claim_to_paid_30d": 0,
                                   "minted_rate_pct": 0.0,
                                   "claim_to_paid_rate_pct": 0.0,
                                   "threshold": 3})
    ab = data["ab_status"]
    plats = data["platforms"]
    evs = data["events"]
    missing = data["missing_tables"]

    admin_key_safe = _esc(admin_key, quote=True)

    # Hero KPIs.
    keys_by_tier = k.get("dev_keys_by_tier") or {}
    tier_strs = " · ".join(
        f"{_fmt_n(v)} {_esc(t)}" for t, v in sorted(keys_by_tier.items(),
                                                   key=lambda x: -int(x[1] or 0))
    ) or "no keys"

    # Funnel waterfall (vertical).
    stages = [
        ("Tool calls (30d)",          f["calls_30d"],
            None),
        ("Distinct paid-tool users",  f["distinct_paid_users_30d"],
            f["stage_drops_pct"].get("calls→distinct")),
        ("Upgrade signals",           f["signals_30d"],
            f["stage_drops_pct"].get("distinct→signals")),
        ("Pair codes minted",         f["codes_minted_30d"],
            f["stage_drops_pct"].get("signals→codes")),
        ("Pages viewed (/redeem)",    f["pages_viewed_30d"],
            f["stage_drops_pct"].get("codes→viewed")),
        ("Stripe clicks",             f["stripe_clicked_30d"],
            f["stage_drops_pct"].get("viewed→clicked")),
        ("Conversions",               f["conversions_30d"],
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

    # A/B table.
    cA = ab["cohorts"].get("A") or {}
    cB = ab["cohorts"].get("B") or {}
    sig_pct = ab.get("sig_pct") or 0.0
    z = ab.get("z") or 0.0
    winner = ab.get("winner") or "no_data"
    ab_active = ab.get("ab_active")
    ab_kill = ab.get("kill_switch")
    ab_state = ("KILL SWITCH ON" if ab_kill
                else "ACTIVE" if ab_active
                else "OFF (arm A only)")
    ab_state_color = ("#ef4444" if ab_kill
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

  <!-- HERO KPIS -->
  <div class="heros">
    <div class="hero">
      <div class="hero-l">MRR</div>
      <div class="hero-v">${_fmt_n(k['mrr_usd'])}</div>
      <div class="hero-d">/mo — sum of active subscriptions × plan price</div>
    </div>
    <div class="hero">
      <div class="hero-l">Conversions 30d</div>
      <div class="hero-v">{_fmt_n(k['conversions_30d'])}</div>
      <div class="hero-d">mcp_conversions OR pair_codes.redeemed_at</div>
    </div>
    <div class="hero">
      <div class="hero-l">Active dev keys</div>
      <div class="hero-v">{_fmt_n(k['active_dev_keys'])}</div>
      <div class="hero-d">{_esc(tier_strs)}</div>
    </div>
    <div class="hero">
      <div class="hero-l">Tool calls 7d</div>
      <div class="hero-v">{_fmt_n(k['tool_calls_7d'])}</div>
      <div class="hero-d">mcp_call_log row count</div>
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
      </div>
    </div>
  </div>

  <!-- NEW TABLES row 2 -->
  <div class="grid">
    <div class="card">
      <h2>Signals with tool_requested tagged <span style="font-size:10px;color:var(--ok);">FIX C</span></h2>
      <div class="card-row">
        <div class="sub-stat">
          <div class="ss-l">Total 30d</div>
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
      </div>
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

  <!-- 2026-06-07: 3-STRIKE HIGH-INTENT CLAIM (closes 0% MCP-conversion gap) -->
  <div class="card" style="margin-bottom:18px;">
    <h2>3-strike high-intent claim <span style="font-size:10px;color:var(--accent);">NEW · session-bound · threshold={hi.get('threshold',3)}</span></h2>
    <div class="card-row">
      <div class="sub-stat">
        <div class="ss-l">High-intent sessions 30d</div>
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
        <div class="ss-l">Claims used 30d</div>
        <div class="ss-v">{_fmt_n(hi.get('claims_used_30d',0))}</div>
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
      mcp-server tracks every paid-tool hit per (session_id, tool); on the 3rd hit in 24h
      the paywall response embeds a signed
      <code>https://dchub.cloud/claim/&lt;token&gt;</code> URL. Human enters email →
      trial key emailed via Resend. First conversion target: within 7 days of ship.
    </div>
  </div>

  <!-- PER-AI-PLATFORM table -->
  <div class="card" style="margin-bottom:18px;">
    <h2>Per-AI-platform conversion (30d)</h2>
    <table>
      <thead><tr>
        <th>Platform</th>
        <th style="text-align:right">Requests</th>
        <th style="text-align:right">Distinct sessions</th>
        <th style="text-align:right">Signals</th>
        <th style="text-align:right">Conversions</th>
        <th style="text-align:right">Conv %</th>
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
