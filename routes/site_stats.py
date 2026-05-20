"""Phase PP (2026-05-15) — Live site stats for homepage + /intelligence hub.

A single JSON endpoint the frontend can hit on page load to replace ALL
hardcoded numbers ("20,000+ facilities", "276 markets", "9,000+ substations"
etc.). Every count is queried live; nothing is hardcoded.

Why a dedicated endpoint instead of /api/v1/stats?
  - /api/v1/stats is facility-focused, returns 50+ source breakdowns.
  - Homepage needs ~10 specific numbers and a small grid-pulse block.
  - Caching strategy is different: this endpoint should be edge-cacheable
    (60s public cache) because every visitor sees the same numbers; the
    facility-stats endpoint is admin-y.

Public, no auth. 60s CDN cache.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import psycopg2
from flask import Blueprint, jsonify

site_stats_bp = Blueprint("site_stats", __name__)


# In-process cache so hammering this endpoint doesn't blow up the DB
# even if CF cache is bypassed. 60s TTL matches the public Cache-Control.
_CACHE: dict = {"payload": None, "ts": 0.0}
_CACHE_TTL = 60.0


def _conn():
    url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not url:
        return None
    try:
        return psycopg2.connect(url, connect_timeout=6)
    except Exception as e:
        print(f"[site_stats] DB connect failed: {e}", file=sys.stderr)
        return None


def _scalar(cur, sql: str, default=0):
    """Run a single-cell query with rollback safety. Returns `default`
    on any error (table missing, column missing, permission, etc.) so
    one bad query never breaks the whole endpoint."""
    try:
        cur.execute(sql)
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else default
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return default


def _build_stats() -> dict:
    """Pull every homepage stat in one connection, one cursor.

    Failure isolation: each _scalar() rolls back on error, so missing
    tables don't cascade. The endpoint should always return something.
    """
    conn = _conn()
    if conn is None:
        return {"ok": False, "error": "no_database",
                "stats": {}, "as_of": _now()}

    s: dict = {}
    try:
        with conn.cursor() as cur:
            # ── Coverage ───────────────────────────────────────────
            # Phase AAA-2 (2026-05-17) — match the truth flip from Phase HH:
            # the homepage was painting 12,553 (legacy `facilities` table
            # count) while /api/v1/stats reports 21,374 (real `discovered_
            # facilities` count). User flagged this mismatch directly.
            # Now site/stats returns the same truth — discovered count if
            # available, fallback to legacy table. countries also pulls
            # from the larger pool.
            try:
                s["facilities"] = int(_scalar(cur,
                    "SELECT COUNT(*) FROM discovered_facilities"))
            except Exception:
                s["facilities"] = int(_scalar(cur,
                    "SELECT COUNT(*) FROM facilities"))
            # Expose both for backwards compatibility
            try:
                s["facilities_legacy_published"] = int(_scalar(cur,
                    "SELECT COUNT(*) FROM facilities"))
            except Exception:
                pass
            try:
                s["countries"] = int(_scalar(cur,
                    "SELECT COUNT(DISTINCT country) FROM discovered_facilities WHERE country IS NOT NULL"))
            except Exception:
                s["countries"] = int(_scalar(cur,
                    "SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL"))
            s["markets_tracked"] = int(_scalar(cur,
                "SELECT COUNT(DISTINCT market_slug) FROM market_power_scores WHERE published = true"))
            s["build_markets"]   = int(_scalar(cur,
                "SELECT COUNT(DISTINCT market_slug) FROM market_power_scores WHERE published = true AND verdict = 'BUILD'"))
            s["avoid_markets"]   = int(_scalar(cur,
                "SELECT COUNT(DISTINCT market_slug) FROM market_power_scores WHERE published = true AND verdict = 'AVOID'"))
            s["substations"]     = int(_scalar(cur,
                "SELECT COUNT(*) FROM substations"))
            s["air_permits"]     = int(_scalar(cur,
                "SELECT COUNT(*) FROM air_permits"))
            s["transactions"]    = int(_scalar(cur,
                "SELECT COUNT(*) FROM dc_transactions"))

            # ── Power capacity (real, queryable numbers) ───────────
            s["total_mw_tracked"] = float(_scalar(cur,
                "SELECT COALESCE(SUM(power_mw), 0) FROM facilities WHERE power_mw IS NOT NULL", 0.0))
            s["operational_mw"]   = float(_scalar(cur,
                "SELECT COALESCE(SUM(power_mw), 0) FROM facilities WHERE power_mw IS NOT NULL AND LOWER(COALESCE(status,'')) IN ('operational','live','active')", 0.0))
            s["pipeline_mw"]      = float(_scalar(cur,
                "SELECT COALESCE(SUM(capacity_mw), 0) FROM capacity_pipeline", 0.0))

            # ── Energy / grid ──────────────────────────────────────
            s["states_with_rates"] = int(_scalar(cur,
                "SELECT COUNT(DISTINCT state) FROM eia_retail_rates WHERE rate_cents_kwh > 0"))
            s["isos_covered"]      = int(_scalar(cur,
                "SELECT COUNT(DISTINCT iso) FROM market_power_scores WHERE iso IS NOT NULL AND iso != ''"))

            # ── MCP / AI traffic (the "agents are using us" signal) ─
            # mcp_calls_7d is the GROSS count (includes our own QA probes).
            # mcp_calls_7d_real (Phase FF+25-followup-r3, 2026-05-20)
            # filters out the self-traffic platforms so the homepage tile
            # reflects external AI-agent demand only. CF WAF over-blocking
            # of our probes May 17-19 dragged the gross count from 38k→27k
            # while real external traffic was unchanged; we now ship both
            # so the public-facing number is robust against probe noise.
            s["mcp_calls_7d"]      = int(_scalar(cur,
                "SELECT COUNT(*) FROM mcp_tool_calls WHERE created_at > NOW() - INTERVAL '7 days'"))
            try:
                s["mcp_calls_7d_real"] = int(_scalar(cur, """
                    SELECT COUNT(*) FROM mcp_tool_calls
                     WHERE created_at > NOW() - INTERVAL '7 days'
                       AND COALESCE(LOWER(user_agent),'') NOT LIKE '%curl%'
                       AND COALESCE(LOWER(user_agent),'') NOT LIKE '%python%'
                       AND COALESCE(LOWER(user_agent),'') NOT LIKE '%requests%'
                       AND COALESCE(LOWER(user_agent),'') NOT LIKE '%node%'
                       AND COALESCE(LOWER(user_agent),'') NOT LIKE '%axios%'
                       AND COALESCE(LOWER(user_agent),'') NOT LIKE '%postman%'
                       AND COALESCE(LOWER(user_agent),'') NOT LIKE '%insomnia%'
                       AND COALESCE(LOWER(user_agent),'') NOT LIKE 'dchub%'
                       AND COALESCE(LOWER(user_agent),'') NOT LIKE '%dchub-%'
                       AND user_agent IS NOT NULL
                       AND user_agent != ''
                """))
            except Exception:
                # Schema may not have user_agent yet on every deploy
                s["mcp_calls_7d_real"] = s["mcp_calls_7d"]
            s["mcp_unique_callers_7d"] = int(_scalar(cur,
                "SELECT COUNT(DISTINCT COALESCE(NULLIF(client_name,''),NULLIF(platform,''),ip_address)) FROM mcp_tool_calls WHERE created_at > NOW() - INTERVAL '7 days'"))
            s["mcp_developers"]    = int(_scalar(cur,
                "SELECT COUNT(*) FROM mcp_dev_keys"))

            # ── Trust signals ──────────────────────────────────────
            s["testimonials"]      = int(_scalar(cur,
                "SELECT COUNT(*) FROM ai_testimonials WHERE approved = true"))
            s["press_releases"]    = int(_scalar(cur,
                "SELECT COUNT(*) FROM press_releases WHERE published = true"))

            # ── Freshness (when did our biggest tables last update) ─
            s["facilities_last_updated"] = _to_iso(_scalar(cur,
                "SELECT MAX(discovered_at) FROM discovered_facilities", None))
            s["dcpi_last_updated"]       = _to_iso(_scalar(cur,
                "SELECT MAX(computed_at) FROM market_power_scores", None))

            # ── New this week — for the "DC Hub is growing" narrative ─
            s["new_facilities_7d"] = int(_scalar(cur,
                "SELECT COUNT(*) FROM discovered_facilities WHERE discovered_at > NOW() - INTERVAL '7 days'"))
            s["new_mcp_devs_7d"]   = int(_scalar(cur,
                "SELECT COUNT(*) FROM mcp_dev_keys WHERE created_at > NOW() - INTERVAL '7 days'"))

            # ── Grid pulse — per-ISO snapshot for the hero widget ──
            # Returns up to 7 ISOs with their market footprint + avg
            # DCPI excess/constraint. The hero shows the top 3-4.
            s["grid_pulse"] = _grid_pulse(cur)
    finally:
        try: conn.close()
        except Exception: pass

    return {"ok": True, "stats": s, "as_of": _now()}


def _grid_pulse(cur) -> list:
    """Per-ISO grid snapshot. Powers the homepage Grid Pulse widget."""
    try:
        cur.execute("""
            SELECT iso,
                   COUNT(DISTINCT market_slug) AS markets,
                   ROUND(AVG(excess_power_score)::numeric, 1) AS avg_excess,
                   ROUND(AVG(constraint_score)::numeric, 1)   AS avg_constraint,
                   COUNT(DISTINCT CASE WHEN verdict = 'BUILD' THEN market_slug END) AS build_count
              FROM (
                SELECT DISTINCT ON (market_slug)
                       iso, market_slug, excess_power_score,
                       constraint_score, verdict
                  FROM market_power_scores
                 WHERE published = true AND iso IS NOT NULL AND iso != ''
                 ORDER BY market_slug, computed_at DESC
              ) latest
             GROUP BY iso
             ORDER BY markets DESC
             LIMIT 7
        """)
        return [
            {"iso": r[0],
             "markets": int(r[1] or 0),
             "avg_excess": float(r[2] or 0),
             "avg_constraint": float(r[3] or 0),
             "build_count": int(r[4] or 0)}
            for r in cur.fetchall()
        ]
    except Exception:
        try: cur.connection.rollback()
        except Exception: pass
        return []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_iso(val) -> str | None:
    if val is None: return None
    try: return val.isoformat()
    except Exception: return str(val)


@site_stats_bp.get("/api/v1/site/stats")
def site_stats():
    """Live site stats for the homepage hero + /intelligence hub.

    Cached at the edge (60s public) AND in-process (60s) so that even
    a thundering herd at deploy time can't take the DB down. Frontend
    can poll every 30s without amplifying load."""
    now = time.time()
    if _CACHE["payload"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        body = _CACHE["payload"]
        cached = True
    else:
        body = _build_stats()
        _CACHE["payload"] = body
        _CACHE["ts"] = now
        cached = False

    from flask import make_response
    resp = make_response(jsonify(body))
    # 60s public cache + 30s stale-while-revalidate so first paint never
    # waits on the DB even when the cache misses.
    resp.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=30"
    resp.headers["X-Stats-Cache"] = "hit" if cached else "miss"
    return resp
