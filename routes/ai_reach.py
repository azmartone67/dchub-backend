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
import os, time
from flask import Blueprint, jsonify
import psycopg2, psycopg2.extras

ai_reach_bp = Blueprint("ai_reach_r86", __name__)

# private/loopback ranges = definitely internal; public IPs = real external reach.
_PRIVATE_IP = r"^(10\.|127\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|169\.254\.|::1|fc|fd|0\.0\.0\.0|$)"
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
    c = _conn()
    if c is None:
        return jsonify(error="no_db"), 503
    out = {"distinct_agents_7d": 0, "distinct_platforms": 0, "per_platform": [], "requests_7d": 0,
           "window": "~recent (id-bounded ≈7d)",
           "note": "Honest reach = DISTINCT public IPs per platform (real agent sources), not cumulative request volume. The big 'requests served' counts are real traffic but loop-inflated; this is the addressable reach."}
    try:
        with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # cap the heavy scan so a slow run fails fast → falls back to last-good cache (below)
            cur.execute("SET statement_timeout = '9000'")
            # id-bounded recent window (~7d of traffic) — avoids the slow TEXT-timestamp cast
            cur.execute("SELECT MAX(id) AS m FROM agent_requests")
            maxid = (cur.fetchone() or {}).get("m") or 0
            lo = maxid - 900000
            plats = "(" + ",".join("%s" for _ in _INTERNAL_PLAT) + ")"
            cur.execute(f"""
                SELECT platform_id,
                       COUNT(DISTINCT ip_address) AS agents,
                       COUNT(*)                   AS requests
                FROM agent_requests
                WHERE id > %s
                  AND ip_address IS NOT NULL AND ip_address <> ''
                  AND ip_address !~ %s
                  AND COALESCE(platform_id,'') NOT IN {plats}
                GROUP BY platform_id
                HAVING COUNT(DISTINCT ip_address) >= 1
                ORDER BY agents DESC, requests DESC
                LIMIT 25
            """, (lo, _PRIVATE_IP, *_INTERNAL_PLAT))
            rows = [dict(r) for r in cur.fetchall()]
            out["per_platform"] = rows
            out["distinct_platforms"] = len(rows)
            # overall distinct external agents (an IP can span platforms — count once)
            cur.execute(f"""
                SELECT COUNT(DISTINCT ip_address) AS agents, COUNT(*) AS reqs
                FROM agent_requests
                WHERE id > %s AND ip_address IS NOT NULL AND ip_address <> '' AND ip_address !~ %s
                  AND COALESCE(platform_id,'') NOT IN {plats}
            """, (lo, _PRIVATE_IP, *_INTERNAL_PLAT))
            tot = cur.fetchone() or {}
            out["distinct_agents_7d"] = int(tot.get("agents") or 0)
            out["requests_7d"] = int(tot.get("reqs") or 0)
    except Exception as e:
        # never blank the /ai reach lines on a slow cold-refresh: serve last-good cache if we have it
        if _cache["data"] is not None:
            stale = dict(_cache["data"]); stale["stale"] = True
            return jsonify(stale), 200
        return jsonify(error="query_failed", detail=str(e)[:200]), 500
    finally:
        try: c.close()
        except Exception: pass
    _cache["data"] = out
    _cache["ts"] = now
    return jsonify(out), 200
