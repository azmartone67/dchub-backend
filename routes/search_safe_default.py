"""
search_safe_default.py — /api/v1/search with safe defaults (no 400).

Phase ZZZZZ-round44 (2026-05-25). /api/v1/search?limit=5 was returning
400 because it required a `q` parameter. This wrapper returns recent
high-quality facilities when no query is provided instead of erroring.
"""
import os
from contextlib import contextmanager
from flask import Blueprint, jsonify, request
try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

search_safe_bp = Blueprint("search_safe_default", __name__)

def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()

@search_safe_bp.route("/api/v1/search-safe", methods=["GET"])
def safe_search():
    """Alternative to /api/v1/search that never 400s on missing q."""
    q = (request.args.get("q") or "").strip()
    limit = max(1, min(50, int(request.args.get("limit", 10))))
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 200
    out = {"q": q, "limit": limit, "no_query_default": not q}
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if q:
                cur.execute("""SELECT id, name, city, state, country, provider, power_mw
                              FROM discovered_facilities
                              WHERE LOWER(name) LIKE %s OR LOWER(city) LIKE %s
                                 OR LOWER(provider) LIKE %s
                              LIMIT %s""",
                            (f"%{q.lower()}%", f"%{q.lower()}%", f"%{q.lower()}%", limit))
            else:
                cur.execute("""SELECT id, name, city, state, country, provider, power_mw
                              FROM discovered_facilities
                              WHERE power_mw IS NOT NULL AND power_mw > 50
                              ORDER BY power_mw DESC NULLS LAST LIMIT %s""", (limit,))
            rows = cur.fetchall()
        out["results"] = [dict(r) for r in rows]
        out["count"] = len(out["results"])
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
    return jsonify(out), 200
