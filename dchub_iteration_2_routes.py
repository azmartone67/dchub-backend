"""DC Hub iteration 2 — additive Flask routes.

Three new endpoints registered as a Blueprint-style add_url_rule pass:

  POST /api/v1/transactions/ingest               — real deal scraping (best-effort dispatch)
  GET  /api/v1/facilities/<id>/infrastructure    — substations/transmission/gas/fiber near facility
  GET  /api/v1/land-power/snapshot?bbox=...&layers=...
                                                  — bbox-filtered land-power layers, single envelope

Designed to be safe on a Flask + PostGIS + psycopg2 stack. Uses tolerant
entry-point dispatch and tolerant DB connection lookup so it can land in
api_server.py without knowing the precise local conventions.

Wire by adding to api_server.py:

    try:
        from dchub_iteration_2_routes import register_iteration_2_routes
        register_iteration_2_routes(app)
    except Exception as e:
        import logging; logging.getLogger('dchub.iteration2').warning('register failed: %s', e)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from flask import jsonify, request

logger = logging.getLogger('dchub.iteration2')

# ============================================================
# Tolerant helpers
# ============================================================

def _try_run(*candidates):
    """Try (module, attr) pairs until one resolves and returns its result."""
    for module_name, attr in candidates:
        try:
            mod = __import__(module_name, fromlist=[attr])
            fn = getattr(mod, attr, None)
            if callable(fn):
                logger.info("iteration2: dispatching %s.%s", module_name, attr)
                return fn(), f"{module_name}.{attr}", None
        except Exception as e:
            logger.debug("iteration2: tried %s.%s: %s", module_name, attr, e)
            continue
    return None, None, 'no-entrypoint-found'


def _get_pg_conn():
    """Get a psycopg2 connection. Tries common patterns first, env var as fallback."""
    for module_name, attr in [
        ('db_persistence',       'get_pg_connection'),
        ('db_persistence',       'get_connection'),
        ('db_utils',             'get_pg_connection'),
        ('db_utils',             'get_connection'),
        ('mcp_connection_pool',  'get_connection'),
    ]:
        try:
            mod = __import__(module_name, fromlist=[attr])
            fn = getattr(mod, attr, None)
            if callable(fn):
                return fn()
        except Exception:
            continue
    # Last-resort env var
    import psycopg2
    url = (os.environ.get('DATABASE_URL')
           or os.environ.get('NEON_DATABASE_URL')
           or os.environ.get('DCHUB_DATABASE_URL'))
    if not url:
        raise RuntimeError("No DATABASE_URL / NEON_DATABASE_URL set")
    return psycopg2.connect(url)


def _q(conn, sql, *args, label='query'):
    """Execute SELECT, return list of dicts. Logs and returns [] on error."""
    try:
        c = conn.cursor()
        c.execute(sql, args)
        cols = [d[0] for d in c.description]
        rows = [dict(zip(cols, r)) for r in c.fetchall()]
        c.close()
        return rows
    except Exception as e:
        logger.warning("iteration2: %s failed: %s", label, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return []


# ============================================================
# /api/v1/transactions/ingest
# ============================================================
def transactions_ingest():
    started = datetime.utcnow().isoformat()
    result, entry, err = _try_run(
        ('deal_scraper',             'scrape_all'),
        ('deal_scraper',             'run'),
        ('deal_scraper',             'main'),
        ('deal_ingestion_scheduler', 'run_deal_ingestion'),
        ('deal_ingestion_scheduler', 'ingest_deals'),
        ('deal_ingestion_scheduler', 'run'),
        ('seed_comprehensive_deals', 'run'),
        ('news_facility_extractor',  'extract_deals'),
        ('news_facility_extractor',  'run'),
    )
    inserted = 0
    if isinstance(result, dict):
        inserted = (result.get('inserted', 0)
                    or result.get('count', 0)
                    or result.get('new_deals', 0)
                    or result.get('added', 0))
    elif isinstance(result, int):
        inserted = result
    elif isinstance(result, list):
        inserted = len(result)
    return jsonify({
        'ok': err is None,
        'started': started,
        'finished': datetime.utcnow().isoformat(),
        'entrypoint': entry,
        'inserted': inserted,
        'note': err,
    }), 200


# ============================================================
# /api/v1/facilities/<facility_id>/infrastructure
# ============================================================
def facility_infrastructure(facility_id):
    """Return nearby substations, transmission, gas, fiber for one facility."""
    try:
        radius_km = float(request.args.get('radius_km', 50))
    except ValueError:
        radius_km = 50.0
    radius_km = max(1.0, min(radius_km, 200.0))

    conn = _get_pg_conn()
    try:
        c = conn.cursor()
        # Resolve facility coords by id (numeric or string), fallback to slug column if present
        c.execute(
            "SELECT lat, lon FROM facilities WHERE id::text = %s LIMIT 1",
            (str(facility_id),)
        )
        row = c.fetchone()
        if not row:
            try:
                c.execute("SELECT lat, lon FROM facilities WHERE slug = %s LIMIT 1",
                          (str(facility_id),))
                row = c.fetchone()
            except Exception:
                conn.rollback()
        if not row or row[0] is None or row[1] is None:
            return jsonify({
                'error': 'facility-not-found-or-missing-coordinates',
                'facility_id': facility_id,
            }), 404
        lat, lon = float(row[0]), float(row[1])
        c.close()

        radius_m = radius_km * 1000.0
        fiber_radius_m = min(radius_km, 25.0) * 1000.0

        substations = _q(conn, """
            SELECT id, name, voltage_kv, operator,
                   ST_DistanceSphere(geom::geometry,
                                     ST_SetSRID(ST_MakePoint(%s, %s), 4326))/1000.0 AS distance_km
            FROM substations
            WHERE ST_DWithin(geom::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            ORDER BY distance_km LIMIT 25
        """, lon, lat, lon, lat, radius_m, label='substations')

        transmission = _q(conn, """
            SELECT id, name, voltage_kv, owner,
                   ST_DistanceSphere(geom::geometry,
                                     ST_SetSRID(ST_MakePoint(%s, %s), 4326))/1000.0 AS distance_km
            FROM transmission_lines
            WHERE ST_DWithin(geom::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            ORDER BY distance_km LIMIT 25
        """, lon, lat, lon, lat, radius_m, label='transmission')

        gas = _q(conn, """
            SELECT id, name, operator, diameter_in,
                   ST_DistanceSphere(geom::geometry,
                                     ST_SetSRID(ST_MakePoint(%s, %s), 4326))/1000.0 AS distance_km
            FROM gas_pipelines
            WHERE ST_DWithin(geom::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            ORDER BY distance_km LIMIT 25
        """, lon, lat, lon, lat, radius_m, label='gas')

        fiber_rows = _q(conn, """
            SELECT id, provider AS carrier, route_type,
                   ST_DistanceSphere(geom::geometry,
                                     ST_SetSRID(ST_MakePoint(%s, %s), 4326))/1000.0 AS distance_km
            FROM fiber_routes
            WHERE ST_DWithin(geom::geography,
                             ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            ORDER BY distance_km LIMIT 50
        """, lon, lat, lon, lat, fiber_radius_m, label='fiber')

        carriers = {}
        for r in fiber_rows:
            cname = r.get('carrier') or 'Unknown'
            if cname not in carriers or r['distance_km'] < carriers[cname]['nearest_km']:
                carriers[cname] = {'name': cname, 'nearest_km': r['distance_km'], 'route_count': 0}
            carriers[cname]['route_count'] += 1
        carriers_list = sorted(carriers.values(), key=lambda x: x['nearest_km'])

        def _summary(rows, key='distance_km'):
            return {
                'count': len(rows),
                'nearest_km': float(rows[0][key]) if rows else None,
            }

        return jsonify({
            'facility_id': facility_id,
            'lat': lat, 'lon': lon,
            'radius_km': radius_km,
            'substations': substations,
            'transmission_lines': transmission,
            'gas_pipelines': gas,
            'fiber': {'carriers': carriers_list, 'routes': fiber_rows},
            'summary': {
                'substations':        _summary(substations),
                'transmission_lines': _summary(transmission),
                'gas_pipelines':      _summary(gas),
                'fiber': {
                    'carrier_count': len(carriers_list),
                    'nearest_km':    carriers_list[0]['nearest_km'] if carriers_list else None,
                },
            },
        }), 200
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# /api/v1/land-power/snapshot?bbox=west,south,east,north&layers=...
# ============================================================
ALL_LAYERS = {'facilities', 'substations', 'transmission', 'gas', 'fiber', 'pipeline'}

def land_power_snapshot():
    bbox = request.args.get('bbox', '')
    try:
        west, south, east, north = (float(x) for x in bbox.split(','))
    except Exception:
        return jsonify({'error': "bbox must be 'west,south,east,north'"}), 400
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        return jsonify({'error': 'bbox out of range'}), 400

    layers_param = request.args.get('layers', '')
    requested = set(layers_param.split(',')) if layers_param else set(ALL_LAYERS)
    requested = requested & ALL_LAYERS
    if not requested:
        return jsonify({'error': f'layers must include at least one of {sorted(ALL_LAYERS)}'}), 400

    conn = _get_pg_conn()
    try:
        result = {}
        if 'facilities' in requested:
            result['facilities'] = _q(conn, """
                SELECT id, name, operator, capacity_mw, status, lat, lon
                FROM facilities
                WHERE lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s
                LIMIT 500
            """, south, north, west, east, label='facilities')
        if 'substations' in requested:
            result['substations'] = _q(conn, """
                SELECT id, name, voltage_kv, operator,
                       ST_Y(geom::geometry) AS lat, ST_X(geom::geometry) AS lon
                FROM substations
                WHERE geom && ST_MakeEnvelope(%s,%s,%s,%s,4326) AND voltage_kv >= 69
                LIMIT 1000
            """, west, south, east, north, label='substations')
        if 'transmission' in requested:
            result['transmission'] = _q(conn, """
                SELECT id, name, voltage_kv, owner,
                       ST_AsGeoJSON(geom)::json AS geometry
                FROM transmission_lines
                WHERE geom && ST_MakeEnvelope(%s,%s,%s,%s,4326)
                LIMIT 2000
            """, west, south, east, north, label='transmission')
        if 'gas' in requested:
            result['gas'] = _q(conn, """
                SELECT id, name, operator, diameter_in,
                       ST_AsGeoJSON(geom)::json AS geometry
                FROM gas_pipelines
                WHERE geom && ST_MakeEnvelope(%s,%s,%s,%s,4326)
                LIMIT 1000
            """, west, south, east, north, label='gas')
        if 'fiber' in requested:
            result['fiber'] = _q(conn, """
                SELECT id, provider AS carrier, route_type,
                       ST_AsGeoJSON(geom)::json AS geometry
                FROM fiber_routes
                WHERE geom && ST_MakeEnvelope(%s,%s,%s,%s,4326)
                LIMIT 2000
            """, west, south, east, north, label='fiber')
        if 'pipeline' in requested:
            result['pipeline'] = _q(conn, """
                SELECT id, project_name, company, capacity_mw, delivery_quarter, status, lat, lon
                FROM dc_pipeline
                WHERE lat BETWEEN %s AND %s AND lon BETWEEN %s AND %s
                  AND status IN ('announced','construction')
                LIMIT 200
            """, south, north, west, east, label='pipeline')
        return jsonify({
            'bbox':   {'west': west, 'south': south, 'east': east, 'north': north},
            'layers': result,
            'counts': {k: len(v) for k, v in result.items()},
        }), 200
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============================================================
# Registration
# ============================================================
def register_iteration_2_routes(app):
    """Register the three iteration-2 routes onto the given Flask app."""
    app.add_url_rule(
        '/api/v1/transactions/ingest',
        endpoint='it2_transactions_ingest',
        view_func=transactions_ingest,
        methods=['POST', 'GET'],
    )
    app.add_url_rule(
        '/api/v1/facilities/<facility_id>/infrastructure',
        endpoint='it2_facility_infrastructure',
        view_func=facility_infrastructure,
        methods=['GET'],
    )
    app.add_url_rule(
        '/api/v1/land-power/snapshot',
        endpoint='it2_land_power_snapshot',
        view_func=land_power_snapshot,
        methods=['GET'],
    )
    logger.info("iteration2: registered transactions/ingest, facilities/<id>/infrastructure, land-power/snapshot")
