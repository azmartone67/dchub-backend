"""
search_routes.py — Smart facility search endpoint for DC Hub
Route: GET /api/search/facilities?q=<query>&limit=<n>&state=<code>

Add to main.py (near the end, before if __name__ == "__main__":):
    from search_routes import register_search_routes
    register_search_routes(app)
"""
import os
import logging
from flask import request, jsonify

log = logging.getLogger(__name__)

_TABLES = ("facilities", "data_centers", "datacenters", "dc_facilities")


def _get_conn():
    import psycopg2
    url = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("DCHUB_DATABASE_URL")
        or os.environ.get("NEON_DATABASE_URL", "")
    )
    if not url:
        raise RuntimeError("No database URL configured")
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url)


def register_search_routes(app):
    @app.route("/api/search/facilities", methods=["GET"])
    def smart_search_facilities():
        q = request.args.get("q", "").strip()
        limit = min(int(request.args.get("limit", 8)), 50)
        state = request.args.get("state", "").strip()

        if not q or len(q) < 2:
            return jsonify({"success": False, "results": [], "error": "Query too short"})

        try:
            import psycopg2.extras
            conn = _get_conn()
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            search = f"%{q}%"
            state_clause = f"AND UPPER(state) = '{state.upper()}'" if state else ""

            for tbl in _TABLES:
                try:
                    cur.execute(
                        f"""
                        SELECT id, name, provider, city, state, country,
                               status, power_mw, latitude, longitude
                        FROM {tbl}
                        WHERE (name ILIKE %s OR provider ILIKE %s OR city ILIKE %s)
                        {state_clause}
                        ORDER BY
                            CASE WHEN name ILIKE %s THEN 0 ELSE 1 END,
                            CASE WHEN status = 'Operational' THEN 0
                                 WHEN status = 'Under Construction' THEN 1
                                 ELSE 2 END,
                            COALESCE(power_mw, 0) DESC
                        LIMIT %s
                        """,
                        (search, search, search, f"{q}%", limit),
                    )
                    rows = [dict(r) for r in cur.fetchall()]
                    cur.close()
                    conn.close()
                    return jsonify({"success": True, "results": rows, "count": len(rows), "table": tbl})
                except Exception as tbl_err:
                    log.debug("Table %s failed: %s", tbl, tbl_err)
                    conn.rollback()
                    continue

            cur.close()
            conn.close()
            return jsonify({"success": False, "results": [], "error": "No facilities table found"})

        except Exception as e:
            log.error("smart_search_facilities error: %s", e)
            return jsonify({"success": False, "results": [], "error": str(e)}), 500
