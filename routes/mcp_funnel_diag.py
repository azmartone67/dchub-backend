"""
mcp_funnel_diag.py — MCP signup funnel leak diagnostic.

Phase ZZZZZ-round36 (2026-05-24). The brain consistency radar flagged:
  - mcp_conversion_rate_below_floor
  - mcp_conversion_stale_critical (945 signals → 0 conversions 24h)
  - paywall_click_leak_critical
  - addressable_demand_unconverted: 2

This endpoint surfaces the funnel state so we can SEE where the leak
is happening (instead of just knowing it exists). Read-only — pulls
from mcp_upgrade_signals + mcp_conversions tables.
"""
import os
import datetime
from contextlib import contextmanager
from flask import Blueprint, jsonify

try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

mcp_funnel_bp = Blueprint("mcp_funnel", __name__,
                           url_prefix="/api/v1/mcp")


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()


@mcp_funnel_bp.route("/funnel", methods=["GET"])
def funnel_diag():
    out = {
        "at": datetime.datetime.utcnow().isoformat() + "Z",
        "stages": {},
        "leaks": [],
    }
    if not (_pg and _dsn()):
        out["error"] = "no_db"
        return jsonify(out), 200

    # Stage 1: total MCP tool calls 24h (top of funnel)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(call_count),0)::int FROM mcp_tool_usage "
                "WHERE date >= CURRENT_DATE - INTERVAL '1 day'")
            out["stages"]["1_tool_calls_24h"] = cur.fetchone()[0]
    except Exception as e:
        out["stages"]["1_tool_calls_24h"] = {"_error": type(e).__name__}

    # Stage 2: paywall signals (limit_hit, tier_required)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT signal_type, COUNT(*)::int "
                "FROM mcp_upgrade_signals "
                "WHERE created_at > NOW() - INTERVAL '1 day' "
                "GROUP BY signal_type ORDER BY 2 DESC")
            out["stages"]["2_paywall_signals_24h"] = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:
        out["stages"]["2_paywall_signals_24h"] = {"_error": type(e).__name__}

    # Stage 3: signals with email (identified users)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*)::int FROM mcp_upgrade_signals "
                "WHERE created_at > NOW() - INTERVAL '7 days' "
                "AND user_email IS NOT NULL AND user_email != ''")
            out["stages"]["3_identified_signals_7d"] = cur.fetchone()[0]
    except Exception as e:
        out["stages"]["3_identified_signals_7d"] = {"_error": type(e).__name__}

    # Stage 4: outreach sent
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*)::int FROM mcp_upgrade_signals "
                "WHERE outreach_sent = TRUE AND outreach_sent_at > NOW() - INTERVAL '7 days'")
            out["stages"]["4_outreach_sent_7d"] = cur.fetchone()[0]
    except Exception as e:
        out["stages"]["4_outreach_sent_7d"] = {"_error": type(e).__name__}

    # Stage 5: actual conversions
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT plan_to, COUNT(*)::int, COALESCE(SUM(mrr_cents),0)::int "
                "FROM mcp_conversions "
                "WHERE created_at > NOW() - INTERVAL '30 days' "
                "GROUP BY plan_to")
            out["stages"]["5_conversions_30d"] = [
                {"plan": r[0], "count": r[1], "mrr_cents": r[2]}
                for r in cur.fetchall()
            ]
    except Exception as e:
        out["stages"]["5_conversions_30d"] = {"_error": type(e).__name__}

    # Top tools triggering paywall hits (sales lead intel)
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT tool_requested, COUNT(*)::int "
                "FROM mcp_upgrade_signals "
                "WHERE created_at > NOW() - INTERVAL '7 days' "
                "AND tool_requested IS NOT NULL "
                "GROUP BY tool_requested ORDER BY 2 DESC LIMIT 10")
            out["top_tools_blocked_7d"] = {r[0]: r[1] for r in cur.fetchall()}
    except Exception as e:
        out["top_tools_blocked_7d"] = {"_error": type(e).__name__}

    # Leak detection
    s = out["stages"]
    sig_24h = s.get("2_paywall_signals_24h", {})
    if isinstance(sig_24h, dict):
        total_24h = sum(v for v in sig_24h.values() if isinstance(v, int))
        if total_24h > 100 and not s.get("5_conversions_30d"):
            out["leaks"].append({
                "name": "no_conversions_despite_signals",
                "severity": "critical",
                "detail": f"{total_24h} paywall signals 24h, 0 conversions 30d",
                "likely_cause": "Stripe price IDs not wired, /pricing CTA broken, or upgrade_url 401-locking",
            })
    ident = s.get("3_identified_signals_7d")
    outr = s.get("4_outreach_sent_7d")
    if isinstance(ident, int) and isinstance(outr, int):
        if ident > 0 and outr == 0:
            out["leaks"].append({
                "name": "identified_users_no_outreach",
                "severity": "high",
                "detail": f"{ident} users left email, 0 received outreach in 7d",
                "likely_cause": "Outreach cron disabled or SENDGRID/RESEND env vars unset",
            })

    return jsonify(out), 200
