"""
facilities_by_dims.py — Phase r51 (2026-05-25).

Adds the two API endpoints that /ai-inventory and other pages were
hitting and getting 404:

  GET /api/v1/facilities/by-market[?limit=15&market=]
  GET /api/v1/facilities/by-provider[?limit=50&provider=]

Both group facilities by their natural dimension (market or operator)
and return counts + sample facility names. Designed for dashboard
consumption — small response, cacheable, 60s edge TTL.

Cause of the 404 (per the user's r51 report):
  ai-inventory.js fetches these paths but the routes were never
  registered. They likely existed in an earlier branch and were
  removed during the SQLite→Neon migration without the frontend
  being updated.
"""
from __future__ import annotations

import os
from flask import Blueprint, jsonify, request

try:
    import psycopg2
    import psycopg2.extras
except Exception:
    psycopg2 = None


facilities_by_dims_bp = Blueprint("facilities_by_dims", __name__)


def _conn():
    if not psycopg2:
        return None
    db = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db:
        return None
    try:
        return psycopg2.connect(db, sslmode="require", connect_timeout=5)
    except Exception:
        return None


# AUTO-REPAIR: duplicate route '/api/v1/facilities/by-market' also in main.py:13090 — review and remove one
@facilities_by_dims_bp.route("/api/v1/facilities/by-market", methods=["GET"])
def facilities_by_market():
    """Top markets by facility count, with sample names per market."""
    try:
        limit = max(1, min(int(request.args.get("limit", 15)), 100))
    except Exception:
        limit = 15
    market_filter = (request.args.get("market") or "").strip()

    c = _conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable",
                         "markets": []}), 200
    try:
        with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if market_filter:
                cur.execute("""
                    SELECT market, COUNT(*) AS facility_count,
                           ARRAY_AGG(name ORDER BY power_mw DESC NULLS LAST)
                             FILTER (WHERE name IS NOT NULL) AS sample_names,
                           SUM(power_mw) AS total_power_mw,
                           COUNT(DISTINCT provider) AS operator_count
                      FROM facilities
                     WHERE market ILIKE %s
                       AND market IS NOT NULL
                     GROUP BY market
                     ORDER BY COUNT(*) DESC
                     LIMIT %s
                """, (f"%{market_filter}%", limit))
            else:
                cur.execute("""
                    SELECT market, COUNT(*) AS facility_count,
                           ARRAY_AGG(name ORDER BY power_mw DESC NULLS LAST)
                             FILTER (WHERE name IS NOT NULL) AS sample_names,
                           SUM(power_mw) AS total_power_mw,
                           COUNT(DISTINCT provider) AS operator_count
                      FROM facilities
                     WHERE market IS NOT NULL AND market != ''
                     GROUP BY market
                     ORDER BY COUNT(*) DESC
                     LIMIT %s
                """, (limit,))
            rows = []
            for r in cur.fetchall():
                samples = (r.get("sample_names") or [])[:5]
                rows.append({
                    "market":          r["market"],
                    "facility_count":  int(r["facility_count"]),
                    "operator_count":  int(r["operator_count"] or 0),
                    "total_power_mw":  float(r["total_power_mw"] or 0),
                    "sample_names":    samples,
                })
        resp = jsonify({
            "ok":      True,
            "markets": rows,
            "count":   len(rows),
            "source":  "Neon facilities table",
        })
        # Edge-cacheable; r51 graceful-on-slow-origin
        resp.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=600"
        return resp, 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:160],
                         "markets": []}), 200

# AUTO-REPAIR: duplicate route '/api/v1/facilities/by-provider' also in main.py:13121 — review and remove one

@facilities_by_dims_bp.route("/api/v1/facilities/by-provider", methods=["GET"])
def facilities_by_provider():
    """Top operators by facility count, with sample facility names."""
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except Exception:
        limit = 50
    provider_filter = (request.args.get("provider") or "").strip()

    c = _conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable",
                         "providers": []}), 200
    try:
        with c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if provider_filter:
                cur.execute("""
                    SELECT provider, COUNT(*) AS facility_count,
                           ARRAY_AGG(name ORDER BY power_mw DESC NULLS LAST)
                             FILTER (WHERE name IS NOT NULL) AS sample_names,
                           SUM(power_mw) AS total_power_mw,
                           COUNT(DISTINCT market) AS market_count
                      FROM facilities
                     WHERE provider ILIKE %s
                       AND provider IS NOT NULL
                     GROUP BY provider
                     ORDER BY COUNT(*) DESC
                     LIMIT %s
                """, (f"%{provider_filter}%", limit))
            else:
                cur.execute("""
                    SELECT provider, COUNT(*) AS facility_count,
                           ARRAY_AGG(name ORDER BY power_mw DESC NULLS LAST)
                             FILTER (WHERE name IS NOT NULL) AS sample_names,
                           SUM(power_mw) AS total_power_mw,
                           COUNT(DISTINCT market) AS market_count
                      FROM facilities
                     WHERE provider IS NOT NULL AND provider != ''
                     GROUP BY provider
                     ORDER BY COUNT(*) DESC
                     LIMIT %s
                """, (limit,))
            rows = []
            for r in cur.fetchall():
                samples = (r.get("sample_names") or [])[:5]
                rows.append({
                    "provider":       r["provider"],
                    "facility_count": int(r["facility_count"]),
                    "market_count":   int(r["market_count"] or 0),
                    "total_power_mw": float(r["total_power_mw"] or 0),
                    "sample_names":   samples,
                })
        resp = jsonify({
            "ok":        True,
            "providers": rows,
            "count":     len(rows),
            "source":    "Neon facilities table",
        })
        resp.headers["Cache-Control"] = "public, max-age=60, stale-while-revalidate=600"
        return resp, 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:160],
                         "providers": []}), 200


# r51-C: canonical facility count — single source of truth so the
# homepage, /daily, Gemini, and AI agents all see the SAME number.
# Currently: site says 21,400+, /daily shows 12,877, Gemini sees
# 10,700. This divergence is the user-reported "what is the truth"
# question. This endpoint resolves it from the live count.
@facilities_by_dims_bp.route("/api/v1/stats/canonical", methods=["GET"])
def stats_canonical():
    """Single authoritative count of facilities + key totals."""
    c = _conn()
    if not c:
        return jsonify({"ok": False, "error": "db_unavailable"}), 200
    try:
        with c, c.cursor() as cur:
            stats: dict = {}
            cur.execute("SELECT COUNT(*) FROM facilities")
            stats["total_facilities"] = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(*) FROM facilities WHERE country IS NOT NULL AND country != ''")
            stats["facilities_with_country"] = int(cur.fetchone()[0] or 0)
            cur.execute("SELECT COUNT(DISTINCT country) FROM facilities WHERE country IS NOT NULL")
            stats["countries_covered"] = int(cur.fetchone()[0] or 0)
            try:
                cur.execute("SELECT COUNT(*) FROM news")
                stats["news_articles"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
            try:
                cur.execute("SELECT COUNT(*) FROM deals")
                stats["deals_tracked"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
            try:
                cur.execute("SELECT COUNT(*) FROM market_power_scores")
                stats["dcpi_markets_scored"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
        resp = jsonify({
            "ok":         True,
            "stats":      stats,
            "source":     "Neon — live COUNT() at request time",
            "purpose":    ("Canonical truth for facility/news/deals/DCPI "
                            "counts. Use this endpoint when site copy, "
                            "AI agents, and reports need to agree."),
        })
        # Edge-cache 5 min — these counts change slowly
        resp.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=1800"
        return resp, 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:160]}), 200
