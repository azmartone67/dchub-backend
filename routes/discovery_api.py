"""
DC Hub Discovery API Routes — GeoJSON map layer endpoints
Serves power, gas, fiber, and facility data for the frontend map.
Import and register with your main Flask app:
    from routes.discovery_api import discovery_bp
    app.register_blueprint(discovery_bp, url_prefix='/api')
"""
from flask import Blueprint, jsonify, request
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'discovery'))
from db import get_cursor

discovery_bp = Blueprint('discovery', __name__)


@discovery_bp.route('/layers/power')
def layer_power():
    market = request.args.get('market')
    infra_type = request.args.get('type')
    query = """SELECT json_build_object('type','FeatureCollection','features',
        COALESCE(json_agg(json_build_object('type','Feature',
        'geometry',ST_AsGeoJSON(geom)::json,
        'properties',json_build_object('id',id,'type',type,'name',name,
        'capacity_mw',capacity_mw,'voltage_kv',voltage_kv,'fuel_type',fuel_type,
        'operator',operator,'source_market',source_market,'status',status)
        )) FILTER (WHERE geom IS NOT NULL),'[]'::json)) AS geojson
        FROM infrastructure_power WHERE 1=1"""
    params = []
    if market:
        query += " AND source_market=%s"; params.append(market)
    if infra_type:
        query += " AND type=%s"; params.append(infra_type)
    with get_cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    return jsonify(row["geojson"] if row else {"type":"FeatureCollection","features":[]})


@discovery_bp.route('/layers/gas')
def layer_gas():
    market = request.args.get('market')
    query = """SELECT json_build_object('type','FeatureCollection','features',
        COALESCE(json_agg(json_build_object('type','Feature',
        'geometry',ST_AsGeoJSON(geom)::json,
        'properties',json_build_object('id',id,'name',name,'operator',operator,
        'diameter_inches',diameter_inches,'pressure_psi',pressure_psi,
        'source_market',source_market,'status',status)
        )) FILTER (WHERE geom IS NOT NULL),'[]'::json)) AS geojson
        FROM infrastructure_gas WHERE 1=1"""
    params = []
    if market:
        query += " AND source_market=%s"; params.append(market)
    with get_cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    return jsonify(row["geojson"] if row else {"type":"FeatureCollection","features":[]})


@discovery_bp.route('/layers/fiber')
def layer_fiber():
    carrier = request.args.get('carrier')
    route_type = request.args.get('type')
    query = """SELECT json_build_object('type','FeatureCollection','features',
        COALESCE(json_agg(json_build_object('type','Feature',
        'geometry',COALESCE(geojson->'geometry',ST_AsGeoJSON(geom)::jsonb),
        'properties',json_build_object('id',id,'carrier',carrier,
        'route_name',route_name,'route_type',route_type,'distance_km',distance_km,
        'endpoint_a',endpoint_a,'endpoint_b',endpoint_b)
        )),'[]'::json)) AS geojson
        FROM infrastructure_fiber WHERE 1=1"""
    params = []
    if carrier:
        query += " AND carrier=%s"; params.append(carrier)
    if route_type:
        query += " AND route_type=%s"; params.append(route_type)
    with get_cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    return jsonify(row["geojson"] if row else {"type":"FeatureCollection","features":[]})


@discovery_bp.route('/layers/facilities')
def layer_facilities():
    state = request.args.get('state')
    provider = request.args.get('provider')
    status_filter = request.args.get('status')
    query = """SELECT json_build_object('type','FeatureCollection','features',
        COALESCE(json_agg(json_build_object('type','Feature',
        'geometry',ST_AsGeoJSON(geom)::json,
        'properties',json_build_object('id',id,'dchub_id',dchub_id,
        'name',name,'provider',provider,'city',city,'state',state,
        'status',status,'capacity_mw',capacity_mw)
        )) FILTER (WHERE geom IS NOT NULL),'[]'::json)) AS geojson
        FROM facilities WHERE 1=1"""
    params = []
    if state:
        query += " AND state=%s"; params.append(state)
    if provider:
        query += " AND provider ILIKE %s"; params.append(f"%{provider}%")
    if status_filter:
        query += " AND status=%s"; params.append(status_filter)
    with get_cursor() as cur:
        cur.execute(query, params)
        row = cur.fetchone()
    return jsonify(row["geojson"] if row else {"type":"FeatureCollection","features":[]})


@discovery_bp.route('/layers/all')
def layers_all():
    result = {}
    views = [("power","v_power_geojson"),("gas","v_gas_geojson"),
             ("fiber","v_fiber_geojson"),("facilities","v_facilities_geojson")]
    for name, view in views:
        try:
            with get_cursor() as cur:
                cur.execute(f"SELECT geojson FROM {view}")
                row = cur.fetchone()
                result[name] = row["geojson"] if row else {"type":"FeatureCollection","features":[]}
        except Exception:
            result[name] = {"type":"FeatureCollection","features":[]}
    return jsonify(result)


@discovery_bp.route('/intelligence-index')
def intelligence_index():
    with get_cursor() as cur:
        cur.execute("""SELECT pulse_score, version, agent_queries_24h,
            active_integrations, unique_facilities_queried_24h, fetched_at
            FROM intelligence_index ORDER BY fetched_at DESC LIMIT 1""")
        row = cur.fetchone()
    return jsonify(row or {"error":"No data"})


@discovery_bp.route('/news')
def news():
    category = request.args.get('category')
    limit = min(int(request.args.get('limit', 25)), 100)
    query = "SELECT * FROM news_articles"
    params = []
    if category:
        query += " WHERE category=%s"; params.append(category)
    query += " ORDER BY published_at DESC LIMIT %s"; params.append(limit)
    with get_cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return jsonify({"articles": rows, "count": len(rows)})


@discovery_bp.route('/energy-prices')
def energy_prices():
    state = request.args.get('state')
    data_type = request.args.get('type', 'retail_rates')
    query = "SELECT * FROM energy_prices WHERE data_type=%s"
    params = [data_type]
    if state:
        query += " AND state=%s"; params.append(state)
    query += " ORDER BY fetched_at DESC LIMIT 50"
    with get_cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return jsonify({"prices": rows, "count": len(rows)})


@discovery_bp.route('/discovery/status')
def discovery_status():
    with get_cursor() as cur:
        cur.execute("SELECT * FROM discovery_runs ORDER BY run_date DESC LIMIT 5")
        rows = cur.fetchall()
    return jsonify({"runs": rows})
