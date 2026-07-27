"""DC Hub — honest AI-platform REACH (r86-reach, 2026-06-14).

The /ai "AI Wars" page headlines cumulative request COUNTS (866K, Claude 102K) that are
really ~22-25 distinct IPs per platform looping + internal traffic. This endpoint returns
the HONEST reach: DISTINCT public IPs per platform over the recent window — the real number
of agent sources, not loop-inflated volume. Robust (doesn't depend on fragile internal/
external UA tagging) and cached (the agent_requests scan is heavy).

Standalone file + own blueprint so the concurrent backend refactors can't revert it.
Register in main.py:  from routes.ai_reach import ai_reach_bp; app.register_blueprint(ai_reach_bp)

  GET /api/v1/ai/reach   -> { distinct_agents_7d, distinct_platforms, per_platform:[...], requests_7d, note }
"""
from __future__ import annotations
import os, time, json
from flask import Blueprint, jsonify
import psycopg2, psycopg2.extras

ai_reach_bp = Blueprint("ai_reach_r86", __name__)

# private/loopback ranges = definitely internal; public IPs = real external reach.
# 100.64.0.0/10 = CGNAT (RFC 6598) = Railway's INTERNAL proxy fleet — a server only
# sees a 100.64.x source via an internal proxy, never a real client. agent_requests
# recorded ONLY these (no X-Forwarded-For), which is why its "reach" was ~16 proxy
# nodes; reach now sources from mcp_tool_calls (real client IPs). Keep CGNAT excluded
# everywhere as defense-in-depth.
_PRIVATE_IP = r"^(10\.|127\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.|::1|fc|fd|0\.0\.0\.0|$)"
_INTERNAL_PLAT = ('internal', 'mcp_generic', 'direct', 'unknown', 'unknown_ai', 'mcp', 'Unknown', '')
_cache = {"ts": 0.0, "data": None}
_TTL = 1800  # 30 min — the scan is heavy; stale-on-error below covers the cold-refresh window


def _conn():
    db = os.environ.get("DATABASE_URL")
    if not db:
        return None
    try:
        c = psycopg2.connect(db, sslmode="require", connect_timeout=5)
        c.autocommit = True
        return c
    except Exception:
        return None


@ai_reach_bp.route("/api/v1/ai/reach", methods=["GET"])
def ai_reach():
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < _TTL:
        return jsonify(_cache["data"]), 200
    out = {"distinct_agents_7d": 0, "distinct_platforms": 0, "per_platform": [], "requests_7d": 0,
           "window": "~recent (id-bounded ≈7d)",
           "note": "Honest reach = DISTINCT public IPs per platform (real agent sources), not cumulative request volume. The big 'requests served' counts are real traffic but loop-inflated; this is the addressable reach."}
    # fail-soft (2026-06-14): this is a PUBLIC DISPLAY endpoint for the /ai page —
    # it must NEVER return 5xx. A 5xx throws an F12 console error and can blank the
    # reach lines (caught live: a cold replica whose first request hit the 9s scan
    # timeout with an empty in-memory cache returned 500). On ANY failure: serve
    # last-good cache, else the valid empty skeleton at 200 with degraded=true so the
    # page renders gracefully and quietly retries on the next poll.
    def _soft():
        if _cache["data"] is not None:
            stale = dict(_cache["data"]); stale["stale"] = True
            return jsonify(stale), 200
        out["degraded"] = True
        return jsonify(out), 200
    c = _conn()
    if c is None:
        return _soft()
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ── FAST PATH (r-reach-rollup 2026-06-22): read the precomputed weekly
            # rollup (reach_weekly, built by routes/ai_reach_rollup.run_reach_rollup
            # on the daily cron). O(2-row PK read), NO agent_requests scan — so this
            # endpoint never times out and never exhausts the 1-replica pool when the
            # Leadership/Utilization engines self-call it concurrently (the documented
            # anti-pattern that froze those dashboards at 0.0). MAX over the last 2
            # weeks so the current (partial) ISO week never dips the metric to a
            # Monday-morning low. Falls through to a single capped live scan only if
            # the rollup table is empty (cold start) — same _PRIVATE_IP / _INTERNAL_PLAT
            # filters, so the two sources can never drift.
            rolled = None
            try:
                cur.execute("SET statement_timeout = '4000'")
                cur.execute("""
                    SELECT week_start, distinct_external_ips, distinct_platforms,
                           requests, per_platform
                    FROM reach_weekly ORDER BY week_start DESC LIMIT 2
                """)
                rolled = [dict(r) for r in cur.fetchall()] or None
            except Exception:
                rolled = None   # table missing / cold → live-scan fallback below

            if rolled:
                agents = max(int(r.get("distinct_external_ips") or 0) for r in rolled)
                nplats = max(int(r.get("distinct_platforms") or 0) for r in rolled)
                reqs   = max(int(r.get("requests") or 0) for r in rolled)
                pp = rolled[0].get("per_platform") or []
                if isinstance(pp, str):
                    try: pp = json.loads(pp)
                    except Exception: pp = []
                out["distinct_agents_7d"] = agents
                # ★2026-07-27: the `or len(pp)` fallback counted the RAW
                # per_platform array — every distinct platform string, including
                # `mcp` (the protocol), `reviewer-sim` (our test simulator) and
                # three separate entries for Anthropic. That is exactly the
                # inflated 15 this fix removes, so falling back to it would
                # resurrect the bad number the moment the rollup column read 0.
                # Count canonical vendors instead, via the shared module.
                if not nplats and pp:
                    try:
                        from ai_platform_canon import count_platforms
                        nplats = count_platforms(
                            d.get("platform_id") for d in pp if isinstance(d, dict))
                    except Exception:
                        nplats = 0
                out["distinct_platforms"] = nplats
                out["per_platform"] = pp
                out["requests_7d"] = reqs
                out["window"] = "weekly rollup (reach_weekly · precomputed daily)"
                out["source"] = "rollup"
                _cache["data"] = out
                _cache["ts"] = now
                return jsonify(out), 200

            # ── COLD-START FALLBACK (rollup empty): live query over mcp_tool_calls ──
            # r-reach-mcp-source (2026-06-24): mcp_tool_calls captures REAL public client
            # IPs (agent_requests only ever had CGNAT proxy IPs), is small + created_at-
            # indexed → a 7d live query is fast. Inlined predicate (no bound params) so
            # the literal % in PLATFORM_CASE's ILIKE patterns are left alone; reuse the
            # canonical de-loop (real_calls_predicate) so this == the funnel's real reach.
            cur.execute("SET statement_timeout = '8000'")
            from mcp_calls_deloop import PLATFORM_CASE as _PC, real_calls_predicate as _rcp
            _w = ("created_at >= NOW() - INTERVAL '7 days' "
                  "AND ip_address IS NOT NULL AND ip_address <> '' "
                  "AND ip_address !~ '" + _PRIVATE_IP + "' "
                  "AND (" + _rcp() + ")")
            cur.execute(
                "SELECT (" + _PC.strip() + ") AS platform_id, "
                "       COUNT(DISTINCT ip_address) AS agents, COUNT(*) AS requests "
                "FROM mcp_tool_calls WHERE " + _w +
                " GROUP BY 1 HAVING COUNT(DISTINCT ip_address) >= 1 "
                " ORDER BY agents DESC, requests DESC LIMIT 25")
            rows = [dict(r) for r in cur.fetchall()]
            out["per_platform"] = rows
            out["distinct_platforms"] = len(rows)
            # overall distinct external agents (an IP can span platforms — count once)
            cur.execute(
                "SELECT COUNT(DISTINCT ip_address) AS agents, COUNT(*) AS reqs "
                "FROM mcp_tool_calls WHERE " + _w)
            tot = cur.fetchone() or {}
            out["distinct_agents_7d"] = int(tot.get("agents") or 0)
            out["requests_7d"] = int(tot.get("reqs") or 0)
            out["source"] = "live_scan_mcp_tool_calls"
    except Exception:
        return _soft()   # fail-soft: never 5xx (last-good cache, else degraded 200)
    finally:
        try: c.close()
        except Exception: pass
    _cache["data"] = out
    _cache["ts"] = now
    return jsonify(out), 200
