"""hyperscaler_reclassify.py — re-run actor classifier on existing alerts."""
import os
from contextlib import contextmanager
from flask import Blueprint, jsonify
try:
    import psycopg2 as _pg
    import psycopg2.extras
except Exception:
    _pg = None

hyperscaler_reclassify_bp = Blueprint("hyperscaler_reclassify", __name__)

def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""

@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    try: yield c
    finally: c.close()

@hyperscaler_reclassify_bp.route("/api/v1/hyperscaler-alerts/reclassify", methods=["POST", "GET"])
def reclassify():
    if not (_pg and _dsn()): return jsonify({"error": "no_db"}), 200
    from routes.hyperscaler_alerts import _classify_actor
    updated = 0
    try:
        with _conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, headline FROM hyperscaler_alerts")
            rows = cur.fetchall()
        for r in rows:
            new_actor = _classify_actor(r["headline"])
            try:
                with _conn() as c, c.cursor() as cur:
                    cur.execute("UPDATE hyperscaler_alerts SET actor=%s WHERE id=%s", (new_actor, r["id"]))
                    c.commit()
                    updated += 1
            except Exception: pass
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500
    return jsonify({"reclassified": updated}), 200
