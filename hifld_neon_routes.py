"""
hifld_neon_routes.py - HIFLD infrastructure from Neon (no ArcGIS proxy)
"""
from flask import Blueprint, request, jsonify
import logging
import os

logger = logging.getLogger(__name__)
hifld_neon_bp = Blueprint('hifld_neon', __name__)


def _pg_query(sql, params=None):
    """Safe Neon query - returns (rows, columns) or ([], []) on any error"""
    conn = None
    try:
        import psycopg2
        conn = psycopg2.connect(os.environ.get('DATABASE_URL', ''))
        cur = conn.cursor()
        cur.execute(sql, params or ())
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        cur.close()
        return rows, columns
    except Exception as e:
        logger.warning(f"_pg_query error: {e}")
        return [], []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# AUTO-REPAIR: duplicate route '/api/v2/infrastructure/hifld/substations' also in expanded_infrastructure_api.py:401 — review and remove one
@hifld_neon_bp.route('/api/v2/infrastructure/hifld/substations')
def substations():
    try:
        lat = float(request.args.get('lat', 0))
        lng = float(request.args.get('lng', 0))
        radius = float(request.args.get('radius', 50))
        min_kv = int(request.args.get('min_kv', 69))
        limit = min(int(request.args.get('limit', 500)), 2000)

        if lat == 0 and lng == 0:
            return jsonify({'substations': [], 'count': 0}), 200

        deg = radius / 69.0
        rows, cols = _pg_query("""
            SELECT name, city, state, status, max_volt, owner, latitude, longitude
            FROM substations
            WHERE latitude BETWEEN %s AND %s
              AND longitude BETWEEN %s AND %s
              AND COALESCE(max_volt, 0) >= %s
            ORDER BY max_volt DESC NULLS LAST
            LIMIT %s
        """, (lat - deg, lat + deg, lng - deg, lng + deg, min_kv, limit))

        subs = []
        for row in rows:
            d = dict(zip(cols, row))
            subs.append({
                'name': d.get('name', ''),
                'city': d.get('city', ''),
                'state': d.get('state', ''),
                'status': d.get('status', 'Active'),
                'max_voltage_kv': d.get('max_volt') or 0,
                'owner': d.get('owner', ''),
                'lat': float(d.get('latitude') or 0),
                'lng': float(d.get('longitude') or 0),
            })

        logger.info(f"HIFLD Neon: {len(subs)} substations")
        return jsonify({'substations': subs, 'count': len(subs), 'source': 'neon'})

    except Exception as e:
        logger.error(f"substations error: {e}")
        return jsonify({'substations': [], 'count': 0, 'error': str(e)}), 200

# AUTO-REPAIR: duplicate route '/api/v2/infrastructure/hifld/gas-pipelines' also in expanded_infrastructure_api.py:628 — review and remove one

@hifld_neon_bp.route('/api/v2/infrastructure/hifld/gas-pipelines')
def hifld_gas_pipelines():
    try:
        lat = float(request.args.get('lat', 0))
        lng = float(request.args.get('lng', 0))
        radius = float(request.args.get('radius', 50))
        limit = min(int(request.args.get('limit', 500)), 2000)

        if lat == 0 and lng == 0:
            return jsonify({'pipelines': [], 'count': 0}), 200

        deg = radius / 69.0
        rows, cols = _pg_query("""
            SELECT name, operator, type, status, latitude, longitude, state
            FROM discovered_pipelines
            WHERE latitude BETWEEN %s AND %s
              AND longitude BETWEEN %s AND %s
            ORDER BY name
            LIMIT %s
        """, (lat - deg, lat + deg, lng - deg, lng + deg, limit))

        pipes = []
        for row in rows:
            d = dict(zip(cols, row))
            pipes.append({
                'name': d.get('name') or d.get('operator') or 'Unknown',
                'operator': d.get('operator') or d.get('name') or 'Unknown',
                'pipeline_type': d.get('type', 'Interstate'),
                'status': d.get('status', 'Active'),
                'lat': float(d.get('latitude') or 0),
                'lng': float(d.get('longitude') or 0),
                'state': d.get('state', ''),
            })

        logger.info(f"HIFLD Neon: {len(pipes)} gas pipelines")
        return jsonify({'pipelines': pipes, 'count': len(pipes), 'source': 'neon'})

    except Exception as e:
        logger.error(f"hifld_gas_pipelines error: {e}")
        return jsonify({'pipelines': [], 'count': 0, 'error': str(e)}), 200
# AUTO-REPAIR: duplicate route '/api/v2/infrastructure/hifld/transmission' also in expanded_infrastructure_api.py:499 — review and remove one


@hifld_neon_bp.route('/api/v2/infrastructure/hifld/transmission')
def hifld_transmission():
    return jsonify({'features': [], 'source': 'neon'}), 200


def register_hifld_neon_routes(app):
    app.register_blueprint(hifld_neon_bp)
    logger.info("HIFLD Neon routes registered (substations + gas from Neon)")
