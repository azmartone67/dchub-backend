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


# AUTO-REPAIR: duplicate route '/api/v1/facilities/by-market' also in main.py:19912 — review and remove one
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

# AUTO-REPAIR: duplicate route '/api/v1/facilities/by-provider' also in main.py:19943 — review and remove one

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
            # ★2026-07-27: added `country != ''` — without it the empty-string
            # bucket counts as a country and this returned 181 while
            # /api/v1/stats (which has the guard) returned 180.
            cur.execute("SELECT COUNT(DISTINCT country) FROM facilities "
                        "WHERE country IS NOT NULL AND country != ''")
            stats["countries_covered"] = int(cur.fetchone()[0] or 0)
            try:
                cur.execute("SELECT COUNT(*) FROM news")
                stats["news_articles"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
            # ★2026-07-27: `deals_tracked` was COUNT(*) FROM deals — the RAW row
            # pile (4,484). The AUTO id embeds the ingest date
            # (AUTO-<yyyymmdd>-<hash>) so a re-ingest of the same deal never
            # conflicts and accrues one row per DAY; one atNorth deal held 945
            # rows. Publishing rows as "deals tracked" over-stated reality ~3.2x
            # — on the very endpoint whose stated purpose is making surfaces
            # agree. Now serves the deduped count (canonical_stats does the
            # AUTO-by-content-hash / tuple dedup and drops data_flag quarantine
            # rows); the raw row count stays available as `deals_rows`.
            try:
                cur.execute("SELECT COUNT(*) FROM deals")
                stats["deals_rows"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
            try:
                from canonical_stats import get_canonical_stats as _gcs_deals
                _dd = int((_gcs_deals() or {}).get("deals") or 0)
                if _dd > 0:
                    stats["deals_tracked"] = _dd
            except Exception:
                pass
            stats.setdefault("deals_tracked", stats.get("deals_rows", 0))
            try:
                cur.execute("SELECT COUNT(*) FROM market_power_scores")
                stats["dcpi_markets_scored"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
            # provenance-v1 (2026-07-11): the PUBLICLY CITEABLE verified count —
            # the canonical fleet filter (COALESCE(is_duplicate,0)=0 on
            # discovered_facilities; issue #1539). This is the number we grow in
            # public; facilities_tracked = the raw discovery pile it comes from.
            # Placed LAST in the tx block (a failed statement aborts the tx for
            # any query after it); falls back to cached canonical_stats.
            # ★★2026-07-27 data QA — `facilities_verified` is AMBIGUOUS and must
            # not be cited until the dedup repair lands. Its documented method
            # (COALESCE(is_duplicate,0)=0) returns 5,737, but the deployed
            # surface serves 13,395 (duplicate_of_id IS NULL) — repo and prod
            # disagree, so consumers cannot know which they got.
            #
            # ROOT CAUSE of the 5,737: the dedup pipeline flags rows
            # is_duplicate=1 WITHOUT electing a keeper. Grouping by
            # canonical_slug, 9,318 of 14,686 distinct facilities have NO row
            # with is_duplicate=0 — every member flagged, so the facility is
            # invisible to any is_duplicate-based count. The suppressed set
            # includes Meta Hyperion, Stargate Abilene, CoreWeave Project
            # Horizon and Microsoft Wisconsin (confidence 0.85-0.95).
            #
            # The three fields below are UNAMBIGUOUS by construction. New
            # consumers should read `facilities_distinct`; the two legacy fields
            # are kept only for back-compat.
            try:
                cur.execute("SELECT COUNT(DISTINCT canonical_slug) "
                            "FROM discovered_facilities "
                            "WHERE canonical_slug IS NOT NULL")
                stats["facilities_distinct"] = int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(*) FROM discovered_facilities")
                stats["facilities_records"] = int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                            "WHERE COALESCE(is_duplicate,0)=0")
                stats["facilities_with_keeper"] = int(cur.fetchone()[0] or 0)
            except Exception:
                pass
            try:
                # canonical fleet = distinct sites after cross-source dedup
                # (duplicate_of_id IS NULL, r-facility-dedup 2026-07-20), which
                # supersedes the older is_duplicate flag.
                cur.execute("SELECT COUNT(*) FROM discovered_facilities "
                            "WHERE duplicate_of_id IS NULL")
                stats["facilities_verified"] = int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(*) FROM discovered_facilities")
                stats["facilities_tracked"] = int(cur.fetchone()[0] or 0)
            except Exception:
                try:
                    from canonical_stats import get_canonical_stats
                    _cs = get_canonical_stats()
                    stats.setdefault("facilities_verified",
                                     _cs.get("facilities_verified"))
                    stats.setdefault("facilities_tracked", _cs.get("facilities"))
                except Exception:
                    pass
        _canon_payload = {
            "ok":         True,
            "stats":      stats,
            "source":     "Neon — live COUNT() at request time",
            "purpose":    ("Canonical truth for facility/news/deals/DCPI "
                            "counts. Use this endpoint when site copy, "
                            "AI agents, and reports need to agree."),
        }
        # provenance-v1: standard envelope on the citeable stats surface itself.
        try:
            from routes.provenance import (attach_provenance,
                                           FACILITIES_FALLBACK_URL)
            attach_provenance(
                _canon_payload,
                source="DC Hub canonical stats (Neon live counts)",
                method=("live COUNT() at request time; facilities_verified = "
                        "canonical fleet filter COALESCE(is_duplicate,0)=0 "
                        "over discovered_facilities"),
                # v1: facilities surface — counts cover the whole discovery
                # pile, so the conservative facilities tier is tracked; the
                # explicit fallback pins the facilities directory (no
                # cite_url_template on a stats surface).
                fallback_url=FACILITIES_FALLBACK_URL,
                default_v="tracked",
            )
        except Exception:
            pass
        resp = jsonify(_canon_payload)
        # Edge-cache 5 min — these counts change slowly
        resp.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=1800"
        return resp, 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:160]}), 200
