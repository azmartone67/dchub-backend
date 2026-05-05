"""DC Hub iteration 2 v2 — schema-correct against Neon.

Schema discovered from production:
  facilities        — provider, latitude, longitude, power_mw, status, slug, id (text)
  substations       — operator, lat, lng (lng not lon!), voltage_kv, name
  fiber_routes      — provider AS carrier, route_type, start_lat/lng, end_lat/lng
  gas_pipelines     — operator, pipeline_type, diameter_inches, lat, lng (empty: 0 rows)
  transmission_lines — RELATION DOES NOT EXIST: tolerant table-name discovery below
  capacity_pipeline / discovered_pipelines / ps_pipeline — pipeline candidates

No PostGIS geom columns — using plain lat/lng with haversine for "nearby"
queries and bbox for snapshot. Bbox prefilter keeps haversine fast even
without a spatial index.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from flask import jsonify, request

logger = logging.getLogger('dchub.iteration2')


# ============================================================
# Connection + helpers
# ============================================================

def _get_pg_conn():
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
    import psycopg2
    url = (os.environ.get('DATABASE_URL')
           or os.environ.get('NEON_DATABASE_URL')
           or os.environ.get('DCHUB_DATABASE_URL'))
    if not url:
        raise RuntimeError("No DATABASE_URL set")
    return psycopg2.connect(url)


def _q(conn, sql, *args, label='query'):
    try:
        c = conn.cursor()
        c.execute(sql, args)
        cols = [d[0] for d in c.description]
        rows = [dict(zip(cols, r)) for r in c.fetchall()]
        c.close()
        return rows
    except Exception as e:
        logger.warning("iteration2: %s failed: %s", label, e)
        try: conn.rollback()
        except Exception: pass
        return []


def _table_exists(conn, table_name):
    try:
        c = conn.cursor()
        c.execute("SELECT to_regclass('public.' || %s) IS NOT NULL", (table_name,))
        exists = c.fetchone()[0]
        c.close()
        return bool(exists)
    except Exception:
        try: conn.rollback()
        except Exception: pass
        return False


def _find_first_existing(conn, candidates):
    for t in candidates:
        if _table_exists(conn, t):
            return t
    return None


# Bounding-box helper for "within radius_km of (lat, lon)".
# 1 degree latitude ≈ 111 km; longitude shrinks by cos(lat).
import math
def _bbox_for_radius(lat, lon, radius_km):
    dlat = radius_km / 111.0
    dlon = radius_km / (111.0 * max(math.cos(math.radians(lat)), 0.01))
    return (lat - dlat, lat + dlat, lon - dlon, lon + dlon)


# ============================================================
# /api/v1/transactions/ingest
# ============================================================
def transactions_ingest():
    started = datetime.utcnow().isoformat()
    result, entry, err = None, None, None

    # Primary entry: deal_scraper.run_scrape(dry_run=False) -> Dict
    try:
        from deal_scraper import run_scrape as _scrape
        result = _scrape(dry_run=False)
        entry = 'deal_scraper.run_scrape'
    except Exception as e:
        logger.info("iteration2: run_scrape failed: %s", e)
        # Fallback: deal_ingestion_scheduler.run_ingestion(get_db)
        try:
            from deal_ingestion_scheduler import run_ingestion as _ingest
            result = _ingest(_get_pg_conn)
            entry = 'deal_ingestion_scheduler.run_ingestion'
        except Exception as e2:
            err = f'no-entrypoint: run_scrape={e}; run_ingestion={e2}'

    inserted = 0
    if isinstance(result, dict):
        inserted = (result.get('deals_inserted', 0)
                    or result.get('inserted', 0)
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
        'result_summary': str(result)[:300] if result else None,
        'note': err,
    }), 200


# ============================================================
# /api/v1/facilities/<facility_id>/infrastructure
# ============================================================
def facility_infrastructure(facility_id):
    try:
        radius_km = float(request.args.get('radius_km', 50))
    except ValueError:
        radius_km = 50.0
    radius_km = max(1.0, min(radius_km, 200.0))

    conn = _get_pg_conn()
    try:
        # Resolve facility coords by id (text) or slug
        c = conn.cursor()
        c.execute("""
            SELECT latitude, longitude, name, provider, power_mw
            FROM facilities WHERE id = %s OR slug = %s LIMIT 1
        """, (str(facility_id), str(facility_id)))
        row = c.fetchone()
        c.close()
        if not row or row[0] is None or row[1] is None:
            return jsonify({
                'error': 'facility-not-found-or-missing-coordinates',
                'facility_id': facility_id,
            }), 404
        lat, lon = float(row[0]), float(row[1])
        facility_meta = {
            'name':     row[2],
            'provider': row[3],
            'power_mw': float(row[4]) if row[4] is not None else None,
        }

        s_lat, n_lat, w_lon, e_lon = _bbox_for_radius(lat, lon, radius_km)
        s_lat25, n_lat25, w_lon25, e_lon25 = _bbox_for_radius(lat, lon, min(radius_km, 25))

        # Haversine distance template (km), works on lat/lng columns
        # Note: substations + gas use lng (not lon)
        substations = _q(conn, """
            SELECT id, name, voltage_kv, operator, lat, lng AS lon,
                   2 * 6371 * asin(sqrt(
                       power(sin(radians((lat - %s)/2)), 2) +
                       cos(radians(%s)) * cos(radians(lat)) *
                       power(sin(radians((lng - %s)/2)), 2)
                   )) AS distance_km
            FROM substations
            WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
              AND voltage_kv >= 69
            ORDER BY distance_km LIMIT 25
        """, lat, lat, lon, s_lat, n_lat, w_lon, e_lon, label='substations')

        gas = _q(conn, """
            SELECT id, name, operator, pipeline_type, diameter_inches,
                   lat, lng AS lon,
                   2 * 6371 * asin(sqrt(
                       power(sin(radians((lat - %s)/2)), 2) +
                       cos(radians(%s)) * cos(radians(lat)) *
                       power(sin(radians((lng - %s)/2)), 2)
                   )) AS distance_km
            FROM gas_pipelines
            WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
            ORDER BY distance_km LIMIT 25
        """, lat, lat, lon, s_lat, n_lat, w_lon, e_lon, label='gas')

        # Fiber routes are line segments; check if EITHER endpoint is in bbox.
        # Use start point for distance ordering (good enough for "nearby" UX).
        fiber_rows = _q(conn, """
            SELECT id, provider AS carrier, route_type, distance_miles,
                   start_lat, start_lng, end_lat, end_lng,
                   2 * 6371 * asin(sqrt(
                       power(sin(radians((start_lat - %s)/2)), 2) +
                       cos(radians(%s)) * cos(radians(start_lat)) *
                       power(sin(radians((start_lng - %s)/2)), 2)
                   )) AS distance_km
            FROM fiber_routes
            WHERE (start_lat BETWEEN %s AND %s AND start_lng BETWEEN %s AND %s)
               OR (end_lat   BETWEEN %s AND %s AND end_lng   BETWEEN %s AND %s)
            ORDER BY distance_km LIMIT 50
        """, lat, lat, lon,
             s_lat25, n_lat25, w_lon25, e_lon25,
             s_lat25, n_lat25, w_lon25, e_lon25, label='fiber')

        # Transmission: discover the actual table name
        tx_table = _find_first_existing(conn, [
            'transmission_lines', 'hifld_transmission_lines',
            'transmission', 'hifld_transmission', 'tx_lines',
        ])
        if tx_table:
            # Conservative SELECT — common columns only; missing ones become None
            transmission = _q(conn, f"""
                SELECT id, name, voltage_kv, lat, lng AS lon,
                       2 * 6371 * asin(sqrt(
                           power(sin(radians((lat - %s)/2)), 2) +
                           cos(radians(%s)) * cos(radians(lat)) *
                           power(sin(radians((lng - %s)/2)), 2)
                       )) AS distance_km
                FROM {tx_table}
                WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
                ORDER BY distance_km LIMIT 25
            """, lat, lat, lon, s_lat, n_lat, w_lon, e_lon, label=f'transmission({tx_table})')
        else:
            transmission = []

        carriers = {}
        for r in fiber_rows:
            cname = r.get('carrier') or 'Unknown'
            d = r.get('distance_km')
            if d is None:
                continue
            if cname not in carriers or d < carriers[cname]['nearest_km']:
                carriers[cname] = {'name': cname, 'nearest_km': d, 'route_count': 0}
            carriers[cname]['route_count'] += 1
        carriers_list = sorted(carriers.values(), key=lambda x: x['nearest_km'])

        def _summary(rows):
            return {
                'count': len(rows),
                'nearest_km': float(rows[0]['distance_km']) if rows and rows[0].get('distance_km') is not None else None,
            }

        return jsonify({
            'facility_id': facility_id,
            'facility': facility_meta,
            'lat': lat, 'lon': lon,
            'radius_km': radius_km,
            'substations': substations,
            'transmission_lines': transmission,
            'transmission_table_used': tx_table,
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
        try: conn.close()
        except Exception: pass


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
        meta = {}

        if 'facilities' in requested:
            result['facilities'] = _q(conn, """
                SELECT id, name, provider, power_mw AS capacity_mw, status,
                       latitude AS lat, longitude AS lon, slug
                FROM facilities
                WHERE latitude BETWEEN %s AND %s AND longitude BETWEEN %s AND %s
                LIMIT 500
            """, south, north, west, east, label='facilities')

        if 'substations' in requested:
            result['substations'] = _q(conn, """
                SELECT id, name, voltage_kv, operator, lat, lng AS lon
                FROM substations
                WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
                  AND voltage_kv >= 69
                LIMIT 1000
            """, south, north, west, east, label='substations')

        if 'transmission' in requested:
            tx_table = _find_first_existing(conn, [
                'transmission_lines', 'hifld_transmission_lines',
                'transmission', 'hifld_transmission', 'tx_lines',
            ])
            meta['transmission_table'] = tx_table
            if tx_table:
                result['transmission'] = _q(conn, f"""
                    SELECT id, name, voltage_kv, lat, lng AS lon
                    FROM {tx_table}
                    WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
                    LIMIT 2000
                """, south, north, west, east, label=f'transmission({tx_table})')
            else:
                result['transmission'] = []

        if 'gas' in requested:
            result['gas'] = _q(conn, """
                SELECT id, name, operator, pipeline_type, diameter_inches,
                       lat, lng AS lon
                FROM gas_pipelines
                WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
                LIMIT 1000
            """, south, north, west, east, label='gas')

        if 'fiber' in requested:
            # Include route if either endpoint is in bbox.
            result['fiber'] = _q(conn, """
                SELECT id, provider AS carrier, route_type, distance_miles,
                       start_lat, start_lng, end_lat, end_lng
                FROM fiber_routes
                WHERE (start_lat BETWEEN %s AND %s AND start_lng BETWEEN %s AND %s)
                   OR (end_lat   BETWEEN %s AND %s AND end_lng   BETWEEN %s AND %s)
                LIMIT 2000
            """, south, north, west, east, south, north, west, east, label='fiber')

        if 'pipeline' in requested:
            # Future-build inventory — iterate candidates, pick first that has BOTH
            # a usable lat-column and lon-column; remember which candidates lacked coords.
            candidates = ['capacity_pipeline', 'discovered_pipelines',
                          'ps_pipeline', 'dc_properties']
            pl_table = None
            cols = set()
            wanted_lat = None
            wanted_lon = None
            attempted = []
            for cand in candidates:
                if not _table_exists(conn, cand):
                    continue
                cur = conn.cursor()
                cur.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = %s
                """, (cand,))
                cand_cols = {r[0] for r in cur.fetchall()}
                cur.close()
                cand_lat = 'lat' if 'lat' in cand_cols else ('latitude' if 'latitude' in cand_cols else None)
                cand_lon = ('lng' if 'lng' in cand_cols
                            else ('lon' if 'lon' in cand_cols
                                  else ('longitude' if 'longitude' in cand_cols else None)))
                attempted.append({'table': cand, 'has_coords': bool(cand_lat and cand_lon)})
                if cand_lat and cand_lon:
                    pl_table, cols = cand, cand_cols
                    wanted_lat, wanted_lon = cand_lat, cand_lon
                    break
            meta['pipeline_table'] = pl_table
            meta['pipeline_attempted'] = attempted
            if pl_table:
                if wanted_lat and wanted_lon:
                    pick = [c for c in [
                        'id','name','project_name','company','operator','provider',
                        'power_mw','capacity_mw','status','delivery_quarter',
                        'expected_completion','market'
                    ] if c in cols]
                    select_cols = ', '.join(pick + [f"{wanted_lat} AS lat", f"{wanted_lon} AS lon"])
                    sql = f"""
                        SELECT {select_cols}
                        FROM {pl_table}
                        WHERE {wanted_lat} BETWEEN %s AND %s AND {wanted_lon} BETWEEN %s AND %s
                        LIMIT 200
                    """
                    result['pipeline'] = _q(conn, sql, south, north, west, east, label=f'pipeline({pl_table})')
                else:
                    result['pipeline'] = []
                    meta['pipeline_warning'] = f"{pl_table} has no lat/lon columns"
            else:
                result['pipeline'] = []

        return jsonify({
            'bbox':   {'west': west, 'south': south, 'east': east, 'north': north},
            'layers': result,
            'counts': {k: len(v) for k, v in result.items()},
            'meta':   meta,
        }), 200
    finally:
        try: conn.close()
        except Exception: pass


# ============================================================
# Registration
# ============================================================
def register_iteration_2_routes(app):
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
    logger.info("iteration2 v2: registered transactions/ingest, facilities/<id>/infrastructure, land-power/snapshot")
