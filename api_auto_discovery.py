"""
DC Hub Nexus - API Auto-Discovery & Registration Engine v2.0
=============================================================
Autonomous system that continuously discovers, validates, registers,
and monitors government and industry data APIs.

CAPABILITIES:
- Scans EIA v2 API catalog for new energy datasets
- Monitors USGS, FCC, EPA endpoint catalogs for changes
- Crawls HIFLD and Data.gov for new ArcGIS/REST services
- Auto-validates discovered endpoints (schema, response, latency)
- Registers working APIs into the platform registry
- Detects deprecated/changed endpoints and flags them
- Health-checks existing registered APIs on schedule
- Background scheduler runs autonomously (6-hour cycle)

SOURCES:
- EIA (Energy Information Administration) - v2 catalog
- USGS (US Geological Survey) - earthquake, water, hazards
- FCC (Federal Communications Commission) - broadband
- EPA (Environmental Protection Agency) - emissions, facilities
- HIFLD (Homeland Infrastructure Foundation-Level Data)
- Data.gov CKAN catalog
- State GIS portals
"""

import os
import json
import requests
import hashlib
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import urljoin
from db_utils import get_db, get_read_db

logger = logging.getLogger(__name__)

DB_PATH = 'dc_nexus.db'

KNOWN_API_SOURCES = [
    {
        'name': 'HIFLD Electric Substations',
        'category': 'power',
        'type': 'arcgis',
        'url': None,  # HIFLD decommissioned Aug 2025 — data in Neon,
        'record_count': '70000+',
        'fields': ['NAME', 'CITY', 'STATE', 'STATUS', 'OWNER', 'VOLTAGE']
    },
    {
        'name': 'HIFLD Transmission Lines',
        'category': 'power',
        'type': 'arcgis',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0',
        'record_count': '300000+',
        'fields': ['OWNER', 'VOLTAGE', 'STATUS', 'VOLT_CLASS']
    },
    {
        'name': 'HIFLD Power Plants',
        'category': 'power',
        'type': 'arcgis',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Power_Plants/FeatureServer/0',
        'record_count': '10000+',
        'fields': ['NAME', 'CITY', 'STATE', 'PRIM_FUEL', 'TOTAL_MW', 'OPERATOR']
    },
    {
        'name': 'HIFLD Natural Gas Pipelines',
        'category': 'gas',
        'type': 'arcgis',
        'url': 'https://geo.dot.gov/server/rest/services/Hosted/Natural_Gas_Pipelines_US_EIA/FeatureServer/0',  # replaces HIFLD
        'record_count': '300000+',
        'fields': ['OPERATOR', 'TYPEPIPE', 'DIAMETER']
    },
    {
        'name': 'HIFLD Gas Compressor Stations',
        'category': 'gas',
        'type': 'arcgis',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0',
        'record_count': '1500+',
        'fields': ['NAME', 'OPERATOR', 'STATE']
    },
    {
        'name': 'HIFLD Gas Storage Facilities',
        'category': 'gas',
        'type': 'arcgis',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Storage/FeatureServer/0',
        'record_count': '400+',
        'fields': ['NAME', 'OPERATOR', 'STATE', 'STATUS']
    },
    {
        'name': 'HIFLD Gas Processing Plants',
        'category': 'gas',
        'type': 'arcgis',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Processing_Plants/FeatureServer/0',
        'record_count': '500+',
        'fields': ['NAME', 'OPERATOR', 'STATE', 'CAPACITY']
    },
    {
        'name': 'FRA Railroads',
        'category': 'transportation',
        'type': 'arcgis',
        'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/ArcGIS/rest/services/USA_Railroads_1/FeatureServer/0',
        'record_count': '200000+',
        'fields': ['RROWNER1', 'TRACKS', 'FRAESSION']
    },
    {
        'name': 'HIFLD Airports',
        'category': 'transportation',
        'type': 'arcgis',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Airports_1/FeatureServer/0',
        'record_count': '19000+',
        'fields': ['NAME', 'CITY', 'STATE', 'FAC_TYPE', 'ICAO']
    },
    {
        'name': 'HIFLD Internet Exchange Points',
        'category': 'fiber',
        'type': 'arcgis',
        'url': 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/InternetExchange_Points/FeatureServer/0',
        'record_count': '100+',
        'fields': ['NAME', 'CITY', 'STATE']
    },
    {
        'name': 'USGS Aquifers',
        'category': 'water',
        'type': 'arcgis',
        'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/ArcGIS/rest/services/USA_Principal_Aquifers/FeatureServer/0',
        'record_count': '100+',
        'fields': ['AQ_NAME', 'AQ_CODE', 'ROCK_TYPE']
    },
    {
        'name': 'FEMA Flood Hazard Zones',
        'category': 'environmental',
        'type': 'arcgis',
        'url': 'https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28',
        'record_count': 'dynamic',
        'fields': ['FLD_ZONE', 'ZONE_SUBTY']
    },
    {
        'name': 'USGS Seismic Hazards',
        'category': 'environmental',
        'type': 'arcgis',
        'url': 'https://earthquake.usgs.gov/arcgis/rest/services/eq/pga_2018/MapServer/0',
        'record_count': '681+',
        'fields': ['PGA', 'SA0P2', 'SA1P0']
    },
]

DISCOVERY_TARGETS = [
    {
        'name': 'HIFLD Open Data',
        'url': None,  # HIFLD Open portal decommissioned Aug 2025,
        'type': 'catalog'
    },
    {
        'name': 'EIA Open Data',
        'url': 'https://api.eia.gov/v2/',
        'type': 'catalog'
    },
    {
        'name': 'Data.gov Energy',
        'url': 'https://catalog.data.gov/api/3/action/package_search?q=energy+infrastructure&rows=50',
        'type': 'ckan'
    },
    {
        'name': 'Data.gov Power',
        'url': 'https://catalog.data.gov/api/3/action/package_search?q=power+plants&rows=50',
        'type': 'ckan'
    },
    {
        'name': 'Data.gov Natural Gas',
        'url': 'https://catalog.data.gov/api/3/action/package_search?q=natural+gas+pipeline&rows=50',
        'type': 'ckan'
    },
    {
        'name': 'Data.gov Fiber',
        'url': 'https://catalog.data.gov/api/3/action/package_search?q=fiber+optic+broadband&rows=50',
        'type': 'ckan'
    },
]

EIA_ROUTE_CATALOG = [
    {'route': 'electricity/retail-sales/data/', 'name': 'EIA Retail Electricity Sales', 'category': 'power', 'description': 'Monthly retail electricity prices and sales by state/sector'},
    {'route': 'electricity/facility-fuel/data/', 'name': 'EIA Facility Fuel Data', 'category': 'power', 'description': 'Power plant generation by fuel type'},
    {'route': 'electricity/operating-generator-capacity/data/', 'name': 'EIA Generator Capacity', 'category': 'power', 'description': 'Operating generator nameplate capacity'},
    {'route': 'electricity/state-electricity-profiles/emissions-by-state-by-fuel/data/', 'name': 'EIA Emissions by State', 'category': 'environmental', 'description': 'CO2 emissions from electricity generation by state'},
    {'route': 'electricity/rto/fuel-type-data/data/', 'name': 'EIA RTO Fuel Mix', 'category': 'power', 'description': 'Real-time fuel mix by RTO/ISO region'},
    {'route': 'electricity/rto/region-data/data/', 'name': 'EIA RTO Demand', 'category': 'power', 'description': 'Real-time demand by RTO/ISO region'},
    {'route': 'electricity/rto/interchange-data/data/', 'name': 'EIA RTO Interchange', 'category': 'power', 'description': 'Inter-regional power flows'},
    {'route': 'natural-gas/sum/lsum/data/', 'name': 'EIA Natural Gas Summary', 'category': 'gas', 'description': 'Natural gas production, consumption, and prices'},
    {'route': 'natural-gas/pri/sum/data/', 'name': 'EIA Gas Prices Summary', 'category': 'gas', 'description': 'Natural gas price summaries'},
    {'route': 'natural-gas/move/poe1/data/', 'name': 'EIA Gas Pipeline Imports', 'category': 'gas', 'description': 'Natural gas pipeline import volumes'},
    {'route': 'natural-gas/stor/wkly/data/', 'name': 'EIA Gas Storage Weekly', 'category': 'gas', 'description': 'Weekly natural gas storage report'},
    {'route': 'petroleum/pri/spt/data/', 'name': 'EIA Petroleum Spot Prices', 'category': 'power', 'description': 'Spot prices for crude oil and petroleum products'},
    {'route': 'co2-emissions/co2-emissions-aggregates/data/', 'name': 'EIA CO2 Emissions', 'category': 'environmental', 'description': 'CO2 emissions by sector and fuel'},
    {'route': 'seds/data/', 'name': 'EIA State Energy Data', 'category': 'power', 'description': 'Comprehensive state energy consumption and expenditure'},
    {'route': 'total-energy/data/', 'name': 'EIA Total Energy Monthly', 'category': 'power', 'description': 'Monthly energy review statistics'},
]

USGS_ENDPOINTS = [
    {'name': 'USGS Earthquake Feed 30d M2.5+', 'category': 'environmental', 'type': 'geojson',
     'url': 'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_month.geojson',
     'description': 'Past 30 days earthquakes magnitude 2.5+'},
    {'name': 'USGS Earthquake Feed 7d M4.5+', 'category': 'environmental', 'type': 'geojson',
     'url': 'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_week.geojson',
     'description': 'Past 7 days earthquakes magnitude 4.5+'},
    {'name': 'USGS Water Services Sites', 'category': 'water', 'type': 'rest',
     'url': 'https://waterservices.usgs.gov/nwis/site/?format=rdb&stateCd=VA&siteType=GW&siteStatus=active&hasDataTypeCd=gw',
     'description': 'Active groundwater monitoring sites'},
    {'name': 'USGS Water Services Daily Values', 'category': 'water', 'type': 'rest',
     'url': 'https://waterservices.usgs.gov/nwis/dv/?format=json&stateCd=TX&parameterCd=72019&siteStatus=active',
     'description': 'Daily groundwater level values'},
    {'name': 'USGS Streamflow Instantaneous', 'category': 'water', 'type': 'rest',
     'url': 'https://waterservices.usgs.gov/nwis/iv/?format=json&stateCd=VA&parameterCd=00060&siteStatus=active',
     'description': 'Real-time streamflow data'},
]

FCC_ENDPOINTS = [
    {'name': 'FCC Broadband Map Fixed', 'category': 'fiber', 'type': 'rest',
     'url': 'https://broadbandmap.fcc.gov/api/public/map/listAvailabilityFixed',
     'description': 'Fixed broadband availability by location'},
    {'name': 'FCC Broadband Map Summary', 'category': 'fiber', 'type': 'rest',
     'url': 'https://broadbandmap.fcc.gov/api/public/map/summary/fixed',
     'description': 'Broadband deployment summary statistics'},
    {'name': 'FCC Area API Census Block', 'category': 'fiber', 'type': 'rest',
     'url': 'https://geo.fcc.gov/api/census/area',
     'description': 'Census block/tract lookup for coverage analysis'},
]

EPA_ENDPOINTS = [
    {'name': 'EPA FLIGHT GHG Facility', 'category': 'environmental', 'type': 'rest',
     'url': 'https://ghgdata.epa.gov/ghgp/service/facilityDetail?id=1000001&year=2023',
     'description': 'Individual facility GHG emissions detail'},
    {'name': 'EPA Envirofacts ICIS Air', 'category': 'environmental', 'type': 'rest',
     'url': 'https://data.epa.gov/efservice/ICIS_AIR/STATE_CODE/VA/JSON/0:10',
     'description': 'EPA ICIS air emissions data by state'},
    {'name': 'EPA Envirofacts TRI', 'category': 'environmental', 'type': 'rest',
     'url': 'https://data.epa.gov/efservice/TRI_FACILITY/STATE_ABBR/TX/JSON/0:10',
     'description': 'Toxic Release Inventory facility data'},
    {'name': 'EPA Envirofacts SDWIS Water', 'category': 'water', 'type': 'rest',
     'url': 'https://data.epa.gov/efservice/WATER_SYSTEM/STATE_CODE/VA/JSON/0:10',
     'description': 'Safe Drinking Water Information System'},
    {'name': 'EPA AirNow Current AQI', 'category': 'environmental', 'type': 'rest',
     'url': 'https://www.airnowapi.org/aq/observation/latLong/current/?format=application/json&latitude=38.9&longitude=-77.0&distance=25',
     'description': 'Real-time Air Quality Index by location'},
]

DATA_GOV_SEARCHES = [
    {'query': 'data+center+colocation', 'category': 'fiber'},
    {'query': 'energy+infrastructure+power', 'category': 'power'},
    {'query': 'natural+gas+pipeline', 'category': 'gas'},
    {'query': 'fiber+optic+broadband+telecom', 'category': 'fiber'},
    {'query': 'water+supply+groundwater', 'category': 'water'},
    {'query': 'seismic+earthquake+hazard', 'category': 'environmental'},
    {'query': 'electric+grid+transmission', 'category': 'power'},
    {'query': 'solar+wind+renewable+energy', 'category': 'power'},
    {'query': 'building+permit+construction', 'category': 'other'},
    {'query': 'land+use+zoning+parcel', 'category': 'other'},
]

STATE_GIS_ENDPOINTS = [
    {'name': 'TX RRC Oil/Gas Wells', 'category': 'power', 'type': 'rest',
     'url': 'https://gis.rrc.texas.gov/arcgis/rest/services/public/RRCGIS_Wells/MapServer/0/query?where=1%3D1&outFields=*&f=json&resultRecordCount=5',
     'description': 'Texas Railroad Commission oil/gas well locations', 'scan_frequency': 'daily'},
    {'name': 'TX RRC Pipelines', 'category': 'gas', 'type': 'arcgis',
     'url': 'https://gis.rrc.texas.gov/arcgis/rest/services/public/RRCGIS_Pipeline/MapServer/0',
     'description': 'Texas pipeline permits and routes', 'scan_frequency': 'daily'},
    {'name': 'CA CEC Power Plants', 'category': 'power', 'type': 'rest',
     'url': 'https://www.energy.ca.gov/data-reports/energy-almanac/california-power-plants',
     'description': 'California Energy Commission power plant database', 'scan_frequency': 'daily'},
    {'name': 'VA DEQ Environmental Data', 'category': 'environmental', 'type': 'rest',
     'url': 'https://apps.deq.virginia.gov/connector/services',
     'description': 'Virginia DEQ environmental monitoring for NoVA market', 'scan_frequency': 'daily'},
    {'name': 'AZ Corporation Commission Solar', 'category': 'power', 'type': 'rest',
     'url': 'https://azcc.gov/utilities/electric',
     'description': 'Arizona Corporation Commission utility filings and solar data', 'scan_frequency': 'daily'},
]

FEDERAL_AGENCY_ENDPOINTS = [
    {'name': 'NOAA Climate Normals Monthly', 'category': 'environmental', 'type': 'rest',
     'url': 'https://www.ncei.noaa.gov/cdo-web/api/v2/data%sdatasetid=NORMAL_MLY&limit=5',
     'description': 'NOAA monthly climate normals for cooling degree day analysis', 'scan_frequency': 'daily'},
    {'name': 'NOAA Climate Daily Summaries', 'category': 'environmental', 'type': 'rest',
     'url': 'https://www.ncei.noaa.gov/cdo-web/api/v2/data%sdatasetid=GHCND&limit=5',
     'description': 'NOAA daily climate summaries for temperature analysis', 'scan_frequency': 'daily'},
    {'name': 'DOE Grid Modernization Projects', 'category': 'power', 'type': 'rest',
     'url': 'https://www.energy.gov/oe/services/technology-development/smart-grid',
     'description': 'DOE Office of Electricity grid modernization data', 'scan_frequency': 'daily'},
    {'name': 'BLM Public Lands', 'category': 'other', 'type': 'arcgis',
     'url': 'https://gis.blm.gov/arcgis/rest/services/lands/BLM_Natl_SMA_LimitedDisp_Cached_Tiles/MapServer/0',
     'description': 'BLM federal land surface management for large DC site selection', 'scan_frequency': 'daily'},
    {'name': 'Census TIGER Urban Areas', 'category': 'other', 'type': 'rest',
     'url': 'https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_ACS2023/MapServer/0/query?where=1%3D1&outFields=*&f=json&resultRecordCount=5',
     'description': 'Census TIGER urban area boundaries for demographic analysis', 'scan_frequency': 'daily'},
]

INTERCONNECTION_QUEUE_ENDPOINTS = [
    {'name': 'PJM Interconnection Queue', 'category': 'power', 'type': 'rest',
     'url': 'https://www.pjm.com/pub/planning/queue-new-services-requests.xlsx',
     'description': 'PJM generation and load interconnection queue (Mid-Atlantic)', 'scan_frequency': '6hours'},
    {'name': 'ERCOT Interconnection Queue', 'category': 'power', 'type': 'rest',
     'url': 'https://www.ercot.com/gridinfo/generation',
     'description': 'ERCOT Texas generation interconnection queue', 'scan_frequency': '6hours'},
    {'name': 'CAISO Interconnection Queue', 'category': 'power', 'type': 'rest',
     'url': 'https://rimspub.caiso.com/rimsui/logon.do',
     'description': 'CAISO California interconnection queue', 'scan_frequency': '6hours'},
    {'name': 'FERC eLibrary Filings', 'category': 'power', 'type': 'rest',
     'url': 'https://elibrary.ferc.gov/eLibrary/search',
     'description': 'FERC regulatory filings and rate cases', 'scan_frequency': 'daily'},
]

ALL_EXPANDED_SOURCES = STATE_GIS_ENDPOINTS + FEDERAL_AGENCY_ENDPOINTS + INTERCONNECTION_QUEUE_ENDPOINTS


class APIAutoDiscovery:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'DC-Hub-Nexus/2.0 (Infrastructure Intelligence Platform)'
        })
        self.eia_api_key = os.environ.get('EIA_API_KEY', '')
        self._cache = {
            'last_cycle': None,
            'last_results': None,
            'new_apis_this_cycle': [],
            'deprecated_apis': [],
            'health_report': {}
        }
        self._scheduler_running = False
        self.init_tables()

    def init_tables(self):
        conn = get_db(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS discovered_apis (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                category TEXT,
                api_type TEXT,
                url TEXT UNIQUE,
                record_count TEXT,
                fields TEXT,
                status TEXT DEFAULT 'discovered',
                last_tested TEXT,
                test_result TEXT,
                integrated INTEGER DEFAULT 0,
                discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_discovery_log (
                id SERIAL PRIMARY KEY,
                action TEXT,
                source TEXT,
                apis_found INTEGER DEFAULT 0,
                apis_tested INTEGER DEFAULT 0,
                apis_integrated INTEGER DEFAULT 0,
                details TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS learned_infrastructure (
                id SERIAL PRIMARY KEY,
                category TEXT,
                item_type TEXT,
                name TEXT,
                location TEXT,
                source_api TEXT,
                metadata TEXT,
                learned_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(category, item_type, name, location)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_health_checks (
                id SERIAL PRIMARY KEY,
                api_id INTEGER,
                url TEXT,
                status_code INTEGER,
                response_time_ms REAL,
                record_count INTEGER,
                schema_hash TEXT,
                is_healthy INTEGER DEFAULT 1,
                error_message TEXT,
                checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(api_id) REFERENCES discovered_apis(id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_change_events (
                id SERIAL PRIMARY KEY,
                api_id INTEGER,
                event_type TEXT,
                old_value TEXT,
                new_value TEXT,
                description TEXT,
                detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
                acknowledged INTEGER DEFAULT 0,
                FOREIGN KEY(api_id) REFERENCES discovered_apis(id)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS api_registry (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                url TEXT UNIQUE,
                api_type TEXT,
                auth_type TEXT DEFAULT 'none',
                last_success TEXT,
                items_fetched INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                discovered_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        conn.close()

    def seed_known_apis(self) -> Dict:
        results = {'added': 0, 'existing': 0}

        conn = get_db(self.db_path)
        cursor = conn.cursor()

        for api in KNOWN_API_SOURCES:
            try:
                cursor.execute('''
                    INSERT INTO discovered_apis
                    (name, category, api_type, url, record_count, fields, status)
                    VALUES (%s, %s, %s, %s, %s, %s, 'verified') ON CONFLICT DO NOTHING
                ''', (
                    api['name'], api['category'], api['type'],
                    api['url'], api['record_count'], json.dumps(api['fields'])
                ))
                if cursor.rowcount > 0:
                    results['added'] += 1
                else:
                    results['existing'] += 1
            except Exception:
                pass

        conn.commit()
        conn.close()
        return results

    def test_api(self, url: str, api_type: str = 'arcgis') -> Dict:
        result = {'success': False, 'record_count': 0, 'sample_fields': [], 'response_time_ms': 0}

        try:
            start = time.time()

            if api_type == 'arcgis':
                test_url = f"{url}/query?where=1=1&outFields=*&returnCountOnly=true&f=json"
                response = self.session.get(test_url, timeout=15)
                result['response_time_ms'] = round((time.time() - start) * 1000, 1)

                if response.status_code == 200:
                    data = response.json()
                    if 'count' in data:
                        result['success'] = True
                        result['record_count'] = data['count']

                    fields_url = f"{url}?f=json"
                    fields_resp = self.session.get(fields_url, timeout=10)
                    if fields_resp.status_code == 200:
                        fields_data = fields_resp.json()
                        if 'fields' in fields_data:
                            result['sample_fields'] = [f['name'] for f in fields_data['fields'][:10]]

            elif api_type == 'eia':
                params = {'api_key': self.eia_api_key, 'length': 1} if self.eia_api_key else {'length': 1}
                response = self.session.get(url, params=params, timeout=15)
                result['response_time_ms'] = round((time.time() - start) * 1000, 1)

                if response.status_code == 200:
                    data = response.json()
                    resp_data = data.get('response', {})
                    if 'data' in resp_data or 'total' in resp_data:
                        result['success'] = True
                        result['record_count'] = resp_data.get('total', len(resp_data.get('data', [])))
                        if resp_data.get('data') and len(resp_data['data']) > 0:
                            result['sample_fields'] = list(resp_data['data'][0].keys())[:10]

            elif api_type == 'geojson':
                response = self.session.get(url, timeout=20)
                result['response_time_ms'] = round((time.time() - start) * 1000, 1)

                if response.status_code == 200:
                    data = response.json()
                    features = data.get('features', [])
                    result['success'] = True
                    result['record_count'] = len(features)
                    if features and 'properties' in features[0]:
                        result['sample_fields'] = list(features[0]['properties'].keys())[:10]

            elif api_type == 'rest':
                response = self.session.get(url, timeout=15)
                result['response_time_ms'] = round((time.time() - start) * 1000, 1)

                if response.status_code == 200:
                    result['success'] = True
                    try:
                        data = response.json()
                        if isinstance(data, list):
                            result['record_count'] = len(data)
                            if data and isinstance(data[0], dict):
                                result['sample_fields'] = list(data[0].keys())[:10]
                        elif isinstance(data, dict):
                            result['record_count'] = 1
                            result['sample_fields'] = list(data.keys())[:10]
                    except Exception:
                        pass

        except requests.exceptions.Timeout:
            result['error'] = 'timeout'
        except requests.exceptions.ConnectionError:
            result['error'] = 'connection_failed'
        except Exception as e:
            result['error'] = str(e)[:200]

        return result

    def discover_eia_catalog(self) -> Dict:
        results = {'checked': 0, 'new': 0, 'updated': 0, 'errors': 0}

        if not self.eia_api_key:
            logger.warning("No EIA_API_KEY set, skipping EIA catalog discovery")
            return results

        base_url = 'https://api.eia.gov/v2/'

        for route_info in EIA_ROUTE_CATALOG:
            try:
                full_url = base_url + route_info['route']
                results['checked'] += 1

                test_result = self.test_api(full_url, 'eia')
                status = 'working' if test_result['success'] else 'failed'

                added = self._add_discovered_api({
                    'name': route_info['name'],
                    'category': route_info['category'],
                    'type': 'eia',
                    'url': full_url,
                    'description': route_info.get('description', '')
                }, status=status, test_result=test_result)

                if added:
                    results['new'] += 1
                    if test_result['success']:
                        self._cache['new_apis_this_cycle'].append(route_info['name'])

                time.sleep(0.5)
            except Exception as e:
                results['errors'] += 1
                logger.debug(f"EIA catalog error for {route_info['name']}: {e}")

        try:
            catalog_url = f"{base_url}?api_key={self.eia_api_key}"
            response = self.session.get(catalog_url, timeout=15)
            if response.status_code == 200:
                data = response.json()
                routes = data.get('response', {}).get('routes', [])
                for route in routes:
                    route_id = route.get('id', '')
                    route_name = route.get('name', '')
                    route_desc = route.get('description', '')

                    if route_id and route_id not in ['electricity', 'natural-gas', 'petroleum',
                                                      'coal', 'co2-emissions', 'seds', 'total-energy',
                                                      'steo', 'aeo', 'ieo', 'densified-biomass']:
                        sub_url = f"{base_url}{route_id}/"
                        self._add_discovered_api({
                            'name': f'EIA {route_name}',
                            'category': self._categorize_api(route_name),
                            'type': 'eia',
                            'url': sub_url,
                            'description': route_desc
                        })
        except Exception as e:
            logger.debug(f"EIA root catalog scan error: {e}")

        logger.info(f"EIA Catalog: checked={results['checked']}, new={results['new']}")
        return results

    def discover_usgs_endpoints(self) -> Dict:
        results = {'checked': 0, 'new': 0, 'working': 0}

        for ep in USGS_ENDPOINTS:
            try:
                results['checked'] += 1
                test_result = self.test_api(ep['url'], ep['type'])
                status = 'working' if test_result['success'] else 'failed'

                if test_result['success']:
                    results['working'] += 1

                added = self._add_discovered_api({
                    'name': ep['name'],
                    'category': ep['category'],
                    'type': ep['type'],
                    'url': ep['url'],
                    'description': ep.get('description', '')
                }, status=status, test_result=test_result)

                if added:
                    results['new'] += 1
                    if test_result['success']:
                        self._cache['new_apis_this_cycle'].append(ep['name'])

                time.sleep(0.5)
            except Exception as e:
                logger.debug(f"USGS endpoint error: {e}")

        try:
            eq_catalog_url = 'https://earthquake.usgs.gov/fdsnws/event/1/catalogs'
            resp = self.session.get(eq_catalog_url, timeout=10)
            if resp.status_code == 200:
                self._add_discovered_api({
                    'name': 'USGS FDSN Event Catalogs',
                    'category': 'environmental',
                    'type': 'rest',
                    'url': eq_catalog_url,
                    'description': 'FDSN earthquake event catalog listing'
                }, status='working')
        except Exception:
            pass

        logger.info(f"USGS Endpoints: checked={results['checked']}, new={results['new']}, working={results['working']}")
        return results

    def discover_fcc_endpoints(self) -> Dict:
        results = {'checked': 0, 'new': 0, 'working': 0}

        for ep in FCC_ENDPOINTS:
            try:
                results['checked'] += 1
                test_result = self.test_api(ep['url'], ep['type'])
                status = 'working' if test_result['success'] else 'discovered'

                if test_result['success']:
                    results['working'] += 1

                added = self._add_discovered_api({
                    'name': ep['name'],
                    'category': ep['category'],
                    'type': ep['type'],
                    'url': ep['url'],
                    'description': ep.get('description', '')
                }, status=status, test_result=test_result)

                if added:
                    results['new'] += 1

                time.sleep(0.3)
            except Exception as e:
                logger.debug(f"FCC endpoint error: {e}")

        logger.info(f"FCC Endpoints: checked={results['checked']}, new={results['new']}, working={results['working']}")
        return results

    def discover_epa_endpoints(self) -> Dict:
        results = {'checked': 0, 'new': 0, 'working': 0}

        for ep in EPA_ENDPOINTS:
            try:
                results['checked'] += 1
                test_result = self.test_api(ep['url'], ep['type'])
                status = 'working' if test_result['success'] else 'discovered'

                if test_result['success']:
                    results['working'] += 1

                added = self._add_discovered_api({
                    'name': ep['name'],
                    'category': ep['category'],
                    'type': ep['type'],
                    'url': ep['url'],
                    'description': ep.get('description', '')
                }, status=status, test_result=test_result)

                if added:
                    results['new'] += 1

                time.sleep(0.3)
            except Exception as e:
                logger.debug(f"EPA endpoint error: {e}")

        logger.info(f"EPA Endpoints: checked={results['checked']}, new={results['new']}, working={results['working']}")
        return results

    def discover_from_data_gov(self) -> Dict:
        results = {'sources_checked': 0, 'apis_found': 0, 'apis_added': 0}

        for search in DATA_GOV_SEARCHES[:5]:
            try:
                url = f"https://catalog.data.gov/api/3/action/package_search?q={search['query']}&rows=50"
                response = self.session.get(url, timeout=20)
                results['sources_checked'] += 1

                if response.status_code == 200:
                    data = response.json()
                    if data.get('success') and 'result' in data:
                        packages = data['result'].get('results', [])

                        for pkg in packages:
                            resources = pkg.get('resources', [])
                            for res in resources:
                                res_url = res.get('url', '')
                                res_format = res.get('format', '').lower()

                                api_type = None
                                if 'arcgis' in res_url.lower() or 'featureserver' in res_url.lower():
                                    api_type = 'arcgis'
                                elif res_format in ('json', 'api', 'geojson'):
                                    api_type = 'rest'
                                elif 'api' in res_url.lower() and res_format not in ('csv', 'pdf', 'xlsx', 'zip', 'html'):
                                    api_type = 'rest'

                                if api_type:
                                    api_info = {
                                        'name': pkg.get('title', res.get('name', 'Unknown'))[:200],
                                        'category': search['category'],
                                        'type': api_type,
                                        'url': res_url
                                    }
                                    if self._add_discovered_api(api_info):
                                        results['apis_added'] += 1
                                    results['apis_found'] += 1

                time.sleep(1)
            except Exception as e:
                logger.debug(f"Data.gov search error for '{search['query']}': {e}")

        logger.info(f"Data.gov: sources_checked={results['sources_checked']}, found={results['apis_found']}, added={results['apis_added']}")
        return results

    def discover_from_hifld(self) -> Dict:
        results = {'apis_found': 0, 'apis_added': 0}

        try:
            search_url = 'https://hifld-geoplatform.opendata.arcgis.com/api/v2/datasets?page[size]=100'
            response = self.session.get(search_url, timeout=20)

            if response.status_code == 200:
                data = response.json()
                datasets = data.get('data', [])

                for ds in datasets:
                    attrs = ds.get('attributes', {})
                    name = attrs.get('name', '')
                    url = attrs.get('url', '')

                    if 'FeatureServer' in url or 'MapServer' in url:
                        api_info = {
                            'name': f"HIFLD {name}",
                            'category': self._categorize_api(name),
                            'type': 'arcgis',
                            'url': url
                        }
                        if self._add_discovered_api(api_info):
                            results['apis_added'] += 1
                        results['apis_found'] += 1

        except Exception as e:
            logger.debug(f"HIFLD discovery error: {e}")

        logger.info(f"HIFLD: found={results['apis_found']}, added={results['apis_added']}")
        return results

    def health_check_registered_apis(self) -> Dict:
        results = {'checked': 0, 'healthy': 0, 'degraded': 0, 'down': 0, 'schema_changes': 0}

        # PHASE 1: short DB read to grab the work list, then RELEASE the connection
        # so it can't be held across the slow ArcGIS HTTP calls below.
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, url, api_type, record_count, test_result
            FROM discovered_apis
            WHERE (status = 'working' OR status = 'verified')
            AND (last_tested IS NULL OR last_tested < datetime('now', '-6 hours'))
            LIMIT 30
        ''')
        apis = cursor.fetchall()
        conn.close()

        # PHASE 2: do all HTTP work without holding any DB connection.
        # Buffer per-API outcomes in memory.
        update_rows = []   # for discovered_apis UPDATE
        health_rows = []   # for api_health_checks INSERT
        change_events = [] # (api_id, event_type, old, new, description)

        for api_id, name, url, api_type, prev_count, prev_result_json in apis:
            try:
                test_result = self.test_api(url, api_type or 'arcgis')
                results['checked'] += 1

                is_healthy = test_result['success']
                response_time = test_result.get('response_time_ms', 0)

                if is_healthy and response_time > 5000:
                    is_healthy = False
                    results['degraded'] += 1
                elif is_healthy:
                    results['healthy'] += 1
                else:
                    results['down'] += 1

                prev_fields = []
                if prev_result_json:
                    try:
                        prev_result = json.loads(prev_result_json)
                        prev_fields = prev_result.get('sample_fields', [])
                    except Exception:
                        pass

                new_fields = test_result.get('sample_fields', [])
                if prev_fields and new_fields and set(prev_fields) != set(new_fields):
                    results['schema_changes'] += 1
                    schema_hash = hashlib.sha256(json.dumps(sorted(new_fields)).encode()).hexdigest()[:16]
                    change_events.append((api_id, 'schema_change',
                        json.dumps(prev_fields), json.dumps(new_fields),
                        f"Schema changed: fields went from {len(prev_fields)} to {len(new_fields)}"))
                else:
                    schema_hash = hashlib.sha256(json.dumps(sorted(new_fields)).encode()).hexdigest()[:16] if new_fields else ''

                prev_count_int = 0
                try:
                    prev_count_int = int(prev_count or 0)
                except (ValueError, TypeError):
                    pass

                new_count = test_result.get('record_count', 0)
                if prev_count_int > 0 and new_count > 0:
                    change_pct = abs(new_count - prev_count_int) / prev_count_int * 100
                    if change_pct > 20:
                        change_events.append((api_id, 'record_count_change',
                            str(prev_count_int), str(new_count),
                            f"Record count changed by {change_pct:.1f}% ({prev_count_int} -> {new_count})"))

                status = 'working' if is_healthy else 'degraded' if response_time > 5000 else 'failed'
                now_iso = datetime.now().isoformat()

                update_rows.append((status, now_iso, json.dumps(test_result),
                    str(new_count) if new_count else prev_count, now_iso, api_id))
                health_rows.append((api_id, url, 200 if is_healthy else 0, response_time,
                    new_count, schema_hash, 1 if is_healthy else 0,
                    test_result.get('error', '')))

                time.sleep(0.5)
            except Exception as e:
                logger.debug(f"Health check error for {name}: {e}")

        # PHASE 3: short DB write to flush all buffered results in one connection.
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            for row in update_rows:
                try:
                    cursor.execute('''
                        UPDATE discovered_apis
                        SET status = %s, last_tested = %s, test_result = %s,
                            record_count = %s, updated_at = %s
                        WHERE id = %s
                    ''', row)
                except Exception:
                    pass
            for row in health_rows:
                try:
                    cursor.execute('''
                        INSERT INTO api_health_checks
                        (api_id, url, status_code, response_time_ms, record_count, schema_hash, is_healthy, error_message)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                    ''', row)
                except Exception:
                    pass
            for ev in change_events:
                try:
                    self._log_change_event(cursor, ev[0], ev[1], ev[2], ev[3], ev[4])
                except Exception:
                    pass
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Health check write phase failed: {e}")

        self._cache['health_report'] = results
        logger.info(f"Health Check: checked={results['checked']}, healthy={results['healthy']}, down={results['down']}, schema_changes={results['schema_changes']}")
        return results

    def auto_register_new_apis(self) -> Dict:
        results = {'candidates': 0, 'registered': 0, 'skipped': 0}

        conn = get_db(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, name, category, url, api_type, record_count, test_result
            FROM discovered_apis
            WHERE status = 'working' AND integrated = 0
            AND last_tested > NOW() - INTERVAL '24 hours'
        ''')

        candidates = cursor.fetchall()
        results['candidates'] = len(candidates)

        for api_id, name, category, url, api_type, record_count, test_result_json in candidates:
            try:
                record_ct = 0
                try:
                    record_ct = int(record_count or 0)
                except (ValueError, TypeError):
                    pass

                response_time = 0
                if test_result_json:
                    try:
                        tr = json.loads(test_result_json)
                        response_time = tr.get('response_time_ms', 0)
                    except Exception:
                        pass

                if response_time > 10000:
                    results['skipped'] += 1
                    continue

                for attempt in range(3):
                    try:
                        cursor.execute('''
                            INSERT INTO api_registry 
                            (name, url, api_type, auth_type, last_success, items_fetched, enabled, discovered_at)
                            VALUES (%s, %s, %s, %s, %s, %s, 1, %s) ON CONFLICT DO NOTHING
                        ''', (name, url, api_type, 'none' if api_type != 'eia' else 'api_key',
                              datetime.now().isoformat(), record_ct, datetime.now().isoformat()))

                        cursor.execute('''
                            UPDATE discovered_apis SET integrated = 1, updated_at = %s WHERE id = %s
                        ''', (datetime.now().isoformat(), api_id))

                        results['registered'] += 1
                        break
                    except Exception:
                        time.sleep(0.5)

            except Exception as e:
                logger.debug(f"Auto-register error for {name}: {e}")
                results['skipped'] += 1

        conn.commit()
        conn.close()

        logger.info(f"Auto-Register: candidates={results['candidates']}, registered={results['registered']}, skipped={results['skipped']}")
        return results

    def detect_deprecated_apis(self) -> Dict:
        # 3-phase: brief read → in-memory decision → batched write.
        # Eliminates pool starvation observed when this method ran for 80+s
        # under the api-auto-discovery-scheduler thread (Apr 2026).
        results = {'checked': 0, 'deprecated': 0, 'recovered': 0}

        # PHASE 1: brief read to fetch the work list, then close the connection.
        rows = []
        conn = get_db(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT da.id, da.name, da.url, da.api_type, da.status,
                       COUNT(hc.id) as check_count,
                       SUM(CASE WHEN hc.is_healthy = 0 THEN 1 ELSE 0 END) as fail_count
                FROM discovered_apis da
                LEFT JOIN api_health_checks hc ON da.id = hc.api_id
                    AND hc.checked_at > datetime('now', '-7 days')
                WHERE da.status IN ('working', 'verified', 'failed', 'degraded')
                GROUP BY da.id
                HAVING COUNT(hc.id) >= 3
            ''')
            rows = cursor.fetchall()
        finally:
            try: conn.close()
            except Exception: pass
        results['checked'] = len(rows)

        # PHASE 2: in-memory decisions (no DB held).
        deprecate_ops = []  # (api_id, current_status, fail_count, check_count)
        recover_ops = []
        for api_id, name, url, api_type, current_status, check_count, fail_count in rows:
            check_count = check_count or 0
            fail_count = fail_count or 0
            fail_rate = fail_count / check_count if check_count > 0 else 0

            if fail_rate >= 0.8 and current_status not in ('deprecated',):
                deprecate_ops.append((api_id, current_status, fail_count, check_count))
                self._cache['deprecated_apis'].append(name)
            elif fail_rate <= 0.2 and current_status in ('failed', 'degraded', 'deprecated'):
                recover_ops.append((api_id, current_status, fail_count, check_count))
        results['deprecated'] = len(deprecate_ops)
        results['recovered'] = len(recover_ops)

        # PHASE 3: batched writes in a fresh connection (only if we have work).
        if deprecate_ops or recover_ops:
            conn = get_db(self.db_path)
            try:
                cursor = conn.cursor()
                now = datetime.now().isoformat()
                for api_id, current_status, fail_count, check_count in deprecate_ops:
                    try:
                        cursor.execute(
                            'UPDATE discovered_apis SET status = %s, updated_at = %s WHERE id = %s',
                            ('deprecated', now, api_id))
                        self._log_change_event(cursor, api_id, 'deprecated', current_status, 'deprecated',
                            f"API failed {fail_count}/{check_count} health checks in 7 days")
                    except Exception as e:
                        logger.debug(f"Deprecate write failed for api_id={api_id}: {e}")
                for api_id, current_status, fail_count, check_count in recover_ops:
                    try:
                        cursor.execute(
                            'UPDATE discovered_apis SET status = %s, updated_at = %s WHERE id = %s',
                            ('working', now, api_id))
                        self._log_change_event(cursor, api_id, 'recovered', current_status, 'working',
                            f"API recovered - passing {check_count - fail_count}/{check_count} health checks")
                    except Exception as e:
                        logger.debug(f"Recover write failed for api_id={api_id}: {e}")
                conn.commit()
            finally:
                try: conn.close()
                except Exception: pass

        logger.info(f"Deprecation Check: checked={results['checked']}, deprecated={results['deprecated']}, recovered={results['recovered']}")
        return results

    def test_all_apis(self) -> Dict:
        results = {'tested': 0, 'working': 0, 'failed': 0}

        # PHASE 1: brief DB read to fetch the work list, then close the connection.
        conn = get_db(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, name, url, api_type FROM discovered_apis
            WHERE status NOT IN ('deprecated')
            AND (last_tested IS NULL OR last_tested < datetime('now', '-7 days'))
            LIMIT 25
        ''')
        apis = cursor.fetchall()
        conn.close()

        # PHASE 2: HTTP work without holding a DB connection.
        update_rows = []
        for api_id, name, url, api_type in apis:
            test_result = self.test_api(url, api_type or 'arcgis')
            results['tested'] += 1

            status = 'working' if test_result['success'] else 'failed'
            if test_result['success']:
                results['working'] += 1
            else:
                results['failed'] += 1

            now_iso = datetime.now().isoformat()
            update_rows.append((status, now_iso, json.dumps(test_result),
                str(test_result.get('record_count', 0)), now_iso, api_id))
            time.sleep(0.5)

        # PHASE 3: brief DB write to flush all buffered updates.
        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()
            for row in update_rows:
                try:
                    cursor.execute('''
                        UPDATE discovered_apis
                        SET status = %s, last_tested = %s, test_result = %s, record_count = %s, updated_at = %s
                        WHERE id = %s
                    ''', row)
                except Exception:
                    pass
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"test_all_apis write phase failed: {e}")

        logger.info(f"API Testing: tested={results['tested']}, working={results['working']}, failed={results['failed']}")
        return results

    def learn_from_apis(self) -> Dict:
        results = {'apis_queried': 0, 'items_learned': 0}

        conn = get_db(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, name, category, url, api_type FROM discovered_apis
            WHERE (status = 'working' OR status = 'verified')
            AND api_type = 'arcgis'
            LIMIT 10
        ''')

        apis = cursor.fetchall()
        conn.close()

        learned_rows = []

        for api_id, name, category, url, api_type in apis:
            try:
                query_url = f"{url}/query?where=1=1&outFields=*&resultRecordCount=100&f=json"
                response = self.session.get(query_url, timeout=20)

                if response.status_code == 200:
                    data = response.json()
                    features = data.get('features', [])
                    results['apis_queried'] += 1

                    for feature in features:
                        attrs = feature.get('attributes', {})
                        item_name = attrs.get('NAME') or attrs.get('name') or attrs.get('FACILITY_NAME', 'Unknown')
                        location = f"{attrs.get('STATE', '')}, {attrs.get('CITY', '')}"
                        learned_rows.append((category, name, str(item_name)[:200], location[:100],
                                             url, json.dumps(attrs)[:5000]))

                time.sleep(1)
            except Exception:
                pass

        if learned_rows:
            conn2 = get_db(self.db_path)
            for row in learned_rows:
                try:
                    conn2.execute('''
                        INSERT INTO learned_infrastructure
                        (category, item_type, name, location, source_api, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
                    ''', row)
                    conn2.commit()
                    results['items_learned'] += 1
                except Exception:
                    try:
                        conn2.rollback()
                    except Exception:
                        pass
            conn2.close()

        logger.info(f"Learning: queried={results['apis_queried']}, learned={results['items_learned']}")
        return results

    def get_discovery_status(self) -> Dict:
        # Wrapped in try/finally so a slow query or exception cannot leak the
        # connection — previously this method was observed holding a pool slot
        # for 84s under the scheduler thread (Apr 2026), starving the pool.
        conn = get_db(self.db_path)
        try:
            cursor = conn.cursor()

            cursor.execute('SELECT COUNT(*) FROM discovered_apis')
            total_apis = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM discovered_apis WHERE status IN ('working', 'verified')")
            working_apis = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM discovered_apis WHERE status = 'failed'")
            failed_apis = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM discovered_apis WHERE status = 'deprecated'")
            deprecated_apis = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM discovered_apis WHERE integrated = 1")
            integrated_apis = cursor.fetchone()[0]

            cursor.execute('SELECT category, COUNT(*) FROM discovered_apis GROUP BY category')
            by_category = dict(cursor.fetchall())

            cursor.execute('SELECT api_type, COUNT(*) FROM discovered_apis GROUP BY api_type')
            by_type = dict(cursor.fetchall())

            cursor.execute('SELECT COUNT(*) FROM learned_infrastructure')
            items_learned = cursor.fetchone()[0]

            cursor.execute('SELECT category, COUNT(*) FROM learned_infrastructure GROUP BY category')
            learned_by_category = dict(cursor.fetchall())

            cursor.execute('SELECT COUNT(*) FROM api_registry WHERE enabled = 1')
            registry_count = cursor.fetchone()[0]

            # Phase RRR-shadow-fix (2026-05-18): `checked_at` and `detected_at`
            # columns are TEXT (legacy schema), not TIMESTAMP, so `> NOW() -
            # INTERVAL` errors with "operator does not exist: text > timestamp
            # with time zone". Cast at query time via ::timestamptz.
            # Brain's /api/discovery/status SQL error caught this.
            cursor.execute("SELECT COUNT(*) FROM api_health_checks WHERE checked_at::timestamptz > NOW() - INTERVAL '24 hours'")
            checks_24h = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM api_change_events WHERE detected_at::timestamptz > NOW() - INTERVAL '7 days'")
            changes_7d = cursor.fetchone()[0]

            cursor.execute('''
                SELECT name, category, api_type, record_count, status, last_tested
                FROM discovered_apis
                WHERE status IN ('working', 'verified')
                ORDER BY updated_at DESC
                LIMIT 15
            ''')
            top_apis = [{'name': r[0], 'category': r[1], 'type': r[2], 'records': r[3],
                          'status': r[4], 'last_tested': r[5]} for r in cursor.fetchall()]

            cursor.execute('''
                SELECT event_type, COUNT(*) FROM api_change_events
                WHERE detected_at > datetime('now', '-30 days')
                GROUP BY event_type
            ''')
            recent_changes = dict(cursor.fetchall())
        finally:
            try: conn.close()
            except Exception: pass

        return {
            'total_apis_discovered': total_apis,
            'working_apis': working_apis,
            'failed_apis': failed_apis,
            'deprecated_apis': deprecated_apis,
            'integrated_apis': integrated_apis,
            'registered_in_platform': registry_count,
            'apis_by_category': by_category,
            'apis_by_type': by_type,
            'items_learned': items_learned,
            'learned_by_category': learned_by_category,
            'health_checks_24h': checks_24h,
            'change_events_7d': changes_7d,
            'recent_change_types': recent_changes,
            'top_apis': top_apis,
            'last_cycle': self._cache.get('last_cycle'),
            'new_apis_last_cycle': self._cache.get('new_apis_this_cycle', []),
            'deprecated_last_cycle': self._cache.get('deprecated_apis', []),
            'scheduler_running': self._scheduler_running
        }

    def discover_expanded_sources(self) -> Dict:
        """Scan state GIS, federal agency, and interconnection queue sources"""
        results = {'checked': 0, 'new': 0, 'working': 0, 'sources': {
            'state_gis': 0, 'federal': 0, 'interconnection': 0
        }}

        for ep in STATE_GIS_ENDPOINTS:
            try:
                results['checked'] += 1
                test_result = self.test_api(ep['url'], ep['type'])
                status = 'working' if test_result['success'] else 'failed'
                if test_result['success']:
                    results['working'] += 1
                    results['sources']['state_gis'] += 1
                added = self._add_discovered_api({
                    'name': ep['name'], 'category': ep['category'],
                    'type': ep['type'], 'url': ep['url'],
                    'description': ep.get('description', '')
                }, status=status, test_result=test_result)
                if added:
                    results['new'] += 1
                    if test_result['success']:
                        self._cache['new_apis_this_cycle'].append(ep['name'])
                time.sleep(0.3)
            except Exception as e:
                logger.debug(f"State GIS endpoint error: {e}")

        for ep in FEDERAL_AGENCY_ENDPOINTS:
            try:
                results['checked'] += 1
                test_result = self.test_api(ep['url'], ep['type'])
                status = 'working' if test_result['success'] else 'failed'
                if test_result['success']:
                    results['working'] += 1
                    results['sources']['federal'] += 1
                added = self._add_discovered_api({
                    'name': ep['name'], 'category': ep['category'],
                    'type': ep['type'], 'url': ep['url'],
                    'description': ep.get('description', '')
                }, status=status, test_result=test_result)
                if added:
                    results['new'] += 1
                    if test_result['success']:
                        self._cache['new_apis_this_cycle'].append(ep['name'])
                time.sleep(0.3)
            except Exception as e:
                logger.debug(f"Federal agency endpoint error: {e}")

        for ep in INTERCONNECTION_QUEUE_ENDPOINTS:
            try:
                results['checked'] += 1
                test_result = self.test_api(ep['url'], ep['type'])
                status = 'working' if test_result['success'] else 'failed'
                if test_result['success']:
                    results['working'] += 1
                    results['sources']['interconnection'] += 1
                added = self._add_discovered_api({
                    'name': ep['name'], 'category': ep['category'],
                    'type': ep['type'], 'url': ep['url'],
                    'description': ep.get('description', '')
                }, status=status, test_result=test_result)
                if added:
                    results['new'] += 1
                    if test_result['success']:
                        self._cache['new_apis_this_cycle'].append(ep['name'])
                time.sleep(0.3)
            except Exception as e:
                logger.debug(f"Interconnection queue endpoint error: {e}")

        logger.info(f"Expanded Sources: checked={results['checked']}, new={results['new']}, working={results['working']} (state={results['sources']['state_gis']}, federal={results['sources']['federal']}, interconnection={results['sources']['interconnection']})")
        return results

    def run_discovery_cycle(self) -> Dict:
        if getattr(self, '_cycle_in_progress', False):
            logger.info("API Discovery cycle skipped (previous cycle still running)")
            return {'skipped': True, 'reason': 'cycle_in_progress'}
        self._cycle_in_progress = True
        try:
            return self._run_discovery_cycle_inner()
        finally:
            self._cycle_in_progress = False

    def _run_discovery_cycle_inner(self) -> Dict:
        logger.info("=" * 60)
        logger.info("API AUTO-DISCOVERY CYCLE v2.0 STARTING")
        logger.info("=" * 60)

        self._cache['new_apis_this_cycle'] = []
        self._cache['deprecated_apis'] = []
        cycle_start = time.time()

        results = {}

        try:
            results['seed'] = self.seed_known_apis()
        except Exception as e:
            results['seed'] = {'error': str(e)}
            logger.error(f"Seed error: {e}")

        try:
            results['eia_catalog'] = self.discover_eia_catalog()
        except Exception as e:
            results['eia_catalog'] = {'error': str(e)}
            logger.error(f"EIA catalog error: {e}")

        try:
            results['usgs'] = self.discover_usgs_endpoints()
        except Exception as e:
            results['usgs'] = {'error': str(e)}
            logger.error(f"USGS error: {e}")

        try:
            results['fcc'] = self.discover_fcc_endpoints()
        except Exception as e:
            results['fcc'] = {'error': str(e)}
            logger.error(f"FCC error: {e}")

        try:
            results['epa'] = self.discover_epa_endpoints()
        except Exception as e:
            results['epa'] = {'error': str(e)}
            logger.error(f"EPA error: {e}")

        try:
            results['data_gov'] = self.discover_from_data_gov()
        except Exception as e:
            results['data_gov'] = {'error': str(e)}
            logger.error(f"Data.gov error: {e}")

        try:
            results['hifld'] = self.discover_from_hifld()
        except Exception as e:
            results['hifld'] = {'error': str(e)}
            logger.error(f"HIFLD error: {e}")

        try:
            results['expanded_sources'] = self.discover_expanded_sources()
        except Exception as e:
            results['expanded_sources'] = {'error': str(e)}
            logger.error(f"Expanded sources error: {e}")

        try:
            results['testing'] = self.test_all_apis()
        except Exception as e:
            results['testing'] = {'error': str(e)}
            logger.error(f"Testing error: {e}")

        try:
            results['health'] = self.health_check_registered_apis()
        except Exception as e:
            results['health'] = {'error': str(e)}
            logger.error(f"Health check error: {e}")

        try:
            results['deprecation'] = self.detect_deprecated_apis()
        except Exception as e:
            results['deprecation'] = {'error': str(e)}
            logger.error(f"Deprecation check error: {e}")

        try:
            results['registration'] = self.auto_register_new_apis()
        except Exception as e:
            results['registration'] = {'error': str(e)}
            logger.error(f"Auto-register error: {e}")

        try:
            results['learning'] = self.learn_from_apis()
        except Exception as e:
            results['learning'] = {'error': str(e)}
            logger.error(f"Learning error: {e}")

        results['status'] = self.get_discovery_status()

        cycle_duration = round(time.time() - cycle_start, 1)
        results['cycle_duration_seconds'] = cycle_duration

        try:
            conn = get_db(self.db_path)
            cursor = conn.cursor()

            total_found = sum([
                results.get('eia_catalog', {}).get('new', 0),
                results.get('usgs', {}).get('new', 0),
                results.get('fcc', {}).get('new', 0),
                results.get('epa', {}).get('new', 0),
                results.get('data_gov', {}).get('apis_added', 0),
                results.get('hifld', {}).get('apis_added', 0),
                results.get('expanded_sources', {}).get('new', 0),
            ])

            cursor.execute('''
                INSERT INTO api_discovery_log (action, source, apis_found, apis_tested, apis_integrated, details)
                VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            ''', (
                'discovery_cycle_v2',
                'auto_scheduler',
                total_found,
                results.get('testing', {}).get('tested', 0),
                results.get('registration', {}).get('registered', 0),
                json.dumps({
                    'duration_seconds': cycle_duration,
                    'sources': ['eia', 'usgs', 'fcc', 'epa', 'data_gov', 'hifld', 'state_gis', 'federal', 'interconnection'],
                    'new_apis': self._cache.get('new_apis_this_cycle', []),
                    'deprecated': self._cache.get('deprecated_apis', [])
                })
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Log write error: {e}")

        self._cache['last_cycle'] = datetime.now().isoformat()
        self._cache['last_results'] = results

        logger.info("=" * 60)
        logger.info(f"DISCOVERY CYCLE v2.0 COMPLETE ({cycle_duration}s)")
        logger.info(f"   Total APIs: {results['status']['total_apis_discovered']}")
        logger.info(f"   Working: {results['status']['working_apis']}")
        logger.info(f"   Registered: {results['status']['registered_in_platform']}")
        logger.info(f"   New this cycle: {len(self._cache.get('new_apis_this_cycle', []))}")
        logger.info(f"   Deprecated: {len(self._cache.get('deprecated_apis', []))}")
        logger.info("=" * 60)

        return results

    def _categorize_api(self, name: str) -> str:
        name_lower = name.lower()
        if any(w in name_lower for w in ['substation', 'transmission', 'power', 'electric', 'grid', 'generator', 'capacity', 'energy', 'fuel']):
            return 'power'
        elif any(w in name_lower for w in ['gas', 'pipeline', 'compressor', 'lng', 'petroleum', 'coal']):
            return 'gas'
        elif any(w in name_lower for w in ['fiber', 'broadband', 'telecom', 'internet', 'cable', 'fcc']):
            return 'fiber'
        elif any(w in name_lower for w in ['railroad', 'rail', 'airport', 'port', 'highway', 'transportation']):
            return 'transportation'
        elif any(w in name_lower for w in ['water', 'aquifer', 'river', 'flood', 'groundwater', 'streamflow', 'drinking']):
            return 'water'
        elif any(w in name_lower for w in ['seismic', 'hazard', 'wetland', 'environmental', 'earthquake', 'emission', 'ghg', 'co2', 'air quality']):
            return 'environmental'
        else:
            return 'other'

    def _add_discovered_api(self, api_info: Dict, status: str = 'discovered', test_result: Dict = None) -> bool:
        conn = get_db(self.db_path)
        cursor = conn.cursor()

        try:
            cursor.execute('SELECT id, status FROM discovered_apis WHERE url = %s', (api_info['url'],))
            existing = cursor.fetchone()

            if existing:
                if test_result:
                    cursor.execute('''
                        UPDATE discovered_apis SET status = %s, last_tested = %s, test_result = %s, updated_at = %s
                        WHERE url = %s
                    ''', (status, datetime.now().isoformat(), json.dumps(test_result),
                          datetime.now().isoformat(), api_info['url']))
                    conn.commit()
                return False

            cursor.execute('''
                INSERT INTO discovered_apis (name, category, api_type, url, status, last_tested, test_result)
                VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            ''', (
                api_info['name'][:200], api_info.get('category', 'other'),
                api_info.get('type', 'rest'), api_info['url'],
                status,
                datetime.now().isoformat() if test_result else None,
                json.dumps(test_result) if test_result else None
            ))
            added = cursor.rowcount > 0
            conn.commit()
            return added
        except Exception:
            return False
        finally:
            conn.close()

    def _log_change_event(self, cursor, api_id: int, event_type: str,
                          old_value: str, new_value: str, description: str):
        try:
            cursor.execute('''
                INSERT INTO api_change_events (api_id, event_type, old_value, new_value, description)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            ''', (api_id, event_type, old_value, new_value, description))
        except Exception:
            pass


_discovery_instance = None
_scheduler_thread = None


def _run_scheduler(interval: int = 21600):
    global _discovery_instance
    if _discovery_instance:
        _discovery_instance._scheduler_running = True
    logger.info(f"API Discovery scheduler started (interval={interval}s / {interval//3600}h)")

    time.sleep(300)

    cycle_count = 0
    while _discovery_instance and _discovery_instance._scheduler_running:
        cycle_count += 1
        try:
            logger.info(f"API Discovery scheduler: starting cycle #{cycle_count}...")
            start_time = time.time()
            results = _discovery_instance.run_discovery_cycle()
            elapsed = round(time.time() - start_time, 1)
            logger.info(f"API Discovery scheduler: cycle #{cycle_count} completed in {elapsed}s")
        except Exception as e:
            logger.error(f"Discovery scheduler cycle #{cycle_count} error: {e}", exc_info=True)

        for _ in range(interval // 10):
            if not (_discovery_instance and _discovery_instance._scheduler_running):
                break
            time.sleep(10)

    logger.info("API Discovery scheduler stopped")


def start_discovery_scheduler(interval: int = 21600):
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        if _discovery_instance:
            _discovery_instance._scheduler_running = True
        logger.info("API Discovery scheduler already running")
        return

    _scheduler_thread = threading.Thread(
        target=_run_scheduler,
        args=(interval,),
        daemon=True,
        name='api-auto-discovery-scheduler'
    )
    _scheduler_thread.start()


def register_api_discovery_routes(app, start_scheduler=True):
    from flask import Blueprint, jsonify, request

    global _discovery_instance
    if _discovery_instance is not None:
        if start_scheduler:
            _discovery_instance._scheduler_running = True
        logger.info("API Auto-Discovery v2.0 already initialized, skipping duplicate registration")
        return

    _discovery_instance = APIAutoDiscovery()

    discovery_bp = Blueprint('api_discovery', __name__)

# AUTO-REPAIR: duplicate route '/api/discovery/status' also in api_server.py:2838 — review and remove one
    @discovery_bp.route('/api/discovery/status')
    def api_discovery_status():
        return jsonify({
            'success': True,
            'engine': 'API Auto-Discovery v2.0',
            **_discovery_instance.get_discovery_status()
        })
# AUTO-REPAIR: duplicate route '/api/discovery/run' also in api_server.py:2584 — review and remove one

    @discovery_bp.route('/api/discovery/run', methods=['POST'])
    def run_api_discovery():
        admin_key = os.environ.get('DCHUB_ADMIN_KEY', '')
        provided_key = request.headers.get('Authorization', '').replace('Bearer ', '') or \
                       request.headers.get('X-API-Key', '') or \
                       request.headers.get('X-Internal-Key', '')
        if not admin_key or provided_key != admin_key:
            return jsonify({'error': 'Unauthorized — use POST /api/jobs/discovery instead'}), 401
        mode = request.args.get('mode', 'async')
        if mode == 'sync':
            results = _discovery_instance.run_discovery_cycle()
            return jsonify({'success': True, 'results': results})
        import threading
        def _run():
            try:
                _discovery_instance.run_discovery_cycle()
            except Exception as e:
                logger.error(f"API discovery cycle error: {e}")
        threading.Thread(target=_run, daemon=True).start()
        return jsonify({
            'success': True,
            'message': 'API discovery cycle started in background',
            'check_status': '/api/discovery/status'
        })

    @discovery_bp.route('/api/discovery/health')
    def api_health_status():
        return jsonify({
            'success': True,
            'health_report': _discovery_instance._cache.get('health_report', {}),
            'last_cycle': _discovery_instance._cache.get('last_cycle'),
            'scheduler_running': _discovery_instance._scheduler_running
        })

    @discovery_bp.route('/api/discovery/changes')
    def api_change_events():
        days = request.args.get('days', 30, type=int)

        conn = get_read_db()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT ce.id, da.name, ce.event_type, ce.old_value, ce.new_value,
                   ce.description, ce.detected_at, ce.acknowledged
            FROM api_change_events ce
            JOIN discovered_apis da ON ce.api_id = da.id
            WHERE ce.detected_at > datetime('now', %s)
            ORDER BY ce.detected_at DESC
            LIMIT 100
        ''', (f'-{days} days',))

        events = [{'id': r[0], 'api_name': r[1], 'event_type': r[2],
                   'old_value': r[3], 'new_value': r[4], 'description': r[5],
                   'detected_at': r[6], 'acknowledged': bool(r[7])} for r in cursor.fetchall()]

        conn.close()

        return jsonify({'success': True, 'events': events, 'total': len(events)})

    @discovery_bp.route('/api/discovery/registry')
    def api_registry_list():
        conn = get_read_db()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT name, url, api_type, auth_type, last_success, items_fetched, enabled, discovered_at
            FROM api_registry
            WHERE enabled = 1
            ORDER BY last_success DESC
        ''')

        apis = [{'name': r[0], 'url': r[1], 'type': r[2], 'auth': r[3],
                 'last_success': r[4], 'items': r[5], 'enabled': bool(r[6]),
                 'discovered_at': r[7]} for r in cursor.fetchall()]

        conn.close()

        return jsonify({'success': True, 'registered_apis': apis, 'total': len(apis)})

    @discovery_bp.route('/api/discovery/apis')
    def list_discovered_apis():
        category = request.args.get('category')
        status = request.args.get('status')
        api_type = request.args.get('type')
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)

        conn = get_read_db()
        cursor = conn.cursor()

        query = 'SELECT id, name, category, api_type, url, record_count, status, last_tested, integrated FROM discovered_apis WHERE 1=1'
        params = []

        if category:
            query += ' AND category = %s'
            params.append(category)
        if status:
            query += ' AND status = %s'
            params.append(status)
        if api_type:
            query += ' AND api_type = %s'
            params.append(api_type)

        count_query = query.replace('SELECT id, name, category, api_type, url, record_count, status, last_tested, integrated', 'SELECT COUNT(*)')
        cursor.execute(count_query, params)
        total = cursor.fetchone()[0]

        query += ' ORDER BY updated_at DESC LIMIT %s OFFSET %s'
        params.extend([per_page, (page - 1) * per_page])

        cursor.execute(query, params)

        apis = [{'id': r[0], 'name': r[1], 'category': r[2], 'type': r[3], 'url': r[4],
                 'records': r[5], 'status': r[6], 'last_tested': r[7],
                 'integrated': bool(r[8])} for r in cursor.fetchall()]

        conn.close()

        return jsonify({
            'success': True,
            'apis': apis,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page
        })

    @discovery_bp.route('/api/discovery/apis/seed', methods=['POST'])
    def seed_apis():
        results = _discovery_instance.seed_known_apis()
        return jsonify({'success': True, **results})

    @discovery_bp.route('/api/discovery/apis/test', methods=['POST'])
    def test_apis():
        results = _discovery_instance.test_all_apis()
        return jsonify({'success': True, **results})

    @discovery_bp.route('/api/discovery/apis/learn', methods=['POST'])
    def learn_from_discovered():
        results = _discovery_instance.learn_from_apis()
        return jsonify({'success': True, **results})

    app.register_blueprint(discovery_bp)

    if start_scheduler:
        start_discovery_scheduler()
        logger.info("🔍 API Auto-Discovery v2.0: ✅ Registered (6-hour auto-cycle)")
    else:
        logger.info("🔍 API Auto-Discovery v2.0: ✅ Registered (scheduler PAUSED - manual POST only)")
    logger.info("   GET  /api/discovery/status   - Full discovery status")
    logger.info("   GET  /api/discovery/health    - API health report")
    logger.info("   GET  /api/discovery/changes   - Change events log")
    logger.info("   GET  /api/discovery/registry  - Registered APIs")
    logger.info("   GET  /api/discovery/apis      - Browse discovered APIs")
    logger.info("   POST /api/discovery/run       - Trigger discovery cycle")


if __name__ == '__main__':
    discovery = APIAutoDiscovery()
    discovery.run_discovery_cycle()
