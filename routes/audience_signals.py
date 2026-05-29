"""
Phase FF+25-followup-audience (2026-05-20) — audience signals aggregator.
==========================================================================

Powers the public /advertise page and the internal /audience dashboard.

One endpoint returns ALL the eyeball signals an advertiser would want:
  - MCP tool call volume (7d / 30d)
  - Distinct AI platforms hitting our content
  - Top platforms by request count
  - Facility inventory size (proof of authority)
  - Estimated monthly request volume
  - Optional: Plausible.io stats if PLAUSIBLE_API_KEY is set

Endpoints:
  GET /api/v1/audience/summary       Public — what /advertise shows
  GET /api/v1/audience/full          Admin — adds breakdowns, geo, etc.

Designed to gracefully degrade: every external dependency (Plausible,
Clearbit, etc.) is optional. Missing keys → that section returns null,
endpoint still 200s with what we DO know.
"""
import os
import logging
from datetime import datetime, timezone, timedelta
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
audience_signals_bp = Blueprint("audience_signals", __name__)


# ── Auth helpers ────────────────────────────────────────────────────
_INTERNAL_KEYS = {"dchub-internal-sync-2026"}
for _n in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "MCP_INTERNAL_KEY", "DCHUB_ADMIN_KEY"):
    _v = os.environ.get(_n)
    if _v:
        _INTERNAL_KEYS.add(_v)


def _admin_ok():
    sent = (request.headers.get("X-Internal-Key")
            or request.args.get("admin_key") or "").strip()
    return sent in _INTERNAL_KEYS


# ── Performance guards (r43-H, 2026-05-28) ──────────────────────────
# /api/v1/audience/summary was hard-timing-out (000 at 35-38s): four
# collectors run sequentially, each doing unindexed COUNT(*) full-scans
# over mcp_tool_calls / ai_usage_tracking (the latter compares a TEXT
# timestamp, so no index applies). No per-query timeout meant one slow
# scan hung the whole request past gunicorn's 30s budget, and the cold
# request never produced a 200 for CF's max-age=300 to cache — so EVERY
# cold hit re-timed-out. Fix: (1) bound each query with statement_timeout
# so a slow scan degrades to partial data instead of hanging; (2) memoize
# the whole summary for 10 min so steady-state hits are instant.
import time as _time

_STMT_TIMEOUT_MS = 5000

def _bound(cur):
    """Cap any single query so it degrades instead of hanging the request."""
    try:
        cur.execute(f"SET statement_timeout = {int(_STMT_TIMEOUT_MS)}")
    except Exception:
        pass

_SUMMARY_TTL = 600  # seconds
_SUMMARY_CACHE = {"exp": 0.0, "data": None}


# ── Data collectors (each independent, fail-safe) ───────────────────

def _mcp_signals():
    """Pull MCP tool-call volume from mcp_tool_calls table."""
    try:
        from main import get_db
        conn = get_db()
        if conn is None:
            return {"_error": "no_db"}
    except Exception as e:
        return {"_error": f"db_init: {str(e)[:80]}"}
    out = {"tool_calls_7d": 0, "tool_calls_30d": 0,
           "distinct_clients_7d": 0, "top_tools": []}
    try:
        with conn.cursor() as cur:
            _bound(cur)
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '7 days'"
            )
            out["tool_calls_7d"] = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '30 days'"
            )
            out["tool_calls_30d"] = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT COUNT(DISTINCT client_name) FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '7 days' "
                "AND client_name IS NOT NULL AND client_name != 'unknown'"
            )
            out["distinct_clients_7d"] = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT tool_name, COUNT(*) AS n FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '30 days' "
                "GROUP BY tool_name ORDER BY n DESC LIMIT 10"
            )
            out["top_tools"] = [{"name": r[0], "calls": int(r[1])}
                                for r in cur.fetchall()]
    except Exception as e:
        out["_error"] = str(e)[:120]
    finally:
        try: conn.close()
        except Exception: pass
    return out


def _ai_platform_signals():
    """Pull AI platform footprint from ai_usage_tracking table."""
    try:
        from main import get_db
        conn = get_db()
        if conn is None:
            return {"_error": "no_db"}
    except Exception as e:
        return {"_error": f"db_init: {str(e)[:80]}"}
    out = {"distinct_platforms": 0, "total_requests_30d": 0, "top_platforms": []}
    try:
        with conn.cursor() as cur:
            _bound(cur)
            cur.execute(
                "SELECT COUNT(DISTINCT platform) FROM ai_usage_tracking "
                "WHERE platform IS NOT NULL "
                "AND platform NOT IN ('Unknown', 'API Client', 'direct')"
            )
            out["distinct_platforms"] = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT COUNT(*) FROM ai_usage_tracking "
                "WHERE timestamp >= (NOW() - INTERVAL '30 days')::text"
            )
            out["total_requests_30d"] = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT platform, COUNT(*) AS n FROM ai_usage_tracking "
                "WHERE timestamp >= (NOW() - INTERVAL '30 days')::text "
                "AND platform IS NOT NULL "
                "AND platform NOT IN ('Unknown', 'API Client', 'direct') "
                "GROUP BY platform ORDER BY n DESC LIMIT 12"
            )
            out["top_platforms"] = [{"name": r[0], "count": int(r[1])}
                                     for r in cur.fetchall()]
    except Exception as e:
        out["_error"] = str(e)[:120]
    finally:
        try: conn.close()
        except Exception: pass
    return out


def _funnel_signals():
    """Pull signed-up users + conversion data."""
    try:
        from main import get_db
        conn = get_db()
        if conn is None:
            return {"_error": "no_db"}
    except Exception as e:
        return {"_error": f"db_init: {str(e)[:80]}"}
    out = {"active_keys": 0, "minted_30d": 0, "converted_30d": 0,
           "trials_signed_up": 0}
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.api_keys') AS t"
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return out
            cur.execute(
                "SELECT COUNT(*) FROM api_keys "
                "WHERE COALESCE(revoked, false) = false"
            )
            out["active_keys"] = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT to_regclass('public.auto_trial_keys')")
            row = cur.fetchone()
            if row and row[0]:
                cur.execute(
                    "SELECT COUNT(*) FROM auto_trial_keys "
                    "WHERE minted_at >= NOW() - INTERVAL '30 days'"
                )
                out["minted_30d"] = int(cur.fetchone()[0] or 0)
                cur.execute(
                    "SELECT COUNT(*) FROM auto_trial_keys "
                    "WHERE minted_at >= NOW() - INTERVAL '30 days' "
                    "AND upgraded_tier IS NOT NULL"
                )
                out["converted_30d"] = int(cur.fetchone()[0] or 0)
                cur.execute(
                    "SELECT COUNT(*) FROM auto_trial_keys "
                    "WHERE minted_at >= NOW() - INTERVAL '30 days' "
                    "AND signed_up_email IS NOT NULL"
                )
                out["trials_signed_up"] = int(cur.fetchone()[0] or 0)
    except Exception as e:
        out["_error"] = str(e)[:120]
    finally:
        try: conn.close()
        except Exception: pass
    return out


def _facility_count():
    """Authority signal — how many facilities are we maintaining?"""
    try:
        from main import get_db
        conn = get_db()
        if conn is None:
            return 0
        try:
            with conn.cursor() as cur:
                _bound(cur)
                cur.execute(
                    "SELECT COUNT(*) FROM discovered_facilities "
                    "WHERE COALESCE(is_duplicate, 0) = 0"
                )
                return int(cur.fetchone()[0] or 0)
        finally:
            conn.close()
    except Exception:
        return 0


def _plausible_signals():
    """Pull human-visitor stats from Plausible.io Stats API.

    Requires PLAUSIBLE_API_KEY env var. Returns None if unset — endpoint
    still works without it, just without the human-visitor signal.
    Docs: https://plausible.io/docs/stats-api
    """
    token = os.environ.get("PLAUSIBLE_API_KEY", "").strip()
    if not token:
        return None
    site_id = os.environ.get("PLAUSIBLE_SITE_ID", "dchub.cloud")
    out = {"site": site_id}
    try:
        import requests
        base = "https://plausible.io/api/v1/stats/aggregate"
        params = {
            "site_id": site_id,
            "period": "30d",
            "metrics": "visitors,pageviews,visit_duration,bounce_rate",
        }
        r = requests.get(
            base,
            headers={"Authorization": f"Bearer {token}"},
            params=params, timeout=4,
        )
        if r.status_code == 200:
            data = r.json().get("results") or {}
            out["visitors_30d"] = (data.get("visitors") or {}).get("value")
            out["pageviews_30d"] = (data.get("pageviews") or {}).get("value")
            out["avg_visit_seconds"] = (data.get("visit_duration") or {}).get("value")
            out["bounce_rate_pct"] = (data.get("bounce_rate") or {}).get("value")
        else:
            out["_error"] = f"plausible HTTP {r.status_code}"
    except Exception as e:
        out["_error"] = str(e)[:120]
    return out


# ── Public endpoint: /api/v1/audience/summary ───────────────────────
@audience_signals_bp.route("/api/v1/audience/summary", methods=["GET"])
def audience_summary():
    """Public — feeds the /advertise page hero stats.
    No auth; safe to expose all numbers (they're already on the
    /alive operator dashboard)."""
    # Serve the memoized summary if fresh (10-min TTL). Keeps the
    # expensive collector scans off the request path in steady state.
    now = _time.time()
    cached = _SUMMARY_CACHE.get("data")
    if cached is not None and _SUMMARY_CACHE.get("exp", 0) > now:
        resp = jsonify(cached)
        resp.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
        resp.headers["X-Cache"] = "hit"
        return resp

    mcp = _mcp_signals()
    ai = _ai_platform_signals()
    facilities = _facility_count()

    # Compute a "monthly requests" estimate from CF analytics-style math
    # (we extrapolate from 7d MCP × 4.3, then add AI-platform 30d)
    mcp7 = mcp.get("tool_calls_7d", 0) or 0
    ai30 = ai.get("total_requests_30d", 0) or 0
    estimate_30d = int(mcp7 * 4.3) + int(ai30)

    out = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "mcp_tool_calls_7d": mcp.get("tool_calls_7d"),
        "mcp_tool_calls_30d": mcp.get("tool_calls_30d"),
        "ai_platforms_distinct": ai.get("distinct_platforms"),
        "ai_requests_30d": ai.get("total_requests_30d"),
        "requests_30d_estimate": estimate_30d,
        "facilities_tracked": facilities,
        "top_platforms": ai.get("top_platforms") or [],
        "top_tools": mcp.get("top_tools") or [],
    }
    # Plausible: only show if configured (otherwise advertiser-facing page
    # just uses the AI/MCP numbers which are already strong)
    plausible = _plausible_signals()
    if plausible and "visitors_30d" in plausible:
        out["human_visitors_30d"] = plausible["visitors_30d"]
        out["pageviews_30d"] = plausible["pageviews_30d"]

    _SUMMARY_CACHE["data"] = out
    _SUMMARY_CACHE["exp"] = _time.time() + _SUMMARY_TTL

    resp = jsonify(out)
    # Cache 5 min at edge — these don't change minute to minute
    resp.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=600"
    resp.headers["X-Cache"] = "miss"
    return resp


# ── Admin endpoint: /api/v1/audience/full ───────────────────────────
@audience_signals_bp.route("/api/v1/audience/full", methods=["GET"])
def audience_full():
    """Admin-only — adds funnel + Plausible details + raw collector
    outputs (including any _error fields)."""
    if not _admin_ok():
        return jsonify(error="forbidden", hint="X-Internal-Key required"), 403
    return jsonify(
        as_of=datetime.now(timezone.utc).isoformat(),
        mcp=_mcp_signals(),
        ai_platforms=_ai_platform_signals(),
        funnel=_funnel_signals(),
        facilities=_facility_count(),
        plausible=_plausible_signals(),
        environment={
            "plausible_configured": bool(os.environ.get("PLAUSIBLE_API_KEY")),
            "enrichment_configured": bool(os.environ.get("CLEARBIT_API_KEY")
                                            or os.environ.get("ABSTRACT_API_KEY")),
        },
    )


def _smoke():
    logger.info("[audience-signals] ready · plausible=%s · enrichment=%s",
                 bool(os.environ.get("PLAUSIBLE_API_KEY")),
                 bool(os.environ.get("CLEARBIT_API_KEY")
                       or os.environ.get("ABSTRACT_API_KEY")))

_smoke()
