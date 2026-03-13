"""
DC Hub Nexus - Automatic KMZ/KML Infrastructure Discovery v3.0
================================================================
Autonomous system that discovers, downloads, and parses KMZ/KML
infrastructure files from public government and industry sources.

v3.0 CHANGES (Mar 2026):
  - Migrated from SQLite to Neon PostgreSQL (data persists across Railway deploys)
  - Uses late-binding DB connection pattern (injected from main.py)
  - PostgreSQL parameterized queries (%s instead of ?)
  - ON CONFLICT instead of INSERT OR IGNORE
  - datetime('now', '-7 days') → NOW() - INTERVAL '7 days'

FIBER SOURCES:
- NTIA Broadband Infrastructure maps
- State broadband offices (BroadbandUSA)
- FCC broadband deployment GIS data
- USGS/HIFLD infrastructure GIS layers
- Public carrier fiber route maps
- State DOT fiber route data

GAS PIPELINE SOURCES:
- HIFLD Natural Gas Pipelines (nationwide)
- EIA Natural Gas Interstate/Intrastate Pipelines
- EIA Crude Oil Trunk Pipelines
- EIA Gulf Oil and Gas Pipelines

Runs every 12 hours as a background daemon thread.
"""

import os
import json
import hashlib
import time
import logging
import threading
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from math import radians, sin, cos, sqrt, atan2

logger = logging.getLogger(__name__)

KMZ_DOWNLOAD_DIR = os.path.join(os.getcwd(), 'uploads', 'kmz')

# ---------------------------------------------------------------------------
# Late-binding DB connection (injected from main.py)
# ---------------------------------------------------------------------------
_get_pg = None
_return_pg = None


def _conn():
    if _get_pg is None:
        raise RuntimeError("kmz_auto_discovery not initialized — call init first")
    return _get_pg()


def _release(conn):
    if _return_pg and conn:
        try:
            _return_pg(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

# ---------------------------------------------------------------------------
# PUBLIC SOURCES
# ---------------------------------------------------------------------------

PUBLIC_KMZ_SOURCES = [
    {
        'name': 'NTIA National Broadband Map - Fiber Routes',
        'url': 'https://broadbandmap.fcc.gov/api/public/map/listHandshake',
        'type': 'api_discover',
        'provider': 'FCC/NTIA',
        'category': 'federal'
    },
    {
        'name': 'HIFLD Electric Power Transmission Lines',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'power'
    },
    {
        'name': 'HIFLD Fiber Optic Cable Landing Points',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Submarine_Cable_Landing_Points/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'HIFLD',
        'category': 'fiber'
    },
    {
        'name': 'NTIA Broadband Infrastructure - Middle Mile',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/NTIA_BIP_Round_1_Middle_Mile/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'NTIA',
        'category': 'fiber'
    },
    {
        'name': 'NTIA Broadband Infrastructure - Last Mile',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/NTIA_BIP_Round_1_Last_Mile/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'NTIA',
        'category': 'fiber'
    },
    {
        'name': 'EIA Natural Gas Interstate/Intrastate Pipelines',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'gas'
    },
    {
        'name': 'EIA Crude Oil Trunk Pipelines',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Crude_Oil_Trunk_Pipelines_1/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'gas'
    },
    {
        'name': 'EIA Gulf Oil and Gas Pipelines',
        'url': 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Oil_And_Natural_Gas_Pipelines_Gulf_2024Q4/FeatureServer/0',
        'type': 'arcgis_kml',
        'provider': 'EIA',
        'category': 'gas'
    },
]

ARCGIS_FIBER_SEARCH_URLS = [
    'https://www.arcgis.com/sharing/rest/search?q=fiber%20optic%20routes&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=broadband%20infrastructure%20fiber&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=telecommunications%20network%20routes&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=dark%20fiber%20network%20map&sortField=modified&sortOrder=desc&num=10&f=json',
]

ARCGIS_GAS_SEARCH_URLS = [
    'https://www.arcgis.com/sharing/rest/search?q=natural%20gas%20pipeline%20infrastructure&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=gas%20pipeline%20transmission%20interstate&sortField=modified&sortOrder=desc&num=20&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=natural%20gas%20compressor%20station&sortField=modified&sortOrder=desc&num=15&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=LNG%20terminal%20natural%20gas%20storage&sortField=modified&sortOrder=desc&num=10&f=json',
    'https://www.arcgis.com/sharing/rest/search?q=PHMSA%20pipeline%20hazardous%20materials&sortField=modified&sortOrder=desc&num=10&f=json',
]

STATE_BROADBAND_GIS = [
    {'name': 'Virginia Broadband', 'state': 'VA', 'url': 'https://gismaps.vdem.virginia.gov/arcgis/rest/services/Broadband', 'provider': 'Virginia'},
    {'name': 'Texas Broadband', 'state': 'TX', 'url': 'https://services.arcgis.com/KTcxiTD9dsQw4r7Z/arcgis/rest/services', 'provider': 'Texas'},
    {'name': 'Ohio Broadband', 'state': 'OH', 'url': 'https://gis.broadband.ohio.gov/arcgis/rest/services', 'provider': 'Ohio'},
    {'name': 'Georgia Broadband', 'state': 'GA', 'url': 'https://services1.arcgis.com/2iUE8l8JKrP2tygQ/arcgis/rest/services', 'provider': 'Georgia'},
    {'name': 'Iowa Broadband', 'state': 'IA', 'url': 'https://services.arcgis.com/8lRhdTsQyJpO52F1/arcgis/rest/services', 'provider': 'Iowa'},
    {'name': 'Nevada Broadband', 'state': 'NV', 'url': 'https://services.arcgis.com/njFNhDsUCentVYJW/arcgis/rest/services', 'provider': 'Nevada'},
    {'name': 'Utah Broadband', 'state': 'UT', 'url': 'https://services1.arcgis.com/99lidPhWCzftIe9K/arcgis/rest/services', 'provider': 'Utah'},
    {'name': 'Arizona Broadband', 'state': 'AZ', 'url': 'https://services.arcgis.com/pdeMzRDpb5JCadVO/arcgis/rest/services', 'provider': 'Arizona'},
]


# =============================================================================
# KMZ AUTO-DISCOVERY ENGINE (Neon PostgreSQL)
# =============================================================================

class KMZAutoDiscovery:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'DCHub-Infrastructure/3.0'})
        self._scheduler_running = False
        self._cycle_in_progress = False
        self._cache = {
            'last_cycle': None,
            'total_routes_discovered': 0,
            'total_kmz_processed': 0,
            'sources_checked': 0
        }
        self.init_tables()

    def init_tables(self):
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()

            cur.execute('''
                CREATE TABLE IF NOT EXISTS fiber_kmz_routes (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    provider TEXT,
                    route_type TEXT DEFAULT 'fiber',
                    start_point TEXT,
                    end_point TEXT,
                    distance_km REAL DEFAULT 0,
                    coordinates TEXT,
                    kmz_file TEXT,
                    source_url TEXT,
                    discovered_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS kmz_discovery_log (
                    id SERIAL PRIMARY KEY,
                    source_name TEXT,
                    source_url TEXT,
                    source_type TEXT,
                    routes_found INTEGER DEFAULT 0,
                    total_km REAL DEFAULT 0,
                    status TEXT DEFAULT 'success',
                    error_message TEXT,
                    discovered_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')

            cur.execute('''
                CREATE TABLE IF NOT EXISTS kmz_discovered_sources (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    url TEXT UNIQUE,
                    provider TEXT,
                    category TEXT,
                    source_type TEXT,
                    status TEXT DEFAULT 'discovered',
                    routes_count INTEGER DEFAULT 0,
                    last_checked TIMESTAMPTZ,
                    discovered_at TIMESTAMPTZ DEFAULT NOW()
                )
            ''')

            # Index for dedup on routes
            cur.execute('''
                CREATE INDEX IF NOT EXISTS idx_kmz_routes_dedup
                ON fiber_kmz_routes(provider, name, start_point)
            ''')

            conn.commit()
            cur.close()
            logger.info("KMZ Auto-Discovery tables initialized (Neon PostgreSQL)")
        except Exception as e:
            logger.error(f"KMZ table init error: {e}")
        finally:
            _release(conn)

    # ── Discovery Cycle ────────────────────────────────────────

    def run_discovery_cycle(self) -> Dict:
        if self._cycle_in_progress:
            logger.info("KMZ Discovery cycle skipped (previous cycle still running)")
            return {'skipped': True, 'reason': 'cycle_in_progress'}
        self._cycle_in_progress = True
        try:
            return self._run_discovery_cycle_inner()
        finally:
            self._cycle_in_progress = False

    def _run_discovery_cycle_inner(self) -> Dict:
        logger.info("=" * 60)
        logger.info("KMZ AUTO-DISCOVERY CYCLE STARTING")
        logger.info("=" * 60)

        cycle_start = time.time()
        results = {
            'arcgis_search': {'checked': 0, 'new_sources': 0},
            'known_sources': {'checked': 0, 'routes_found': 0, 'total_km': 0},
            'state_broadband': {'checked': 0, 'services_found': 0},
            'arcgis_kml_export': {'exported': 0, 'routes_parsed': 0, 'total_km': 0},
            'total_new_routes': 0,
            'total_new_km': 0
        }

        try:
            r = self._discover_arcgis_sources()
            results['arcgis_search'] = r
        except Exception as e:
            logger.error(f"ArcGIS search error: {e}")
            results['arcgis_search']['error'] = str(e)

        try:
            r = self._process_known_sources()
            results['known_sources'] = r
            results['total_new_routes'] += r.get('routes_found', 0)
            results['total_new_km'] += r.get('total_km', 0)
        except Exception as e:
            logger.error(f"Known sources error: {e}")
            results['known_sources']['error'] = str(e)

        try:
            r = self._discover_state_broadband()
            results['state_broadband'] = r
        except Exception as e:
            logger.error(f"State broadband error: {e}")
            results['state_broadband']['error'] = str(e)

        try:
            r = self._export_arcgis_as_kml()
            results['arcgis_kml_export'] = r
            results['total_new_routes'] += r.get('routes_parsed', 0)
            results['total_new_km'] += r.get('total_km', 0)
        except Exception as e:
            logger.error(f"ArcGIS KML export error: {e}")
            results['arcgis_kml_export']['error'] = str(e)

        cycle_duration = round(time.time() - cycle_start, 1)
        results['cycle_duration_seconds'] = cycle_duration

        self._cache['last_cycle'] = datetime.now().isoformat()
        self._cache['last_results'] = results
        self._cache['total_routes_discovered'] += results['total_new_routes']
        self._cache['total_kmz_processed'] += results['arcgis_kml_export'].get('exported', 0)

        self._log_cycle(results)

        logger.info("=" * 60)
        logger.info(f"KMZ DISCOVERY CYCLE COMPLETE ({cycle_duration}s)")
        logger.info(f"   New routes: {results['total_new_routes']}")
        logger.info(f"   New km: {results['total_new_km']:.1f}")
        logger.info(f"   ArcGIS sources found: {results['arcgis_search'].get('new_sources', 0)}")
        logger.info(f"   State services: {results['state_broadband'].get('services_found', 0)}")
        logger.info("=" * 60)

        return results

    # ── ArcGIS Source Discovery ────────────────────────────────

    def _discover_arcgis_sources(self) -> Dict:
        results = {'checked': 0, 'new_sources': 0, 'total_found': 0}

        all_search_urls = [
            (url, 'fiber') for url in ARCGIS_FIBER_SEARCH_URLS
        ] + [
            (url, 'gas') for url in ARCGIS_GAS_SEARCH_URLS
        ]

        for search_url, category in all_search_urls:
            try:
                response = self.session.get(search_url, timeout=20)
                results['checked'] += 1

                if response.status_code == 200:
                    data = response.json()
                    items = data.get('results', [])

                    for item in items:
                        item_url = item.get('url', '')
                        item_name = item.get('title', item.get('name', 'Unknown'))
                        item_type = item.get('type', '')

                        if not item_url:
                            continue

                        if any(k in item_type.lower() for k in ['feature', 'map service', 'kml']):
                            results['total_found'] += 1
                            added = self._add_discovered_source({
                                'name': item_name,
                                'url': item_url,
                                'provider': item.get('owner', 'ArcGIS'),
                                'category': category,
                                'source_type': 'arcgis'
                            })
                            if added:
                                results['new_sources'] += 1

                time.sleep(1)
            except Exception as e:
                logger.debug(f"ArcGIS search error: {e}")

        logger.info(f"ArcGIS Search: checked={results['checked']}, found={results['total_found']}, new={results['new_sources']}")
        return results

    # ── Process Known Sources ──────────────────────────────────

    def _process_known_sources(self) -> Dict:
        results = {'checked': 0, 'routes_found': 0, 'total_km': 0}

        for source in PUBLIC_KMZ_SOURCES:
            try:
                results['checked'] += 1

                if source['type'] == 'arcgis_kml':
                    route_type = 'gas' if source.get('category') == 'gas' else 'fiber'
                    r = self._fetch_arcgis_routes(source['url'], source['provider'], source['name'], route_type=route_type)
                    results['routes_found'] += r.get('routes_found', 0)
                    results['total_km'] += r.get('total_km', 0)

                self._add_discovered_source({
                    'name': source['name'],
                    'url': source['url'],
                    'provider': source['provider'],
                    'category': source['category'],
                    'source_type': source['type']
                })

                time.sleep(1)
            except Exception as e:
                logger.debug(f"Known source error for {source['name']}: {e}")

        logger.info(f"Known Sources: checked={results['checked']}, routes={results['routes_found']}, km={results['total_km']:.1f}")
        return results

    # ── Fetch ArcGIS Routes ────────────────────────────────────

    def _fetch_arcgis_routes(self, url: str, provider: str, source_name: str, route_type: str = 'fiber') -> Dict:
        results = {'routes_found': 0, 'total_km': 0}

        try:
            query_url = f"{url}/query?where=1=1&outFields=*&resultRecordCount=200&returnGeometry=true&f=json"
            response = self.session.get(query_url, timeout=30)

            if response.status_code != 200:
                return results

            data = response.json()
            features = data.get('features', [])

            if not features:
                return results

            conn = _conn()
            try:
                cur = conn.cursor()

                for feature in features:
                    attrs = feature.get('attributes', {})
                    geom = feature.get('geometry', {})

                    name = (attrs.get('NAME') or attrs.get('name') or
                            attrs.get('OWNER') or attrs.get('OPERATOR') or
                            attrs.get('ID', f'{provider}_route'))

                    if isinstance(name, (int, float)):
                        name = f"{provider}_route_{name}"

                    coordinates = []
                    if 'paths' in geom:
                        for path in geom['paths']:
                            for point in path[:50]:
                                if len(point) >= 2:
                                    coordinates.append([point[1], point[0]])
                    elif 'rings' in geom:
                        for ring in geom['rings']:
                            for point in ring[:50]:
                                if len(point) >= 2:
                                    coordinates.append([point[1], point[0]])
                    elif 'x' in geom and 'y' in geom:
                        coordinates.append([geom['y'], geom['x']])

                    if not coordinates:
                        continue

                    distance_km = self._calculate_route_distance(coordinates) if len(coordinates) > 1 else 0
                    start_point = f"{coordinates[0][0]:.4f},{coordinates[0][1]:.4f}"
                    end_point = f"{coordinates[-1][0]:.4f},{coordinates[-1][1]:.4f}"

                    url_hash = hashlib.sha256(f"{provider}_{name}_{start_point}".encode()).hexdigest()[:16]

                    try:
                        cur.execute('''
                            INSERT INTO fiber_kmz_routes
                            (name, provider, route_type, start_point, end_point,
                             distance_km, coordinates, kmz_file, source_url)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                        ''', (
                            str(name)[:200], provider, route_type,
                            start_point, end_point,
                            round(distance_km, 2),
                            json.dumps(coordinates[:100]),
                            f"arcgis_export_{url_hash}",
                            url
                        ))
                        if cur.rowcount > 0:
                            results['routes_found'] += 1
                            results['total_km'] += distance_km
                    except Exception as e:
                        logger.debug(f"Route insert error: {e}")

                conn.commit()
                cur.close()
            finally:
                _release(conn)

        except Exception as e:
            logger.debug(f"ArcGIS route fetch error for {source_name}: {e}")

        return results

    # ── State Broadband Discovery ──────────────────────────────

    def _discover_state_broadband(self) -> Dict:
        results = {'checked': 0, 'services_found': 0, 'new_sources': 0}

        for state in STATE_BROADBAND_GIS:
            try:
                results['checked'] += 1

                catalog_url = f"{state['url']}?f=json"
                response = self.session.get(catalog_url, timeout=15)

                if response.status_code == 200:
                    data = response.json()
                    services = data.get('services', [])

                    for svc in services:
                        svc_name = svc.get('name', '')
                        svc_type = svc.get('type', '')

                        if any(k in svc_name.lower() for k in ['fiber', 'broadband', 'telecom', 'network', 'cable', 'internet']):
                            svc_url = f"{state['url']}/{svc_name}/{svc_type}"
                            results['services_found'] += 1

                            added = self._add_discovered_source({
                                'name': f"{state['provider']} - {svc_name}",
                                'url': svc_url,
                                'provider': state['provider'],
                                'category': 'fiber',
                                'source_type': 'state_gis'
                            })
                            if added:
                                results['new_sources'] += 1

                time.sleep(1)
            except Exception as e:
                logger.debug(f"State broadband error for {state['name']}: {e}")

        logger.info(f"State Broadband: checked={results['checked']}, services={results['services_found']}, new={results['new_sources']}")
        return results

    # ── Export ArcGIS as KML ───────────────────────────────────

    def _export_arcgis_as_kml(self) -> Dict:
        results = {'exported': 0, 'routes_parsed': 0, 'total_km': 0, 'errors': 0}

        conn = None
        sources = []
        try:
            conn = _conn()
            cur = conn.cursor()
            cur.execute('''
                SELECT id, name, url, provider FROM kmz_discovered_sources
                WHERE source_type IN ('arcgis', 'state_gis')
                AND (last_checked IS NULL OR last_checked < NOW() - INTERVAL '7 days')
                AND status != 'failed'
                LIMIT 10
            ''')
            sources = cur.fetchall()
            cur.close()
        except Exception as e:
            logger.error(f"ArcGIS KML export query error: {e}")
        finally:
            _release(conn)

        for row in sources:
            src_id, name, url, provider = row[0], row[1], row[2], row[3]
            try:
                r = self._fetch_arcgis_routes(url, provider or 'Unknown', name or 'Unknown')
                results['routes_parsed'] += r.get('routes_found', 0)
                results['total_km'] += r.get('total_km', 0)

                if r.get('routes_found', 0) > 0:
                    results['exported'] += 1
                    self._update_source_status(src_id, 'active', r['routes_found'])
                else:
                    self._update_source_status(src_id, 'empty', 0)

                time.sleep(2)
            except Exception as e:
                results['errors'] += 1
                self._update_source_status(src_id, 'failed', 0)
                logger.debug(f"ArcGIS export error for {name}: {e}")

        logger.info(f"ArcGIS Export: exported={results['exported']}, routes={results['routes_parsed']}, km={results['total_km']:.1f}")
        return results

    # ── Helpers ─────────────────────────────────────────────────

    def _add_discovered_source(self, source: Dict) -> bool:
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO kmz_discovered_sources
                (name, url, provider, category, source_type)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (url) DO NOTHING
            ''', (
                source['name'][:200],
                source['url'],
                source.get('provider', 'Unknown'),
                source.get('category', 'fiber'),
                source.get('source_type', 'unknown')
            ))
            added = cur.rowcount > 0
            conn.commit()
            cur.close()
            return added
        except Exception:
            return False
        finally:
            _release(conn)

    def _update_source_status(self, source_id: int, status: str, routes_count: int):
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()
            cur.execute('''
                UPDATE kmz_discovered_sources
                SET status = %s, routes_count = %s, last_checked = NOW()
                WHERE id = %s
            ''', (status, routes_count, source_id))
            conn.commit()
            cur.close()
        except Exception:
            pass
        finally:
            _release(conn)

    def _calculate_route_distance(self, coordinates: List[List[float]]) -> float:
        total_distance = 0
        for i in range(len(coordinates) - 1):
            lat1, lng1 = coordinates[i]
            lat2, lng2 = coordinates[i + 1]
            R = 6371
            lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
            dlat = lat2 - lat1
            dlng = lng2 - lng1
            a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlng/2)**2
            c = 2 * atan2(sqrt(a), sqrt(1-a))
            total_distance += R * c
        return round(total_distance, 2)

    def _log_cycle(self, results: Dict):
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO kmz_discovery_log
                (source_name, source_url, source_type, routes_found, total_km, status)
                VALUES (%s, %s, %s, %s, %s, %s)
            ''', (
                'auto_discovery_cycle',
                'scheduler',
                'full_cycle',
                results.get('total_new_routes', 0),
                results.get('total_new_km', 0),
                'success'
            ))
            conn.commit()
            cur.close()
        except Exception as e:
            logger.debug(f"KMZ log error: {e}")
        finally:
            _release(conn)

    def get_status(self) -> Dict:
        status = {
            'running': self._scheduler_running,
            'last_cycle': self._cache.get('last_cycle'),
            'total_routes_discovered': self._cache.get('total_routes_discovered', 0),
            'total_kmz_processed': self._cache.get('total_kmz_processed', 0),
        }

        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM fiber_kmz_routes")
            status['total_routes_in_db'] = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM kmz_discovered_sources")
            status['total_sources'] = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM kmz_discovered_sources WHERE status = 'active'")
            status['active_sources'] = cur.fetchone()[0]

            cur.execute("SELECT COALESCE(SUM(distance_km), 0) FROM fiber_kmz_routes")
            status['total_km'] = round(float(cur.fetchone()[0]), 1)

            cur.execute('''
                SELECT provider, COUNT(*) AS cnt, COALESCE(SUM(distance_km), 0) AS km
                FROM fiber_kmz_routes
                GROUP BY provider
                ORDER BY cnt DESC
                LIMIT 10
            ''')
            status['routes_by_provider'] = [
                {'provider': r[0], 'routes': r[1], 'km': round(float(r[2]), 1)}
                for r in cur.fetchall()
            ]

            cur.close()
        except Exception as e:
            logger.debug(f"KMZ status query error: {e}")
        finally:
            _release(conn)

        return status


# =============================================================================
# SCHEDULER THREAD
# =============================================================================

_kmz_instance = None
_kmz_scheduler_thread = None


def _run_kmz_scheduler(interval: int = 43200):
    global _kmz_instance
    if _kmz_instance:
        _kmz_instance._scheduler_running = True
    logger.info(f"KMZ Discovery scheduler started (interval={interval}s / {interval//3600}h)")

    time.sleep(360)  # Wait 6 min after boot

    cycle_count = 0
    while _kmz_instance and _kmz_instance._scheduler_running:
        cycle_count += 1
        try:
            logger.info(f"KMZ Discovery scheduler: starting cycle #{cycle_count}...")
            start_time = time.time()
            _kmz_instance.run_discovery_cycle()
            elapsed = round(time.time() - start_time, 1)
            logger.info(f"KMZ Discovery scheduler: cycle #{cycle_count} completed in {elapsed}s")
        except Exception as e:
            logger.error(f"KMZ Discovery cycle #{cycle_count} error: {e}", exc_info=True)

        for _ in range(interval // 10):
            if not (_kmz_instance and _kmz_instance._scheduler_running):
                break
            time.sleep(10)

    logger.info("KMZ Discovery scheduler stopped")


def start_kmz_scheduler(interval: int = 43200):
    global _kmz_scheduler_thread
    if _kmz_scheduler_thread and _kmz_scheduler_thread.is_alive():
        if _kmz_instance:
            _kmz_instance._scheduler_running = True
        logger.info("KMZ Discovery scheduler already running")
        return

    _kmz_scheduler_thread = threading.Thread(
        target=_run_kmz_scheduler,
        args=(interval,),
        daemon=True,
        name='kmz-auto-discovery-scheduler'
    )
    _kmz_scheduler_thread.start()


# =============================================================================
# FLASK REGISTRATION
# =============================================================================

def register_kmz_discovery_routes(app, get_pg_fn, return_pg_fn, start_scheduler=True):
    """
    Register KMZ discovery routes and initialize Neon connection.

    Usage in main.py:
        from kmz_auto_discovery import register_kmz_discovery_routes
        register_kmz_discovery_routes(app, get_pg_connection, return_pg_connection)
    """
    from flask import Blueprint, jsonify, request as flask_request

    global _kmz_instance, _get_pg, _return_pg

    # Inject DB connections
    _get_pg = get_pg_fn
    _return_pg = return_pg_fn

    if _kmz_instance is not None:
        if start_scheduler:
            _kmz_instance._scheduler_running = True
        logger.info("KMZ Auto-Discovery already initialized, skipping duplicate registration")
        return

    _kmz_instance = KMZAutoDiscovery()

    kmz_bp = Blueprint('kmz_discovery', __name__)

    @kmz_bp.route('/api/kmz-discovery/status')
    def kmz_discovery_status():
        return jsonify({
            'success': True,
            'engine': 'KMZ Auto-Discovery v3.0 (Neon)',
            **_kmz_instance.get_status()
        })

    @kmz_bp.route('/api/kmz-discovery/run', methods=['POST'])
    def run_kmz_discovery():
        results = _kmz_instance.run_discovery_cycle()
        return jsonify({'success': True, 'results': results})

    @kmz_bp.route('/api/kmz-discovery/routes')
    def get_kmz_routes():
        page = flask_request.args.get('page', 1, type=int)
        per_page = min(flask_request.args.get('per_page', 50, type=int), 200)
        provider = flask_request.args.get('provider')

        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()

            where_clause = ""
            params = []
            if provider:
                where_clause = "WHERE provider = %s"
                params.append(provider)

            cur.execute(f"SELECT COUNT(*) FROM fiber_kmz_routes {where_clause}", params)
            total = cur.fetchone()[0]

            offset = (page - 1) * per_page
            cur.execute(f'''
                SELECT id, name, provider, route_type, start_point, end_point,
                       distance_km, source_url, discovered_at
                FROM fiber_kmz_routes
                {where_clause}
                ORDER BY discovered_at DESC
                LIMIT %s OFFSET %s
            ''', params + [per_page, offset])

            cols = [d[0] for d in cur.description]
            routes = [dict(zip(cols, r)) for r in cur.fetchall()]

            # Convert timestamps to string
            for route in routes:
                if route.get('discovered_at'):
                    route['discovered_at'] = str(route['discovered_at'])

            cur.close()

            return jsonify({
                'success': True,
                'routes': routes,
                'total': total,
                'page': page,
                'per_page': per_page
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            _release(conn)

    @kmz_bp.route('/api/kmz-discovery/sources')
    def get_kmz_sources():
        conn = None
        try:
            conn = _conn()
            cur = conn.cursor()

            cur.execute('''
                SELECT id, name, url, provider, category, source_type, status,
                       routes_count, last_checked, discovered_at
                FROM kmz_discovered_sources
                ORDER BY discovered_at DESC
                LIMIT 100
            ''')

            cols = [d[0] for d in cur.description]
            sources = []
            for r in cur.fetchall():
                src = dict(zip(cols, r))
                for ts_field in ('last_checked', 'discovered_at'):
                    if src.get(ts_field):
                        src[ts_field] = str(src[ts_field])
                sources.append(src)

            cur.close()

            return jsonify({
                'success': True,
                'sources': sources,
                'total': len(sources)
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        finally:
            _release(conn)

    app.register_blueprint(kmz_bp)

    os.makedirs(KMZ_DOWNLOAD_DIR, exist_ok=True)

    if start_scheduler:
        start_kmz_scheduler()
        logger.info("🗺️  KMZ Auto-Discovery v3.0: ✅ Registered (Neon, 12-hour auto-cycle)")
    else:
        logger.info("🗺️  KMZ Auto-Discovery v3.0: ✅ Registered (Neon, scheduler PAUSED)")
    logger.info("   GET  /api/kmz-discovery/status  - Discovery status")
    logger.info("   POST /api/kmz-discovery/run     - Trigger discovery cycle")
    logger.info("   GET  /api/kmz-discovery/routes  - Browse discovered routes")
    logger.info("   GET  /api/kmz-discovery/sources - View discovered sources")
