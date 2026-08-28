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
from internal_auth import accepted_internal_keys
import logging
from datetime import datetime, timezone, timedelta
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)
audience_signals_bp = Blueprint("audience_signals", __name__)


# ── Auth helpers ────────────────────────────────────────────────────
_INTERNAL_KEYS = accepted_internal_keys()
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
# timestamp, so no index applies).
# ★ 2026-08-28: that TEXT comparison was treated here as a PERFORMANCE
# problem. It was also a CORRECTNESS one — the string compare silently
# returned near-nothing, and _ai_platform_signals now reads ai_daily_stats
# instead. This module no longer queries ai_usage_tracking at all. No per-query timeout meant one slow
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

# Honest-numbers fence: the public /advertise hero must headline the
# DE-LOOPED tool-call volume — the same `tool_calls_7d_real` definition the
# /api/v1/mcp/funnel endpoint + the funnel-health dashboard use — not a gross
# COUNT(*) that includes our own selfheal/probe/sweep loop (~35-41k/wk). The
# de-loop predicate is single-sourced from mcp_calls_deloop. Import is
# defensive: if it fails, we fall back to gross BUT label it incl-loops so we
# never silently re-inflate.
try:
    from mcp_calls_deloop import deloop_calls_where as _deloop_where
except Exception:  # pragma: no cover - defensive
    _deloop_where = None


def _mcp_signals():
    """Pull MCP tool-call volume from mcp_tool_calls table.

    Headline numbers (tool_calls_7d / 30d) are DE-LOOPED — real external AI
    agent traffic only. The gross incl-loops counts are also returned, clearly
    labeled, for transparency / debugging."""
    try:
        from main import get_db
        conn = get_db()
        if conn is None:
            return {"_error": "no_db"}
    except Exception as e:
        return {"_error": f"db_init: {str(e)[:80]}"}
    out = {"tool_calls_7d": 0, "tool_calls_30d": 0,
           "tool_calls_7d_incl_loops": 0, "tool_calls_30d_incl_loops": 0,
           "deloop_applied": False,
           "distinct_clients_7d": 0, "top_tools": []}
    # Build the de-loop AND-fragment once (or '' if the shared helper is
    # unavailable → gross fallback, flagged via deloop_applied=False).
    _dl = ""
    if _deloop_where is not None:
        try:
            _dl = " AND " + _deloop_where()
            out["deloop_applied"] = True
        except Exception:
            _dl = ""
    try:
        with conn.cursor() as cur:
            _bound(cur)
            # Gross incl-loops (kept, explicitly labeled — never headlined).
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '7 days'"
            )
            out["tool_calls_7d_incl_loops"] = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '30 days'"
            )
            out["tool_calls_30d_incl_loops"] = int(cur.fetchone()[0] or 0)
            # Headline = de-looped (falls back to gross if _dl is '').
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '7 days'" + _dl
            )
            out["tool_calls_7d"] = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT COUNT(*) FROM mcp_tool_calls "
                "WHERE created_at >= NOW() - INTERVAL '30 days'" + _dl
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


# Platform buckets that are NOT an external AI platform: three that the dead
# table produced for unidentified callers, and three internal buckets that
# get_cumulative_totals() already excludes from the public /ai roster. All
# three figures below share this list, so the total, the distinct count and
# the breakdown are mutually consistent — a total padded with our own internal
# traffic is the defect this whole workstream exists to remove.
_NON_PLATFORM_BUCKETS = (
    # Unidentified callers — the three buckets the dead table produced.
    "Unknown", "API Client", "direct",
    # Internal buckets get_cumulative_totals() already hides from /ai.
    "internal", "mcp", "mcp_generic",
    # ★ 2026-08-28, found by reading the FIRST real result set after the
    # repoint — all three were in the PUBLIC top_platforms list. 15 requests
    # of 82,939: negligible by volume, wrong by kind. Two are our own traffic
    # and one is a scanner bucket, published as though they were AI platforms.
    "dchub-internal", "authorized-mcp-assessment", "mcp-ssrf-generic",
)

# ★ An exact list only ever excludes what someone already found in public.
# Nothing external is named dchub-anything, so fence the whole namespace and
# the next `dchub-*` bucket never reaches a published figure. Cheap here:
# ai_daily_stats holds one row per (date, platform), so there is no index for
# a prefix match to defeat — do not copy this onto a large table without
# reading the LIKE-prefix planner note first.
_SELF_PLATFORM_PREFIX = "dchub%"

# Shared 30-day window. `ai_daily_stats.date` is a real DATE, so this is an
# actual date comparison — see the docstring for why that is worth saying.
_AI_30D_WINDOW = "date >= CURRENT_DATE - 30"


def _ai_platform_signals():
    """AI platform footprint over the last 30 days, from `ai_daily_stats`.

    ★★★ 2026-08-28. This read `ai_usage_tracking`, which is DEAD: 11,029 rows,
    last write 2026-08-20, `tracked_at` NULL throughout, and every recent row
    `platform='Unknown'`. Worse, its `timestamp` column is TEXT, so the 30-day
    filter compared it to `(NOW() - INTERVAL '30 days')::text` — a STRING
    comparison standing in for a date filter. That could not raise
    (`timestamptz >= text` has no operator, so the fact that it returned at all
    proved the column was text), so the failure was SILENT: public, keyless
    `/api/v1/audience/summary` served `top_platforms: []` and
    `ai_requests_30d: 16` while the live source carried 365,256 all-time
    requests across 16 platforms.

    `ai_daily_stats (date DATE, platform, request_count)` is the per-day source
    `update_7d_rolling()` already sums to populate `ai_cumulative.requests_7d`.
    Its `date` is a real DATE, so the window is a genuine date comparison and a
    schema drift would raise here instead of quietly returning nothing.

    ★★★ DO NOT reach for `ai_cumulative` to satisfy this function. It is a
    one-row-per-platform rollup holding an ALL-TIME `total_requests` and a 7-day
    column; it contains no 30-day figure at all. Publishing `total_requests`
    under a `_30d` key would restate a 365k all-time number as a monthly one —
    a worse defect than the one being fixed. ★ Its `last_seen` is also TEXT in
    production despite the DDL saying TIMESTAMPTZ, and threw on every bare
    comparison for weeks (see `get_cumulative_totals` in ai_tracking.py).

    Returns zeros with `_error` set on failure; the caller renders absent
    rather than inventing a number.
    """
    try:
        from main import get_db
        conn = get_db()
        if conn is None:
            return {"_error": "no_db"}
    except Exception as e:
        return {"_error": f"db_init: {str(e)[:80]}"}
    out = {"distinct_platforms": 0, "total_requests_30d": 0, "top_platforms": []}
    excl = _NON_PLATFORM_BUCKETS
    try:
        with conn.cursor() as cur:
            _bound(cur)
            cur.execute(
                "SELECT COALESCE(SUM(request_count), 0) FROM ai_daily_stats "
                f"WHERE {_AI_30D_WINDOW} "
                "AND platform IS NOT NULL AND platform <> ALL(%s) "
                "AND platform NOT ILIKE %s",
                (list(excl), _SELF_PLATFORM_PREFIX)
            )
            out["total_requests_30d"] = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT COUNT(DISTINCT platform) FROM ai_daily_stats "
                f"WHERE {_AI_30D_WINDOW} "
                "AND platform IS NOT NULL AND platform <> ALL(%s) "
                "AND platform NOT ILIKE %s",
                (list(excl), _SELF_PLATFORM_PREFIX)
            )
            out["distinct_platforms"] = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT platform, SUM(request_count) AS n FROM ai_daily_stats "
                f"WHERE {_AI_30D_WINDOW} "
                "AND platform IS NOT NULL AND platform <> ALL(%s) "
                "AND platform NOT ILIKE %s "
                "GROUP BY platform ORDER BY n DESC LIMIT 12",
                (list(excl), _SELF_PLATFORM_PREFIX)
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
                    "AND COALESCE(signed_up_email, operator_email) IS NOT NULL"
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
                    "WHERE duplicate_of_id IS NULL"
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
