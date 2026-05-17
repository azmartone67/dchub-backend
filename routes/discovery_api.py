"""
DC Hub Discovery API — GeoJSON map layers + intelligence data
Blueprint: dchub_discovery_api_bp (registered at /api prefix)

Endpoints:
  GET /api/layers/power       — Power infrastructure GeoJSON
  GET /api/layers/gas         — Gas infrastructure GeoJSON
  GET /api/layers/fiber       — Fiber infrastructure GeoJSON
  GET /api/layers/facilities  — Facilities GeoJSON
  GET /api/layers/all         — All layers combined
  GET /api/intelligence-index — Latest intelligence index
  GET /api/news               — Recent news articles
  GET /api/energy-prices      — Energy price data
  GET /api/discovery/status   — Pipeline run status
"""

from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras
import os
from contextlib import contextmanager

dchub_discovery_api_bp = Blueprint('dchub_discovery_api', __name__)

# ---------------------------------------------------------------------------
# DB helpers (uses same NEON_DATABASE_URL / DATABASE_URL as the main app)
# ---------------------------------------------------------------------------
def _get_discovery_conn():
    url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)

@contextmanager
def _discovery_cursor():
    conn = _get_discovery_conn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Helper: run query -> GeoJSON FeatureCollection
# ---------------------------------------------------------------------------
def _geojson_response(view_name, filters=None):
    """Query a GeoJSON view with optional WHERE filters."""
    where_clauses = []
    params = []
    if filters:
        for col, val in filters.items():
            if val:
                where_clauses.append(f"{col} ILIKE %s")
                params.append(f"%{val}%")

    sql = f"SELECT json_build_object('type','FeatureCollection','features',COALESCE(json_agg(feature),'[]'::json)) AS fc FROM (SELECT json_build_object('type','Feature','geometry',ST_AsGeoJSON(geom)::json,'properties',to_jsonb(t.*) - 'geom') AS feature FROM {view_name} t"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += ") sub"

    try:
        with _discovery_cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return jsonify(row['fc'] if row and row['fc'] else {"type": "FeatureCollection", "features": []})
    except psycopg2.errors.UndefinedTable:
        return jsonify({"type": "FeatureCollection", "features": [], "_note": "Table not yet created -- run migration 002_discovery_tables.sql"}), 200
    except Exception as e:
        return jsonify({"type": "FeatureCollection", "features": [], "error": str(e)}), 200

# ---------------------------------------------------------------------------
# GeoJSON layer endpoints
# ---------------------------------------------------------------------------
@dchub_discovery_api_bp.route('/layers/power')
def layer_power():
    return _geojson_response('v_power_geojson', {'market': request.args.get('market'), 'type': request.args.get('type')})

@dchub_discovery_api_bp.route('/layers/gas')
def layer_gas():
    return _geojson_response('v_gas_geojson', {'market': request.args.get('market')})

@dchub_discovery_api_bp.route('/layers/fiber')
def layer_fiber():
    return _geojson_response('v_fiber_geojson', {'carrier': request.args.get('carrier')})

@dchub_discovery_api_bp.route('/layers/facilities')
def layer_facilities():
    return _geojson_response('v_facilities_geojson', {'state': request.args.get('state'), 'provider': request.args.get('provider'), 'status': request.args.get('status')})

@dchub_discovery_api_bp.route('/layers/all')
def layers_all():
    """Return all four layers in one response for initial map load."""
    results = {}
    for name, view, filters in [
        ('power', 'v_power_geojson', {}),
        ('gas', 'v_gas_geojson', {}),
        ('fiber', 'v_fiber_geojson', {}),
        ('facilities', 'v_facilities_geojson', {}),
    ]:
        try:
            with _discovery_cursor() as cur:
                cur.execute(f"SELECT json_build_object('type','FeatureCollection','features',COALESCE(json_agg(json_build_object('type','Feature','geometry',ST_AsGeoJSON(geom)::json,'properties',to_jsonb(t.*) - 'geom')),'[]'::json)) AS fc FROM {view} t")
                row = cur.fetchone()
                results[name] = row['fc'] if row and row['fc'] else {"type": "FeatureCollection", "features": []}
        except Exception:
            results[name] = {"type": "FeatureCollection", "features": []}
    return jsonify(results)

# ---------------------------------------------------------------------------
# Data endpoints
# ---------------------------------------------------------------------------
@dchub_discovery_api_bp.route('/intelligence-index')
def intelligence_index():
    try:
        with _discovery_cursor() as cur:
            cur.execute("SELECT * FROM intelligence_index ORDER BY captured_at DESC LIMIT 30")
            rows = cur.fetchall()
            return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e), "data": []}), 200

# AUTO-REPAIR: duplicate route '/news' also in main.py:11290 — review and remove one
@dchub_discovery_api_bp.route('/news')
def news():
    limit = request.args.get('limit', 50, type=int)
    try:
        with _discovery_cursor() as cur:
            cur.execute("SELECT * FROM news_articles ORDER BY published_at DESC NULLS LAST LIMIT %s", (limit,))
            rows = cur.fetchall()
            return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e), "data": []}), 200

@dchub_discovery_api_bp.route('/energy-prices')
def energy_prices():
    try:
        with _discovery_cursor() as cur:
            cur.execute("SELECT * FROM energy_prices ORDER BY captured_at DESC LIMIT 100")
            rows = cur.fetchall()
            return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e), "data": []}), 200

@dchub_discovery_api_bp.route('/discovery/status')
def discovery_status():
    try:
        with _discovery_cursor() as cur:
            cur.execute("SELECT * FROM discovery_runs ORDER BY started_at DESC LIMIT 10")
            rows = cur.fetchall()
            return jsonify({"runs": [dict(r) for r in rows]})
    except Exception as e:
        return jsonify({"error": str(e), "runs": []}), 200
