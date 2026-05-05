"""
DC Hub Energy Auto-Discovery v3.0 (PostgreSQL/Neon)
=====================================================
Discovers and syncs energy infrastructure data from public APIs:
  - Power plants (HIFLD + EIA)
  - Transmission lines (HIFLD)
  - Gas pipelines (HIFLD DOT NPMS)
  - Substations (HIFLD)

PostgreSQL/Neon compatible. No SQLite dependencies.
Called by dchub-scheduler.py via /api/jobs/infrastructure-sync.

ArcGIS REST endpoints (HIFLD):
  Power Plants:      https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Power_Plants/FeatureServer/0
  Transmission:      https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0
  Substations:       https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0
  Gas Pipelines:     https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0
  Gas Processing:    https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Processing_Plants/FeatureServer/0
"""

import os
import json
import logging
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from internal_auth import is_valid_internal_key, get_internal_key_for_client

# ============================================================================
# Phase 25/27/28 — replace static seed values with live DB counts.
# Called from energy_discovery_status() via stats.update(_phase25_real_counts())
# inserted after the seed assignments. All exceptions swallowed → never
# breaks the response.
# ============================================================================
def _phase25_real_counts():
    out = {
        'markets_monitored': 23,
        'hifld_sources': 5,
        'running': True,
        'recent_syncs': [],
        'seed_data': True,
        'total_capacity_mw': 0,
        'total_gas_compressors': 0,
        'total_gas_processings': 0,
        'total_pipelines': 0,
        'total_power_plants': 0,
        'total_substations': 0,
        'total_transmissions': 0,
        'total_wind_projects': 0,
    }
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if not conn:
            return out
        cur = conn.cursor()
        for label, table in [
            ('total_substations',     'substations'),
            ('total_pipelines',       'pipelines'),
            ('total_power_plants',    'power_plants'),
            ('total_transmissions',   'transmission'),
            ('total_wind_projects',   'wind_projects'),
            ('total_gas_compressors', 'gas_compressors'),
            ('total_gas_processings', 'gas_processings'),
        ]:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                r = cur.fetchone() or (0,)
                out[label] = int(r[0] or 0)
            except Exception:
                try: conn.rollback()
                except Exception: pass
        try:
            cur.execute("SELECT COALESCE(SUM(capacity_mw), 0) FROM power_plants")
            r = cur.fetchone() or (0,)
            out['total_capacity_mw'] = int(r[0] or 0)
        except Exception:
            try: conn.rollback()
            except Exception: pass
        try:
            cur.execute(
                "SELECT 'substations' AS source, MAX(updated_at) AS at FROM substations "
                "UNION ALL SELECT 'pipelines',    MAX(updated_at) FROM pipelines "
                "UNION ALL SELECT 'power_plants', MAX(updated_at) FROM power_plants"
            )
            out['recent_syncs'] = [
                {'source': r[0], 'at': str(r[1]) if r[1] else None}
                for r in cur.fetchall()
            ]
        except Exception:
            try: conn.rollback()
            except Exception: pass
        out['seed_data'] = (out['total_substations'] < 1000)
        try: conn.close()
        except Exception: pass
    except Exception as _e:
        out['_error'] = type(_e).__name__ + ': ' + str(_e)[:200]
    return out


# phase12m: ArcGIS REST rejects URL-encoded = and , in where/outFields.
def _phase12m_build_query(params_dict):
    from urllib.parse import quote
    parts = []
    for k, v in params_dict.items():
        parts.append(f"{quote(str(k), safe='')}={quote(str(v), safe='=,+*<>:')}")
    return "&".join(parts)

logger = logging.getLogger('energy_discovery')

# =============================================================================
# HIFLD ArcGIS REST ENDPOINTS (verified March 2026)
# =============================================================================

HIFLD_SOURCES = {
    'power_plants': {
        'name': 'HIFLD Power Plants',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Power_Plants/FeatureServer/0',
        'table': 'discovered_power_plants',
        'fields': 'OBJECTID,NAME,PRIMSOURCE,TOTAL_MW,LATITUDE,LONGITUDE,STATE,COUNTY,STATUS,NAICS_DESC',
    },
    'substations': {
        'name': 'HIFLD Substations',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0',
        'table': 'infrastructure_layers',
        'fields': 'OBJECTID,NAME,STATE,STATUS,OWNER,MAX_VOLT,MIN_VOLT,LATITUDE,LONGITUDE',
        'category': 'substation',
    },
    'transmission_lines': {
        'name': 'HIFLD Transmission Lines',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0',
        'table': 'infrastructure_layers',
        'fields': 'OBJECTID,OWNER,VOLTAGE,STATUS,SHAPE_Length',
        'category': 'transmission',
    },
    'gas_compressor_stations': {
        'name': 'HIFLD Gas Compressor Stations',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0',
        'table': 'infrastructure_layers',
        'fields': 'OBJECTID,NAME,OPERATOR,STATE,LATITUDE,LONGITUDE,STATUS',
        'category': 'gas_compressor',
    },
    'gas_processing': {
        'name': 'HIFLD Gas Processing Plants',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Processing_Plants/FeatureServer/0',
        'table': 'infrastructure_layers',
        'fields': 'OBJECTID,NAME,OPERATOR,STATE,LATITUDE,LONGITUDE,STATUS',
        'category': 'gas_processing',
    },
}

# Monitored metro markets (lat, lng, radius_km)
MONITORED_MARKETS = {
    'phoenix_az': (33.4484, -112.0740, 80),
    'dallas_tx': (32.7767, -96.7970, 100),
    'northern_virginia': (38.9072, -77.4369, 80),
    'atlanta_ga': (33.7490, -84.3880, 80),
    'las_vegas_nv': (36.1699, -115.1398, 80),
    'salt_lake_city_ut': (40.7608, -111.8910, 80),
    'columbus_oh': (39.9612, -82.9988, 80),
    'des_moines_ia': (41.5868, -93.6250, 80),
    'chicago_il': (41.8781, -87.6298, 100),
    'portland_or': (45.5152, -122.6784, 80),
    'sacramento_ca': (38.5816, -121.4944, 80),
    'san_antonio_tx': (29.4241, -98.4936, 80),
    'new_albany_oh': (40.0812, -82.8088, 60),
    'quincy_wa': (47.2343, -119.8526, 50),
    'council_bluffs_ia': (41.2619, -95.8608, 50),
    'hillsboro_or': (45.5229, -122.9898, 50),
    'ashburn_va': (39.0438, -77.4874, 40),
    'chandler_az': (33.3062, -111.8413, 40),
    'mesa_az': (33.4152, -111.8315, 40),
    'richmond_va': (37.5407, -77.4360, 60),
    'reno_nv': (39.5296, -119.8138, 50),
    'cheyenne_wy': (41.1400, -104.8202, 50),
    'papillion_ne': (41.1544, -96.0419, 50),
}


# =============================================================================
# ARCGIS QUERY HELPER
# =============================================================================

def query_arcgis(url, fields='*', where='1=1', result_count=5000,
                 geometry_filter=None, timeout=30):
    """
    Query an ArcGIS FeatureServer REST endpoint.
    Returns list of feature attributes (dicts).
    """
    params = {
        'where': where,
        'outFields': fields,
        'f': 'json',
        'resultRecordCount': str(result_count),
        'returnGeometry': 'false',
    }

    if geometry_filter:
        lat, lng, radius_km = geometry_filter
        # ArcGIS envelope: approximate degrees from km
        delta = radius_km / 111.0
        params['geometry'] = json.dumps({
            'xmin': lng - delta,
            'ymin': lat - delta,
            'xmax': lng + delta,
            'ymax': lat + delta,
            'spatialReference': {'wkid': 4326}
        })
        params['geometryType'] = 'esriGeometryEnvelope'
        params['spatialRel'] = 'esriSpatialRelIntersects'
        params['inSR'] = '4326'

    from urllib.parse import urlencode
    query_string = _phase12m_build_query({k: v for k, v in params.items()})
    full_url = f"{url}/query?{query_string}"

    try:
        req = Request(full_url, headers={'User-Agent': 'DCHub/3.0'})
        resp = urlopen(req, timeout=timeout)
        data = json.loads(resp.read().decode('utf-8'))

        if 'error' in data:
            logger.error(f"ArcGIS error from {url}: {data['error']}")
            return None, data['error']

        features = data.get('features', [])
        return [f.get('attributes', {}) for f in features], None

    except (URLError, HTTPError) as e:
        logger.error(f"ArcGIS request failed for {url}: {e}")
        return None, str(e)
    except Exception as e:
        logger.error(f"ArcGIS parse error for {url}: {e}")
        return None, str(e)


# =============================================================================
# HEALTH CHECK — VERIFY HIFLD ENDPOINTS ARE ALIVE
# =============================================================================

def check_hifld_health():
    """
    Quick health check on all HIFLD endpoints.
    Returns dict of source_key → {alive: bool, count: int, error: str|None}
    """
    results = {}
    for key, source in HIFLD_SOURCES.items():
        try:
            url = f"{source['url']}/query?where=1=1&returnCountOnly=true&f=json"
            req = Request(url, headers={'User-Agent': 'DCHub/3.0'})
            resp = urlopen(req, timeout=15)
            data = json.loads(resp.read().decode('utf-8'))
            count = data.get('count', 0)
            results[key] = {'alive': True, 'count': count, 'error': None}
        except Exception as e:
            results[key] = {'alive': False, 'count': 0, 'error': str(e)[:100]}
    return results


# =============================================================================
# DB INIT — CREATE TABLES IF MISSING (PostgreSQL)
# =============================================================================

def init_energy_tables(conn):
    """Create energy discovery tables if they don't exist (Neon/PostgreSQL)."""
    cur = conn.cursor()

    # discovered_power_plants
    cur.execute("""
        CREATE TABLE IF NOT EXISTS discovered_power_plants (
            id SERIAL PRIMARY KEY,
            object_id INTEGER,
            name TEXT,
            primary_source TEXT,
            total_mw NUMERIC,
            latitude NUMERIC,
            longitude NUMERIC,
            state TEXT,
            county TEXT,
            status TEXT,
            naics_desc TEXT,
            source TEXT DEFAULT 'HIFLD',
            discovered_at TIMESTAMPTZ DEFAULT NOW(),
            is_new INTEGER DEFAULT 1,
            UNIQUE(object_id, source)
        )
    """)

    # energy_sync_log
    cur.execute("""
        CREATE TABLE IF NOT EXISTS energy_sync_log (
            id SERIAL PRIMARY KEY,
            source TEXT,
            market TEXT,
            records_found INTEGER DEFAULT 0,
            records_new INTEGER DEFAULT 0,
            duration_seconds NUMERIC,
            error TEXT,
            synced_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # infrastructure_layers (may already exist from KMZ processor)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS infrastructure_layers (
            id SERIAL PRIMARY KEY,
            name TEXT,
            category TEXT,
            provider TEXT,
            source TEXT,
            state TEXT,
            latitude NUMERIC,
            longitude NUMERIC,
            properties JSONB,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMPTZ DEFAULT NOW()
            
        )
    """)

    conn.commit()
    cur.close()
    logger.info("✅ Energy discovery tables initialized")


# =============================================================================
# SYNC: POWER PLANTS
# =============================================================================

def sync_power_plants(conn, market_name=None, geometry_filter=None):
    """Sync power plants from HIFLD. Returns count of new records."""
    source = HIFLD_SOURCES['power_plants']
    start = time.time()

    features, error = query_arcgis(
        source['url'],
        fields=source['fields'],
        geometry_filter=geometry_filter,
        result_count=5000
    )

    if error or features is None:
        _log_sync(conn, 'power_plants', market_name, 0, 0,
                  time.time() - start, str(error))
        return 0

    cur = conn.cursor()
    new_count = 0

    for f in features:
        try:
            cur.execute("""
                INSERT INTO discovered_power_plants
                    (object_id, name, primary_source, total_mw, latitude,
                     longitude, state, county, status, naics_desc, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'HIFLD')
                ON CONFLICT (object_id, source) DO NOTHING
            """, (
                f.get('OBJECTID'),
                f.get('NAME', ''),
                f.get('PRIMSOURCE', ''),
                f.get('TOTAL_MW'),
                f.get('LATITUDE'),
                f.get('LONGITUDE'),
                f.get('STATE', ''),
                f.get('COUNTY', ''),
                f.get('STATUS', ''),
                f.get('NAICS_DESC', ''),
            ))
            if cur.rowcount > 0:
                new_count += 1
        except Exception as e:
            logger.warning(f"Power plant insert error: {e}")
            continue

    conn.commit()
    cur.close()

    _log_sync(conn, 'power_plants', market_name, len(features),
              new_count, time.time() - start)
    return new_count


# =============================================================================
# SYNC: SUBSTATIONS
# =============================================================================

def sync_substations(conn, market_name=None, geometry_filter=None):
    """Sync substations from HIFLD into infrastructure_layers."""
    source = HIFLD_SOURCES['substations']
    start = time.time()

    # Only high-voltage substations (>69kV)
    where = "MAX_VOLT > 69000" if not geometry_filter else "MAX_VOLT > 69000"

    features, error = query_arcgis(
        source['url'],
        fields=source['fields'],
        where=where,
        geometry_filter=geometry_filter,
        result_count=5000
    )

    if error or features is None:
        _log_sync(conn, 'substations', market_name, 0, 0,
                  time.time() - start, str(error))
        return 0

    cur = conn.cursor()
    new_count = 0

    for f in features:
        try:
            name = f.get('NAME', '') or f.get('OWNER', 'Unknown')
            cur.execute("""
                INSERT INTO infrastructure_layers
                    (name, category, provider, source, state, latitude, longitude,
                     properties, status)
                VALUES (%s, 'substation', %s, 'HIFLD', %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (
                name,
                f.get('OWNER', ''),
                f.get('STATE', ''),
                f.get('LATITUDE'),
                f.get('LONGITUDE'),
                json.dumps({
                    'max_volt': f.get('MAX_VOLT'),
                    'min_volt': f.get('MIN_VOLT'),
                    'object_id': f.get('OBJECTID'),
                }),
                f.get('STATUS', 'active'),
            ))
            if cur.rowcount > 0:
                new_count += 1
        except Exception as e:
            logger.warning(f"Substation insert error: {e}")
            continue

    conn.commit()
    cur.close()

    _log_sync(conn, 'substations', market_name, len(features),
              new_count, time.time() - start)
    return new_count


# =============================================================================
# SYNC: GAS INFRASTRUCTURE
# =============================================================================

def sync_gas_infrastructure(conn, market_name=None, geometry_filter=None):
    """Sync gas compressor stations + processing plants from HIFLD."""
    total_new = 0

    for source_key in ['gas_compressor_stations', 'gas_processing']:
        source = HIFLD_SOURCES[source_key]
        start = time.time()

        features, error = query_arcgis(
            source['url'],
            fields=source['fields'],
            geometry_filter=geometry_filter,
            result_count=5000
        )

        if error or features is None:
            _log_sync(conn, source_key, market_name, 0, 0,
                      time.time() - start, str(error))
            continue

        cur = conn.cursor()
        new_count = 0

        for f in features:
            try:
                name = f.get('NAME', '') or f.get('OPERATOR', 'Unknown')
                cur.execute("""
                    INSERT INTO infrastructure_layers
                        (name, category, provider, source, state, latitude, longitude,
                         properties, status)
                    VALUES (%s, %s, %s, 'HIFLD', %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (
                    name,
                    source['category'],
                    f.get('OPERATOR', ''),
                    f.get('STATE', ''),
                    f.get('LATITUDE'),
                    f.get('LONGITUDE'),
                    json.dumps({
                        'object_id': f.get('OBJECTID'),
                        'status': f.get('STATUS'),
                    }),
                    f.get('STATUS', 'active'),
                ))
                if cur.rowcount > 0:
                    new_count += 1
            except Exception as e:
                logger.warning(f"Gas infra insert error ({source_key}): {e}")
                continue

        conn.commit()
        cur.close()

        _log_sync(conn, source_key, market_name, len(features),
                  new_count, time.time() - start)
        total_new += new_count

    return total_new


# =============================================================================
# SYNC LOG
# =============================================================================

def _log_sync(conn, source, market, found, new, duration, error=None):
    """Log a sync run to energy_sync_log."""
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO energy_sync_log
                (source, market, records_found, records_new, duration_seconds, error)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (source, market, found, new, round(duration, 2), error))
        conn.commit()
        cur.close()
    except Exception as e:
        logger.warning(f"Sync log error: {e}")


# =============================================================================
# FULL SYNC CYCLE
# =============================================================================

def run_full_sync(conn, markets=None):
    """
    Run a full energy infrastructure sync cycle.
    If markets is None, syncs all monitored markets.
    Returns summary dict.
    """
    init_energy_tables(conn)

    results = {
        'power_plants_new': 0,
        'substations_new': 0,
        'gas_infra_new': 0,
        'markets_synced': 0,
        'errors': [],
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }

    target_markets = markets or MONITORED_MARKETS

    for market_name, (lat, lng, radius_km) in target_markets.items():
        geo_filter = (lat, lng, radius_km)
        try:
            pp = sync_power_plants(conn, market_name, geo_filter)
            results['power_plants_new'] += pp

            sub = sync_substations(conn, market_name, geo_filter)
            results['substations_new'] += sub

            gas = sync_gas_infrastructure(conn, market_name, geo_filter)
            results['gas_infra_new'] += gas

            results['markets_synced'] += 1
            logger.info(
                f"  {market_name}: +{pp} plants, +{sub} substations, +{gas} gas"
            )

            # Small delay between markets to avoid rate limiting
            time.sleep(1)

        except Exception as e:
            logger.error(f"Market sync error ({market_name}): {e}")
            results['errors'].append(f"{market_name}: {e}")

    logger.info(
        f"Full sync complete: {results['markets_synced']} markets, "
        f"+{results['power_plants_new']} plants, "
        f"+{results['substations_new']} substations, "
        f"+{results['gas_infra_new']} gas"
    )
    return results


# =============================================================================
# FLASK ROUTE REGISTRATION
# =============================================================================

def register_energy_discovery_routes(app):
    """
    Register energy auto-discovery routes. Call from main.py:
        from energy_auto_discovery_pg import register_energy_discovery_routes
        register_energy_discovery_routes(app)
    """
    from flask import jsonify, request

    def _get_neon_conn():
        import psycopg2
        db_url = os.environ.get('DATABASE_URL') or os.environ.get('NEON_DATABASE_URL')
        if not db_url:
            raise RuntimeError("No DATABASE_URL configured")
        return psycopg2.connect(db_url)

    def _check_auth():
        internal_key = request.headers.get('X-Internal-Key', '')
        admin_key = request.headers.get('X-Admin-Key', '')
        expected_admin = os.environ.get('DCHUB_ADMIN_KEY', '')
        if is_valid_internal_key(internal_key):
            return True
        if expected_admin and admin_key == expected_admin:
            return True
        return False

    @app.route('/api/jobs/infrastructure-sync', methods=['POST'])
    def job_infrastructure_sync():
        """Scheduler-triggered full infrastructure sync."""
        if not _check_auth():
            return jsonify({'error': 'Unauthorized'}), 401

        try:
            conn = _get_neon_conn()
            results = run_full_sync(conn)
            conn.close()
            return jsonify({'success': True, **results})
        except Exception as e:
            logger.error(f"Infrastructure sync job failed: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/energy-discovery/status', methods=['GET'])
    def energy_discovery_status():
        """Phase 29 — clean replacement. Returns real DB counts."""
        from flask import jsonify
        data = _phase25_real_counts()
        return jsonify({'success': True, 'data': data})

    @app.route('/api/energy-discovery/health', methods=['GET'])
    def energy_discovery_health():
        """Check if HIFLD endpoints are alive."""
        results = check_hifld_health()
        all_alive = all(r['alive'] for r in results.values())
        return jsonify({
            'success': True,
            'all_healthy': all_alive,
            'sources': results
        })

    @app.route('/api/energy-discovery/sync-now', methods=['POST'])
    def energy_discovery_sync_now():
        """Trigger immediate sync for a specific market or all."""
        if not _check_auth():
            return jsonify({'error': 'Unauthorized'}), 401

        market = request.args.get('market')
        try:
            conn = _get_neon_conn()
            if market and market in MONITORED_MARKETS:
                lat, lng, radius = MONITORED_MARKETS[market]
                geo = (lat, lng, radius)
                pp = sync_power_plants(conn, market, geo)
                sub = sync_substations(conn, market, geo)
                gas = sync_gas_infrastructure(conn, market, geo)
                conn.close()
                return jsonify({
                    'success': True,
                    'market': market,
                    'power_plants_new': pp,
                    'substations_new': sub,
                    'gas_infra_new': gas
                })
            else:
                results = run_full_sync(conn)
                conn.close()
                return jsonify({'success': True, **results})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    logger.info(f"⚡ Energy Auto-Discovery v3.0 (PostgreSQL) registered — {len(MONITORED_MARKETS)} markets, {len(HIFLD_SOURCES)} sources")
    return True


# ============================================================================
# Phase 30A — sync-all-tables endpoint
# ============================================================================
def phase30_sync_all_tables_register(app):
    """Register /api/jobs/sync-all-tables. Called from main.py."""
    from flask import jsonify, request
    import os, time, traceback

    @app.route('/api/jobs/sync-all-tables', methods=['POST'])
    def phase30_sync_all_tables():
        # Auth via X-Internal-Key
        internal_key = request.headers.get('X-Internal-Key', '')
        expected = os.environ.get('DCHUB_INTERNAL_KEY', '')
        if not expected or internal_key != expected:
            return jsonify({'success': False, 'error': 'unauthorized'}), 401
        results = {
            'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'tables': {},
            'errors': [],
        }
        # Try each loader if it exists. Each block is isolated so one failure
        # doesn't kill the whole sync.
        loader_attempts = [
            ('pipelines',         'pipeline_loader',          'load_pipelines'),
            ('transmission',      'transmission_loader',      'load_transmission'),
            ('wind_projects',     'wind_loader',              'load_wind_projects'),
            ('gas_compressors',   'gas_compressor_loader',    'load_gas_compressors'),
            ('gas_processings',   'gas_processing_loader',    'load_gas_processings'),
            ('power_plants',      'power_plant_loader',       'load_power_plants'),
            ('substations',       'osm_substations_loader',   'load_osm_substations'),
        ]
        for table, mod, fn in loader_attempts:
            try:
                m = __import__(mod, fromlist=[fn])
                f = getattr(m, fn, None)
                if not f:
                    results['tables'][table] = {'status': 'skipped', 'reason': f'{mod}.{fn} missing'}
                    continue
                t0 = time.time()
                got = f()
                results['tables'][table] = {
                    'status': 'ok',
                    'rows_or_result': got if isinstance(got, (int, str)) else 'completed',
                    'elapsed_s': round(time.time() - t0, 2),
                }
            except ImportError as _e:
                results['tables'][table] = {'status': 'skipped', 'reason': f'import: {_e}'[:120]}
            except Exception as _e:
                results['tables'][table] = {'status': 'error', 'error': type(_e).__name__ + ': ' + str(_e)[:200]}
                results['errors'].append({'table': table, 'error': str(_e)[:200]})
        results['ended_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        results['ok_count'] = sum(1 for v in results['tables'].values() if v.get('status') == 'ok')
        return jsonify({'success': True, 'data': results})
