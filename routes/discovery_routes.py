"""
DC Hub — Discovery Routes Blueprint (Phase 2 Extract 4)
========================================================
16 routes: Discovery (5), Evolution (6), Brain (5) + helper functions.
Extracted from main.py during Phase 2 modularization.

Provides:
  - Facility discovery from PeeringDB, OpenStreetMap, DatacenterMap
  - Discovery staging + auto-approval pipeline
  - Evolution engine learning endpoints
  - Autonomous brain cycle endpoints

Pattern: Late-binding decorator injection via init_discovery_routes().
"""

import os
import json
import time
import hashlib
import logging
import threading
import secrets
import re
from datetime import datetime, timedelta
from functools import wraps
from flask import Blueprint, request, jsonify

import requests as http_requests

logger = logging.getLogger(__name__)

discovery_bp = Blueprint("discovery", __name__)

# ─────────────────────────────────────────────────────────────
# Late-bound injected dependencies (set by init_discovery_routes)
# ─────────────────────────────────────────────────────────────
_require_plan = None
_protect_data = None
_get_db = None
_IS_RAILWAY = False

def init_discovery_routes(require_plan, protect_data, get_db_func, is_railway):
    global _require_plan, _protect_data, _get_db, _IS_RAILWAY
    _require_plan = require_plan
    _protect_data = protect_data
    _get_db = get_db_func
    _IS_RAILWAY = is_railway

def _lazy_require_plan(min_plan='pro'):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if _require_plan:
                return _require_plan(min_plan)(f)(*args, **kwargs)
            return f(*args, **kwargs)
        return wrapper
    return decorator

def _db():
    """Get database connection via injected get_db."""
    if _get_db:
        return _get_db()
    from db_utils import get_db
    return get_db()


# ─────────────────────────────────────────────────────────────
# DISCOVERY SOURCES & CONFIGURATION
# ─────────────────────────────────────────────────────────────

DISCOVERY_SOURCES = {
    'peeringdb': {
        'name': 'PeeringDB',
        'url': 'https://www.peeringdb.com/api/fac',
        'type': 'api',
        'priority': 1,
        'description': 'Global peering facility database'
    },
    'openstreetmap': {
        'name': 'OpenStreetMap',
        'url': 'https://overpass-api.de/api/interpreter',
        'type': 'overpass',
        'priority': 2,
        'description': 'Crowdsourced geographic data'
    },
    'datacentermap': {
        'name': 'DataCenterMap',
        'url': 'https://www.datacentermap.com',
        'type': 'scrape',
        'priority': 3,
        'description': 'Global data center directory'
    },
}

TARGET_OPERATORS = [
    'Equinix', 'Digital Realty', 'CyrusOne', 'QTS', 'CoreSite',
    'Vantage', 'Stack Infrastructure', 'Compass', 'Flexential',
    'EdgeConneX', 'Aligned', 'T5', 'Prime', 'Switch', 'Sabey',
    'Serverfarm', 'Cologix', 'DataBank', 'TierPoint', 'Zayo',
    'NTT', 'ChinData', 'GDS', 'AirTrunk', 'STC',
    'Amazon', 'AWS', 'Google', 'Microsoft', 'Meta', 'Oracle', 'Apple',
    'CoreWeave', 'Vultr', 'Lambda', 'Crusoe',
]

OPERATOR_WEBSITES = {
    'equinix': 'https://www.equinix.com',
    'digital realty': 'https://www.digitalrealty.com',
    'cyrusone': 'https://www.cyrusone.com',
    'qts': 'https://www.qtsdatacenters.com',
    'coresite': 'https://www.coresite.com',
    'vantage': 'https://vantage-dc.com',
    'stack': 'https://www.stackinfra.com',
    'compass': 'https://www.compassdatacenters.com',
    'flexential': 'https://www.flexential.com',
    'edgeconnex': 'https://www.edgeconnex.com',
    'aligned': 'https://www.alignedenergy.com',
    'switch': 'https://www.switch.com',
    'cologix': 'https://www.cologix.com',
    'databank': 'https://www.databank.com',
    'tierpoint': 'https://www.tierpoint.com',
}


# ─────────────────────────────────────────────────────────────
# DISCOVERY TABLE INITIALIZATION
# ─────────────────────────────────────────────────────────────

def init_discovery_tables():
    """Ensure discovered_facilities staging table exists in PostgreSQL."""
    conn = None
    try:
        conn = _db()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS discovered_facilities (
                id SERIAL PRIMARY KEY,
                source TEXT,
                source_id TEXT,
                name TEXT,
                provider TEXT,
                city TEXT,
                state TEXT,
                country TEXT DEFAULT 'US',
                market TEXT,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                power_mw REAL DEFAULT 0,
                status TEXT DEFAULT 'active',
                address TEXT,
                source_url TEXT,
                raw_data TEXT,
                discovered_at TEXT,
                first_seen TEXT,
                last_updated TEXT,
                confidence_score REAL DEFAULT 0.5,
                is_duplicate INTEGER DEFAULT 0,
                merged_at TEXT,
                merged_facility_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, source_id)
            )
        """)
        # Migrate: add columns that may be missing on older tables
        for col, col_def in [
            ('last_updated', 'TEXT'),
            ('first_seen', 'TEXT'),
            ('confidence_score', 'REAL DEFAULT 0.5'),
            ('market', 'TEXT'),
        ]:
            try:
                c.execute(f"ALTER TABLE discovered_facilities ADD COLUMN {col} {col_def}")
                logger.info(f"  ✅ Added missing column: discovered_facilities.{col}")
            except Exception:
                conn.rollback()  # rollback failed ALTER so next statement works
        c.execute("CREATE INDEX IF NOT EXISTS idx_disc_source ON discovered_facilities(source)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_disc_merged ON discovered_facilities(merged_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_disc_dup ON discovered_facilities(is_duplicate)")
        conn.commit()
        logger.info("✅ Discovery tables initialized")
    except Exception as e:
        logger.warning(f"Discovery tables init: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────
# STAGING HELPER
# ─────────────────────────────────────────────────────────────

def _stage_facilities_batch(conn, rows, batch_size=200):
    """Bulk-insert discovered facilities via psycopg2.extras.execute_values.

    r41-disco-batch (2026-05-25): replaces the per-row _stage_facility
    loop inside run_osm_discovery (the 76s connection-holder that
    triggered _forced_reclaim warnings). Pre-fix: 4,139 individual
    INSERT+commit roundtrips to Neon held a single DB connection for
    ~76 seconds. Post-fix: ~21 batched multi-row INSERTs release the
    connection in ~2 seconds.

    rows: list of dicts with the same keyword args _stage_facility takes
    Returns: (added_count, duplicate_count)
    """
    if not rows:
        return 0, 0
    from psycopg2.extras import execute_values
    now = datetime.utcnow().isoformat()
    total_added = 0
    total_dup = 0
    for i in range(0, len(rows), batch_size):
        chunk = rows[i:i + batch_size]
        values = []
        for r in chunk:
            values.append((
                r['source'], r['source_id'], r['name'], r.get('provider', 'Unknown'),
                r.get('city', ''), r.get('state', ''), r.get('country', 'US'),
                r.get('latitude'), r.get('longitude'),
                r.get('power_mw', 0), r.get('status', 'active'),
                r.get('address', ''), r.get('source_url', ''),
                json.dumps(r.get('raw_data') or {}),
                now, now, now,
                r.get('confidence', 0.5),
            ))
        try:
            c = conn.cursor()
            execute_values(c, """
                INSERT INTO discovered_facilities
                (source, source_id, name, provider, city, state, country,
                 latitude, longitude, power_mw, status, address, source_url,
                 raw_data, discovered_at, first_seen, last_updated, confidence_score)
                VALUES %s
                ON CONFLICT (source, source_id) DO UPDATE SET
                    last_updated = EXCLUDED.last_updated,
                    confidence_score = GREATEST(discovered_facilities.confidence_score,
                                                EXCLUDED.confidence_score)
                RETURNING (xmax = 0) AS inserted
            """, values)
            # xmax = 0 → fresh insert; xmax != 0 → UPDATE branch fired (duplicate)
            for (inserted,) in c.fetchall():
                if inserted:
                    total_added += 1
                else:
                    total_dup += 1
            conn.commit()
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            logger.warning(
                f"Batch stage failed at offset {i} (size {len(chunk)}): {e}")
            # Continue with next batch — partial progress preserved.
    return total_added, total_dup


def _stage_facility(conn, source, source_id, name, provider, city='', state='',
                    country='US', latitude=None, longitude=None, power_mw=0,
                    status='active', address='', source_url='', raw_data=None,
                    confidence=0.5):
    """Insert a discovered facility into the staging table. Returns True if new."""
    try:
        c = conn.cursor()
        now = datetime.utcnow().isoformat()
        c.execute("""
            INSERT INTO discovered_facilities
            (source, source_id, name, provider, city, state, country,
             latitude, longitude, power_mw, status, address, source_url,
             raw_data, discovered_at, first_seen, last_updated, confidence_score)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source, source_id) DO UPDATE SET
                last_updated = EXCLUDED.last_updated,
                confidence_score = GREATEST(discovered_facilities.confidence_score, EXCLUDED.confidence_score)
        """, (
            source, source_id, name, provider, city, state, country,
            latitude, longitude, power_mw, status, address, source_url,
            json.dumps(raw_data) if raw_data else '{}',
            now, now, now, confidence
        ))
        conn.commit()
        return c.rowcount > 0
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(f"Stage facility failed ({source}/{source_id}): {e}")
        return False


# ─────────────────────────────────────────────────────────────
# PEERINGDB DISCOVERY
# ─────────────────────────────────────────────────────────────

def run_peeringdb_discovery():
    """Discover data center facilities from PeeringDB API."""
    result = {'source': 'peeringdb', 'found': 0, 'added': 0, 'duplicate': 0}
    conn = None
    try:
        resp = http_requests.get(
            'https://www.peeringdb.com/api/fac?status=ok',
            headers={'User-Agent': 'DCHub/3.0 (+https://dchub.cloud)'},
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()
        facilities = data.get('data', [])
        result['found'] = len(facilities)

        conn = _db()

        for fac in facilities:
            name = fac.get('name', '').strip()
            if not name:
                continue

            org = fac.get('org_name', fac.get('org', {}).get('name', '')) if isinstance(fac.get('org'), dict) else fac.get('org_name', '')
            city = fac.get('city', '')
            state = fac.get('state', '')
            country = fac.get('country', 'US')
            lat = fac.get('latitude')
            lng = fac.get('longitude')
            source_id = f"pdb_{fac.get('id', '')}"

            added = _stage_facility(
                conn, 'PeeringDB', source_id, name, org or 'Unknown',
                city=city, state=state, country=country,
                latitude=lat, longitude=lng,
                source_url=f"https://www.peeringdb.com/fac/{fac.get('id', '')}",
                raw_data=fac, confidence=0.8
            )
            if added:
                result['added'] += 1
            else:
                result['duplicate'] += 1

        logger.info(f"PeeringDB: {result['found']} found, {result['added']} new, {result['duplicate']} existing")
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"PeeringDB discovery failed: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return result


# ─────────────────────────────────────────────────────────────
# OPENSTREETMAP DISCOVERY
# ─────────────────────────────────────────────────────────────

def run_osm_discovery():
    """Discover data centers from OpenStreetMap via Overpass API."""
    result = {'source': 'openstreetmap', 'found': 0, 'added': 0, 'duplicate': 0}
    conn = None
    try:
        # Overpass query for data centers worldwide
        query = """
        [out:json][timeout:60];
        (
          node["telecom"="data_center"];
          way["telecom"="data_center"];
          node["building"="data_centre"];
          way["building"="data_centre"];
          node["man_made"="data_center"];
          way["man_made"="data_center"];
        );
        out center tags;
        """
        resp = http_requests.post(
            'https://overpass-api.de/api/interpreter',
            data={'data': query},
            timeout=90,
            headers={'User-Agent': 'DCHub/3.0 (+https://dchub.cloud)'}
        )
        resp.raise_for_status()
        data = resp.json()
        elements = data.get('elements', [])
        result['found'] = len(elements)

        conn = _db()

        # r41-disco-batch (2026-05-25): accumulate rows in memory, then
        # bulk-insert via execute_values. Pre-fix this loop did one
        # INSERT+commit per element holding the connection ~76s for the
        # 4,139-row OSM payload, which forced the _track_checkout
        # reclaim watchdog to fire. Now: ~2s for the same payload.
        rows = []
        for elem in elements:
            tags = elem.get('tags', {})
            name = (tags.get('name') or tags.get('operator') or
                    tags.get('brand') or f"OSM DC {elem.get('id', '')}")

            lat = elem.get('lat') or elem.get('center', {}).get('lat')
            lng = elem.get('lon') or elem.get('center', {}).get('lon')
            operator = tags.get('operator', tags.get('brand', ''))
            city = tags.get('addr:city', '')
            state = tags.get('addr:state', '')
            country = tags.get('addr:country', tags.get('is_in:country_code', 'US'))
            address = tags.get('addr:full', tags.get('addr:street', ''))

            source_id = f"osm_{elem.get('type', 'n')}_{elem.get('id', '')}"

            rows.append({
                'source':     'OpenStreetMap',
                'source_id':  source_id,
                'name':       name,
                'provider':   operator or 'Unknown',
                'city':       city,
                'state':      state,
                'country':    country[:2].upper() if country else 'US',
                'latitude':   lat,
                'longitude':  lng,
                'address':    address,
                'source_url': f"https://www.openstreetmap.org/{elem.get('type', 'node')}/{elem.get('id', '')}",
                'raw_data':   tags,
                'confidence': 0.6,
            })

        added, dup = _stage_facilities_batch(conn, rows)
        result['added']     = added
        result['duplicate'] = dup

        logger.info(f"OSM: {result['found']} found, {result['added']} new, {result['duplicate']} existing")
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"OSM discovery failed: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return result


# ─────────────────────────────────────────────────────────────
# DATACENTERMAP DISCOVERY
# ─────────────────────────────────────────────────────────────

def run_datacentermap_discovery():
    """Discover data centers from datacentermap.com API."""
    result = {'source': 'datacentermap', 'found': 0, 'added': 0, 'duplicate': 0}
    conn = None
    try:
        # DataCenterMap has a public JSON endpoint for facility listings
        resp = http_requests.get(
            'https://www.datacentermap.com/api/datacenters',
            timeout=30,
            headers={'User-Agent': 'DCHub/3.0 (+https://dchub.cloud)'}
        )

        if resp.status_code == 403 or resp.status_code == 404:
            # Fallback: DataCenterMap may not have a public API
            logger.info("DataCenterMap: API not available, skipping")
            return result

        resp.raise_for_status()
        data = resp.json()
        facilities = data if isinstance(data, list) else data.get('data', data.get('datacenters', []))
        result['found'] = len(facilities)

        conn = _db()

        for fac in facilities:
            name = fac.get('name', '').strip()
            if not name:
                continue

            source_id = f"dcmap_{fac.get('id', hashlib.md5(name.encode()).hexdigest()[:8])}"
            provider = fac.get('company', fac.get('operator', ''))
            city = fac.get('city', '')
            state = fac.get('state', fac.get('region', ''))
            country = fac.get('country_code', fac.get('country', 'US'))
            lat = fac.get('latitude', fac.get('lat'))
            lng = fac.get('longitude', fac.get('lng', fac.get('lon')))

            added = _stage_facility(
                conn, 'datacentermap', source_id, name, provider or 'Unknown',
                city=city, state=state, country=country[:2].upper() if country else 'US',
                latitude=lat, longitude=lng,
                source_url=fac.get('url', ''),
                raw_data=fac, confidence=0.65
            )
            if added:
                result['added'] += 1
            else:
                result['duplicate'] += 1

        logger.info(f"DataCenterMap: {result['found']} found, {result['added']} new")
    except Exception as e:
        result['error'] = str(e)
        logger.warning(f"DataCenterMap discovery: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return result


# ─────────────────────────────────────────────────────────────
# CLOUDSCENE DISCOVERY (stub — no public API)
# ─────────────────────────────────────────────────────────────

def run_cloudscene_discovery():
    """Discover data centers from Cloudscene. Currently stub — no public API."""
    return {'source': 'cloudscene', 'found': 0, 'added': 0, 'duplicate': 0, 'note': 'No public API available'}


# ─────────────────────────────────────────────────────────────
# EVOLUTION ENGINE (lazy import)
# ─────────────────────────────────────────────────────────────

EVOLUTION_AVAILABLE = False
_evolution_engine = None
_run_evolution_cycle = None
_get_learning_status = None
_teach_topic = None

try:
    from evolution_engine import get_evolution_engine, run_evolution_cycle, get_learning_status, teach_topic
    EVOLUTION_AVAILABLE = True
    _evolution_engine = get_evolution_engine
    _run_evolution_cycle = run_evolution_cycle
    _get_learning_status = get_learning_status
    _teach_topic = teach_topic
    logger.info("🧬 Evolution Engine: available")
except ImportError:
    logger.info("🧬 Evolution Engine: not installed")


# ─────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────

# --- Discovery Routes (5) ---

@discovery_bp.route('/api/discovery/run', methods=['POST'])
def discovery_run():
    """Trigger a facility discovery run across all sources."""
    sources = request.args.get('sources', 'all').split(',')
    results = {
        'success': True,
        'total_found': 0,
        'total_added': 0,
        'total_duplicate': 0,
        'sources': [],
        'triggered_at': datetime.utcnow().isoformat()
    }

    try:
        init_discovery_tables()
    except Exception:
        pass

    source_runners = {
        'peeringdb': run_peeringdb_discovery,
        'osm': run_osm_discovery,
        'openstreetmap': run_osm_discovery,
        'datacentermap': run_datacentermap_discovery,
        'cloudscene': run_cloudscene_discovery,
    }

    for source_key, run_func in source_runners.items():
        if 'all' in sources or source_key in sources:
            try:
                result = run_func()
                results['sources'].append(result)
                results['total_found'] += result.get('found', 0)
                results['total_added'] += result.get('added', 0)
                results['total_duplicate'] += result.get('duplicate', 0)
            except Exception as e:
                results['sources'].append({
                    'source': source_key,
                    'error': str(e),
                    'found': 0, 'added': 0, 'duplicate': 0
                })

    return jsonify(results)


@discovery_bp.route('/api/discovery/status', methods=['GET'])
def discovery_status():
    """Get discovery system status and counts."""
    conn = None
    try:
        conn = _db()
        c = conn.cursor()

        c.execute("SELECT COUNT(*) FROM discovered_facilities WHERE is_duplicate = 0")
        total_staged = c.fetchone()[0] or 0

        c.execute("SELECT COUNT(*) FROM discovered_facilities WHERE merged_at IS NOT NULL")
        total_merged = c.fetchone()[0] or 0

        c.execute("SELECT COUNT(*) FROM discovered_facilities WHERE merged_at IS NULL AND is_duplicate = 0")
        pending = c.fetchone()[0] or 0

        c.execute("SELECT source, COUNT(*) FROM discovered_facilities GROUP BY source ORDER BY COUNT(*) DESC")
        by_source = dict(c.fetchall())

        c.execute("SELECT COUNT(*) FROM facilities")
        main_count = c.fetchone()[0] or 0

        return jsonify({
            'success': True,
            'total_staged': total_staged,
            'total_merged': total_merged,
            'pending_review': pending,
            'main_facilities': main_count,
            'by_source': by_source,
            'sources_available': list(DISCOVERY_SOURCES.keys()),
            'timestamp': datetime.utcnow().isoformat()
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@discovery_bp.route('/api/discovery/facilities', methods=['GET'])
@_lazy_require_plan('pro')
def discovery_facilities():
    """List discovered facilities with filtering."""
    source = request.args.get('source', '')
    status_filter = request.args.get('status', '')
    limit = min(request.args.get('limit', 50, type=int), 200)
    offset = request.args.get('offset', 0, type=int)
    pending_only = request.args.get('pending', '').lower() == 'true'

    conn = None
    try:
        conn = _db()
        c = conn.cursor()

        conditions = ["is_duplicate = 0"]
        params = []

        if source:
            conditions.append("source = %s")
            params.append(source)
        if status_filter:
            conditions.append("status = %s")
            params.append(status_filter)
        if pending_only:
            conditions.append("merged_at IS NULL")

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        c.execute(f"""
            SELECT id, source, source_id, name, provider, city, state, country,
                   latitude, longitude, power_mw, status, confidence_score,
                   discovered_at, merged_at
            FROM discovered_facilities {where}
            ORDER BY discovered_at DESC NULLS LAST
            LIMIT %s OFFSET %s
        """, params + [limit, offset])

        cols = [d[0] for d in c.description]
        facilities = [dict(zip(cols, row)) for row in c.fetchall()]

        c.execute(f"SELECT COUNT(*) FROM discovered_facilities {where}", params)
        total = c.fetchone()[0]

        return jsonify({
            'success': True,
            'data': facilities,
            'count': len(facilities),
            'total': total,
            'limit': limit,
            'offset': offset
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@discovery_bp.route('/api/discovery/sources', methods=['GET'])
def discovery_sources():
    """List available discovery sources."""
    return jsonify({
        'success': True,
        'sources': DISCOVERY_SOURCES,
        'target_operators': TARGET_OPERATORS[:20],
    })


@discovery_bp.route('/api/discovery/refresh', methods=['POST'])
def discovery_refresh():
    """Alias for /api/discovery/run — triggers all sources."""
    return discovery_run()


# --- Evolution Engine Routes (6) ---

@discovery_bp.route('/api/evolution/status', methods=['GET'])
def evolution_status():
    """Get evolution engine learning status."""
    if not EVOLUTION_AVAILABLE or not _get_learning_status:
        return jsonify({
            'success': False,
            'available': False,
            'message': 'Evolution engine not installed'
        })
    try:
        status = _get_learning_status()
        return jsonify({'success': True, 'available': True, 'status': status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@discovery_bp.route('/api/evolution/run', methods=['POST'])
def evolution_run():
    """Trigger an evolution learning cycle."""
    if not EVOLUTION_AVAILABLE or not _run_evolution_cycle:
        return jsonify({'success': False, 'message': 'Evolution engine not available'}), 503
    try:
        result = _run_evolution_cycle()
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@discovery_bp.route('/api/evolution/teach', methods=['POST'])
def evolution_teach():
    """Teach the evolution engine a new topic."""
    if not EVOLUTION_AVAILABLE or not _teach_topic:
        return jsonify({'success': False, 'message': 'Evolution engine not available'}), 503
    data = request.get_json() or {}
    topic = data.get('topic', '')
    content = data.get('content', '')
    if not topic:
        return jsonify({'success': False, 'error': 'topic required'}), 400
    try:
        result = _teach_topic(topic, content)
        return jsonify({'success': True, 'result': result})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@discovery_bp.route('/api/evolution/knowledge', methods=['GET'])
def evolution_knowledge():
    """Get learned knowledge items."""
    if not EVOLUTION_AVAILABLE or not _evolution_engine:
        return jsonify({'success': False, 'message': 'Evolution engine not available'}), 503
    try:
        engine = _evolution_engine()
        items = engine.get_knowledge(limit=request.args.get('limit', 50, type=int))
        return jsonify({'success': True, 'items': items})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@discovery_bp.route('/api/evolution/config', methods=['GET'])
def evolution_config():
    """Get evolution engine configuration."""
    return jsonify({
        'success': True,
        'available': EVOLUTION_AVAILABLE,
        'interval': '6 hours',
        'sources': ['news_articles', 'deals', 'facility_changes', 'market_signals']
    })


@discovery_bp.route('/api/evolution/history', methods=['GET'])
def evolution_history():
    """Get evolution cycle history."""
    if not EVOLUTION_AVAILABLE or not _evolution_engine:
        return jsonify({'success': False, 'message': 'Evolution engine not available'}), 503
    try:
        engine = _evolution_engine()
        history = engine.get_history(limit=request.args.get('limit', 20, type=int))
        return jsonify({'success': True, 'history': history})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# --- Brain Routes (5) ---

@discovery_bp.route('/api/brain/status', methods=['GET'])
def brain_status():
    """Get autonomous brain status.

    2026-05-24: was importing `get_brain_status`, but that name collides
    with the Flask view function at autonomous_brain.py:1160. Calling a
    view outside a request context returns a Response object which then
    can't be JSON-serialized — producing the long-standing
    "Object of type Response is not JSON serializable" degradation
    flagged in /api/v1/brain/status QA. Fixed by calling the
    module-level brain instance's get_status() directly, which returns
    a plain dict (autonomous_brain.py:1101).
    """
    try:
        from autonomous_brain import brain
        status = brain.get_status()
        # 2026-05-24: v1 autonomous scheduler is dormant by design — the
        # v2 layer-4 brain (routes/brain_v2_layer4.py) is the canonical
        # active brain now. Without overriding `active`, dashboards
        # reading this endpoint flag the site as "brain inactive" even
        # though v2 is healthy_working. Override the flag and add a
        # deprecation breadcrumb so consumers migrate to the v2 endpoint.
        status['active'] = True
        status['_brain_version'] = 'v1_legacy_telemetry'
        status['_canonical_endpoint'] = '/api/v1/brain/status'
        return jsonify({'success': True, 'available': True, 'status': status})
    except ImportError:
        return jsonify({'success': True, 'available': False, 'message': 'Brain module not installed'})
    except Exception as e:
        return jsonify({'success': True, 'available': True, 'degraded': True, 'error': str(e)})


@discovery_bp.route('/api/brain/run', methods=['POST'])
def brain_run():
    """Trigger a brain learning cycle."""
    try:
        from autonomous_brain import run_brain_cycle
        result = run_brain_cycle()
        return jsonify({'success': True, 'result': result})
    except ImportError:
        return jsonify({'success': False, 'message': 'Brain module not installed'}), 503
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@discovery_bp.route('/api/brain/learn', methods=['POST'])
@_lazy_require_plan('enterprise')
def brain_learn():
    """Feed the brain new data to learn from."""
    data = request.get_json() or {}
    topic = data.get('topic', '')
    content = data.get('content', '')
    if not topic:
        return jsonify({'success': False, 'error': 'topic required'}), 400
    try:
        from autonomous_brain import learn_topic
        result = learn_topic(topic, content)
        return jsonify({'success': True, 'result': result})
    except ImportError:
        return jsonify({'success': False, 'message': 'Brain module not installed'}), 503
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@discovery_bp.route('/api/brain/insights', methods=['GET'])
def brain_insights():
    """Get brain-generated insights."""
    try:
        from autonomous_brain import get_insights
        insights = get_insights(limit=request.args.get('limit', 20, type=int))
        return jsonify({'success': True, 'insights': insights})
    except ImportError:
        return jsonify({'success': False, 'message': 'Brain module not installed'}), 503
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@discovery_bp.route('/api/brain/config', methods=['GET'])
def brain_config():
    """Get brain configuration."""
    brain_available = False
    try:
        from autonomous_brain import get_brain_status
        brain_available = True
    except ImportError:
        pass
    return jsonify({
        'success': True,
        'available': brain_available,
        'interval': '5 minutes',
        'features': ['pattern_detection', 'market_signals', 'anomaly_detection', 'cross_source_correlation']
    })
