"""
Infrastructure Discovery Module
Tracks fiber routes, commercial real estate, construction permits, and substations
All reads/writes go to PostgreSQL via db_utils.
"""

import requests
import json
import logging
from datetime import datetime, timedelta
from threading import Thread
import time
import os
from db_utils import get_db

logger = logging.getLogger(__name__)

DB_PATH = 'dc_nexus.db'


def _safe_write(sql, params=None, retries=3):
    """Write to PostgreSQL via db_utils (handles SQL translation)."""
    for attempt in range(retries):
        try:
            conn = get_db()
            try:
                cursor = conn.cursor()
                if params:
                    cursor.execute(sql, params)
                else:
                    cursor.execute(sql)
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
            else:
                logger.warning(f"Infrastructure write failed after {retries} attempts: {e}")
                return 0
    return 0

def init_infrastructure_tables():
    """Initialize tables for infrastructure data"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fiber_routes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            provider TEXT,
            route_type TEXT,
            start_location TEXT,
            end_location TEXT,
            start_lat REAL,
            start_lng REAL,
            end_lat REAL,
            end_lng REAL,
            distance_miles REAL,
            fiber_count INTEGER,
            lit_capacity_gbps REAL,
            status TEXT DEFAULT 'active',
            source TEXT,
            source_id TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dc_properties (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            country TEXT DEFAULT 'US',
            lat REAL,
            lng REAL,
            property_type TEXT,
            square_feet INTEGER,
            power_capacity_mw REAL,
            asking_price REAL,
            price_per_sqft REAL,
            cap_rate REAL,
            zoning TEXT,
            utility_provider TEXT,
            fiber_providers TEXT,
            listing_url TEXT,
            broker TEXT,
            status TEXT DEFAULT 'available',
            source TEXT,
            source_id TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS construction_permits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            permit_number TEXT,
            project_name TEXT,
            address TEXT,
            city TEXT,
            state TEXT,
            country TEXT DEFAULT 'US',
            lat REAL,
            lng REAL,
            permit_type TEXT,
            project_type TEXT,
            square_feet INTEGER,
            estimated_power_mw REAL,
            estimated_cost REAL,
            contractor TEXT,
            owner TEXT,
            issue_date DATE,
            expiration_date DATE,
            status TEXT DEFAULT 'active',
            source TEXT,
            source_id TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS substations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            operator TEXT,
            substation_type TEXT,
            voltage_kv REAL,
            capacity_mva REAL,
            lat REAL,
            lng REAL,
            city TEXT,
            state TEXT,
            country TEXT DEFAULT 'US',
            connected_transmission TEXT,
            status TEXT DEFAULT 'active',
            source TEXT,
            source_id TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS gas_pipelines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            operator TEXT,
            pipeline_type TEXT,
            diameter_inches REAL,
            capacity_mcf REAL,
            status TEXT DEFAULT 'active',
            lat REAL,
            lng REAL,
            city TEXT,
            state TEXT,
            country TEXT DEFAULT 'US',
            source TEXT,
            source_id TEXT UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS linkedin_weekly_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week_start DATE,
            week_end DATE,
            content TEXT,
            stats_snapshot TEXT,
            posted_at TIMESTAMP,
            post_id TEXT,
            engagement TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info("✅ Infrastructure tables initialized")

DC_MARKETS = [
    {"name": "Northern Virginia", "lat": 39.0438, "lng": -77.4874, "state": "VA"},
    {"name": "Dallas-Fort Worth", "lat": 32.7767, "lng": -96.7970, "state": "TX"},
    {"name": "Phoenix", "lat": 33.4484, "lng": -112.0740, "state": "AZ"},
    {"name": "Chicago", "lat": 41.8781, "lng": -87.6298, "state": "IL"},
    {"name": "Atlanta", "lat": 33.7490, "lng": -84.3880, "state": "GA"},
    {"name": "Silicon Valley", "lat": 37.3861, "lng": -122.0839, "state": "CA"},
    {"name": "Los Angeles", "lat": 34.0522, "lng": -118.2437, "state": "CA"},
    {"name": "New York Metro", "lat": 40.7128, "lng": -74.0060, "state": "NJ"},
    {"name": "Portland", "lat": 45.5152, "lng": -122.6784, "state": "OR"},
    {"name": "Seattle", "lat": 47.6062, "lng": -122.3321, "state": "WA"},
    {"name": "Salt Lake City", "lat": 40.7608, "lng": -111.8910, "state": "UT"},
    {"name": "Columbus", "lat": 39.9612, "lng": -82.9988, "state": "OH"},
    {"name": "Richmond", "lat": 37.5407, "lng": -77.4360, "state": "VA"},
    {"name": "San Antonio", "lat": 29.4241, "lng": -98.4936, "state": "TX"},
    {"name": "Reno", "lat": 39.5296, "lng": -119.8138, "state": "NV"},
    {"name": "Des Moines", "lat": 41.5868, "lng": -93.6250, "state": "IA"},
    {"name": "Kansas City", "lat": 39.0997, "lng": -94.5786, "state": "MO"},
    {"name": "Minneapolis", "lat": 44.9778, "lng": -93.2650, "state": "MN"},
    {"name": "Denver", "lat": 39.7392, "lng": -104.9903, "state": "CO"},
    {"name": "Houston", "lat": 29.7604, "lng": -95.3698, "state": "TX"},
]

HIFLD_APIS = {
    "substations": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0",
    "transmission_lines": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0",
    "power_plants": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Power_Plants/FeatureServer/0",
    "natural_gas_pipelines": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Pipelines/FeatureServer/0",
    "natural_gas_compressor": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0",
    "natural_gas_processing": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Processing_Plants/FeatureServer/0",
}


def _query_hifld_nearby(api_url, lat, lng, radius_m=80000, max_records=500, return_geometry=True):
    """Query HIFLD ArcGIS API for features near a point"""
    try:
        params = {
            'where': '1=1',
            'geometry': f'{lng},{lat}',
            'geometryType': 'esriGeometryPoint',
            'inSR': '4326',
            'spatialRel': 'esriSpatialRelIntersects',
            'distance': radius_m,
            'units': 'esriSRUnit_Meter',
            'outFields': '*',
            'returnGeometry': 'true' if return_geometry else 'false',
            'outSR': '4326',
            'resultRecordCount': max_records,
            'f': 'json'
        }
        response = requests.get(f"{api_url}/query", params=params, timeout=20)
        if response.ok:
            data = response.json()
            return data.get('features', [])
    except Exception as e:
        logger.warning(f"HIFLD query failed for {api_url}: {e}")
    return []


def _query_hifld_paginated(api_url, where='1=1', max_total=2000, batch_size=1000):
    """Query HIFLD ArcGIS API with pagination for bulk pulls"""
    all_features = []
    offset = 0
    try:
        while offset < max_total:
            params = {
                'where': where,
                'outFields': '*',
                'returnGeometry': 'true',
                'outSR': '4326',
                'resultRecordCount': batch_size,
                'resultOffset': offset,
                'f': 'json'
            }
            response = requests.get(f"{api_url}/query", params=params, timeout=45)
            if not response.ok:
                break
            data = response.json()
            features = data.get('features', [])
            if not features:
                break
            all_features.extend(features)
            offset += len(features)
            if len(features) < batch_size:
                break
            time.sleep(0.5)
    except Exception as e:
        logger.warning(f"HIFLD paginated query failed: {e}")
    return all_features


class FiberRouteDiscovery:
    """Discover fiber routes from HIFLD transmission lines, PeeringDB, and OSM"""
    
    PEERINGDB_API = "https://www.peeringdb.com/api"
    OVERPASS_API = "https://overpass-api.de/api/interpreter"
    
    def __init__(self):
        self.new_routes = 0
        self._market_index = 0
        
    def sync(self):
        """Sync fiber routes from multiple live sources"""
        logger.info("🔌 Syncing fiber routes...")
        self.new_routes = 0
        
        self._sync_hifld_transmission_lines()
        self._sync_peeringdb_exchanges()
        self._sync_osm_fiber_cables()
        self._sync_from_learned_apis()
        
        logger.info(f"   ✅ Fiber routes: {self.new_routes} new")
        return self.new_routes
    
    def _sync_hifld_transmission_lines(self):
        """Pull transmission lines from HIFLD near DC markets (rotates 2 markets per cycle)"""
        markets = DC_MARKETS[self._market_index:self._market_index + 2]
        self._market_index = (self._market_index + 2) % len(DC_MARKETS)
        
        for market in markets:
            try:
                features = _query_hifld_nearby(
                    HIFLD_APIS['transmission_lines'],
                    market['lat'], market['lng'],
                    radius_m=50000, max_records=100,
                    return_geometry=False
                )
                for feat in features:
                    attrs = feat.get('attributes', {})
                    
                    voltage = attrs.get('VOLTAGE', 0) or 0
                    owner = attrs.get('OWNER', '') or attrs.get('OPERATOR', '') or 'Unknown'
                    line_id = attrs.get('ID', '') or attrs.get('OBJECTID', '')
                    
                    route = {
                        "name": f"{owner} {voltage}kV Line - {market['name']}"[:200],
                        "provider": str(owner)[:100],
                        "type": "transmission",
                        "start": market['name'],
                        "end": market['name'],
                        "start_lat": market['lat'],
                        "start_lng": market['lng'],
                        "voltage_kv": voltage,
                        "source_id": f"hifld_tl_{line_id}"
                    }
                    self._save_route(route, source='hifld')
                    
                logger.info(f"   📡 HIFLD transmission {market['name']}: {len(features)} lines found")
                time.sleep(1)
            except Exception as e:
                logger.warning(f"   ⚠️ HIFLD transmission failed for {market['name']}: {e}")

    def _sync_peeringdb_exchanges(self):
        """Get IX locations as fiber endpoints (limited batch)"""
        try:
            response = requests.get(f"{self.PEERINGDB_API}/ix?limit=100", timeout=15)
            if response.ok:
                data = response.json().get('data', [])
                for ix in data[:100]:
                    self._save_fiber_endpoint(ix)
                logger.info(f"   📡 PeeringDB IXs: {len(data[:100])} processed")
        except Exception as e:
            logger.warning(f"   ⚠️ PeeringDB IX sync failed: {e}")
    
    def _sync_osm_fiber_cables(self):
        """Get fiber/telecom cables from OSM in DC markets (rotates 3 per cycle)"""
        markets = DC_MARKETS[self._market_index:self._market_index + 3]
        
        for market in markets:
            try:
                query = f"""
                [out:json][timeout:25];
                (
                  way["communication"="line"](around:50000,{market['lat']},{market['lng']});
                  way["utility"="telecom"](around:50000,{market['lat']},{market['lng']});
                  way["man_made"="pipeline"]["substance"="telecommunication"](around:50000,{market['lat']},{market['lng']});
                );
                out center 50;
                """
                response = requests.post(self.OVERPASS_API, data={'data': query}, timeout=30)
                elements = []
                if response.ok:
                    data = response.json()
                    elements = data.get('elements', [])
                    for element in elements:
                        tags = element.get('tags', {})
                        center = element.get('center', {})
                        name = tags.get('name', f"Telecom line near {market['name']}")
                        operator = tags.get('operator', tags.get('owner', 'Unknown'))
                        route = {
                            "name": name[:200],
                            "provider": operator[:100],
                            "type": "fiber",
                            "start": market['name'],
                            "end": market['name'],
                            "start_lat": center.get('lat', 0),
                            "start_lng": center.get('lon', 0),
                            "source_id": f"osm_fiber_{element.get('id', 0)}"
                        }
                        self._save_route(route, source='osm')
                        
                logger.info(f"   📡 OSM fiber {market['name']}: {len(elements)} cables found")
                time.sleep(2)
            except Exception as e:
                logger.warning(f"   ⚠️ OSM fiber failed for {market['name']}: {e}")
    
    def _sync_from_learned_apis(self):
        """Pull fiber data from auto-discovered APIs in learned_infrastructure"""
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name, location, metadata FROM learned_infrastructure
                WHERE category = 'fiber'
                ORDER BY id DESC LIMIT 200
            """)
            for row in cursor.fetchall():
                try:
                    meta = json.loads(row['metadata']) if row['metadata'] else {}
                    route = {
                        "name": row['name'][:200] if row['name'] else 'Unknown',
                        "provider": meta.get('OWNER', meta.get('OPERATOR', 'Discovered')),
                        "type": meta.get('TYPE', 'fiber'),
                        "start": row['location'][:100] if row['location'] else '',
                        "end": '',
                        "source_id": f"learned_fiber_{hash(row['name']) % 10**8}"
                    }
                    self._save_route(route, source='auto_discovery')
                except Exception:
                    pass
            conn.close()
        except Exception as e:
            logger.warning(f"   ⚠️ Learned API fiber sync failed: {e}")
    
    def _save_fiber_endpoint(self, ix):
        """Save IX as fiber endpoint"""
        params = (
            ix.get('name', 'Unknown IX'),
            'Internet Exchange',
            'IX',
            ix.get('city', 'Unknown'),
            'peeringdb',
            f"peeringdb_ix_{ix.get('id')}"
        )
        sql = '''INSERT OR IGNORE INTO fiber_routes 
                (name, provider, route_type, start_location, source, source_id)
                VALUES (?, ?, ?, ?, ?, ?)'''
        rowcount = _safe_write(sql, params)
        if rowcount and rowcount > 0:
            self.new_routes += 1
    
    def _save_route(self, route, source='discovery'):
        """Save a fiber route"""
        try:
            source_id = route.get('source_id', f"{route['provider']}_{route['name']}".replace(" ", "_").lower()[:100])
            rowcount = _safe_write('''
                INSERT OR IGNORE INTO fiber_routes 
                (name, provider, route_type, start_location, end_location, 
                 start_lat, start_lng, end_lat, end_lng, source, source_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                route['name'][:200],
                route.get('provider', 'Unknown')[:100],
                route.get('type', 'terrestrial'),
                route.get('start', ''),
                route.get('end', ''),
                route.get('start_lat'),
                route.get('start_lng'),
                route.get('end_lat'),
                route.get('end_lng'),
                source,
                source_id[:100]
            ))
            if rowcount and rowcount > 0:
                self.new_routes += 1
        except Exception as e:
            logger.warning(f"Error saving route: {e}")


class DCPropertyDiscovery:
    """Discover data center properties from OSM, news, and learned APIs"""
    
    OVERPASS_API = "https://overpass-api.de/api/interpreter"
    
    def __init__(self):
        self.new_properties = 0
        self._market_index = 0
    
    def sync(self):
        """Sync DC properties from live sources"""
        logger.info("🏢 Syncing DC properties...")
        self.new_properties = 0
        
        self._sync_osm_properties()
        self._sync_from_news()
        self._sync_from_learned_apis()
        
        logger.info(f"   ✅ DC properties: {self.new_properties} new")
        return self.new_properties
    
    def _sync_osm_properties(self):
        """Find data center buildings from OpenStreetMap (rotates 3 markets per cycle)"""
        markets = DC_MARKETS[self._market_index:self._market_index + 3]
        self._market_index = (self._market_index + 3) % len(DC_MARKETS)
        
        for market in markets:
            try:
                query = f"""
                [out:json][timeout:25];
                (
                  node["building"="data_centre"](around:80000,{market['lat']},{market['lng']});
                  way["building"="data_centre"](around:80000,{market['lat']},{market['lng']});
                  node["telecom"="data_center"](around:80000,{market['lat']},{market['lng']});
                  way["telecom"="data_center"](around:80000,{market['lat']},{market['lng']});
                  way["building"="industrial"]["operator"~"data|cloud|hosting|colo",i](around:80000,{market['lat']},{market['lng']});
                );
                out center 100;
                """
                response = requests.post(self.OVERPASS_API, data={'data': query}, timeout=30)
                if response.ok:
                    elements = response.json().get('elements', [])
                    for el in elements:
                        tags = el.get('tags', {})
                        center = el.get('center', {})
                        lat = el.get('lat') or center.get('lat', 0)
                        lng = el.get('lon') or center.get('lon', 0)
                        prop = {
                            "name": tags.get('name', f"DC Property near {market['name']}")[:200],
                            "city": market['name'],
                            "state": market['state'],
                            "type": "data_center",
                            "status": "active",
                            "lat": lat,
                            "lng": lng,
                            "source_id": f"osm_prop_{el.get('id', 0)}"
                        }
                        self._save_property(prop, source='osm')
                    logger.info(f"   🏢 OSM properties {market['name']}: {len(elements)} found")
                time.sleep(2)
            except Exception as e:
                logger.warning(f"   ⚠️ OSM property sync failed for {market['name']}: {e}")
    
    def _sync_from_news(self):
        """Extract property listings from news articles"""
        conn = get_db()
        cursor = conn.cursor()
        
        keywords = ['for sale', 'listing', 'available', 'seeking buyer', 'on the market',
                     'acquisition', 'campus', 'new facility', 'expansion']
        for keyword in keywords:
            try:
                cursor.execute('''
                    SELECT title, summary, companies, locations FROM announcements
                    WHERE (title LIKE ? OR summary LIKE ?)
                    AND discovered_at > datetime('now', '-30 days')
                    LIMIT 20
                ''', (f'%{keyword}%', f'%{keyword}%'))
                
                for row in cursor.fetchall():
                    title = (row['title'] or '').lower()
                    summary = (row['summary'] or '').lower()
                    if 'data center' in title or 'data center' in summary or 'datacenter' in title:
                        prop = {
                            "name": row['title'][:200] if row['title'] else 'Unknown',
                            "city": row['locations'].split(',')[0].strip() if row['locations'] else 'Unknown',
                            "state": "",
                            "type": "listing",
                            "status": "available"
                        }
                        self._save_property(prop, source='news')
            except Exception:
                pass
        
        conn.close()
    
    def _sync_from_learned_apis(self):
        """Pull property data from auto-discovered APIs"""
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name, location, metadata FROM learned_infrastructure
                WHERE category IN ('other', 'environmental')
                AND (name LIKE '%data center%' OR name LIKE '%datacenter%' OR name LIKE '%colocation%')
                ORDER BY id DESC LIMIT 100
            """)
            for row in cursor.fetchall():
                try:
                    meta = json.loads(row['metadata']) if row['metadata'] else {}
                    prop = {
                        "name": row['name'][:200] if row['name'] else 'Unknown',
                        "city": row['location'].split(',')[0].strip() if row['location'] else '',
                        "state": row['location'].split(',')[-1].strip() if row['location'] else '',
                        "type": "discovered",
                        "status": "active",
                        "source_id": f"learned_prop_{hash(row['name']) % 10**8}"
                    }
                    self._save_property(prop, source='auto_discovery')
                except Exception:
                    pass
            conn.close()
        except Exception as e:
            logger.warning(f"   ⚠️ Learned API property sync failed: {e}")
    
    def _save_property(self, prop, source='discovery'):
        """Save a DC property"""
        try:
            source_id = prop.get('source_id', f"{prop['name']}_{prop['city']}".replace(" ", "_").lower()[:100])
            rowcount = _safe_write('''
                INSERT OR IGNORE INTO dc_properties 
                (name, city, state, lat, lng, property_type, square_feet, power_capacity_mw, status, source, source_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                prop['name'][:200],
                prop.get('city', '')[:100],
                prop.get('state', ''),
                prop.get('lat'),
                prop.get('lng'),
                prop.get('type', 'facility'),
                prop.get('sqft', 0),
                prop.get('power_mw', 0),
                prop.get('status', 'available'),
                source,
                source_id[:100]
            ))
            if rowcount and rowcount > 0:
                self.new_properties += 1
        except Exception as e:
            logger.warning(f"Error saving property: {e}")


class ConstructionPermitDiscovery:
    """Discover construction permits from HIFLD power plants, OSM, and news"""
    
    OVERPASS_API = "https://overpass-api.de/api/interpreter"
    
    def __init__(self):
        self.new_permits = 0
        self._market_index = 0
    
    def sync(self):
        """Sync construction permits from live sources"""
        logger.info("🏗️ Syncing construction permits...")
        self.new_permits = 0
        
        self._sync_hifld_power_plants()
        self._sync_osm_construction()
        self._sync_from_news()
        
        logger.info(f"   ✅ Construction permits: {self.new_permits} new")
        return self.new_permits
    
    def _sync_hifld_power_plants(self):
        """Pull power plant data near DC markets from HIFLD (rotates 4 markets per cycle)"""
        markets = DC_MARKETS[self._market_index:self._market_index + 4]
        self._market_index = (self._market_index + 4) % len(DC_MARKETS)
        
        for market in markets:
            try:
                features = _query_hifld_nearby(
                    HIFLD_APIS['power_plants'],
                    market['lat'], market['lng'],
                    radius_m=80000, max_records=100
                )
                for feat in features:
                    attrs = feat.get('attributes', {})
                    geom = feat.get('geometry', {})
                    name = attrs.get('NAME', attrs.get('PLANT_NAME', 'Unknown'))
                    capacity = attrs.get('TOTAL_MW', attrs.get('NAMEPCAP', 0)) or 0
                    status = attrs.get('STATUS', attrs.get('OPERATING', 'unknown'))
                    operator = attrs.get('OPERATOR', attrs.get('UTILITY_NAME', ''))
                    plant_id = attrs.get('OBJECTID', attrs.get('ID', ''))
                    
                    permit = {
                        "name": f"{name} ({capacity:.0f}MW)" if capacity else name,
                        "city": market['name'],
                        "state": market['state'],
                        "power_mw": capacity,
                        "owner": operator or 'Unknown',
                        "status": "active" if str(status).lower() in ('op', 'operating', 'active') else 'planned',
                        "lat": geom.get('y', geom.get('lat', 0)),
                        "lng": geom.get('x', geom.get('lon', 0)),
                        "source_id": f"hifld_pp_{plant_id}"
                    }
                    self._save_permit(permit, source='hifld')
                    
                logger.info(f"   🏗️ HIFLD power plants {market['name']}: {len(features)} found")
                time.sleep(1)
            except Exception as e:
                logger.warning(f"   ⚠️ HIFLD power plants failed for {market['name']}: {e}")
    
    def _sync_osm_construction(self):
        """Find data center construction sites from OSM (rotates 3 markets per cycle)"""
        markets = DC_MARKETS[self._market_index:self._market_index + 3]
        
        for market in markets:
            try:
                query = f"""
                [out:json][timeout:25];
                (
                  node["building"="construction"]["name"~"data|server|cloud|colo",i](around:80000,{market['lat']},{market['lng']});
                  way["building"="construction"]["name"~"data|server|cloud|colo",i](around:80000,{market['lat']},{market['lng']});
                  way["landuse"="construction"]["name"~"data|server|cloud|colo",i](around:80000,{market['lat']},{market['lng']});
                  node["construction"="yes"]["building"~"industrial|commercial"](around:80000,{market['lat']},{market['lng']});
                );
                out center 50;
                """
                response = requests.post(self.OVERPASS_API, data={'data': query}, timeout=30)
                if response.ok:
                    elements = response.json().get('elements', [])
                    for el in elements:
                        tags = el.get('tags', {})
                        center = el.get('center', {})
                        permit = {
                            "name": tags.get('name', f"Construction near {market['name']}")[:200],
                            "city": market['name'],
                            "state": market['state'],
                            "owner": tags.get('operator', tags.get('developer', 'Unknown')),
                            "status": "under_construction",
                            "lat": el.get('lat') or center.get('lat', 0),
                            "lng": el.get('lon') or center.get('lon', 0),
                            "source_id": f"osm_constr_{el.get('id', 0)}"
                        }
                        self._save_permit(permit, source='osm')
                    logger.info(f"   🏗️ OSM construction {market['name']}: {len(elements)} found")
                time.sleep(2)
            except Exception as e:
                logger.warning(f"   ⚠️ OSM construction sync failed for {market['name']}: {e}")
    
    def _sync_from_news(self):
        """Extract construction news from articles - expanded keyword set"""
        conn = get_db()
        cursor = conn.cursor()
        
        keywords = ['construction', 'groundbreaking', 'breaking ground', 'new campus',
                     'expansion', 'building permit', 'megawatt', 'hyperscale',
                     'development', 'approved', 'planning commission', 'zoning']
        for keyword in keywords:
            try:
                cursor.execute('''
                    SELECT title, summary, companies, locations FROM announcements
                    WHERE (title LIKE ? OR summary LIKE ?)
                    AND discovered_at > datetime('now', '-30 days')
                    LIMIT 20
                ''', (f'%{keyword}%', f'%{keyword}%'))
                
                for row in cursor.fetchall():
                    title = (row['title'] or '').lower()
                    summary = (row['summary'] or '').lower()
                    if 'data center' in title or 'data center' in summary or 'datacenter' in title or 'hyperscale' in title:
                        permit = {
                            "name": row['title'][:200] if row['title'] else 'Unknown Project',
                            "city": row['locations'].split(',')[0].strip() if row['locations'] else 'Unknown',
                            "state": "",
                            "owner": row['companies'].split(',')[0].strip() if row['companies'] else 'Unknown',
                            "status": "announced"
                        }
                        self._save_permit(permit, source='news')
            except Exception:
                pass
        
        conn.close()
    
    def _save_permit(self, permit, source='discovery'):
        """Save a construction permit"""
        try:
            source_id = permit.get('source_id', f"{permit['name']}_{permit['city']}".replace(" ", "_").lower()[:100])
            rowcount = _safe_write('''
                INSERT OR IGNORE INTO construction_permits 
                (project_name, city, state, square_feet, estimated_power_mw, owner, status, lat, lng, source, source_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                permit['name'][:200],
                permit.get('city', '')[:100],
                permit.get('state', ''),
                permit.get('sqft', 0),
                permit.get('power_mw', 0),
                permit.get('owner', ''),
                permit.get('status', 'announced'),
                permit.get('lat'),
                permit.get('lng'),
                source,
                source_id[:100]
            ))
            if rowcount and rowcount > 0:
                self.new_permits += 1
        except Exception as e:
            logger.warning(f"Error saving permit: {e}")


class SubstationDiscovery:
    """Discover substations from HIFLD (70k+ US substations), OSM, and learned APIs"""
    
    OVERPASS_API = "https://overpass-api.de/api/interpreter"
    
    def __init__(self):
        self.new_substations = 0
        self._market_index = 0
    
    def sync(self):
        """Sync substations from HIFLD and OSM"""
        logger.info("⚡ Syncing substations...")
        self.new_substations = 0
        
        self._sync_hifld_substations()
        self._sync_osm_substations()
        self._sync_from_learned_apis()
        
        logger.info(f"   ✅ Substations: {self.new_substations} new")
        return self.new_substations
    
    def _sync_hifld_substations(self):
        """Pull substations from HIFLD ArcGIS near DC markets (rotates 3 markets per cycle)"""
        markets = DC_MARKETS[self._market_index:self._market_index + 3]
        self._market_index = (self._market_index + 3) % len(DC_MARKETS)
        
        for market in markets:
            try:
                features = _query_hifld_nearby(
                    HIFLD_APIS['substations'],
                    market['lat'], market['lng'],
                    radius_m=50000, max_records=200
                )
                for feat in features:
                    attrs = feat.get('attributes', {})
                    geom = feat.get('geometry', {})
                    
                    name = attrs.get('NAME', attrs.get('SUBSTATION', 'Unknown'))
                    operator = attrs.get('OWNER', attrs.get('OPERATOR', attrs.get('UTILITY', '')))
                    voltage = attrs.get('MAX_VOLT', attrs.get('MIN_VOLT', 0)) or 0
                    if voltage and voltage > 1000:
                        voltage = voltage / 1000
                    capacity = attrs.get('MAX_LOAD', attrs.get('CAPACITY', 0)) or 0
                    sub_id = attrs.get('OBJECTID', attrs.get('ID', ''))
                    state = attrs.get('STATE', attrs.get('STUSPS', market['state']))
                    city = attrs.get('CITY', attrs.get('COUNTY', market['name']))
                    
                    sub = {
                        "name": str(name)[:200],
                        "operator": str(operator)[:100] if operator else 'Unknown',
                        "voltage_kv": voltage,
                        "capacity_mva": capacity,
                        "city": str(city)[:100] if city else market['name'],
                        "state": str(state)[:10] if state else market['state'],
                        "lat": geom.get('y', geom.get('lat', 0)),
                        "lng": geom.get('x', geom.get('lon', 0)),
                        "source_id": f"hifld_sub_{sub_id}"
                    }
                    self._save_substation(sub, source='hifld')
                    
                logger.info(f"   ⚡ HIFLD substations {market['name']}: {len(features)} found")
                time.sleep(1)
            except Exception as e:
                logger.warning(f"   ⚠️ HIFLD substations failed for {market['name']}: {e}")
    
    def _sync_osm_substations(self):
        """Get substations from OSM in DC markets (rotates 4 markets per cycle)"""
        start = self._market_index
        markets = DC_MARKETS[start:start + 4]
        
        for market in markets:
            try:
                query = f"""
                [out:json][timeout:25];
                (
                  node["power"="substation"](around:80000,{market['lat']},{market['lng']});
                  way["power"="substation"](around:80000,{market['lat']},{market['lng']});
                  node["power"="plant"](around:80000,{market['lat']},{market['lng']});
                );
                out center 100;
                """
                response = requests.post(self.OVERPASS_API, data={'data': query}, timeout=30)
                if response.ok:
                    data = response.json()
                    for element in data.get('elements', []):
                        tags = element.get('tags', {})
                        lat = element.get('lat') or element.get('center', {}).get('lat')
                        lng = element.get('lon') or element.get('center', {}).get('lon')
                        
                        if lat and lng:
                            sub = {
                                "name": tags.get('name', f"Substation near {market['name']}")[:200],
                                "operator": tags.get('operator', 'Unknown')[:100],
                                "voltage_kv": self._parse_voltage(tags.get('voltage', '')),
                                "city": market['name'],
                                "state": market['state'],
                                "lat": lat,
                                "lng": lng,
                                "source_id": f"osm_sub_{element.get('id', 0)}"
                            }
                            self._save_substation(sub, source='osm')
                    logger.info(f"   ⚡ OSM substations {market['name']}: {len(data.get('elements', []))} found")
                time.sleep(2)
            except Exception as e:
                logger.warning(f"   ⚠️ OSM substation sync failed for {market['name']}: {e}")
    
    def _sync_from_learned_apis(self):
        """Pull substation/power data from auto-discovered APIs"""
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name, location, metadata FROM learned_infrastructure
                WHERE category = 'power'
                ORDER BY id DESC LIMIT 200
            """)
            for row in cursor.fetchall():
                try:
                    meta = json.loads(row['metadata']) if row['metadata'] else {}
                    voltage = meta.get('MAX_VOLT', meta.get('VOLTAGE', meta.get('KV', 0))) or 0
                    if voltage and voltage > 1000:
                        voltage = voltage / 1000
                    sub = {
                        "name": str(row['name'])[:200] if row['name'] else 'Unknown',
                        "operator": str(meta.get('OWNER', meta.get('OPERATOR', 'Discovered')))[:100],
                        "voltage_kv": voltage,
                        "capacity_mva": meta.get('CAPACITY', 0) or 0,
                        "city": row['location'].split(',')[0].strip() if row['location'] else '',
                        "state": row['location'].split(',')[-1].strip() if row['location'] and ',' in row['location'] else '',
                        "lat": meta.get('LATITUDE', meta.get('LAT', meta.get('Y'))),
                        "lng": meta.get('LONGITUDE', meta.get('LON', meta.get('X'))),
                        "source_id": f"learned_sub_{hash(str(row['name'])) % 10**8}"
                    }
                    self._save_substation(sub, source='auto_discovery')
                except Exception:
                    pass
            conn.close()
        except Exception as e:
            logger.warning(f"   ⚠️ Learned API substation sync failed: {e}")
    
    def _parse_voltage(self, voltage_str):
        """Parse voltage string to kV"""
        try:
            if not voltage_str:
                return 0
            voltage_str = str(voltage_str).replace('kV', '').replace('V', '').strip()
            if ';' in voltage_str:
                voltage_str = voltage_str.split(';')[0]
            voltage = float(voltage_str)
            if voltage > 1000:
                voltage = voltage / 1000
            return voltage
        except:
            return 0
    
    def _save_substation(self, sub, source='discovery'):
        """Save a substation"""
        try:
            source_id = sub.get('source_id', f"{sub['name']}_{sub.get('lat', 0):.4f}_{sub.get('lng', 0):.4f}".replace(" ", "_").lower()[:100])
            rowcount = _safe_write('''
                INSERT OR IGNORE INTO substations 
                (name, operator, voltage_kv, capacity_mva, city, state, lat, lng, source, source_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                sub['name'][:200],
                sub.get('operator', '')[:100],
                sub.get('voltage_kv', 0),
                sub.get('capacity_mva', 0),
                sub.get('city', '')[:100],
                sub.get('state', ''),
                sub.get('lat'),
                sub.get('lng'),
                source,
                source_id[:100]
            ))
            if rowcount and rowcount > 0:
                self.new_substations += 1
        except Exception as e:
            logger.warning(f"Error saving substation: {e}")


class GasPipelineDiscovery:
    """Discover gas pipelines from HIFLD, EIA, and learned APIs.
    
    v2: Bulk nationwide pull instead of slow 3-market rotation.
    Pulls all pipelines, compressor stations, and processing plants
    from HIFLD in paginated batches. Runs twice daily via Railway cron.
    """
    
    def __init__(self):
        self.new_pipelines = 0
        self._market_index = 0
        self._bulk_done = False  # Track if initial bulk pull completed
    
    def sync(self):
        """Sync gas pipelines from HIFLD (bulk) and learned APIs"""
        logger.info("🔥 Syncing gas pipelines...")
        self.new_pipelines = 0
        
        # Phase 1: Bulk nationwide pull (paginated, all records)
        self._sync_hifld_bulk_pipelines()
        self._sync_hifld_bulk_compressors()
        self._sync_hifld_bulk_processing_plants()
        
        # Phase 2: Market-specific enrichment (rotates 5 per cycle for detail)
        self._sync_hifld_market_detail()
        
        # Phase 3: Auto-discovered/learned APIs
        self._sync_from_learned_apis()
        
        logger.info(f"   ✅ Gas pipelines: {self.new_pipelines} new")
        return self.new_pipelines
    
    def _sync_hifld_bulk_pipelines(self):
        """Bulk pull ALL natural gas pipelines from HIFLD nationwide (paginated)"""
        logger.info("   🔥 HIFLD bulk gas pipelines: starting nationwide pull...")
        try:
            features = _query_hifld_paginated(
                HIFLD_APIS['natural_gas_pipelines'],
                where='1=1',
                max_total=50000,
                batch_size=2000
            )
            count = 0
            for feat in features:
                attrs = feat.get('attributes', {})
                geom = feat.get('geometry', {})
                
                name = attrs.get('Pipename', attrs.get('NAME', attrs.get('OPERATOR', 'Unknown Pipeline')))
                operator = attrs.get('Operator', attrs.get('OPERATOR', ''))
                typepipe = attrs.get('Typepipe', attrs.get('TYPE', 'interstate'))
                diameter = attrs.get('Diameter', attrs.get('DIAMETER', 0)) or 0
                status = attrs.get('Status', attrs.get('STATUS', 'Operating'))
                pipe_id = attrs.get('OBJECTID', attrs.get('ID', ''))
                state_code = attrs.get('STATE', attrs.get('State', ''))
                
                # Get centroid from geometry if available
                lat = lng = None
                if geom:
                    if 'paths' in geom and geom['paths']:
                        # Line geometry — use midpoint of first path
                        path = geom['paths'][0]
                        mid = path[len(path) // 2] if path else None
                        if mid and len(mid) >= 2:
                            lng, lat = mid[0], mid[1]
                    elif 'x' in geom and 'y' in geom:
                        lng, lat = geom['x'], geom['y']
                
                if not lat or not lng:
                    continue
                
                # Determine nearest market
                city = self._nearest_market(lat, lng)
                
                pipeline = {
                    "name": f"{name}"[:200] if name else 'Unknown Pipeline',
                    "operator": str(operator)[:100] if operator else 'Unknown',
                    "pipeline_type": str(typepipe)[:50] if typepipe else 'interstate',
                    "diameter_inches": diameter,
                    "status": 'active' if str(status).lower() in ('operating', 'active', 'in service') else str(status).lower()[:50],
                    "city": city,
                    "state": str(state_code)[:2] if state_code else '',
                    "lat": lat,
                    "lng": lng,
                    "source_id": f"hifld_gas_{pipe_id}"
                }
                self._save_pipeline(pipeline, source='hifld')
                count += 1
            
            logger.info(f"   🔥 HIFLD bulk gas pipelines: {len(features)} fetched, {self.new_pipelines} new saved")
        except Exception as e:
            logger.warning(f"   ⚠️ HIFLD bulk gas pipelines failed: {e}")
    
    def _sync_hifld_bulk_compressors(self):
        """Bulk pull ALL natural gas compressor stations from HIFLD"""
        logger.info("   🔥 HIFLD bulk compressor stations: starting...")
        try:
            features = _query_hifld_paginated(
                HIFLD_APIS['natural_gas_compressor'],
                where='1=1',
                max_total=10000,
                batch_size=2000
            )
            before = self.new_pipelines
            for feat in features:
                attrs = feat.get('attributes', {})
                geom = feat.get('geometry', {})
                
                name = attrs.get('NAME', attrs.get('STATION', 'Unknown Compressor'))
                operator = attrs.get('OPERATOR', attrs.get('OWNER', ''))
                comp_id = attrs.get('OBJECTID', attrs.get('ID', ''))
                state_code = attrs.get('STATE', attrs.get('State', ''))
                lat = geom.get('y', 0)
                lng = geom.get('x', 0)
                
                if not lat or not lng:
                    continue
                
                city = self._nearest_market(lat, lng)
                
                pipeline = {
                    "name": f"{name} Compressor Station"[:200],
                    "operator": str(operator)[:100] if operator else 'Unknown',
                    "pipeline_type": "compressor_station",
                    "status": "active",
                    "city": city,
                    "state": str(state_code)[:2] if state_code else '',
                    "lat": lat,
                    "lng": lng,
                    "source_id": f"hifld_comp_{comp_id}"
                }
                self._save_pipeline(pipeline, source='hifld')
            
            logger.info(f"   🔥 HIFLD compressor stations: {len(features)} fetched, {self.new_pipelines - before} new")
        except Exception as e:
            logger.warning(f"   ⚠️ HIFLD bulk compressors failed: {e}")
    
    def _sync_hifld_bulk_processing_plants(self):
        """Bulk pull ALL natural gas processing plants from HIFLD"""
        logger.info("   🔥 HIFLD bulk processing plants: starting...")
        try:
            features = _query_hifld_paginated(
                HIFLD_APIS['natural_gas_processing'],
                where='1=1',
                max_total=5000,
                batch_size=1000
            )
            before = self.new_pipelines
            for feat in features:
                attrs = feat.get('attributes', {})
                geom = feat.get('geometry', {})
                
                name = attrs.get('NAME', attrs.get('PLANT', 'Unknown Processing Plant'))
                operator = attrs.get('OPERATOR', attrs.get('OWNER', ''))
                plant_id = attrs.get('OBJECTID', attrs.get('ID', ''))
                state_code = attrs.get('STATE', attrs.get('State', ''))
                capacity = attrs.get('CAPACITY', attrs.get('CAP_MMCF', 0)) or 0
                lat = geom.get('y', 0)
                lng = geom.get('x', 0)
                
                if not lat or not lng:
                    continue
                
                city = self._nearest_market(lat, lng)
                
                pipeline = {
                    "name": f"{name} Processing Plant"[:200],
                    "operator": str(operator)[:100] if operator else 'Unknown',
                    "pipeline_type": "processing_plant",
                    "diameter_inches": 0,
                    "capacity_mcf": capacity,
                    "status": "active",
                    "city": city,
                    "state": str(state_code)[:2] if state_code else '',
                    "lat": lat,
                    "lng": lng,
                    "source_id": f"hifld_proc_{plant_id}"
                }
                self._save_pipeline(pipeline, source='hifld')
            
            logger.info(f"   🔥 HIFLD processing plants: {len(features)} fetched, {self.new_pipelines - before} new")
        except Exception as e:
            logger.warning(f"   ⚠️ HIFLD bulk processing plants failed: {e}")
    
    def _sync_hifld_market_detail(self):
        """Detailed market-specific pull (rotates 5 markets per cycle for enrichment)"""
        markets = DC_MARKETS[self._market_index:self._market_index + 5]
        self._market_index = (self._market_index + 5) % len(DC_MARKETS)
        
        before = self.new_pipelines
        for market in markets:
            try:
                features = _query_hifld_nearby(
                    HIFLD_APIS['natural_gas_pipelines'],
                    market['lat'], market['lng'],
                    radius_m=80000, max_records=200,
                    return_geometry=False
                )
                for feat in features:
                    attrs = feat.get('attributes', {})
                    name = attrs.get('Pipename', attrs.get('NAME', attrs.get('OPERATOR', 'Unknown Pipeline')))
                    operator = attrs.get('Operator', attrs.get('OPERATOR', ''))
                    typepipe = attrs.get('Typepipe', attrs.get('TYPE', 'interstate'))
                    diameter = attrs.get('Diameter', attrs.get('DIAMETER', 0)) or 0
                    status = attrs.get('Status', attrs.get('STATUS', 'Operating'))
                    pipe_id = attrs.get('OBJECTID', attrs.get('ID', ''))
                    
                    pipeline = {
                        "name": f"{name} - {market['name']}"[:200],
                        "operator": str(operator)[:100] if operator else 'Unknown',
                        "pipeline_type": str(typepipe)[:50] if typepipe else 'interstate',
                        "diameter_inches": diameter,
                        "status": 'active' if str(status).lower() in ('operating', 'active', 'in service') else str(status).lower(),
                        "city": market['name'],
                        "state": market['state'],
                        "lat": market['lat'],
                        "lng": market['lng'],
                        "source_id": f"hifld_gas_mkt_{pipe_id}_{market['state']}"
                    }
                    self._save_pipeline(pipeline, source='hifld')
                
                time.sleep(0.5)
            except Exception as e:
                logger.warning(f"   ⚠️ HIFLD gas detail failed for {market['name']}: {e}")
        
        logger.info(f"   🔥 Market detail enrichment: {self.new_pipelines - before} new from {len(markets)} markets")
    
    def _nearest_market(self, lat, lng):
        """Find nearest DC market name for a given lat/lng"""
        from math import radians, sin, cos, sqrt, atan2
        best = 'Unknown'
        best_dist = float('inf')
        for m in DC_MARKETS:
            dlat = radians(m['lat'] - lat)
            dlng = radians(m['lng'] - lng)
            a = sin(dlat/2)**2 + cos(radians(lat)) * cos(radians(m['lat'])) * sin(dlng/2)**2
            d = 2 * atan2(sqrt(a), sqrt(1 - a)) * 6371
            if d < best_dist:
                best_dist = d
                best = m['name']
        return best if best_dist < 500 else ''  # Only tag if within 500km
    
    def _sync_from_learned_apis(self):
        """Pull gas data from auto-discovered APIs"""
        try:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT name, location, metadata FROM learned_infrastructure
                WHERE category = 'gas'
                ORDER BY id DESC LIMIT 200
            """)
            for row in cursor.fetchall():
                try:
                    meta = json.loads(row['metadata']) if row['metadata'] else {}
                    pipeline = {
                        "name": str(row['name'])[:200] if row['name'] else 'Unknown',
                        "operator": str(meta.get('Operator', meta.get('OPERATOR', 'Discovered')))[:100],
                        "pipeline_type": meta.get('Typepipe', meta.get('TYPE', 'discovered')),
                        "diameter_inches": meta.get('Diameter', 0) or 0,
                        "city": row['location'].split(',')[0].strip() if row['location'] else '',
                        "state": row['location'].split(',')[-1].strip() if row['location'] and ',' in row['location'] else '',
                        "lat": meta.get('LATITUDE', meta.get('LAT', meta.get('Y'))),
                        "lng": meta.get('LONGITUDE', meta.get('LON', meta.get('X'))),
                        "source_id": f"learned_gas_{hash(str(row['name'])) % 10**8}"
                    }
                    self._save_pipeline(pipeline, source='auto_discovery')
                except Exception:
                    pass
            conn.close()
        except Exception as e:
            logger.warning(f"   ⚠️ Learned API gas sync failed: {e}")
    
    def _save_pipeline(self, pipeline, source='discovery'):
        """Save a gas pipeline"""
        try:
            source_id = pipeline.get('source_id', f"{pipeline['name']}".replace(" ", "_").lower()[:100])
            rowcount = _safe_write('''
                INSERT OR IGNORE INTO gas_pipelines
                (name, operator, pipeline_type, diameter_inches, capacity_mcf, status,
                 lat, lng, city, state, source, source_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                pipeline['name'][:200],
                pipeline.get('operator', '')[:100],
                pipeline.get('pipeline_type', 'interstate'),
                pipeline.get('diameter_inches', 0),
                pipeline.get('capacity_mcf', 0),
                pipeline.get('status', 'active'),
                pipeline.get('lat'),
                pipeline.get('lng'),
                pipeline.get('city', '')[:100],
                pipeline.get('state', ''),
                source,
                source_id[:100]
            ))
            if rowcount and rowcount > 0:
                self.new_pipelines += 1
        except Exception as e:
            logger.warning(f"Error saving pipeline: {e}")


class WeeklyLinkedInSummary:
    """Generate and post weekly market digest to LinkedIn"""
    
    def __init__(self):
        self.linkedin_token = os.environ.get('LINKEDIN_ACCESS_TOKEN')
    
    def generate_weekly_digest(self):
        """Generate weekly market digest"""
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) as cnt FROM facilities")
        new_facilities = cursor.fetchone()['cnt']
        
        cursor.execute("SELECT COUNT(*) as cnt FROM announcements WHERE discovered_at > datetime('now', '-7 days')")
        new_news = cursor.fetchone()['cnt']
        
        cursor.execute("SELECT COUNT(*) as cnt FROM construction_permits")
        new_permits = cursor.fetchone()['cnt']
        
        cursor.execute("SELECT SUM(estimated_power_mw) as total FROM construction_permits WHERE status IN ('approved', 'under_construction')")
        pipeline_mw = cursor.fetchone()['total'] or 0
        
        cursor.execute('''
            SELECT title, companies FROM announcements 
            WHERE discovered_at > datetime('now', '-7 days')
            ORDER BY discovered_at DESC LIMIT 5
        ''')
        top_news = cursor.fetchall()
        
        conn.close()
        
        digest = f"""📊 DC Hub Weekly Market Intelligence

This week in data center infrastructure:

📈 Key Metrics:
• {new_facilities:,} new facilities tracked
• {new_news:,} industry news articles
• {new_permits} new construction permits
• {pipeline_mw:,.0f} MW in development pipeline

🔥 Top Headlines:
"""
        for i, news in enumerate(top_news[:3], 1):
            digest += f"{i}. {news['title'][:80]}...\n"
        
        digest += f"""
🌍 Powered by DC Hub - tracking 10,000+ data centers worldwide

#DataCenter #Infrastructure #CloudComputing #DigitalInfrastructure #MarketIntelligence

📡 Real-time data at dchub.cloud"""
        
        return digest
    
    def post_to_linkedin(self, content):
        """Post content to LinkedIn"""
        if not self.linkedin_token:
            logger.warning("⚠️ LinkedIn token not configured")
            return None
        
        try:
            headers = {
                'Authorization': f'Bearer {self.linkedin_token}',
                'Content-Type': 'application/json',
                'X-Restli-Protocol-Version': '2.0.0'
            }
            
            response = requests.get(
                'https://api.linkedin.com/v2/userinfo',
                headers={'Authorization': f'Bearer {self.linkedin_token}'},
                timeout=10
            )
            
            if not response.ok:
                logger.error(f"Failed to get LinkedIn user: {response.status_code}")
                return None
            
            user_info = response.json()
            user_id = user_info.get('sub')
            
            post_data = {
                "author": f"urn:li:person:{user_id}",
                "lifecycleState": "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {"text": content},
                        "shareMediaCategory": "NONE"
                    }
                },
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
            }
            
            post_response = requests.post(
                'https://api.linkedin.com/v2/ugcPosts',
                headers=headers,
                json=post_data,
                timeout=30
            )
            
            if post_response.ok:
                logger.info("✅ Weekly LinkedIn digest posted")
                return post_response.json()
            else:
                logger.error(f"LinkedIn post failed: {post_response.status_code} - {post_response.text}")
                return None
                
        except Exception as e:
            logger.error(f"LinkedIn post error: {e}")
            return None
    
    def save_weekly_post(self, content, post_id=None):
        """Save weekly post to database"""
        conn = get_db()
        cursor = conn.cursor()
        
        today = datetime.now()
        week_start = today - timedelta(days=today.weekday())
        week_end = week_start + timedelta(days=6)
        
        cursor.execute('''
            INSERT INTO linkedin_weekly_posts (week_start, week_end, content, posted_at, post_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (week_start.date(), week_end.date(), content, datetime.now(), post_id))
        
        conn.commit()
        conn.close()
    
    def run_weekly_post(self):
        """Generate and post weekly digest"""
        content = self.generate_weekly_digest()
        result = self.post_to_linkedin(content)
        post_id = result.get('id') if result else None
        self.save_weekly_post(content, post_id)
        return content


class InfrastructureDiscoveryEngine:
    """Main engine that runs all infrastructure discovery"""
    
    def __init__(self):
        init_infrastructure_tables()
        self.fiber = FiberRouteDiscovery()
        self.properties = DCPropertyDiscovery()
        self.permits = ConstructionPermitDiscovery()
        self.substations = SubstationDiscovery()
        self.gas = GasPipelineDiscovery()
        self.linkedin = WeeklyLinkedInSummary()
        self._scheduler_running = False
    
    def run_full_sync(self):
        """Run full infrastructure sync"""
        logger.info("=" * 60)
        logger.info("🔄 INFRASTRUCTURE DISCOVERY SYNC")
        logger.info("=" * 60)
        
        start_time = datetime.now()
        
        fiber_new = self.fiber.sync()
        properties_new = self.properties.sync()
        permits_new = self.permits.sync()
        substations_new = self.substations.sync()
        gas_new = self.gas.sync()
        
        elapsed = (datetime.now() - start_time).total_seconds()
        
        total_new = fiber_new + properties_new + permits_new + substations_new + gas_new
        
        logger.info("=" * 60)
        logger.info(f"✅ INFRASTRUCTURE SYNC COMPLETE in {elapsed:.1f}s — {total_new} total new records")
        logger.info(f"   🔌 Fiber routes: {fiber_new} new")
        logger.info(f"   🏢 Properties: {properties_new} new")
        logger.info(f"   🏗️ Permits: {permits_new} new")
        logger.info(f"   ⚡ Substations: {substations_new} new")
        logger.info(f"   🔥 Gas pipelines: {gas_new} new")
        logger.info("=" * 60)
        
        return {
            "fiber_routes": fiber_new,
            "properties": properties_new,
            "permits": permits_new,
            "substations": substations_new,
            "gas_pipelines": gas_new,
            "total_new": total_new,
            "elapsed_seconds": elapsed
        }
    
    def get_status(self):
        """Get infrastructure discovery status"""
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) as cnt FROM fiber_routes")
        fiber_count = cursor.fetchone()['cnt']
        
        cursor.execute("SELECT COUNT(*) as cnt FROM dc_properties")
        properties_count = cursor.fetchone()['cnt']
        
        cursor.execute("SELECT COUNT(*) as cnt FROM construction_permits")
        permits_count = cursor.fetchone()['cnt']
        
        cursor.execute("SELECT COUNT(*) as cnt FROM substations")
        substations_count = cursor.fetchone()['cnt']
        
        try:
            cursor.execute("SELECT COUNT(*) as cnt FROM gas_pipelines")
            gas_count = cursor.fetchone()['cnt']
        except:
            gas_count = 0
        
        cursor.execute("SELECT COUNT(*) as cnt FROM linkedin_weekly_posts")
        weekly_posts = cursor.fetchone()['cnt']
        
        conn.close()
        
        return {
            "fiber_routes": fiber_count,
            "dc_properties": properties_count,
            "construction_permits": permits_count,
            "substations": substations_count,
            "gas_pipelines": gas_count,
            "weekly_linkedin_posts": weekly_posts,
            "scheduler_running": self._scheduler_running
        }
    
    def start_scheduler(self, interval_hours=6):
        """Start background scheduler"""
        if self._scheduler_running:
            return
        
        self._scheduler_running = True
        
        def scheduler_loop():
            time.sleep(120)
            while self._scheduler_running:
                try:
                    self.run_full_sync()
                except Exception as e:
                    logger.error(f"Infrastructure sync error: {e}")
                time.sleep(interval_hours * 3600)
        
        thread = Thread(target=scheduler_loop, daemon=True)
        thread.start()
        logger.info(f"🔄 Infrastructure Discovery Scheduler started (every {interval_hours} hours)")


def register_infrastructure_routes(app, start_scheduler=True):
    """Register Flask routes for infrastructure API"""
    from flask import Blueprint, jsonify, request
    import threading
    import os
    
    bp = Blueprint('infrastructure', __name__)
    engine = InfrastructureDiscoveryEngine()
    
    if start_scheduler:
        engine.start_scheduler(interval_hours=6)
    
    @bp.route('/api/infrastructure/status')
    def infrastructure_status():
        return jsonify({"success": True, "data": engine.get_status()})
    
    @bp.route('/api/infrastructure/sync', methods=['POST'])
    def infrastructure_sync():
        result = engine.run_full_sync()
        return jsonify({"success": True, "data": result})
    
    @bp.route('/api/infrastructure/fiber-routes')
    def get_fiber_routes():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM fiber_routes ORDER BY created_at DESC LIMIT 100")
        routes = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({"success": True, "data": routes, "count": len(routes)})
    
    @bp.route('/api/infrastructure/properties')
    def get_properties():
        conn = get_db()
        cursor = conn.cursor()
        status = request.args.get('status', 'available')
        cursor.execute("SELECT * FROM dc_properties WHERE status = ? ORDER BY created_at DESC LIMIT 100", (status,))
        properties = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({"success": True, "data": properties, "count": len(properties)})
    
    @bp.route('/api/infrastructure/permits')
    def get_permits():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM construction_permits ORDER BY created_at DESC LIMIT 100")
        permits = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({"success": True, "data": permits, "count": len(permits)})
    
    @bp.route('/api/infrastructure/substations')
    def get_substations():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM substations ORDER BY voltage_kv DESC LIMIT 100")
        substations = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return jsonify({"success": True, "data": substations, "count": len(substations)})
    
    @bp.route('/api/infrastructure/gas-pipelines')
    def get_gas_pipelines():
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT * FROM gas_pipelines ORDER BY created_at DESC LIMIT 200")
            pipelines = [dict(row) for row in cursor.fetchall()]
        except:
            pipelines = []
        conn.close()
        return jsonify({"success": True, "data": pipelines, "count": len(pipelines)})
    
    @bp.route('/api/infrastructure/weekly-digest')
    def get_weekly_digest():
        content = engine.linkedin.generate_weekly_digest()
        return jsonify({"success": True, "content": content})
    
    @bp.route('/api/infrastructure/weekly-digest/post', methods=['POST'])
    def post_weekly_digest():
        content = engine.linkedin.generate_weekly_digest()
        result = engine.linkedin.post_to_linkedin(content)
        engine.linkedin.save_weekly_post(content, result.get('id') if result else None)
        return jsonify({"success": result is not None, "content": content, "posted": result is not None})
    
    app.register_blueprint(bp)
    
    logger.info("🏗️ Infrastructure Discovery API registered:")
    logger.info("   GET  /api/infrastructure/status")
    logger.info("   POST /api/infrastructure/sync")
    logger.info("   GET  /api/infrastructure/fiber-routes")
    logger.info("   GET  /api/infrastructure/properties")
    logger.info("   GET  /api/infrastructure/permits")
    logger.info("   GET  /api/infrastructure/substations")
    logger.info("   GET  /api/infrastructure/gas-pipelines")
    logger.info("   GET  /api/infrastructure/weekly-digest")
    logger.info("   POST /api/infrastructure/weekly-digest/post")
    
    return engine
