"""
Fiber Network Discovery Module v2.0
====================================
- Seeds Neon fiber_routes table with major carrier routes
- Discovers fiber networks from PeeringDB IX data
- NTIA BEAD Allocations ($42.45B)
- Carrier Hotels & Lit Buildings
- Fiber Provider Coverage APIs

v2.0 CHANGES:
  - Added run_fiber_discovery() for infrastructure-sync job
  - Writes to Neon fiber_routes table (not SQLite)
  - PeeringDB IX peering data as fiber route proxy
  - All 20 major carrier routes seeded on first run
"""

import requests
import logging
import os
import time
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
import json

logger = logging.getLogger(__name__)

fiber_bp = Blueprint('fiber_discovery', __name__)


# ============================================================
# NEON DATABASE HELPERS
# ============================================================

def _get_pg_connection():
    """Get a Neon PostgreSQL connection."""
    try:
        import psycopg2
        db_url = os.environ.get("DATABASE_URL", "")
        if not db_url:
            return None
        return psycopg2.connect(db_url, connect_timeout=10)
    except Exception as e:
        logger.error(f"Fiber discovery DB connection failed: {e}")
        return None


def _ensure_fiber_routes_table():
    """Verify fiber_routes table exists in Neon and has required unique constraint."""
    conn = _get_pg_connection()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM fiber_routes")
        cur.fetchone()
        # Ensure unique constraint for ON CONFLICT (source, source_id) upserts
        try:
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fiber_routes_source_id ON fiber_routes(source, source_id)")
            conn.commit()
        except Exception:
            conn.rollback()
            # Dedup first, then retry
            try:
                cur.execute("""DELETE FROM fiber_routes a USING fiber_routes b
                               WHERE a.id < b.id AND a.source = b.source AND a.source_id = b.source_id""")
                conn.commit()
                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fiber_routes_source_id ON fiber_routes(source, source_id)")
                conn.commit()
            except Exception:
                conn.rollback()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"Fiber routes table check failed: {e}")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _upsert_fiber_route(conn, route):
    """Upsert a single fiber route into Neon.

    Phase ZZZZZ-round5-fiber (2026-05-23): wrapped in SAVEPOINT/ROLLBACK
    so one bad row doesn't poison the rest of the transaction. The
    earlier shape ("try except: log; return False") still left the
    parent transaction in an ABORTED state after a raise — every
    subsequent row then logged 'current transaction is aborted, commands
    ignored until end of transaction block'. The savepoint ROLLBACK
    discards just the failed row's changes and leaves the connection
    usable for the next iteration.

    Brain error class registered: psycopg2_transaction_aborted.
    """
    cur = conn.cursor()
    try:
        cur.execute("SAVEPOINT fiber_upsert")
        cur.execute("""
            INSERT INTO fiber_routes
                (name, provider, route_type, start_location, end_location,
                 start_point, end_point, distance_miles, fiber_count,
                 capacity, status, start_lat, start_lng, end_lat, end_lng,
                 source, source_id, discovered_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
            ON CONFLICT (source, source_id) DO UPDATE SET
                name = EXCLUDED.name,
                provider = EXCLUDED.provider,
                distance_miles = EXCLUDED.distance_miles,
                fiber_count = EXCLUDED.fiber_count,
                updated_at = NOW()
        """, (
            route.get('name', ''),
            route.get('provider', ''),
            route.get('route_type', 'long_haul'),
            route.get('start_location', ''),
            route.get('end_location', ''),
            route.get('start_location', ''),  # start_point = start_location
            route.get('end_location', ''),     # end_point = end_location
            route.get('route_miles'),           # → distance_miles
            route.get('fiber_count'),
            f"{route.get('fiber_count', 0)} fibers" if route.get('fiber_count') else None,  # capacity
            'active',                           # status
            route.get('start_lat'),
            route.get('start_lng'),
            route.get('end_lat'),
            route.get('end_lng'),
            route.get('source', 'seed'),
            route.get('source_id', ''),
        ))
        cur.execute("RELEASE SAVEPOINT fiber_upsert")
        return True
    except Exception as e:
        try:
            cur.execute("ROLLBACK TO SAVEPOINT fiber_upsert")
        except Exception:
            # If the savepoint itself failed, fall back to a full rollback
            # so the connection is at least usable for the next caller.
            try: conn.rollback()
            except Exception: pass
        # Log only the first ~5 bad rows per cycle to avoid log spam
        # when an entire batch is malformed.
        _spam_key = "_fiber_upsert_warns_logged"
        warns = getattr(_upsert_fiber_route, _spam_key, 0)
        if warns < 5:
            logger.warning(f"Fiber route upsert failed (row {route.get('source_id','?')}): {e}")
            setattr(_upsert_fiber_route, _spam_key, warns + 1)
        elif warns == 5:
            logger.warning("Fiber route upsert: further row-level errors suppressed for this cycle.")
            setattr(_upsert_fiber_route, _spam_key, 6)
        return False
    finally:
        try: cur.close()
        except Exception: pass


def _reset_fiber_warn_counter():
    """Call once at the start of each discovery cycle to reset the
    per-cycle log-spam suppressor."""
    if hasattr(_upsert_fiber_route, "_fiber_upsert_warns_logged"):
        delattr(_upsert_fiber_route, "_fiber_upsert_warns_logged")


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
# PeeringDB FIBER DISCOVERY
# ============================================================

def _discover_peeringdb_fiber():
    """
    Discover fiber routes from PeeringDB Internet Exchange data.
    Each IX represents a fiber interconnection point — we create
    routes between IXes in the same region as proxy fiber paths.
    """
    discovered = []
    try:
        # Get US Internet Exchanges from PeeringDB
        # Phase ZZZZZ-round5-peeringdb (2026-05-23): root cause of the
        # "PeeringDB returned 404" log spam was a busted URL — '%s' was
        # never substituted, so the request went to
        # /api/ix%scountry=US&status=ok (literal '%s' character).
        # PeeringDB's API was never wrong; the URL template was. Fixed:
        # the separator between path and query string is '?'.
        resp = requests.get(
            "https://www.peeringdb.com/api/ix?country=US&status=ok",
            headers={"User-Agent": "DCHub/2.0 (dchub.cloud)"},
            timeout=15
        )
        if resp.status_code != 200:
            logger.warning(f"PeeringDB returned {resp.status_code}")
            return discovered

        data = resp.json().get("data", [])
        logger.info(f"PeeringDB: found {len(data)} US Internet Exchanges")

        # Filter IXes with coordinates
        ixes = []
        for ix in data:
            lat = ix.get("latitude")
            lng = ix.get("longitude")
            if lat and lng and abs(lat) > 0.1:
                ixes.append({
                    "id": ix.get("id"),
                    "name": ix.get("name", ""),
                    "city": ix.get("city", ""),
                    "state": ix.get("region_continent", ""),
                    "lat": float(lat),
                    "lng": float(lng),
                    "net_count": ix.get("net_count", 0),
                })

        # Create routes between IXes in nearby cities (< 500 miles apart)
        import math
        for i, ix1 in enumerate(ixes):
            for ix2 in ixes[i+1:]:
                # Haversine distance
                lat1, lon1 = math.radians(ix1["lat"]), math.radians(ix1["lng"])
                lat2, lon2 = math.radians(ix2["lat"]), math.radians(ix2["lng"])
                dlat = lat2 - lat1
                dlon = lon2 - lon1
                a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
                dist_miles = 3959 * 2 * math.asin(math.sqrt(a))

                # Only create routes between 50-500 miles (metro fiber paths)
                if 50 < dist_miles < 500:
                    route_id = f"PDB-{ix1['id']}-{ix2['id']}"
                    discovered.append({
                        'source_id': route_id,
                        'provider': 'PeeringDB/IX',
                        'name': f"{ix1['name']} - {ix2['name']}",
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

        logger.info(f"PeeringDB: generated {len(discovered)} fiber route proxies from IX data")

    except requests.exceptions.Timeout:
        logger.warning("PeeringDB: timeout")
    except Exception as e:
        logger.error(f"PeeringDB discovery failed: {e}")

    return discovered


# ============================================================
# RUN FIBER DISCOVERY (called by infrastructure-sync job)
# ============================================================

def run_fiber_discovery():
    """
    Main fiber discovery function — called by /api/jobs/infrastructure-sync.
    
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
            else:
                results['errors'] += 1

        conn.commit()
        logger.info(f"Fiber seed: {results['seeded']} carrier routes written")

        # Step 2: Discover from PeeringDB
        try:
            pdb_routes = _discover_peeringdb_fiber()
            for route in pdb_routes:
                if _upsert_fiber_route(conn, route):
                    results['discovered'] += 1
                else:
                    results['errors'] += 1
            conn.commit()
            logger.info(f"PeeringDB fiber: {results['discovered']} routes discovered")
        except Exception as e:
            logger.warning(f"PeeringDB discovery failed (non-fatal): {e}")

        # Get total count
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM fiber_routes")
        results['total'] = cur.fetchone()[0]
        cur.close()

    except Exception as e:
        logger.error(f"Fiber discovery error: {e}")
        results['error'] = str(e)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    results['duration_seconds'] = round(time.time() - start, 2)
    results['status'] = 'success' if results['errors'] == 0 else 'partial'
    logger.info(f"Fiber discovery complete: {results}")
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
        return {'error': f'Provider not found: {provider_id}'}

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
        return {'error': f'Market not found: {market}', 'available': list(cls.COVERAGE.keys())}

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
            allocations.append({
                'state': st, 'recipient': info['recipient'], 'amount': info['amount'],
                'amount_formatted': f"${info['amount']/1e9:.2f}B" if info['amount'] >= 1e9 else f"${info['amount']/1e6:.1f}M",
                'status': info['status'], 'priority_tech': info['priority'], 'unserved_locations': info['unserved_locations'],
                'lat': cls.STATE_COORDS.get(st, (0, 0))[0], 'lng': cls.STATE_COORDS.get(st, (0, 0))[1],
            })
        allocations.sort(key=lambda x: x['amount'], reverse=True)
        total = sum(a['amount'] for a in allocations)
        return {'allocations': allocations, 'count': len(allocations), 'total_funding': total, 'total_funding_formatted': f"${total/1e9:.2f}B", 'total_unserved': sum(a['unserved_locations'] for a in allocations)}

    @classmethod
    def get_grant_stats(cls):
        total = sum(v['amount'] for v in cls.BEAD_ALLOCATIONS.values())
        return {'total_grants': len(cls.BEAD_ALLOCATIONS), 'total_funding': total, 'program': 'BEAD'}


# ============================================================
# FLASK ROUTES
# ============================================================

# AUTO-REPAIR: duplicate route '/api/fiber/providers' also in jobs_api.py:483 — review and remove one
@fiber_bp.route('/api/fiber/providers')
def get_providers():
    return jsonify({'success': True, **FiberProviderAPI.get_all_providers()})
# AUTO-REPAIR: duplicate route '/api/fiber/providers/<provider_id>' also in jobs_api.py:487 — review and remove one

@fiber_bp.route('/api/fiber/providers/<provider_id>')
def get_provider(provider_id):
# AUTO-REPAIR: duplicate route '/api/fiber/providers/market' also in jobs_api.py:491 — review and remove one
    return jsonify({'success': True, **FiberProviderAPI.get_provider(provider_id)})

@fiber_bp.route('/api/fiber/providers/market')
def get_providers_by_market():
# AUTO-REPAIR: duplicate route '/api/fiber/routes' also in jobs_api.py:496 — review and remove one
    market = request.args.get('market', 'Ashburn')
    return jsonify({'success': True, **FiberProviderAPI.get_providers_by_market(market)})

@fiber_bp.route('/api/fiber/routes')
# AUTO-REPAIR: duplicate route '/api/fiber/carrier-hotels' also in jobs_api.py:501 — review and remove one
def get_routes():
    provider = request.args.get('provider')
    return jsonify({'success': True, **FiberProviderAPI.get_routes(provider)})

@fiber_bp.route('/api/fiber/carrier-hotels')
# AUTO-REPAIR: duplicate route '/api/fiber/bead-allocations' also in jobs_api.py:507 — review and remove one
def get_carrier_hotels():
    state = request.args.get('state')
    city = request.args.get('city')
    return jsonify({'success': True, **LitBuildingAPI.get_carrier_hotels(state, city)})

@fiber_bp.route('/api/fiber/bead-allocations')
def get_bead_allocations():
# AUTO-REPAIR: duplicate route '/api/fiber/coverage' also in jobs_api.py:515 — review and remove one
    dc_states = request.args.get('dc_markets')
    if dc_states:
        states = [s.strip().upper() for s in dc_states.split(',')]
        return jsonify({'success': True, **NTIAGrantAPI.get_bead_allocations(states)})
    return jsonify({'success': True, **NTIAGrantAPI.get_bead_allocations()})

# AUTO-REPAIR: duplicate route '/api/fiber/summary' also in jobs_api.py:522 — review and remove one
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
    conn = None
    try:
        conn = _get_pg_connection()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM fiber_routes")
            neon_count = cur.fetchone()[0]
            cur.close()
    except Exception:
        pass
    finally:
        if conn:
            try:
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
    logger.info("✅ Fiber Network Discovery v2.0 registered")
    routes = FiberProviderAPI.get_routes()
    bead = NTIAGrantAPI.get_bead_allocations()
    print("🔌 Fiber Network Discovery v2.0: ✅ Registered")
    print(f"   📡 Providers: /api/fiber/providers (8 carriers, {FiberProviderAPI.get_all_providers()['total_route_miles']:,} route miles)")
    print(f"   🛤️ Routes: /api/fiber/routes ({routes['count']} seed routes, {routes['total_miles']:,} miles)")
    print(f"   💰 BEAD: /api/fiber/bead-allocations ({bead['count']} states, {bead['total_funding_formatted']})")
    print("   🔍 Discovery: run_fiber_discovery() → seeds Neon + crawls PeeringDB")
