"""
DC Hub — Infrastructure Data Routes v2
═══════════════════════════════════════
New API endpoints for power_plants_eia, transmission_lines_eia, submarine_cables.
Plus: gas-pipelines added to rate limit bypass list.

Register in main.py:
    from routes.infrastructure_data_routes import register_infra_data_routes
    register_infra_data_routes(app, get_pg_connection)

Tables required (already created by bulk loaders):
    - power_plants_eia (13,441 rows)
    - transmission_lines_eia (94K+ rows) 
    - submarine_cables (690 rows)
    - submarine_cable_landings (landing points)
"""
import math
import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)
infra_data_bp = Blueprint('infra_data', __name__)

_get_db = None

def register_infra_data_routes(app, get_db_func):
    global _get_db
    _get_db = get_db_func
    app.register_blueprint(infra_data_bp)
    logger.info("✅ Infrastructure Data Routes v2 registered")
    logger.info("   📍 /api/v1/power-plants (13K+ EIA plants)")
    logger.info("   ⚡ /api/v1/transmission-lines (94K+ HIFLD lines)")
    logger.info("   🌊 /api/v1/submarine-cables (690 cables + landings)")


def _safe_float(val):
    try:
        return float(val) if val is not None else None
    except:
        return None


# ═══════════════════════════════════════════════════════════════
# POWER PLANTS API — 13,441 EIA plants with lat/lng
# ═══════════════════════════════════════════════════════════════

@infra_data_bp.route('/api/v1/power-plants', methods=['GET'])
def get_power_plants():
    """Get power plants with spatial filtering.
    
    Query params:
        lat, lng, radius (miles) — bounding box
        state — filter by state
        fuel — filter by primary fuel (solar, natural gas, wind, etc.)
        min_mw — minimum nameplate capacity
        limit — max results (default 200, cap 500)
    """
    # Bulletproof param parsing
    lat = request.args.get('lat', None)
    lng = request.args.get('lng', None)
    radius = request.args.get('radius', 50)
    state_filter = request.args.get('state', '').upper()
    fuel_filter = request.args.get('fuel', '').lower()
    min_mw = request.args.get('min_mw', None)
    limit = request.args.get('limit', 200, type=int)

    try:
        lat = float(lat) if lat is not None else None
    except:
        lat = None
    try:
        lng = float(lng) if lng is not None else None
    except:
        lng = None
    try:
        radius = int(float(radius)) if radius else 50
    except:
        radius = 50
    try:
        min_mw = float(min_mw) if min_mw is not None else None
    except:
        min_mw = None

    try:
        conn = _get_db()
        cur = conn.cursor()

        query = """SELECT id, plant_id, name, utility_name, state, city, county,
                   primary_fuel, technology, nameplate_capacity_mw, max_output_mw,
                   natural_gas_mw, solar_mw, wind_mw, nuclear_mw, coal_mw,
                   lat, lng FROM power_plants_eia 
                   WHERE lat IS NOT NULL AND lng IS NOT NULL"""
        params = []

        if lat is not None and lng is not None:
            lat_d = radius / 69.0
            lng_d = radius / (69.0 * max(math.cos(math.radians(lat)), 0.1))
            query += " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s"
            params.extend([lat - lat_d, lat + lat_d, lng - lng_d, lng + lng_d])

        if state_filter:
            query += " AND UPPER(state) = %s"
            params.append(state_filter)
        if fuel_filter:
            query += " AND LOWER(primary_fuel) = %s"
            params.append(fuel_filter)
        if min_mw is not None:
            query += " AND nameplate_capacity_mw >= %s"
            params.append(min_mw)

        query += " ORDER BY nameplate_capacity_mw DESC NULLS LAST LIMIT %s"
        params.append(min(limit, 500))

        cur.execute(query, params)
        rows = cur.fetchall()

        plants = []
        for r in rows:
            plants.append({
                'id': r[0], 'plant_id': r[1], 'name': r[2],
                'utility': r[3], 'state': r[4], 'city': r[5], 'county': r[6],
                'primary_fuel': r[7], 'technology': r[8],
                'capacity_mw': _safe_float(r[9]), 'max_output_mw': _safe_float(r[10]),
                'natural_gas_mw': _safe_float(r[11]), 'solar_mw': _safe_float(r[12]),
                'wind_mw': _safe_float(r[13]), 'nuclear_mw': _safe_float(r[14]),
                'coal_mw': _safe_float(r[15]),
                'lat': float(r[16]), 'lng': float(r[17])
            })

        conn.close()

        return jsonify({
            'success': True,
            'plants': plants,
            'count': len(plants),
            'filters': {
                'state': state_filter or 'all',
                'fuel': fuel_filter or 'all',
                'min_mw': min_mw,
                'spatial': lat is not None and lng is not None
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# TRANSMISSION LINES API — 94K+ HIFLD lines with lat/lng
# ═══════════════════════════════════════════════════════════════

@infra_data_bp.route('/api/v1/transmission-lines', methods=['GET'])
def get_transmission_lines():
    """Get transmission lines with spatial filtering.
    
    Query params:
        lat, lng, radius (miles) — bounding box
        state — filter by state
        min_voltage — minimum voltage in kV
        owner — partial match on owner name
        limit — max results (default 200, cap 500)
    """
    lat = request.args.get('lat', None)
    lng = request.args.get('lng', None)
    radius = request.args.get('radius', 50)
    state_filter = request.args.get('state', '').upper()
    min_voltage = request.args.get('min_voltage', None)
    owner_filter = request.args.get('owner', '')
    limit = request.args.get('limit', 200, type=int)

    try:
        lat = float(lat) if lat is not None else None
    except:
        lat = None
    try:
        lng = float(lng) if lng is not None else None
    except:
        lng = None
    try:
        radius = int(float(radius)) if radius else 50
    except:
        radius = 50
    try:
        min_voltage = float(min_voltage) if min_voltage is not None else None
    except:
        min_voltage = None

    try:
        conn = _get_db()
        cur = conn.cursor()

        query = """SELECT id, owner, voltage_kv, sub_1, sub_2, lat, lng, state
                   FROM transmission_lines_eia
                   WHERE lat IS NOT NULL AND lng IS NOT NULL"""
        params = []

        if lat is not None and lng is not None:
            lat_d = radius / 69.0
            lng_d = radius / (69.0 * max(math.cos(math.radians(lat)), 0.1))
            query += " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s"
            params.extend([lat - lat_d, lat + lat_d, lng - lng_d, lng + lng_d])

        if state_filter:
            query += " AND UPPER(state) = %s"
            params.append(state_filter)
        if min_voltage is not None:
            query += " AND voltage_kv >= %s"
            params.append(min_voltage)
        if owner_filter:
            query += " AND owner ILIKE %s"
            params.append(f"%{owner_filter}%")

        query += " ORDER BY voltage_kv DESC NULLS LAST LIMIT %s"
        params.append(min(limit, 500))

        cur.execute(query, params)
        rows = cur.fetchall()

        lines = []
        for r in rows:
            lines.append({
                'id': r[0], 'owner': r[1],
                'voltage_kv': _safe_float(r[2]),
                'substation_1': r[3], 'substation_2': r[4],
                'lat': float(r[5]), 'lng': float(r[6]),
                'state': r[7]
            })

        conn.close()

        return jsonify({
            'success': True,
            'lines': lines,
            'count': len(lines),
            'filters': {
                'state': state_filter or 'all',
                'min_voltage': min_voltage,
                'owner': owner_filter or 'all',
                'spatial': lat is not None and lng is not None
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# SUBMARINE CABLES API — 690 cables worldwide + landing points
# ═══════════════════════════════════════════════════════════════

@infra_data_bp.route('/api/v1/submarine-cables', methods=['GET'])
def get_submarine_cables():
    """Get submarine cables and landing points.
    
    Query params:
        lat, lng, radius (miles) — filter landing points by location
        country — filter landing points by country
        limit — max results (default 100)
    """
    lat = request.args.get('lat', None)
    lng = request.args.get('lng', None)
    radius = request.args.get('radius', 200)
    country_filter = request.args.get('country', '').upper()
    limit = request.args.get('limit', 100, type=int)

    try:
        lat = float(lat) if lat is not None else None
    except:
        lat = None
    try:
        lng = float(lng) if lng is not None else None
    except:
        lng = None
    try:
        radius = int(float(radius)) if radius else 200
    except:
        radius = 200

    try:
        conn = _get_db()
        cur = conn.cursor()

        # Get cables
        cur.execute("SELECT id, cable_id, name, color, length_km, rfs, owners, url FROM submarine_cables LIMIT %s",
                    [min(limit, 1000)])
        cable_rows = cur.fetchall()
        cables = []
        for r in cable_rows:
            cables.append({
                'id': r[0], 'cable_id': r[1], 'name': r[2], 'color': r[3],
                'length_km': _safe_float(r[4]), 'rfs': r[5],
                'owners': r[6], 'url': r[7]
            })

        # Get landing points (with optional spatial filter)
        lp_query = "SELECT id, name, country, lat, lng, cable_ids FROM submarine_cable_landings WHERE lat IS NOT NULL"
        lp_params = []

        if lat is not None and lng is not None:
            lat_d = radius / 69.0
            lng_d = radius / (69.0 * max(math.cos(math.radians(lat)), 0.1))
            lp_query += " AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s"
            lp_params.extend([lat - lat_d, lat + lat_d, lng - lng_d, lng + lng_d])

        if country_filter:
            lp_query += " AND UPPER(country) = %s"
            lp_params.append(country_filter)

        lp_query += " LIMIT %s"
        lp_params.append(min(limit, 500))

        cur.execute(lp_query, lp_params)
        lp_rows = cur.fetchall()
        landings = []
        for r in lp_rows:
            landings.append({
                'id': r[0], 'name': r[1], 'country': r[2],
                'lat': float(r[3]), 'lng': float(r[4]),
                'cable_ids': r[5]
            })

        conn.close()

        return jsonify({
            'success': True,
            'cables': cables,
            'cable_count': len(cables),
            'landings': landings,
            'landing_count': len(landings)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# INFRASTRUCTURE STATS — Combined counts for all tables
# ═══════════════════════════════════════════════════════════════

@infra_data_bp.route('/api/v1/infrastructure/stats', methods=['GET'])
def get_infrastructure_stats():
    """Get counts from all infrastructure tables."""
    try:
        conn = _get_db()
        cur = conn.cursor()

        stats = {}
        tables = [
            ('gas_pipelines', 'gas_pipelines'),
            ('power_plants', 'power_plants_eia'),
            ('transmission_lines', 'transmission_lines_eia'),
            ('submarine_cables', 'submarine_cables'),
            ('submarine_cable_landings', 'submarine_cable_landings'),
            ('substations', 'substations'),
            ('fiber_routes', 'fiber_routes'),
            ('discovered_facilities', 'discovered_facilities'),
        ]

        for key, table in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                stats[key] = cur.fetchone()[0]
            except:
                stats[key] = 0
                conn.rollback()

        conn.close()

        return jsonify({
            'success': True,
            'stats': stats,
            'total': sum(stats.values())
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
