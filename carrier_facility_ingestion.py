"""
DC Hub — Carrier-Facility Mapper (PeeringDB + FCC BDC)
══════════════════════════════════════════════════════════
Ingests carrier-to-facility relationships from PeeringDB and enriches
fiber route data from FCC Broadband Data Collection.

Sources:
  - PeeringDB API v0: /api/carrier, /api/carrierfac, /api/fac
  - FCC BDC: Fiber availability at the census-block level

Tables created:
  - carrier_profiles         (carrier name, website, aka, policy_url)
  - carrier_facility_presence (carrier ↔ facility cross-ref)
  - fiber_route_geometry     (lit-fiber routes from FCC BDC + OSM)
  - fiber_coverage_zones     (metro/market fiber coverage summaries)

Run: POST /api/jobs/carrier-sync  (admin/internal key required)
Schedule: Weekly via crawler_scheduler.py

v1.0 — March 2026
"""

import json
import logging
import os
import math
from datetime import datetime

import requests

logger = logging.getLogger('dchub-carrier')

# ─────────────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────────────
PEERINGDB_BASE = "https://www.peeringdb.com/api"
PEERINGDB_CARRIERS = f"{PEERINGDB_BASE}/carrier"
PEERINGDB_CARRIER_FAC = f"{PEERINGDB_BASE}/carrierfac"
PEERINGDB_FACILITIES = f"{PEERINGDB_BASE}/fac"
PEERINGDB_NETWORKS = f"{PEERINGDB_BASE}/net"

# FCC BDC bulk data (requires API key — free registration)
FCC_BDC_BASE = "https://broadbandmap.fcc.gov/api/public"

# Rate limiting: PeeringDB allows 20 req/min for anonymous
PEERINGDB_HEADERS = {
    'User-Agent': 'DCHub-Intelligence/1.0 (dchub.cloud; data-center-research)',
    'Accept': 'application/json',
}


# ─────────────────────────────────────────────────────────────
# TABLE CREATION
# ─────────────────────────────────────────────────────────────
def init_carrier_tables(get_db):
    """Create carrier/fiber tables in PostgreSQL (Neon)."""
    conn = None
    try:
        conn = get_db()
    try:
            c = conn.cursor()

            # 1. Carrier profiles from PeeringDB
            c.execute("""
                CREATE TABLE IF NOT EXISTS carrier_profiles (
                    id SERIAL PRIMARY KEY,
                    pdb_id TEXT UNIQUE,
                    name TEXT NOT NULL,
                    aka TEXT,
                    name_long TEXT,
                    website TEXT,
                    notes TEXT,
                    policy_url TEXT,
                    policy_general TEXT,
                    org_id INTEGER,
                    org_name TEXT,
                    status TEXT DEFAULT 'ok',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 2. Carrier ↔ Facility cross-reference
            # Note: PeeringDB IDs can be integers or hex strings, so we use TEXT
            c.execute("""
                CREATE TABLE IF NOT EXISTS carrier_facility_presence (
                    id SERIAL PRIMARY KEY,
                    carrier_pdb_id TEXT NOT NULL,
                    carrier_name TEXT,
                    facility_pdb_id TEXT NOT NULL,
                    facility_name TEXT,
                    facility_city TEXT,
                    facility_state TEXT,
                    facility_country TEXT,
                    facility_lat DOUBLE PRECISION,
                    facility_lng DOUBLE PRECISION,
                    dchub_facility_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(carrier_pdb_id, facility_pdb_id)
                )
            """)

            # 3. Fiber route geometry (FCC BDC + OpenStreetMap)
            c.execute("""
                CREATE TABLE IF NOT EXISTS fiber_route_geometry (
                    id SERIAL PRIMARY KEY,
                    route_id TEXT UNIQUE,
                    source TEXT NOT NULL,
                    provider_name TEXT,
                    provider_id TEXT,
                    technology TEXT DEFAULT 'fiber',
                    tech_code INTEGER,
                    from_location TEXT,
                    to_location TEXT,
                    from_lat DOUBLE PRECISION,
                    from_lng DOUBLE PRECISION,
                    to_lat DOUBLE PRECISION,
                    to_lng DOUBLE PRECISION,
                    distance_km REAL,
                    max_download_mbps REAL,
                    max_upload_mbps REAL,
                    is_business BOOLEAN DEFAULT FALSE,
                    geometry_geojson TEXT,
                    state_fips TEXT,
                    county_fips TEXT,
                    census_block TEXT,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 4. Fiber coverage zone summaries
            c.execute("""
                CREATE TABLE IF NOT EXISTS fiber_coverage_zones (
                    id SERIAL PRIMARY KEY,
                    zone_id TEXT UNIQUE,
                    zone_name TEXT NOT NULL,
                    zone_type TEXT DEFAULT 'metro',
                    state TEXT,
                    country TEXT DEFAULT 'US',
                    center_lat DOUBLE PRECISION,
                    center_lng DOUBLE PRECISION,
                    provider_count INTEGER DEFAULT 0,
                    lit_building_count INTEGER DEFAULT 0,
                    fiber_route_count INTEGER DEFAULT 0,
                    avg_download_mbps REAL,
                    avg_upload_mbps REAL,
                    dark_fiber_available BOOLEAN DEFAULT FALSE,
                    carrier_list TEXT,
                    data_sources TEXT,
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Migration: fix column types from INTEGER to TEXT if needed
            for tbl, col in [
                ('carrier_profiles', 'pdb_id'),
                ('carrier_facility_presence', 'carrier_pdb_id'),
                ('carrier_facility_presence', 'facility_pdb_id'),
            ]:
                try:
                    c.execute(f"ALTER TABLE {tbl} ALTER COLUMN {col} TYPE TEXT USING {col}::TEXT")
                    logger.info(f"  ✅ Migrated {tbl}.{col} to TEXT")
                except Exception:
                    conn.rollback()  # rollback failed ALTER so next statement works

            # Indexes
            c.execute("CREATE INDEX IF NOT EXISTS idx_carrier_profiles_name ON carrier_profiles(name)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_carrier_profiles_pdb ON carrier_profiles(pdb_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cfp_carrier ON carrier_facility_presence(carrier_pdb_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cfp_facility ON carrier_facility_presence(facility_pdb_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cfp_dchub ON carrier_facility_presence(dchub_facility_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_cfp_city ON carrier_facility_presence(facility_city)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_fiber_route_source ON fiber_route_geometry(source)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_fiber_route_state ON fiber_route_geometry(state_fips)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_fiber_zone_state ON fiber_coverage_zones(state)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_fiber_zone_type ON fiber_coverage_zones(zone_type)")

            conn.commit()
            logger.info("✅ Carrier & fiber tables initialized")
        except Exception as e:
            logger.warning(f"Carrier tables init: {e}")
        finally:
            if conn:
                try:


        # ─────────────────────────────────────────────────────────────
        # PEERINGDB DATA FETCHING
        # ─────────────────────────────────────────────────────────────
    finally:
        conn.close()
def _pdb_fetch(endpoint, params=None, timeout=60):
    """Fetch from PeeringDB API with rate-limit awareness."""
    try:
        # Use API key if available (higher rate limits)
        headers = dict(PEERINGDB_HEADERS)
        api_key = os.environ.get('PEERINGDB_API_KEY', '')
        if api_key:
            headers['Authorization'] = f'Api-Key {api_key}'

        resp = requests.get(endpoint, headers=headers, params=params, timeout=timeout)

        # Handle rate limiting
        if resp.status_code == 429:
            logger.warning("PeeringDB rate limit hit — will retry on next sync")
            return None

        resp.raise_for_status()
        data = resp.json()
        return data.get('data', data)
    except Exception as e:
        logger.warning(f"PeeringDB fetch error ({endpoint}): {e}")
        return None


def _haversine_km(lat1, lng1, lat2, lng2):
    """Calculate distance in km between two lat/lng points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng/2)**2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


# ─────────────────────────────────────────────────────────────
# CARRIER PROFILE INGESTION
# ─────────────────────────────────────────────────────────────
def ingest_carriers(get_db):
    """Fetch all carrier profiles from PeeringDB."""
    carriers = _pdb_fetch(PEERINGDB_CARRIERS)
    if not carriers:
        return {'success': False, 'error': 'Failed to fetch carrier data from PeeringDB'}

    conn = None
    upserted = 0
    errors = 0

    try:
        conn = get_db()
    try:
            c = conn.cursor()

            for carrier in carriers:
                try:
                    pdb_id = str(carrier.get('id', ''))
                    if not pdb_id:
                        continue

                    name = carrier.get('name', '')
                    aka = carrier.get('aka', '')
                    name_long = carrier.get('name_long', '')
                    website = carrier.get('website', '')
                    notes = carrier.get('notes', '')
                    policy_url = carrier.get('policy_url', '')
                    policy_general = carrier.get('policy_general', '')
                    org_id = carrier.get('org_id') or carrier.get('org', {}).get('id') if isinstance(carrier.get('org'), dict) else carrier.get('org_id')
                    org_name = carrier.get('org', {}).get('name', '') if isinstance(carrier.get('org'), dict) else ''
                    status = carrier.get('status', 'ok')

                    c.execute("""
                        INSERT INTO carrier_profiles
                            (pdb_id, name, aka, name_long, website, notes,
                             policy_url, policy_general, org_id, org_name, status, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (pdb_id) DO UPDATE SET
                            name = EXCLUDED.name,
                            aka = EXCLUDED.aka,
                            name_long = EXCLUDED.name_long,
                            website = EXCLUDED.website,
                            notes = EXCLUDED.notes,
                            policy_url = EXCLUDED.policy_url,
                            policy_general = EXCLUDED.policy_general,
                            org_id = EXCLUDED.org_id,
                            org_name = EXCLUDED.org_name,
                            status = EXCLUDED.status,
                            updated_at = NOW()
                    """, (pdb_id, name, aka, name_long, website, notes,
                          policy_url, policy_general, org_id, org_name, status))

                    upserted += 1

                except Exception as e:
                    errors += 1
                    if errors < 5:
                        logger.warning(f"Carrier upsert error: {e}")

            conn.commit()
            logger.info(f"✅ Carriers: {upserted} upserted, {errors} errors from {len(carriers)} records")

        except Exception as e:
            logger.error(f"Carrier ingestion failed: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            if conn:
                try:

        return {
            'success': True,
            'source': 'PeeringDB',
            'total_fetched': len(carriers),
            'upserted': upserted,
            'errors': errors,
        }


        # ─────────────────────────────────────────────────────────────
        # CARRIER ↔ FACILITY CROSS-REFERENCE
        # ─────────────────────────────────────────────────────────────
    finally:
        conn.close()
def ingest_carrier_facilities(get_db):
    """
    Fetch carrier-facility relationships from PeeringDB /carrierfac
    and cross-reference with DC Hub's existing facility database.
    """
    carrierfacs = _pdb_fetch(PEERINGDB_CARRIER_FAC)
    if not carrierfacs:
        return {'success': False, 'error': 'Failed to fetch carrier-facility data'}

    # Also fetch facilities for enrichment (name, city, coords)
    facilities_data = _pdb_fetch(PEERINGDB_FACILITIES)
    fac_lookup = {}
    if facilities_data:
        for fac in facilities_data:
            fac_lookup[fac.get('id')] = fac
    logger.info(f"📦 Loaded {len(fac_lookup)} PeeringDB facilities for enrichment")

    # Fetch carrier names for enrichment
    carriers_data = _pdb_fetch(PEERINGDB_CARRIERS)
    carrier_lookup = {}
    if carriers_data:
        for car in carriers_data:
            carrier_lookup[car.get('id')] = car.get('name', '')
    logger.info(f"📦 Loaded {len(carrier_lookup)} carrier names for enrichment")

    conn = None
    upserted = 0
    matched = 0  # matched to DC Hub facilities
    errors = 0

    try:
        conn = get_db()
    try:
            c = conn.cursor()

            # Build a lookup of existing DC Hub facilities by (name, city, state) for matching
            dchub_fac_lookup = {}
            try:
                c.execute("""
                    SELECT id, name, city, state, latitude, longitude
                    FROM facilities
                    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                """)
                for row in c.fetchall():
                    # Key by lowercase (city, state) for fuzzy matching
                    key = (str(row[2] or '').lower().strip(), str(row[3] or '').lower().strip())
                    if key not in dchub_fac_lookup:
                        dchub_fac_lookup[key] = []
                    dchub_fac_lookup[key].append({
                        'id': row[0], 'name': row[1],
                        'lat': float(row[4]) if row[4] else None,
                        'lng': float(row[5]) if row[5] else None,
                    })
                logger.info(f"📦 Loaded {sum(len(v) for v in dchub_fac_lookup.values())} DC Hub facilities for matching")
            except Exception as e:
                logger.warning(f"Could not load DC Hub facilities for matching: {e}")

            for cfac in carrierfacs:
                try:
                    carrier_id = str(cfac.get('carrier_id') or (cfac.get('carrier', {}).get('id') if isinstance(cfac.get('carrier'), dict) else cfac.get('carrier_id', '')))
                    fac_id = str(cfac.get('fac_id') or (cfac.get('facility', {}).get('id') if isinstance(cfac.get('facility'), dict) else cfac.get('fac_id', '')))

                    if not carrier_id or not fac_id:
                        continue

                    # Enrich from lookups (try both string and int keys)
                    carrier_name = carrier_lookup.get(carrier_id, carrier_lookup.get(int(carrier_id), '')) if carrier_id.isdigit() else carrier_lookup.get(carrier_id, '')
                    fac_info = fac_lookup.get(fac_id, fac_lookup.get(int(fac_id), {})) if fac_id.isdigit() else fac_lookup.get(fac_id, {})
                    fac_name = fac_info.get('name', '')
                    fac_city = fac_info.get('city', '')
                    fac_state = fac_info.get('state', '')
                    fac_country = fac_info.get('country', '')
                    fac_lat = fac_info.get('latitude')
                    fac_lng = fac_info.get('longitude')

                    # Try to match to DC Hub facility
                    dchub_id = None
                    city_key = (fac_city.lower().strip(), fac_state.lower().strip())
                    candidates = dchub_fac_lookup.get(city_key, [])
                    if candidates and fac_lat and fac_lng:
                        # Find closest by distance
                        best = None
                        best_dist = 999999
                        for cand in candidates:
                            if cand['lat'] and cand['lng']:
                                dist = _haversine_km(float(fac_lat), float(fac_lng), cand['lat'], cand['lng'])
                                if dist < best_dist:
                                    best_dist = dist
                                    best = cand
                        # Match if within 2km
                        if best and best_dist < 2.0:
                            dchub_id = best['id']
                            matched += 1

                    c.execute("""
                        INSERT INTO carrier_facility_presence
                            (carrier_pdb_id, carrier_name, facility_pdb_id, facility_name,
                             facility_city, facility_state, facility_country,
                             facility_lat, facility_lng, dchub_facility_id, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (carrier_pdb_id, facility_pdb_id) DO UPDATE SET
                            carrier_name = EXCLUDED.carrier_name,
                            facility_name = EXCLUDED.facility_name,
                            facility_city = EXCLUDED.facility_city,
                            facility_state = EXCLUDED.facility_state,
                            facility_country = EXCLUDED.facility_country,
                            facility_lat = EXCLUDED.facility_lat,
                            facility_lng = EXCLUDED.facility_lng,
                            dchub_facility_id = EXCLUDED.dchub_facility_id,
                            updated_at = NOW()
                    """, (carrier_id, carrier_name, fac_id, fac_name,
                          fac_city, fac_state, fac_country,
                          fac_lat, fac_lng, dchub_id))

                    upserted += 1

                except Exception as e:
                    errors += 1
                    if errors < 5:
                        logger.warning(f"CarrierFac upsert error: {e}")

            conn.commit()
            logger.info(f"✅ Carrier-Facility: {upserted} upserted, {matched} matched to DC Hub, {errors} errors")

        except Exception as e:
            logger.error(f"Carrier-facility ingestion failed: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            if conn:
                try:

        return {
            'success': True,
            'source': 'PeeringDB',
            'total_fetched': len(carrierfacs),
            'upserted': upserted,
            'matched_to_dchub': matched,
            'errors': errors,
        }


        # ─────────────────────────────────────────────────────────────
        # FIBER COVERAGE ZONE BUILDER
        # ─────────────────────────────────────────────────────────────
    finally:
        conn.close()
def build_fiber_coverage_zones(get_db):
    """
    Aggregate carrier-facility data + fiber routes into metro coverage zones.
    Creates summary records showing fiber density per market.
    """
    conn = None
    zones_created = 0

    try:
        conn = get_db()
    try:
            c = conn.cursor()

            # Group carrier-facility presence by city/state
            c.execute("""
                SELECT facility_city, facility_state, facility_country,
                       AVG(facility_lat) as center_lat,
                       AVG(facility_lng) as center_lng,
                       COUNT(DISTINCT carrier_pdb_id) as carrier_count,
                       COUNT(DISTINCT facility_pdb_id) as facility_count,
                       STRING_AGG(DISTINCT carrier_name, ', ' ORDER BY carrier_name) as carriers
                FROM carrier_facility_presence
                WHERE facility_city IS NOT NULL AND facility_city != ''
                  AND facility_lat IS NOT NULL
                GROUP BY facility_city, facility_state, facility_country
                HAVING COUNT(DISTINCT carrier_pdb_id) >= 2
                ORDER BY carrier_count DESC
            """)

            zones = c.fetchall()
            logger.info(f"📊 Found {len(zones)} metro zones with 2+ carriers")

            for zone in zones:
                try:
                    city, state, country = zone[0], zone[1], zone[2]
                    center_lat, center_lng = zone[3], zone[4]
                    carrier_count, facility_count = zone[5], zone[6]
                    carrier_list = zone[7]

                    zone_id = f"{city}-{state}-{country}".lower().replace(' ', '-').replace(',', '')
                    zone_name = f"{city}, {state}" if state else city

                    # Count fiber routes in this zone (within ~50km)
                    fiber_count = 0
                    try:
                        if center_lat and center_lng:
                            deg_radius = 50 / 111.0  # ~50km
                            c.execute("""
                                SELECT COUNT(*) FROM fiber_route_geometry
                                WHERE from_lat BETWEEN %s AND %s
                                  AND from_lng BETWEEN %s AND %s
                            """, (center_lat - deg_radius, center_lat + deg_radius,
                                  center_lng - deg_radius, center_lng + deg_radius))
                            fiber_count = c.fetchone()[0]
                    except Exception:
                        pass

                    c.execute("""
                        INSERT INTO fiber_coverage_zones
                            (zone_id, zone_name, zone_type, state, country,
                             center_lat, center_lng, provider_count,
                             lit_building_count, fiber_route_count,
                             carrier_list, data_sources, updated_at)
                        VALUES (%s, %s, 'metro', %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON CONFLICT (zone_id) DO UPDATE SET
                            zone_name = EXCLUDED.zone_name,
                            center_lat = EXCLUDED.center_lat,
                            center_lng = EXCLUDED.center_lng,
                            provider_count = EXCLUDED.provider_count,
                            lit_building_count = EXCLUDED.lit_building_count,
                            fiber_route_count = EXCLUDED.fiber_route_count,
                            carrier_list = EXCLUDED.carrier_list,
                            data_sources = EXCLUDED.data_sources,
                            updated_at = NOW()
                    """, (zone_id, zone_name, state, country or 'US',
                          center_lat, center_lng, carrier_count,
                          facility_count, fiber_count,
                          carrier_list, 'PeeringDB, FCC BDC'))

                    zones_created += 1

                except Exception as e:
                    logger.warning(f"Zone creation error: {e}")

            conn.commit()
            logger.info(f"✅ Fiber coverage zones: {zones_created} created/updated")

        except Exception as e:
            logger.error(f"Coverage zone build failed: {e}")
            return {'success': False, 'error': str(e)}
        finally:
            if conn:
                try:

        return {
            'success': True,
            'zones_created': zones_created,
        }


        # ─────────────────────────────────────────────────────────────
        # MAIN SYNC FUNCTION
        # ─────────────────────────────────────────────────────────────
    finally:
        conn.close()
def run_carrier_sync(get_db):
    """Full sync: carriers + carrier-facilities + coverage zones."""
    results = {
        'source': 'PeeringDB Carrier Intelligence',
        'timestamp': datetime.utcnow().isoformat(),
    }

    # Init tables
    init_carrier_tables(get_db)

    # 1. Carrier profiles
    carrier_result = ingest_carriers(get_db)
    results['carriers'] = carrier_result

    # 2. Carrier ↔ facility cross-ref
    cfac_result = ingest_carrier_facilities(get_db)
    results['carrier_facilities'] = cfac_result

    # 3. Build coverage zones from aggregated data
    zone_result = build_fiber_coverage_zones(get_db)
    results['coverage_zones'] = zone_result

    results['success'] = all([
        carrier_result.get('success', False),
        cfac_result.get('success', False),
        zone_result.get('success', False),
    ])

    total = (carrier_result.get('upserted', 0) +
             cfac_result.get('upserted', 0) +
             zone_result.get('zones_created', 0))
    results['total_records'] = total

    logger.info(f"🔗 Carrier sync complete: {total} total records")
    return results


# ─────────────────────────────────────────────────────────────
# API ENDPOINTS (register with Flask app)
# ─────────────────────────────────────────────────────────────
def register_carrier_routes(app, get_db):
    """Register carrier & fiber API routes with the Flask app."""
    from flask import jsonify, request

    @app.route('/api/v1/carriers', methods=['GET'])
    def carriers_api():
        """List carrier profiles. Optional: search, status."""
        conn = None
        try:
            conn = get_db()
    try:
                c = conn.cursor()

                query = "SELECT pdb_id, name, aka, website, org_name, status FROM carrier_profiles WHERE 1=1"
                params = []

                search = request.args.get('search', '')
                if search:
                    query += " AND (LOWER(name) LIKE %s OR LOWER(aka) LIKE %s OR LOWER(org_name) LIKE %s)"
                    s = f'%{search.lower()}%'
                    params.extend([s, s, s])

                status = request.args.get('status', '')
                if status:
                    query += " AND status = %s"
                    params.append(status)

                query += " ORDER BY name"
                limit = min(request.args.get('limit', 100, type=int), 500)
                query += f" LIMIT {limit}"

                c.execute(query, params)
                carriers = []
                for row in c.fetchall():
                    carriers.append({
                        'pdb_id': row[0], 'name': row[1], 'aka': row[2],
                        'website': row[3], 'org_name': row[4], 'status': row[5],
                    })

                c.execute("SELECT COUNT(*) FROM carrier_profiles")
                total = c.fetchone()[0]

                return jsonify({
                    'success': True,
                    'carriers': carriers,
                    'total': total,
                    'returned': len(carriers),
                    'source': 'PeeringDB via DC Hub Intelligence',
                })
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
            finally:
                if conn:
                    try:

        @app.route('/api/v1/carriers/<int:carrier_id>/facilities', methods=['GET'])
        def carrier_facilities_api(carrier_id):
            """Get all facilities where a specific carrier is present."""
            conn = None
            try:
                conn = get_db()
                c = conn.cursor()

                c.execute("""
                    SELECT facility_pdb_id, facility_name, facility_city, facility_state,
                           facility_country, facility_lat, facility_lng, dchub_facility_id
                    FROM carrier_facility_presence
                    WHERE carrier_pdb_id = %s
                    ORDER BY facility_country, facility_state, facility_city
                """, (carrier_id,))

                facilities = []
                for row in c.fetchall():
                    fac = {
                        'pdb_id': row[0], 'name': row[1], 'city': row[2],
                        'state': row[3], 'country': row[4],
                        'lat': row[5], 'lng': row[6],
                    }
                    if row[7]:
                        fac['dchub_facility_id'] = row[7]
                        fac['dchub_url'] = f"/facility/{row[7]}"
                    facilities.append(fac)

                # Get carrier name
                c.execute("SELECT name FROM carrier_profiles WHERE pdb_id = %s", (carrier_id,))
                name_row = c.fetchone()

                return jsonify({
                    'success': True,
                    'carrier_id': carrier_id,
                    'carrier_name': name_row[0] if name_row else 'Unknown',
                    'facilities': facilities,
                    'total': len(facilities),
                })
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

        @app.route('/api/v1/facility/<int:facility_id>/carriers', methods=['GET'])
        def facility_carriers_api(facility_id):
            """Get all carriers present at a specific DC Hub facility."""
            conn = None
            try:
                conn = get_db()
                c = conn.cursor()

                c.execute("""
                    SELECT carrier_pdb_id, carrier_name
                    FROM carrier_facility_presence
                    WHERE dchub_facility_id = %s
                    ORDER BY carrier_name
                """, (facility_id,))

                carriers = []
                for row in c.fetchall():
                    carriers.append({'pdb_id': row[0], 'name': row[1]})

                return jsonify({
                    'success': True,
                    'facility_id': facility_id,
                    'carriers': carriers,
                    'carrier_count': len(carriers),
                    'connectivity_rating': (
                        'Excellent' if len(carriers) >= 10 else
                        'Good' if len(carriers) >= 5 else
                        'Moderate' if len(carriers) >= 2 else
                        'Limited'
                    ),
                })
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

        @app.route('/api/v1/fiber/coverage', methods=['GET'])
        def fiber_coverage_api():
            """Get fiber coverage zones. Optional: state, min_carriers."""
            conn = None
            try:
                conn = get_db()
                c = conn.cursor()

                query = """SELECT zone_id, zone_name, state, country, center_lat, center_lng,
                                  provider_count, lit_building_count, fiber_route_count,
                                  dark_fiber_available, carrier_list
                           FROM fiber_coverage_zones WHERE 1=1"""
                params = []

                state = request.args.get('state', '')
                if state:
                    query += " AND LOWER(state) = %s"
                    params.append(state.lower())

                min_carriers = request.args.get('min_carriers', type=int)
                if min_carriers:
                    query += " AND provider_count >= %s"
                    params.append(min_carriers)

                query += " ORDER BY provider_count DESC, zone_name"
                limit = min(request.args.get('limit', 100, type=int), 500)
                query += f" LIMIT {limit}"

                c.execute(query, params)
                zones = []
                for row in c.fetchall():
                    zones.append({
                        'zone_id': row[0], 'zone_name': row[1], 'state': row[2],
                        'country': row[3], 'center_lat': row[4], 'center_lng': row[5],
                        'carrier_count': row[6], 'facility_count': row[7],
                        'fiber_route_count': row[8], 'dark_fiber_available': row[9],
                        'carriers': row[10],
                    })

                c.execute("SELECT COUNT(*) FROM fiber_coverage_zones")
                total = c.fetchone()[0]

                return jsonify({
                    'success': True,
                    'coverage_zones': zones,
                    'total': total,
                    'returned': len(zones),
                    'source': 'PeeringDB + FCC BDC via DC Hub Intelligence',
                })
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

        @app.route('/api/v1/fiber/nearby', methods=['GET'])
        def fiber_nearby_api():
            """Find fiber coverage and carriers near a lat/lng. For site selection fiber scoring."""
            lat = request.args.get('lat', type=float)
            lng = request.args.get('lng', type=float)
            radius_km = min(request.args.get('radius_km', 50, type=float), 200)

            if lat is None or lng is None:
                return jsonify({'error': 'lat and lng required'}), 400

            conn = None
            try:
                conn = get_db()
                c = conn.cursor()

                deg_radius = radius_km / 111.0

                # Nearby carrier presence
                c.execute("""
                    SELECT DISTINCT carrier_name, carrier_pdb_id,
                           facility_name, facility_city,
                           facility_lat, facility_lng
                    FROM carrier_facility_presence
                    WHERE facility_lat BETWEEN %s AND %s
                      AND facility_lng BETWEEN %s AND %s
                    ORDER BY carrier_name
                """, (lat - deg_radius, lat + deg_radius,
                      lng - deg_radius, lng + deg_radius))

                carriers_nearby = []
                seen_carriers = set()
                for row in c.fetchall():
                    if row[1] not in seen_carriers:
                        carriers_nearby.append({
                            'name': row[0], 'pdb_id': row[1],
                            'nearest_facility': row[2], 'facility_city': row[3],
                        })
                        seen_carriers.add(row[1])

                # Nearby fiber coverage zone
                c.execute("""
                    SELECT zone_name, provider_count, fiber_route_count,
                           dark_fiber_available, carrier_list,
                           center_lat, center_lng
                    FROM fiber_coverage_zones
                    WHERE center_lat BETWEEN %s AND %s
                      AND center_lng BETWEEN %s AND %s
                    ORDER BY provider_count DESC
                    LIMIT 5
                """, (lat - deg_radius, lat + deg_radius,
                      lng - deg_radius, lng + deg_radius))

                zones_nearby = []
                for row in c.fetchall():
                    zones_nearby.append({
                        'zone': row[0], 'carrier_count': row[1],
                        'fiber_routes': row[2], 'dark_fiber': row[3],
                        'carriers': row[4],
                    })

                # Fiber connectivity score (0-100)
                score = min(100, (
                    len(carriers_nearby) * 5 +
                    sum(z['carrier_count'] for z in zones_nearby) * 2 +
                    sum(z['fiber_routes'] for z in zones_nearby) * 1 +
                    (20 if any(z['dark_fiber'] for z in zones_nearby) else 0)
                ))

                return jsonify({
                    'success': True,
                    'query': {'lat': lat, 'lng': lng, 'radius_km': radius_km},
                    'fiber_score': score,
                    'fiber_rating': (
                        'Excellent' if score >= 80 else
                        'Good' if score >= 50 else
                        'Moderate' if score >= 25 else
                        'Limited'
                    ),
                    'carriers_nearby': carriers_nearby,
                    'carrier_count': len(carriers_nearby),
                    'coverage_zones': zones_nearby,
                    'source': 'PeeringDB + FCC BDC via DC Hub Intelligence',
                })
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)})
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

        logger.info("🔗 Carrier & fiber routes registered: /api/v1/carriers, /api/v1/fiber/coverage, /api/v1/fiber/nearby")
    finally:
        conn.close()
