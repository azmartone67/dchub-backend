"""
Fiber Network Discovery Module v2.2
====================================
- Seeds Neon fiber_routes table with major carrier routes
- Discovers fiber networks from PeeringDB IX data
- NTIA BEAD Allocations ($42.45B)
- Carrier Hotels & Lit Buildings
- Fiber Provider Coverage APIs

v2.2 CHANGES:
  - Added US_CITY_COORDS geocoding fallback for PeeringDB (API no longer returns lat/lng)
  - Fixed ON CONFLICT to use (name, provider) unique constraint
  - Cleaned up dead code in _get_pg_connection
  - PeeringDB discovery now resolves IX city names to coordinates
"""

import requests
import logging
import os
import time
import math
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
import json

logger = logging.getLogger(__name__)

fiber_bp = Blueprint('fiber_discovery', __name__)


# ============================================================
# US CITY COORDINATES — fallback geocoder for PeeringDB IXes
# ============================================================
# PeeringDB stopped returning lat/lng in their API as of early 2026.
# This table maps common US IX cities to coordinates so route discovery
# still works. Covers all major DC/IX markets.

US_CITY_COORDS = {
    # Major DC markets
    'ashburn': (38.95, -77.45), 'reston': (38.96, -77.34),
    'new york': (40.71, -74.01), 'manhattan': (40.71, -74.01),
    'newark': (40.74, -74.17), 'secaucus': (40.79, -74.06),
    'chicago': (41.88, -87.63), 'elk grove village': (42.00, -87.97),
    'dallas': (32.78, -96.80), 'richardson': (32.95, -96.73),
    'houston': (29.76, -95.37), 'san antonio': (29.42, -98.49),
    'austin': (30.27, -97.74),
    'phoenix': (33.45, -112.07), 'mesa': (33.42, -111.83),
    'los angeles': (34.05, -118.24), 'el segundo': (33.92, -118.41),
    'san francisco': (37.77, -122.42), 'san jose': (37.34, -121.89),
    'santa clara': (37.35, -121.95), 'palo alto': (37.44, -122.14),
    'fremont': (37.55, -121.99), 'sacramento': (38.58, -121.49),
    'seattle': (47.61, -122.33), 'portland': (45.52, -122.68),
    'denver': (39.74, -104.99), 'boulder': (40.01, -105.27),
    'atlanta': (33.75, -84.39), 'suwanee': (34.05, -84.07),
    'miami': (25.76, -80.19), 'fort lauderdale': (26.12, -80.14),
    'jacksonville': (30.33, -81.66), 'tampa': (27.95, -82.46),
    'orlando': (28.54, -81.38),
    'boston': (42.36, -71.06), 'cambridge': (42.37, -71.11),
    'philadelphia': (39.95, -75.17),
    'baltimore': (39.29, -76.61),
    'washington': (38.91, -77.04),
    'charlotte': (35.23, -80.84),
    'raleigh': (35.78, -78.64), 'durham': (35.99, -78.90),
    'richmond': (37.54, -77.44),
    'nashville': (36.16, -86.78),
    'memphis': (35.15, -90.05),
    'louisville': (38.25, -85.76),
    'indianapolis': (39.77, -86.16),
    'columbus': (39.96, -82.99),
    'cleveland': (41.50, -81.69),
    'detroit': (42.33, -83.05), 'ann arbor': (42.28, -83.74),
    'pittsburgh': (40.44, -80.00),
    'minneapolis': (44.98, -93.27), 'st. paul': (44.95, -93.09),
    'kansas city': (39.10, -94.58),
    'st. louis': (38.63, -90.20),
    'des moines': (41.59, -93.62),
    'omaha': (41.26, -95.94),
    'salt lake city': (40.76, -111.89),
    'las vegas': (36.17, -115.14),
    'reno': (39.53, -119.81),
    'boise': (43.62, -116.21),
    'albuquerque': (35.08, -106.65),
    'tucson': (32.22, -110.93),
    'el paso': (31.76, -106.44),
    'birmingham': (33.52, -86.80),
    'new orleans': (29.95, -90.07),
    'little rock': (34.75, -92.29),
    'oklahoma city': (35.47, -97.52),
    'tulsa': (36.15, -95.99),
    'milwaukee': (43.04, -87.91),
    'madison': (43.07, -89.40),
    'hartford': (41.76, -72.68),
    'providence': (41.82, -71.41),
    'buffalo': (42.89, -78.88),
    'albany': (42.65, -73.76),
    'syracuse': (43.05, -76.15),
    'rochester': (43.16, -77.61),
    'manchester': (43.00, -71.45),
    'portland me': (43.66, -70.26),
    'charleston': (32.78, -79.93),
    'columbia': (34.00, -81.03),
    'greenville': (34.85, -82.40),
    'knoxville': (35.96, -83.92),
    'chattanooga': (35.05, -85.31),
    'norfolk': (36.85, -76.29), 'virginia beach': (36.85, -75.98),
    'spokane': (47.66, -117.43),
    'tacoma': (47.25, -122.44),
    'vancouver': (45.63, -122.67),
    'san diego': (32.72, -117.16),
    'honolulu': (21.31, -157.86),
    'anchorage': (61.22, -149.90),
    'fargo': (46.88, -96.79),
    'sioux falls': (43.55, -96.73),
    'wichita': (37.69, -97.34),
    'springfield': (37.22, -93.29),
    'lexington': (38.04, -84.50),
    'grand rapids': (42.96, -85.66),
    'jacksonville fl': (30.33, -81.66),
    'dayton': (39.76, -84.19),
    'cincinnati': (39.10, -84.51),
    'akron': (41.08, -81.52),
    'toledo': (41.65, -83.54),
    'harrisburg': (40.27, -76.88),
    'scranton': (41.41, -75.66),
    'trenton': (40.22, -74.76),
    'wilmington': (39.74, -75.55),
    'stamford': (41.05, -73.54),
    'bridgeport': (41.18, -73.20),
    'new haven': (41.31, -72.92),
    'worcester': (42.26, -71.80),
    'springfield ma': (42.10, -72.59),
}


def _geocode_city(city_name):
    """Look up coordinates for a US city name. Returns (lat, lng) or None."""
    if not city_name:
        return None
    city_lower = city_name.strip().lower()
    # Direct lookup
    if city_lower in US_CITY_COORDS:
        return US_CITY_COORDS[city_lower]
    # Try partial match (e.g. "Ashburn, VA" → "ashburn")
    for key, coords in US_CITY_COORDS.items():
        if key in city_lower or city_lower.startswith(key):
            return coords
    return None


# ============================================================
# NEON DATABASE HELPERS
# ============================================================

def _get_pg_connection():
    """Get a Neon PostgreSQL connection — always uses db_utils pool (Neon)."""
    try:
        from db_utils import get_db
        return get_db()
    except Exception as e:
        logger.error('Fiber discovery DB connection failed: %s' % e)
        return None


def _ensure_fiber_routes_table():
    """Verify fiber_routes table exists in Neon."""
    conn = _get_pg_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM fiber_routes")
        cur.fetchone()
        cur.close()
        conn.close()
        return True
    except Exception as e:
        logger.error("Fiber routes table check failed: %s" % e)
        try:
            conn.close()
        except Exception:
            pass
        return False


def _upsert_fiber_route(conn, route):
    """Upsert a single fiber route into Neon.
    
    Uses ON CONFLICT (name, provider) to match the fiber_routes_unique_key constraint.
    """
    try:
        cur = conn.cursor()
        dark = route.get('dark_fiber', 0)
        rtype = route.get('route_type', 'long_haul')
        if dark:
            rtype = 'dark_fiber' if rtype == 'long_haul' else rtype

        cur.execute("""
            INSERT INTO fiber_routes
                (name, provider, route_type, start_location, end_location,
                 distance_miles, fiber_count, status, start_lat, start_lng,
                 end_lat, end_lng, source, source_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name, provider) DO UPDATE SET
                distance_miles = EXCLUDED.distance_miles,
                fiber_count = EXCLUDED.fiber_count,
                route_type = EXCLUDED.route_type,
                start_lat = EXCLUDED.start_lat,
                start_lng = EXCLUDED.start_lng,
                end_lat = EXCLUDED.end_lat,
                end_lng = EXCLUDED.end_lng,
                source = EXCLUDED.source,
                source_id = EXCLUDED.source_id,
                updated_at = NOW()
        """, (
            route.get('name', ''),
            route.get('provider', ''),
            rtype,
            route.get('start_location', ''),
            route.get('end_location', ''),
            route.get('route_miles'),
            route.get('fiber_count'),
            'active',
            route.get('start_lat'),
            route.get('start_lng'),
            route.get('end_lat'),
            route.get('end_lng'),
            route.get('source', 'seed'),
            route.get('source_id', ''),
        ))
        return True
    except Exception as e:
        logger.warning("Fiber route upsert failed: %s" % e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False


# ============================================================
# SEED DATA — 20 Major Carrier Routes
# ============================================================

MAJOR_ROUTES = [
    {'source_id': 'ZAYO-NOVA-DAL', 'provider': 'Zayo', 'name': 'Northern Virginia - Dallas', 'start_location': 'Ashburn, VA', 'end_location': 'Dallas, TX', 'route_miles': 1350, 'fiber_count': 288, 'dark_fiber': 1, 'start_lat': 38.95, 'start_lng': -77.45, 'end_lat': 32.78, 'end_lng': -96.80},
    {'source_id': 'ZAYO-CHI-NOVA', 'provider': 'Zayo', 'name': 'Chicago - Northern Virginia', 'start_location': 'Chicago, IL', 'end_location': 'Ashburn, VA', 'route_miles': 710, 'fiber_count': 432, 'dark_fiber': 1, 'start_lat': 41.88, 'start_lng': -87.63, 'end_lat': 38.95, 'end_lng': -77.45},
    {'source_id': 'ZAYO-NOVA-ATL', 'provider': 'Zayo', 'name': 'Northern Virginia - Atlanta', 'start_location': 'Ashburn, VA', 'end_location': 'Atlanta, GA', 'route_miles': 640, 'fiber_count': 432, 'dark_fiber': 1, 'start_lat': 38.95, 'start_lng': -77.45, 'end_lat': 33.75, 'end_lng': -84.39},
    {'source_id': 'ZAYO-DAL-PHX', 'provider': 'Zayo', 'name': 'Dallas - Phoenix', 'start_location': 'Dallas, TX', 'end_location': 'Phoenix, AZ', 'route_miles': 1065, 'fiber_count': 288, 'dark_fiber': 1, 'start_lat': 32.78, 'start_lng': -96.80, 'end_lat': 33.45, 'end_lng': -112.07},
    {'source_id': 'ZAYO-COL-CHI', 'provider': 'Zayo', 'name': 'Columbus - Chicago', 'start_location': 'Columbus, OH', 'end_location': 'Chicago, IL', 'route_miles': 350, 'fiber_count': 432, 'dark_fiber': 1, 'start_lat': 39.96, 'start_lng': -82.99, 'end_lat': 41.88, 'end_lng': -87.63},
    {'source_id': 'LUMEN-LA-PHX', 'provider': 'Lumen', 'name': 'Los Angeles - Phoenix', 'start_location': 'Los Angeles, CA', 'end_location': 'Phoenix, AZ', 'route_miles': 380, 'fiber_count': 576, 'dark_fiber': 1, 'start_lat': 34.05, 'start_lng': -118.24, 'end_lat': 33.45, 'end_lng': -112.07},
    {'source_id': 'LUMEN-DAL-DEN', 'provider': 'Lumen', 'name': 'Dallas - Denver', 'start_location': 'Dallas, TX', 'end_location': 'Denver, CO', 'route_miles': 780, 'fiber_count': 288, 'dark_fiber': 1, 'start_lat': 32.78, 'start_lng': -96.80, 'end_lat': 39.74, 'end_lng': -104.99},
    {'source_id': 'LUMEN-NOVA-COL', 'provider': 'Lumen', 'name': 'Northern Virginia - Columbus', 'start_location': 'Ashburn, VA', 'end_location': 'Columbus, OH', 'route_miles': 420, 'fiber_count': 432, 'dark_fiber': 1, 'start_lat': 38.95, 'start_lng': -77.45, 'end_lat': 39.96, 'end_lng': -82.99},
    {'source_id': 'LUMEN-CHI-DSM', 'provider': 'Lumen', 'name': 'Chicago - Des Moines', 'start_location': 'Chicago, IL', 'end_location': 'Des Moines, IA', 'route_miles': 340, 'fiber_count': 288, 'dark_fiber': 1, 'start_lat': 41.88, 'start_lng': -87.63, 'end_lat': 41.59, 'end_lng': -93.62},
    {'source_id': 'LUMEN-DEN-SLC', 'provider': 'Lumen', 'name': 'Denver - Salt Lake City', 'start_location': 'Denver, CO', 'end_location': 'Salt Lake City, UT', 'route_miles': 525, 'fiber_count': 288, 'dark_fiber': 1, 'start_lat': 39.74, 'start_lng': -104.99, 'end_lat': 40.76, 'end_lng': -111.89},
    {'source_id': 'LUMEN-SLC-LV', 'provider': 'Lumen', 'name': 'Salt Lake City - Las Vegas', 'start_location': 'Salt Lake City, UT', 'end_location': 'Las Vegas, NV', 'route_miles': 420, 'fiber_count': 288, 'dark_fiber': 1, 'start_lat': 40.76, 'start_lng': -111.89, 'end_lat': 36.17, 'end_lng': -115.14},
    {'source_id': 'LUMEN-LV-PHX', 'provider': 'Lumen', 'name': 'Las Vegas - Phoenix', 'start_location': 'Las Vegas, NV', 'end_location': 'Phoenix, AZ', 'route_miles': 300, 'fiber_count': 432, 'dark_fiber': 1, 'start_lat': 36.17, 'start_lng': -115.14, 'end_lat': 33.45, 'end_lng': -112.07},
    {'source_id': 'CC-HOU-DAL', 'provider': 'Crown Castle', 'name': 'Houston - Dallas', 'start_location': 'Houston, TX', 'end_location': 'Dallas, TX', 'route_miles': 245, 'fiber_count': 432, 'dark_fiber': 1, 'start_lat': 29.76, 'start_lng': -95.37, 'end_lat': 32.78, 'end_lng': -96.80},
    {'source_id': 'CC-PHX-LV', 'provider': 'Crown Castle', 'name': 'Phoenix - Las Vegas', 'start_location': 'Phoenix, AZ', 'end_location': 'Las Vegas, NV', 'route_miles': 300, 'fiber_count': 288, 'dark_fiber': 1, 'start_lat': 33.45, 'start_lng': -112.07, 'end_lat': 36.17, 'end_lng': -115.14},
    {'source_id': 'UNITI-ATL-JAX', 'provider': 'Uniti', 'name': 'Atlanta - Jacksonville', 'start_location': 'Atlanta, GA', 'end_location': 'Jacksonville, FL', 'route_miles': 350, 'fiber_count': 144, 'dark_fiber': 1, 'start_lat': 33.75, 'start_lng': -84.39, 'end_lat': 30.33, 'end_lng': -81.66},
    {'source_id': 'UNITI-ATL-DAL', 'provider': 'Uniti', 'name': 'Atlanta - Dallas', 'start_location': 'Atlanta, GA', 'end_location': 'Dallas, TX', 'route_miles': 780, 'fiber_count': 288, 'dark_fiber': 1, 'start_lat': 33.75, 'start_lng': -84.39, 'end_lat': 32.78, 'end_lng': -96.80},
    {'source_id': 'SEGRA-NOVA-CLT', 'provider': 'Segra', 'name': 'Northern Virginia - Charlotte', 'start_location': 'Ashburn, VA', 'end_location': 'Charlotte, NC', 'route_miles': 330, 'fiber_count': 288, 'dark_fiber': 1, 'start_lat': 38.95, 'start_lng': -77.45, 'end_lat': 35.23, 'end_lng': -80.84},
    {'source_id': 'COGENT-NOVA-CHI', 'provider': 'Cogent', 'name': 'Northern Virginia - Chicago', 'start_location': 'Ashburn, VA', 'end_location': 'Chicago, IL', 'route_miles': 710, 'fiber_count': 576, 'dark_fiber': 0, 'start_lat': 38.95, 'start_lng': -77.45, 'end_lat': 41.88, 'end_lng': -87.63},
    {'source_id': 'WINDSTREAM-DAL-ATL', 'provider': 'Windstream', 'name': 'Dallas - Atlanta', 'start_location': 'Dallas, TX', 'end_location': 'Atlanta, GA', 'route_miles': 780, 'fiber_count': 288, 'dark_fiber': 1, 'start_lat': 32.78, 'start_lng': -96.80, 'end_lat': 33.75, 'end_lng': -84.39},
    {'source_id': 'WINDSTREAM-DEN-DSM', 'provider': 'Windstream', 'name': 'Denver - Des Moines', 'start_location': 'Denver, CO', 'end_location': 'Des Moines, IA', 'route_miles': 660, 'fiber_count': 144, 'dark_fiber': 1, 'start_lat': 39.74, 'start_lng': -104.99, 'end_lat': 41.59, 'end_lng': -93.62},
]


# ============================================================
# PeeringDB FIBER DISCOVERY (with city geocoding fallback)
# ============================================================

def _discover_peeringdb_fiber():
    """
    Discover fiber routes from PeeringDB Internet Exchange data.
    Each IX represents a fiber interconnection point — we create
    routes between IXes in the same region as proxy fiber paths.
    
    v2.2: PeeringDB no longer returns lat/lng coordinates.
    Now uses city name geocoding fallback via US_CITY_COORDS.
    """
    discovered = []
    try:
        # Get US Internet Exchanges from PeeringDB
        resp = requests.get(
            "https://www.peeringdb.com/api/ix?country=US&status=ok",
            headers={"User-Agent": "DCHub/2.0 (dchub.cloud)"},
            timeout=15
        )
        if resp.status_code != 200:
            logger.warning("PeeringDB returned %s" % resp.status_code)
            return discovered

        data = resp.json().get("data", [])
        logger.info("PeeringDB: found %d US Internet Exchanges" % len(data))

        # Build IX list with coordinates (API lat/lng or city geocode fallback)
        ixes = []
        geocoded_count = 0
        for ix in data:
            lat = ix.get("latitude")
            lng = ix.get("longitude")
            city = ix.get("city", "")
            
            # Try API coordinates first
            if lat and lng and abs(float(lat)) > 0.1:
                lat, lng = float(lat), float(lng)
            else:
                # Fallback: geocode from city name
                coords = _geocode_city(city)
                if coords:
                    lat, lng = coords
                    geocoded_count += 1
                else:
                    continue  # Skip IXes we can't locate
            
            ixes.append({
                "id": ix.get("id"),
                "name": ix.get("name", ""),
                "city": city,
                "state": ix.get("region_continent", ""),
                "lat": lat,
                "lng": lng,
                "net_count": ix.get("net_count", 0),
            })

        logger.info("PeeringDB: %d IXes with coordinates (%d geocoded from city name)" % (len(ixes), geocoded_count))

        # Create routes between IXes in nearby cities (50-500 miles apart)
        for i, ix1 in enumerate(ixes):
            for ix2 in ixes[i+1:]:
                # Skip IXes in the same city (same coordinates)
                if abs(ix1['lat'] - ix2['lat']) < 0.01 and abs(ix1['lng'] - ix2['lng']) < 0.01:
                    continue
                    
                # Haversine distance
                lat1, lon1 = math.radians(ix1["lat"]), math.radians(ix1["lng"])
                lat2, lon2 = math.radians(ix2["lat"]), math.radians(ix2["lng"])
                dlat = lat2 - lat1
                dlon = lon2 - lon1
                a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
                dist_miles = 3959 * 2 * math.asin(math.sqrt(a))

                # Only create routes between 50-500 miles (metro/regional fiber paths)
                if 50 < dist_miles < 500:
                    route_id = "PDB-%s-%s" % (ix1['id'], ix2['id'])
                    discovered.append({
                        'source_id': route_id,
                        'provider': 'PeeringDB/IX',
                        'name': "%s - %s" % (ix1['name'], ix2['name']),
                        'route_type': 'ix_interconnect',
                        'start_location': ix1.get('city', ix1['name']),
                        'end_location': ix2.get('city', ix2['name']),
                        'route_miles': round(dist_miles, 1),
                        'fiber_count': max(ix1['net_count'], ix2['net_count']),
                        'dark_fiber': 0,
                        'start_lat': ix1['lat'],
                        'start_lng': ix1['lng'],
                        'end_lat': ix2['lat'],
                        'end_lng': ix2['lng'],
                        'source': 'peeringdb',
                    })

        logger.info("PeeringDB: generated %d fiber route proxies from IX data" % len(discovered))

    except requests.exceptions.Timeout:
        logger.warning("PeeringDB: timeout")
    except Exception as e:
        logger.error("PeeringDB discovery failed: %s" % e)

    return discovered


# ============================================================
# RUN FIBER DISCOVERY (called by fiber-sync job)
# ============================================================

def run_fiber_discovery():
    """
    Main fiber discovery function — called by /api/jobs/fiber-sync.
    
    1. Ensures fiber_routes table exists
    2. Seeds 20 major carrier routes
    3. Discovers additional routes from PeeringDB
    4. Returns summary stats
    """
    start = time.time()
    results = {
        'seeded': 0,
        'discovered': 0,
        'errors': 0,
        'total': 0,
    }

    # Ensure table exists
    if not _ensure_fiber_routes_table():
        return {'status': 'error', 'message': 'Could not create/verify fiber_routes table'}

    conn = _get_pg_connection()
    if not conn:
        return {'status': 'error', 'message': 'Database connection failed'}

    try:
        # Step 1: Seed major carrier routes
        for route in MAJOR_ROUTES:
            route['source'] = 'seed'
            route['route_type'] = 'long_haul'
            if _upsert_fiber_route(conn, route):
                results['seeded'] += 1
                conn.commit()
            else:
                results['errors'] += 1

        logger.info("Fiber seed: %d carrier routes written" % results['seeded'])

        # Step 2: Discover from PeeringDB
        try:
            pdb_routes = _discover_peeringdb_fiber()
            for route in pdb_routes:
                if _upsert_fiber_route(conn, route):
                    results['discovered'] += 1
                    conn.commit()
                else:
                    results['errors'] += 1
            logger.info("PeeringDB fiber: %d routes discovered" % results['discovered'])
        except Exception as e:
            logger.warning("PeeringDB discovery failed (non-fatal): %s" % e)

        # Get total count
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM fiber_routes")
        results['total'] = cur.fetchone()[0]
        cur.close()

    except Exception as e:
        logger.error("Fiber discovery error: %s" % e)
        results['error'] = str(e)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    results['duration_seconds'] = round(time.time() - start, 2)
    results['status'] = 'success' if results['errors'] == 0 else 'partial'
    logger.info("Fiber discovery complete: %s" % results)
    return results


# ============================================================
# STATIC DATA CLASSES (unchanged from v1)
# ============================================================

class FiberProviderAPI:
    """Major fiber carrier data"""
    
    PROVIDERS = {
        'zayo': {'name': 'Zayo Group', 'type': 'Tier 1 Fiber', 'route_miles': 141000, 'lit_buildings': 12500, 'on_net_markets': 400, 'dark_fiber': True, 'coverage_states': ['All 48 Continental US', 'Canada', 'Europe'], 'dc_markets': ['Ashburn', 'Dallas', 'Chicago', 'Denver', 'Phoenix', 'Los Angeles', 'New York', 'Atlanta'], 'website': 'https://www.zayo.com'},
        'lumen': {'name': 'Lumen Technologies', 'type': 'Tier 1 Fiber', 'route_miles': 450000, 'lit_buildings': 25000, 'on_net_markets': 350, 'dark_fiber': True, 'coverage_states': ['All 50 US States', 'Global'], 'dc_markets': ['All Major US Markets'], 'website': 'https://www.lumen.com'},
        'crown_castle': {'name': 'Crown Castle Fiber', 'type': 'Metro Fiber', 'route_miles': 85000, 'lit_buildings': 35000, 'on_net_markets': 75, 'dark_fiber': True, 'coverage_states': ['Major US Metros'], 'dc_markets': ['Los Angeles', 'Houston', 'Philadelphia', 'Phoenix', 'Dallas'], 'website': 'https://www.crowncastle.com'},
        'cogent': {'name': 'Cogent Communications', 'type': 'Tier 1 Internet', 'route_miles': 62000, 'lit_buildings': 3200, 'on_net_markets': 210, 'dark_fiber': False, 'coverage_states': ['US', 'Europe', 'Canada'], 'dc_markets': ['All Major Carrier Hotels'], 'website': 'https://www.cogentco.com'},
        'uniti': {'name': 'Uniti Fiber', 'type': 'Regional Fiber', 'route_miles': 130000, 'lit_buildings': 8500, 'on_net_markets': 150, 'dark_fiber': True, 'coverage_states': ['Southeast US', 'Mid-Atlantic'], 'dc_markets': ['Atlanta', 'Jacksonville', 'New Orleans', 'Birmingham'], 'website': 'https://www.uniti.com'},
        'lightpath': {'name': 'Lightpath', 'type': 'Metro Fiber', 'route_miles': 20000, 'lit_buildings': 15000, 'on_net_markets': 1, 'dark_fiber': True, 'coverage_states': ['New York Metro'], 'dc_markets': ['New York', 'New Jersey'], 'website': 'https://www.lightpathfiber.com'},
        'windstream': {'name': 'Windstream Enterprise', 'type': 'Tier 2 Fiber', 'route_miles': 150000, 'lit_buildings': 12000, 'on_net_markets': 200, 'dark_fiber': True, 'coverage_states': ['Rural US', 'Mid-South'], 'dc_markets': ['Dallas', 'Atlanta', 'Denver'], 'website': 'https://www.windstream.com'},
        'segra': {'name': 'Segra', 'type': 'Regional Fiber', 'route_miles': 45000, 'lit_buildings': 6000, 'on_net_markets': 90, 'dark_fiber': True, 'coverage_states': ['Mid-Atlantic', 'Southeast'], 'dc_markets': ['Northern Virginia', 'Charlotte', 'Richmond'], 'website': 'https://www.segra.com'},
    }

    @classmethod
    def get_all_providers(cls):
        providers = [{'id': k, **v} for k, v in cls.PROVIDERS.items()]
        return {'providers': providers, 'count': len(providers), 'total_route_miles': sum(p['route_miles'] for p in providers), 'total_lit_buildings': sum(p['lit_buildings'] for p in providers)}

    @classmethod
    def get_provider(cls, provider_id):
        provider = cls.PROVIDERS.get(provider_id.lower())
        if provider:
            routes = [r for r in MAJOR_ROUTES if r['provider'].lower() == provider_id.lower()]
            return {'id': provider_id, **provider, 'routes': routes, 'route_count': len(routes)}
        return {'error': 'Provider not found: %s' % provider_id}

    @classmethod
    def get_providers_by_market(cls, market):
        market_lower = market.lower()
        matching = []
        for pid, p in cls.PROVIDERS.items():
            markets = p.get('dc_markets', [])
            if isinstance(markets, list):
                if any(market_lower in m.lower() for m in markets) or 'all' in ' '.join(markets).lower():
                    matching.append({'id': pid, **p})
        return {'market': market, 'providers': matching, 'count': len(matching)}

    @classmethod
    def get_routes(cls, provider=None):
        routes = MAJOR_ROUTES
        if provider:
            routes = [r for r in routes if r['provider'].lower() == provider.lower()]
        return {'routes': routes, 'count': len(routes), 'total_miles': sum(r['route_miles'] for r in routes)}


class FiberCoverageAPI:
    COVERAGE = {
        'Ashburn': {'provider_count': 25, 'lit_buildings': 450, 'dark_fiber_available': True, 'avg_latency_ms': 0.5, 'score': 100},
        'Dallas': {'provider_count': 18, 'lit_buildings': 320, 'dark_fiber_available': True, 'avg_latency_ms': 0.8, 'score': 95},
        'Chicago': {'provider_count': 20, 'lit_buildings': 380, 'dark_fiber_available': True, 'avg_latency_ms': 0.6, 'score': 97},
        'Phoenix': {'provider_count': 12, 'lit_buildings': 180, 'dark_fiber_available': True, 'avg_latency_ms': 1.2, 'score': 85},
        'Las Vegas': {'provider_count': 10, 'lit_buildings': 120, 'dark_fiber_available': True, 'avg_latency_ms': 1.5, 'score': 80},
        'Denver': {'provider_count': 14, 'lit_buildings': 200, 'dark_fiber_available': True, 'avg_latency_ms': 1.0, 'score': 88},
        'Atlanta': {'provider_count': 16, 'lit_buildings': 280, 'dark_fiber_available': True, 'avg_latency_ms': 0.9, 'score': 92},
        'Columbus': {'provider_count': 8, 'lit_buildings': 90, 'dark_fiber_available': True, 'avg_latency_ms': 1.8, 'score': 75},
        'Salt Lake City': {'provider_count': 9, 'lit_buildings': 100, 'dark_fiber_available': True, 'avg_latency_ms': 1.6, 'score': 78},
        'New York': {'provider_count': 30, 'lit_buildings': 520, 'dark_fiber_available': True, 'avg_latency_ms': 0.4, 'score': 100},
        'Los Angeles': {'provider_count': 22, 'lit_buildings': 400, 'dark_fiber_available': True, 'avg_latency_ms': 0.7, 'score': 96},
        'Seattle': {'provider_count': 15, 'lit_buildings': 220, 'dark_fiber_available': True, 'avg_latency_ms': 1.1, 'score': 87},
    }

    @classmethod
    def get_coverage(cls, market):
        coverage = cls.COVERAGE.get(market)
        if coverage:
            return {'market': market, **coverage}
        return {'error': 'Market not found: %s' % market, 'available': list(cls.COVERAGE.keys())}

    @classmethod
    def compare_markets(cls, markets=None):
        if not markets:
            markets = list(cls.COVERAGE.keys())
        results = [{'market': m, **cls.COVERAGE[m]} for m in markets if m in cls.COVERAGE]
        results.sort(key=lambda x: x['score'], reverse=True)
        return {'markets': results, 'best_connected': results[0] if results else None}


class LitBuildingAPI:
    CARRIER_HOTELS = [
        {'building_id': 'CH-60HUD', 'address': '60 Hudson Street', 'city': 'New York', 'state': 'NY', 'providers': ['Zayo', 'Lumen', 'Cogent', 'Telia', 'GTT'], 'provider_count': 45, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-111-8TH', 'address': '111 8th Avenue', 'city': 'New York', 'state': 'NY', 'providers': ['Zayo', 'Lumen', 'Cogent', 'Level3'], 'provider_count': 35, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-ONE-WILSHIRE', 'address': 'One Wilshire', 'city': 'Los Angeles', 'state': 'CA', 'providers': ['Zayo', 'Crown Castle', 'Lumen', 'NTT'], 'provider_count': 50, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-350-CERMAK', 'address': '350 E Cermak Road', 'city': 'Chicago', 'state': 'IL', 'providers': ['Zayo', 'Cogent', 'Lumen', 'PacketFabric'], 'provider_count': 40, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-WESTIN', 'address': '2001 6th Ave (Westin Building)', 'city': 'Seattle', 'state': 'WA', 'providers': ['Zayo', 'Lumen', 'Wave'], 'provider_count': 30, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-56MARIETTA', 'address': '56 Marietta Street', 'city': 'Atlanta', 'state': 'GA', 'providers': ['Zayo', 'Uniti', 'Lumen', 'AT&T'], 'provider_count': 28, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-2121MARKET', 'address': '2121 Market Street', 'city': 'Dallas', 'state': 'TX', 'providers': ['Zayo', 'Crown Castle', 'Lumen'], 'provider_count': 25, 'is_carrier_hotel': 1, 'is_datacenter': 1},
        {'building_id': 'CH-EQUINIX-ASH', 'address': '21715 Filigree Ct', 'city': 'Ashburn', 'state': 'VA', 'providers': ['Zayo', 'Segra', 'Lumen', 'Cogent', 'Crown Castle'], 'provider_count': 60, 'is_carrier_hotel': 1, 'is_datacenter': 1},
    ]

    @classmethod
    def get_carrier_hotels(cls, state=None, city=None):
        hotels = cls.CARRIER_HOTELS
        if state:
            hotels = [h for h in hotels if h['state'].upper() == state.upper()]
        if city:
            hotels = [h for h in hotels if city.lower() in h['city'].lower()]
        return {'carrier_hotels': hotels, 'count': len(hotels), 'total_providers': sum(h['provider_count'] for h in hotels)}


class NTIAGrantAPI:
    BEAD_ALLOCATIONS = {
        'AL': {'recipient': 'Alabama DECA', 'amount': 1401221901.77, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 218000},
        'AZ': {'recipient': 'Arizona Commerce Authority', 'amount': 993112231.37, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 153000},
        'CA': {'recipient': 'California PUC', 'amount': 1864136508.93, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 255000},
        'TX': {'recipient': 'Texas Broadband Office', 'amount': 3312616455.45, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 536000},
        'VA': {'recipient': 'Virginia DHCD', 'amount': 1481489572.87, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 229000},
        'GA': {'recipient': 'Georgia Technology Authority', 'amount': 1307214371.30, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 202000},
        'OH': {'recipient': 'BroadbandOhio', 'amount': 793688107.63, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 117000},
        'IL': {'recipient': 'Illinois Broadband Office', 'amount': 1040420751.50, 'status': 'Final Proposal Approved', 'priority': 'fiber', 'unserved_locations': 161000},
    }

    STATE_COORDS = {
        'AL': (32.8, -86.8), 'AZ': (34.0, -111.1), 'CA': (36.8, -119.4), 'TX': (31.0, -100.0),
        'VA': (37.4, -78.2), 'GA': (33.0, -83.5), 'OH': (40.4, -82.9), 'IL': (40.3, -89.0),
    }

    @classmethod
    def get_bead_allocations(cls, dc_market_states=None):
        filtered = {s: v for s, v in cls.BEAD_ALLOCATIONS.items() if not dc_market_states or s in dc_market_states}
        allocations = []
        for st, info in filtered.items():
            amt = info['amount']
            fmt = "$%.2fB" % (amt / 1e9) if amt >= 1e9 else "$%.1fM" % (amt / 1e6)
            allocations.append({
                'state': st, 'recipient': info['recipient'], 'amount': amt,
                'amount_formatted': fmt,
                'status': info['status'], 'priority_tech': info['priority'], 'unserved_locations': info['unserved_locations'],
                'lat': cls.STATE_COORDS.get(st, (0, 0))[0], 'lng': cls.STATE_COORDS.get(st, (0, 0))[1],
            })
        allocations.sort(key=lambda x: x['amount'], reverse=True)
        total = sum(a['amount'] for a in allocations)
        return {'allocations': allocations, 'count': len(allocations), 'total_funding': total, 'total_funding_formatted': "$%.2fB" % (total / 1e9), 'total_unserved': sum(a['unserved_locations'] for a in allocations)}

    @classmethod
    def get_grant_stats(cls):
        total = sum(v['amount'] for v in cls.BEAD_ALLOCATIONS.values())
        return {'total_grants': len(cls.BEAD_ALLOCATIONS), 'total_funding': total, 'program': 'BEAD'}


# ============================================================
# FLASK ROUTES
# ============================================================

@fiber_bp.route('/api/fiber/providers')
def get_providers():
    return jsonify({'success': True, **FiberProviderAPI.get_all_providers()})

@fiber_bp.route('/api/fiber/providers/<provider_id>')
def get_provider(provider_id):
    return jsonify({'success': True, **FiberProviderAPI.get_provider(provider_id)})

@fiber_bp.route('/api/fiber/providers/market')
def get_providers_by_market():
    market = request.args.get('market', 'Ashburn')
    return jsonify({'success': True, **FiberProviderAPI.get_providers_by_market(market)})

@fiber_bp.route('/api/fiber/routes')
def get_routes():
    provider = request.args.get('provider')
    return jsonify({'success': True, **FiberProviderAPI.get_routes(provider)})

@fiber_bp.route('/api/fiber/carrier-hotels')
def get_carrier_hotels():
    state = request.args.get('state')
    city = request.args.get('city')
    return jsonify({'success': True, **LitBuildingAPI.get_carrier_hotels(state, city)})

@fiber_bp.route('/api/fiber/bead-allocations')
def get_bead_allocations():
    dc_states = request.args.get('dc_markets')
    if dc_states:
        states = [s.strip().upper() for s in dc_states.split(',')]
        return jsonify({'success': True, **NTIAGrantAPI.get_bead_allocations(states)})
    return jsonify({'success': True, **NTIAGrantAPI.get_bead_allocations()})

@fiber_bp.route('/api/fiber/coverage')
def get_fiber_coverage():
    market = request.args.get('market')
    if market:
        return jsonify({'success': True, **FiberCoverageAPI.get_coverage(market)})
    return jsonify({'success': True, **FiberCoverageAPI.compare_markets()})

@fiber_bp.route('/api/fiber/summary')
def get_fiber_summary():
    providers = FiberProviderAPI.get_all_providers()
    routes = FiberProviderAPI.get_routes()
    bead = NTIAGrantAPI.get_bead_allocations()
    # Check Neon count
    neon_count = 0
    try:
        conn = _get_pg_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM fiber_routes")
            neon_count = cur.fetchone()[0]
            cur.close()
            conn.close()
    except Exception:
        pass

    return jsonify({
        'success': True,
        'neon_fiber_routes': neon_count,
        'seed_routes': len(MAJOR_ROUTES),
        'providers': providers['count'],
        'total_route_miles': providers['total_route_miles'],
        'total_lit_buildings': providers['total_lit_buildings'],
        'bead_states': bead['count'],
        'bead_total_funding': bead['total_funding_formatted'],
        'coverage_markets': len(FiberCoverageAPI.COVERAGE),
        'carrier_hotels': len(LitBuildingAPI.CARRIER_HOTELS),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    })


def register_fiber_discovery(app):
    """Register fiber discovery routes."""
    app.register_blueprint(fiber_bp)
    logger.info("✅ Fiber Network Discovery v2.2 registered")
    routes = FiberProviderAPI.get_routes()
    bead = NTIAGrantAPI.get_bead_allocations()
    print("🔌 Fiber Network Discovery v2.2: ✅ Registered")
    print("   📡 Providers: /api/fiber/providers (8 carriers, %s route miles)" % "{:,}".format(FiberProviderAPI.get_all_providers()['total_route_miles']))
    print("   🛤️ Routes: /api/fiber/routes (%d seed routes, %s miles)" % (routes['count'], "{:,}".format(routes['total_miles'])))
    print("   💰 BEAD: /api/fiber/bead-allocations (%d states, %s)" % (bead['count'], bead['total_funding_formatted']))
    print("   🔍 Discovery: run_fiber_discovery() → seeds Neon + crawls PeeringDB (with city geocoding)")
