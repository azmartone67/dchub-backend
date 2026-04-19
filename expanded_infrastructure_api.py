"""
DC Hub Expanded Infrastructure API
===================================
Comprehensive infrastructure data aggregation from 40+ government sources.

Categories:
- HIFLD Power Infrastructure (substations, transmission, power plants)
- Transportation (railroads, airports, submarine cables)
- Water & Environmental (aquifers, rivers, flood zones, wetlands, seismic)
- Energy Markets (ISO/RTO, generation queue, solar/wind resources)
- Gas Midstream (LNG, storage, processing, fractionators, market hubs)
- Fiber & Broadband (metro fiber, long-haul, FCC broadband, IXPs)
- Utilities & Economic (co-ops, utility territories, opportunity zones)
"""

from flask import Blueprint, request, jsonify
import os
import requests
from datetime import datetime
from functools import lru_cache
import json
import time

expanded_infra_bp = Blueprint('expanded_infrastructure', __name__)

HIFLD_BASE = 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services'
# NOTE: HIFLD Open decommissioned Aug 26 2025. Transmission lines still live on Hp6G80Pky0om7QvQ.
# Gas pipelines/substations moved to original source agencies or HIFLD Secure (restricted).
DOT_GAS_PIPELINES = 'https://geo.dot.gov/server/rest/services/Hosted/Natural_Gas_Pipelines_US_EIA/FeatureServer/0'
NASA_HIFLD = 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer'
FEMA_NFHL = 'https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer'
FRA_BASE = 'https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services'
USGS_AQUIFERS = 'https://arcgis.water.nv.gov/arcgis/rest/services/BaseLayers/USGS_Aquifers_Principal/FeatureServer/0'

REQUEST_TIMEOUT = 30
CACHE = {}
CACHE_TTL = 3600

def get_cached(key):
    if key in CACHE:
        data, ts = CACHE[key]
        if time.time() - ts < CACHE_TTL:
            return data
    return None

def set_cache(key, data):
    CACHE[key] = (data, time.time())

def query_arcgis(url, params=None):
    default_params = {
        'f': 'json',
        'outFields': '*',
        'returnGeometry': 'true',
        'resultRecordCount': '1000'
    }
    if params:
        default_params.update(params)
    try:
        resp = requests.get(f"{url}/query", params=default_params, timeout=REQUEST_TIMEOUT)
        if resp.ok:
            return resp.json()
    except Exception as e:
        print(f"ArcGIS query error: {e}")
    return {'features': [], 'error': str(e) if 'e' in dir() else 'Request failed'}

def bbox_from_params(lat, lng, radius_miles=50):
    lat, lng = float(lat), float(lng)
    delta = radius_miles / 69.0
    return f"{lng-delta},{lat-delta},{lng+delta},{lat+delta}"

LAYER_REGISTRY = {
    'substations': {
        'name': 'Electric Substations',
        'url': None,  # HIFLD decommissioned Aug 2025 — data served from Neon (79,755 records)
        'count': '79k+',
        'source': 'HIFLD/Neon',
        'category': 'power'
    },
    'hifld_transmission': {
        'name': 'HIFLD Transmission Lines',
        'url': f'{HIFLD_BASE}/Electric_Power_Transmission_Lines/FeatureServer/0',
        'count': '300k+',
        'source': 'HIFLD/DHS',
        'category': 'power'
    },
    'hifld_power_plants': {
        'name': 'HIFLD Power Plants',
        'url': f'{HIFLD_BASE}/Power_Plants/FeatureServer/0',
        'count': '10k+',
        'source': 'HIFLD/EIA',
        'category': 'power'
    },
    'hifld_gas_pipelines': {
        'name': 'Natural Gas Pipelines',
        'url': DOT_GAS_PIPELINES,  # DOT GeoServer (EIA data) — replaces decommissioned HIFLD
        'count': '300k+',
        'source': 'DOT/EIA',
        'category': 'gas'
    },
    'hifld_gas_compressors': {
        'name': 'Gas Compressor Stations',
        'url': f'{HIFLD_BASE}/Natural_Gas_Compressor_Stations/FeatureServer/0',
        'count': '1.5k+',
        'source': 'HIFLD',
        'category': 'gas'
    },
    'hifld_lng_terminals': {
        'name': 'LNG Terminals',
        'url': f'{HIFLD_BASE}/LNG_Terminals/FeatureServer/0',
        'count': '180+ MTPA',
        'source': 'HIFLD/EIA',
        'category': 'gas'
    },
    'hifld_gas_storage': {
        'name': 'Natural Gas Storage',
        'url': f'{HIFLD_BASE}/Natural_Gas_Underground_Storage/FeatureServer/0',
        'count': '400+',
        'source': 'HIFLD/EIA',
        'category': 'gas'
    },
    'hifld_gas_processing': {
        'name': 'Gas Processing Plants',
        'url': f'{HIFLD_BASE}/Natural_Gas_Processing_Plants/FeatureServer/0',
        'count': '500+',
        'source': 'HIFLD',
        'category': 'gas'
    },
    'fra_railroads': {
        'name': 'North American Rail Network',
        'url': f'{FRA_BASE}/NTAD_North_American_Rail_Network_Lines/FeatureServer/0',
        'count': '140k+ mi',
        'source': 'FRA/DOT',
        'category': 'transportation'
    },
    'fra_rail_yards': {
        'name': 'Rail Yards',
        'url': f'{FRA_BASE}/NTAD_Rail_Yards/FeatureServer/0',
        'count': '5k+',
        'source': 'FRA/DOT',
        'category': 'transportation'
    },
    'fra_grade_crossings': {
        'name': 'Railroad Grade Crossings',
        'url': f'{FRA_BASE}/NTAD_Railroad_Grade_Crossings/FeatureServer/0',
        'count': '200k+',
        'source': 'FRA/DOT',
        'category': 'transportation'
    },
    'usgs_aquifers': {
        'name': 'Principal Aquifers',
        'url': USGS_AQUIFERS,
        'count': '66',
        'source': 'USGS',
        'category': 'water'
    },
    'fema_flood_zones': {
        'name': 'FEMA Flood Hazard Zones',
        'url': f'{FEMA_NFHL}/28',
        'count': 'Live',
        'source': 'FEMA NFHL',
        'category': 'environmental'
    },
    'usgs_seismic': {
        'name': 'Seismic Hazard Zones',
        'url': 'https://earthquake.usgs.gov/arcgis/rest/services/haz/hazfaults2014/MapServer/0',
        'count': '681+',
        'source': 'USGS',
        'category': 'environmental'
    },
    'hifld_airports': {
        'name': 'Public Airports',
        'url': f'{HIFLD_BASE}/Public_Airports/FeatureServer/0',
        'count': '19k+',
        'source': 'HIFLD/FAA',
        'category': 'transportation'
    },
    'hifld_internet_exchanges': {
        'name': 'Internet Exchange Points',
        'url': f'{HIFLD_BASE}/Internet_Exchange_Points/FeatureServer/0',
        'count': '100+',
        'source': 'HIFLD',
        'category': 'fiber'
    },
    'eia_solar_resource': {
        'name': 'Solar Resource Potential',
        'url': 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Solar_Potential/FeatureServer/0',
        'count': 'NREL',
        'source': 'NREL',
        'category': 'renewable'
    },
    'utility_territories': {
        'name': 'Electric Utility Service Territories',
        'url': f'{HIFLD_BASE}/Electric_Retail_Service_Territories/FeatureServer/0',
        'count': '3k+',
        'source': 'HIFLD/EIA',
        'category': 'utilities'
    }
}

STATIC_DATA = {
    'iso_rto': [
        {'name': 'PJM', 'region': 'Mid-Atlantic/Midwest', 'states': 'DE,IL,IN,KY,MD,MI,NJ,NC,OH,PA,TN,VA,WV,DC', 'capacity_gw': 185, 'lat': 39.5, 'lng': -77.5},
        {'name': 'ERCOT', 'region': 'Texas', 'states': 'TX', 'capacity_gw': 92, 'lat': 31.5, 'lng': -99.5},
        {'name': 'CAISO', 'region': 'California', 'states': 'CA', 'capacity_gw': 80, 'lat': 37.0, 'lng': -120.0},
        {'name': 'MISO', 'region': 'Midwest', 'states': 'AR,IL,IN,IA,KY,LA,MI,MN,MS,MO,MT,ND,SD,TX,WI', 'capacity_gw': 198, 'lat': 42.0, 'lng': -93.0},
        {'name': 'SPP', 'region': 'Southwest/Central', 'states': 'AR,KS,LA,MO,NE,NM,OK,TX', 'capacity_gw': 105, 'lat': 36.0, 'lng': -98.0},
        {'name': 'NYISO', 'region': 'New York', 'states': 'NY', 'capacity_gw': 42, 'lat': 42.5, 'lng': -75.5},
        {'name': 'ISO-NE', 'region': 'New England', 'states': 'CT,ME,MA,NH,RI,VT', 'capacity_gw': 32, 'lat': 42.5, 'lng': -71.5}
    ],
    'gas_market_hubs': [
        {'name': 'Henry Hub', 'state': 'LA', 'lat': 30.0, 'lng': -91.5, 'type': 'benchmark'},
        {'name': 'Waha Hub', 'state': 'TX', 'lat': 31.3, 'lng': -103.0, 'type': 'permian'},
        {'name': 'Dominion South', 'state': 'PA', 'lat': 40.0, 'lng': -79.5, 'type': 'appalachian'},
        {'name': 'SoCal Citygate', 'state': 'CA', 'lat': 34.0, 'lng': -118.2, 'type': 'california'},
        {'name': 'PG&E Citygate', 'state': 'CA', 'lat': 37.8, 'lng': -122.4, 'type': 'california'},
        {'name': 'Chicago Citygate', 'state': 'IL', 'lat': 41.9, 'lng': -87.6, 'type': 'midwest'},
        {'name': 'TETCO M3', 'state': 'NJ', 'lat': 40.7, 'lng': -74.0, 'type': 'northeast'},
        {'name': 'Katy Hub', 'state': 'TX', 'lat': 29.8, 'lng': -95.8, 'type': 'gulf_coast'},
        {'name': 'Houston Ship Channel', 'state': 'TX', 'lat': 29.7, 'lng': -95.0, 'type': 'gulf_coast'},
        {'name': 'Opal Hub', 'state': 'WY', 'lat': 41.8, 'lng': -110.3, 'type': 'rockies'},
        {'name': 'Carthage Hub', 'state': 'TX', 'lat': 32.1, 'lng': -94.3, 'type': 'east_texas'},
        {'name': 'Agua Dulce', 'state': 'TX', 'lat': 27.8, 'lng': -97.9, 'type': 'south_texas'},
        {'name': 'Dawn Hub', 'state': 'ON', 'lat': 42.7, 'lng': -82.0, 'type': 'canada'},
        {'name': 'AECO Hub', 'state': 'AB', 'lat': 51.0, 'lng': -114.0, 'type': 'canada'},
        {'name': 'Malin Hub', 'state': 'OR', 'lat': 42.0, 'lng': -121.5, 'type': 'pacific_nw'}
    ],
    'lng_terminals': [
        {'name': 'Sabine Pass', 'state': 'LA', 'lat': 29.73, 'lng': -93.87, 'capacity_mtpa': 30, 'operator': 'Cheniere'},
        {'name': 'Cameron LNG', 'state': 'LA', 'lat': 29.78, 'lng': -93.30, 'capacity_mtpa': 15, 'operator': 'Sempra'},
        {'name': 'Corpus Christi', 'state': 'TX', 'lat': 27.83, 'lng': -97.07, 'capacity_mtpa': 25, 'operator': 'Cheniere'},
        {'name': 'Freeport LNG', 'state': 'TX', 'lat': 28.94, 'lng': -95.31, 'capacity_mtpa': 15, 'operator': 'Freeport LNG'},
        {'name': 'Elba Island', 'state': 'GA', 'lat': 32.09, 'lng': -80.89, 'capacity_mtpa': 2.5, 'operator': 'Kinder Morgan'},
        {'name': 'Cove Point', 'state': 'MD', 'lat': 38.40, 'lng': -76.38, 'capacity_mtpa': 5.25, 'operator': 'Dominion'},
        {'name': 'Golden Pass', 'state': 'TX', 'lat': 29.77, 'lng': -93.93, 'capacity_mtpa': 18, 'operator': 'QatarEnergy/Exxon'},
        {'name': 'Calcasieu Pass', 'state': 'LA', 'lat': 29.78, 'lng': -93.35, 'capacity_mtpa': 12, 'operator': 'Venture Global'},
        {'name': 'Plaquemines LNG', 'state': 'LA', 'lat': 29.35, 'lng': -89.45, 'capacity_mtpa': 20, 'operator': 'Venture Global'},
        {'name': 'Rio Grande LNG', 'state': 'TX', 'lat': 26.07, 'lng': -97.17, 'capacity_mtpa': 27, 'operator': 'NextDecade'}
    ],
    'submarine_cables': [
        {'name': 'MAREA', 'landing_1': 'Virginia Beach, VA', 'landing_2': 'Bilbao, Spain', 'capacity_tbps': 200, 'length_km': 6600},
        {'name': 'DUNANT', 'landing_1': 'Virginia Beach, VA', 'landing_2': 'Saint-Hilaire-de-Riez, France', 'capacity_tbps': 250, 'length_km': 6400},
        {'name': 'Havfrue', 'landing_1': 'Wall Township, NJ', 'landing_2': 'Denmark', 'capacity_tbps': 108, 'length_km': 7200},
        {'name': 'Grace Hopper', 'landing_1': 'New York', 'landing_2': 'UK/Spain', 'capacity_tbps': 352, 'length_km': 6200},
        {'name': 'Amitie', 'landing_1': 'Lynn, MA', 'landing_2': 'UK/France', 'capacity_tbps': 400, 'length_km': 6800},
        {'name': 'Pacific Light Cable', 'landing_1': 'Los Angeles, CA', 'landing_2': 'Hong Kong', 'capacity_tbps': 144, 'length_km': 12800},
        {'name': 'JUPITER', 'landing_1': 'Los Angeles, CA', 'landing_2': 'Philippines/Japan', 'capacity_tbps': 60, 'length_km': 14000},
        {'name': 'Southern Cross NEXT', 'landing_1': 'Los Angeles, CA', 'landing_2': 'Sydney, Australia', 'capacity_tbps': 72, 'length_km': 13000},
        {'name': 'Bifrost', 'landing_1': 'Portland, OR', 'landing_2': 'Singapore', 'capacity_tbps': 15, 'length_km': 16000},
        {'name': 'Echo', 'landing_1': 'Singapore', 'landing_2': 'Indonesia', 'capacity_tbps': 16, 'length_km': 2000},
        {'name': 'Firmina', 'landing_1': 'Myrtle Beach, SC', 'landing_2': 'Argentina', 'capacity_tbps': 24, 'length_km': 12000},
        {'name': 'Curie', 'landing_1': 'Los Angeles, CA', 'landing_2': 'Chile', 'capacity_tbps': 72, 'length_km': 10500},
        {'name': 'AAE-1', 'landing_1': 'Multiple US', 'landing_2': 'Asia/Europe', 'capacity_tbps': 40, 'length_km': 25000},
        {'name': 'SEA-ME-WE 6', 'landing_1': 'Multiple', 'landing_2': 'Asia/Europe', 'capacity_tbps': 100, 'length_km': 19200},
        {'name': '2Africa', 'landing_1': 'Multiple', 'landing_2': 'Africa/Europe', 'capacity_tbps': 180, 'length_km': 45000},
        {'name': 'Equiano', 'landing_1': 'Portugal', 'landing_2': 'South Africa', 'capacity_tbps': 144, 'length_km': 12000},
        {'name': 'Atlantic Crossing-1', 'landing_1': 'New York', 'landing_2': 'UK/Germany', 'capacity_tbps': 4.8, 'length_km': 7700},
        {'name': 'TAT-14', 'landing_1': 'Manasquan, NJ', 'landing_2': 'France/UK/Germany', 'capacity_tbps': 3.2, 'length_km': 15000}
    ],
    'major_rivers': [
        {'name': 'Mississippi River', 'length_mi': 2340, 'states': 'MN,WI,IA,IL,MO,KY,TN,AR,MS,LA'},
        {'name': 'Missouri River', 'length_mi': 2341, 'states': 'MT,ND,SD,NE,IA,KS,MO'},
        {'name': 'Colorado River', 'length_mi': 1450, 'states': 'CO,UT,AZ,NV,CA'},
        {'name': 'Columbia River', 'length_mi': 1243, 'states': 'WA,OR'},
        {'name': 'Ohio River', 'length_mi': 981, 'states': 'PA,WV,OH,KY,IN,IL'},
        {'name': 'Rio Grande', 'length_mi': 1896, 'states': 'CO,NM,TX'},
        {'name': 'Tennessee River', 'length_mi': 652, 'states': 'TN,AL,MS,KY'}
    ],
    'long_haul_fiber': [
        {'name': 'Zayo Network', 'route_miles': 141000, 'lit_buildings': 13000, 'markets': 400},
        {'name': 'Lumen (CenturyLink)', 'route_miles': 450000, 'fiber_miles': 190000, 'countries': 60},
        {'name': 'Crown Castle Fiber', 'route_miles': 85000, 'markets': 75, 'focus': 'metro_dense'},
        {'name': 'GTL Infrastructure', 'route_miles': 36000, 'markets': 50, 'focus': 'enterprise'},
        {'name': 'Uniti Fiber', 'route_miles': 130000, 'lit_buildings': 1600, 'focus': 'wholesale'},
        {'name': 'Windstream', 'route_miles': 190000, 'markets': 100, 'focus': 'enterprise'},
        {'name': 'AT&T Business', 'route_miles': 400000, 'countries': 200, 'focus': 'enterprise'},
        {'name': 'Verizon Business', 'route_miles': 850000, 'countries': 150, 'focus': 'global'},
        {'name': 'Cogent', 'route_miles': 60000, 'buildings': 3000, 'focus': 'internet'},
        {'name': 'Lightpath', 'route_miles': 21000, 'buildings': 10000, 'focus': 'ny_metro'},
        {'name': 'FirstLight', 'route_miles': 25000, 'markets': 100, 'focus': 'northeast'},
        {'name': 'Consolidated Communications', 'route_miles': 57000, 'buildings': 37000, 'focus': 'northern_ne'}
    ]
}


@expanded_infra_bp.route('/api/v2/infrastructure/layers', methods=['GET'])
def list_layers():
    layers = []
    for key, info in LAYER_REGISTRY.items():
        layers.append({
            'id': key,
            'name': info['name'],
            'count': info['count'],
            'source': info['source'],
            'category': info['category'],
            'endpoint': f"/api/v2/infrastructure/{key}"
        })
    for key in STATIC_DATA.keys():
        layers.append({
            'id': key,
            'name': key.replace('_', ' ').title(),
            'count': len(STATIC_DATA[key]),
            'source': 'DC Hub',
            'category': 'static',
            'endpoint': f"/api/v2/infrastructure/static/{key}"
        })
    return jsonify({
        'success': True,
        'layer_count': len(layers),
        'layers': layers,
        'categories': ['power', 'gas', 'transportation', 'water', 'environmental', 'fiber', 'utilities', 'renewable', 'static']
    })


@expanded_infra_bp.route('/api/v2/infrastructure/static/<layer_id>', methods=['GET'])
def get_static_layer(layer_id):
    if layer_id not in STATIC_DATA:
        return jsonify({'success': False, 'error': f'Unknown static layer: {layer_id}'}), 404
    return jsonify({
        'success': True,
        'layer': layer_id,
        'count': len(STATIC_DATA[layer_id]),
        'features': STATIC_DATA[layer_id]
    })


@expanded_infra_bp.route('/api/v2/infrastructure/<layer_id>', methods=['GET'])
def get_infrastructure_layer(layer_id):
    if layer_id not in LAYER_REGISTRY:
        return jsonify({'success': False, 'error': f'Unknown layer: {layer_id}'}), 404
    
    lat = request.args.get('lat')
    lng = request.args.get('lng')
    radius = request.args.get('radius', 50, type=float)
    limit = request.args.get('limit', 500, type=int)
    
    layer_info = LAYER_REGISTRY[layer_id]
    url = layer_info['url']
    
    params = {
        'where': '1=1',
        'outFields': '*',
        'returnGeometry': 'true',
        'resultRecordCount': str(min(limit, 2000)),
        'f': 'json',
        'inSR': '4326',
        'outSR': '4326'
    }
    
    if lat and lng:
        bbox = bbox_from_params(lat, lng, radius)
        params['geometry'] = bbox
        params['geometryType'] = 'esriGeometryEnvelope'
        params['spatialRel'] = 'esriSpatialRelIntersects'
    
    cache_key = f"{layer_id}_{lat}_{lng}_{radius}_{limit}"
    cached = get_cached(cache_key)
    if cached:
        return jsonify(cached)
    
    result = query_arcgis(url, params)
    
    features = []
    for f in result.get('features', []):
        attr = f.get('attributes', {})
        geom = f.get('geometry', {})
        
        feature = {'properties': attr}
        
        if 'x' in geom and 'y' in geom:
            feature['lat'] = geom['y']
            feature['lng'] = geom['x']
            feature['geometry_type'] = 'point'
        elif 'paths' in geom:
            feature['paths'] = geom['paths']
            feature['geometry_type'] = 'polyline'
        elif 'rings' in geom:
            feature['rings'] = geom['rings']
            feature['geometry_type'] = 'polygon'
        
        features.append(feature)
    
    response = {
        'success': True,
        'layer': layer_id,
        'name': layer_info['name'],
        'source': layer_info['source'],
        'category': layer_info['category'],
        'count': len(features),
        'features': features,
        'query': {
            'lat': lat,
            'lng': lng,
            'radius_miles': radius
        }
    }
    
    set_cache(cache_key, response)
    return jsonify(response)


@expanded_infra_bp.route('/api/v2/infrastructure/hifld/substations', methods=['GET'])
def get_substations():
    """Query substations from Neon PostgreSQL (with ArcGIS fallback)."""
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    radius = request.args.get('radius', 50, type=float)
    state = request.args.get('state')
    min_voltage = request.args.get('min_voltage', 0, type=int)
    min_kv = request.args.get('min_kv', min_voltage, type=int)
    limit = min(request.args.get('limit', 100, type=int), 500)

    try:
        from db_utils import get_db
        conn = get_db()
        c = conn.cursor()

        conditions = []
        params = []

        if min_kv:
            conditions.append('voltage_kv >= %s')
            params.append(min_kv)
        if state:
            conditions.append('state = %s')
            params.append(state.upper())

        if lat and lng:
            # Bounding box pre-filter then Haversine
            lat_range = radius / 69.0
            lng_range = radius / (69.0 * max(0.1, abs(__import__('math').cos(__import__('math').radians(lat)))))
            conditions.append('lat BETWEEN %s AND %s')
            params.extend([lat - lat_range, lat + lat_range])
            conditions.append('lng BETWEEN %s AND %s')
            params.extend([lng - lng_range, lng + lng_range])

            where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
            query = f"""
                SELECT name, city, state, status, voltage_kv, capacity_mva, lat, lng, owner,
                    ROUND((3959 * acos(LEAST(1.0, cos(radians(%s)) * cos(radians(lat)) * cos(radians(lng) - radians(%s)) + sin(radians(%s)) * sin(radians(lat)))))::numeric, 2) as distance_miles
                FROM substations
                {where}
                AND lat IS NOT NULL AND lng IS NOT NULL
                ORDER BY distance_miles
                LIMIT %s
            """
            params_full = [lat, lng, lat] + params + [limit]
        else:
            where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
            query = f"""
                SELECT name, city, state, status, voltage_kv, capacity_mva, lat, lng, owner,
                    NULL as distance_miles
                FROM substations
                {where}
                AND lat IS NOT NULL AND lng IS NOT NULL
                ORDER BY voltage_kv DESC NULLS LAST
                LIMIT %s
            """
            params_full = params + [limit]

        c.execute(query, params_full)
        cols = [desc[0] for desc in c.description]
        rows = c.fetchall()

        substations = []
        for row in rows:
            r = dict(zip(cols, row))
            substations.append({
                'name': r.get('name'),
                'city': r.get('city'),
                'state': r.get('state'),
                'status': r.get('status'),
                'max_voltage_kv': r.get('voltage_kv'),
                'capacity_mva': r.get('capacity_mva'),
                'lat': r.get('lat'),
                'lng': r.get('lng'),
                'owner': r.get('owner'),
                'distance_miles': float(r['distance_miles']) if r.get('distance_miles') is not None else None,
                'source': 'HIFLD/Neon'
            })

        return jsonify({
            'success': True,
            'count': len(substations),
            'total_available': '1,042 in Neon',
            'source': 'HIFLD/DHS (Neon PostgreSQL)',
            'substations': substations
        })
    except Exception as e:
        import traceback
        print(f"HIFLD substations Neon error: {traceback.format_exc()}")
        return jsonify({
            'success': False,
            'error': str(e),
            'count': 0,
            'substations': []
        }), 500


@expanded_infra_bp.route('/api/v2/infrastructure/hifld/transmission', methods=['GET'])
def get_hifld_transmission():
    """Query transmission lines from Neon PostgreSQL. Uses market mapping for fast queries."""
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    radius = request.args.get('radius', 30, type=float)
    min_voltage = request.args.get('min_voltage', 69, type=int)
    min_kv = request.args.get('min_kv', min_voltage, type=int)
    state = request.args.get('state')
    market = request.args.get('market')
    limit = min(request.args.get('limit', 200, type=int), 1000)

    # Market centers for lat/lng → market mapping
    MARKET_CENTERS = {
        'northern_virginia': (39.04, -77.49), 'atlanta': (33.75, -84.39),
        'dallas': (32.78, -96.80), 'chicago': (41.88, -87.63),
        'phoenix': (33.45, -112.07), 'las_vegas': (36.17, -115.14),
        'houston': (29.76, -95.37), 'denver': (39.74, -104.99),
        'silicon_valley': (37.39, -122.08), 'seattle_quincy': (47.23, -119.85),
        'portland_hillsboro': (45.52, -122.99), 'salt_lake': (40.76, -111.89),
        'new_york_nj': (40.77, -74.17), 'columbus': (40.00, -82.88),
        'san_antonio': (29.42, -98.49), 'kansas_city': (39.10, -94.58),
        'miami': (25.76, -80.19), 'nashville': (36.16, -86.78),
        'minneapolis': (44.98, -93.27), 'des_moines': (41.59, -93.62),
    }

    def find_nearest_market(lat, lng, radius_miles=100):
        import math
        best_market, best_dist = None, float('inf')
        for mkt, (mlat, mlng) in MARKET_CENTERS.items():
            dist = math.sqrt((lat - mlat)**2 + (lng - mlng)**2) * 69
            if dist < best_dist:
                best_market, best_dist = mkt, dist
        return best_market if best_dist <= radius_miles else None

    try:
        from db_utils import get_db
        conn = get_db()
        c = conn.cursor()

        conditions = []
        params = []

        if min_kv:
            conditions.append('t.voltage_kv >= %s')
            params.append(min_kv)
        if state:
            conditions.append('t.market IN (SELECT DISTINCT market FROM discovered_transmission_lines WHERE market IS NOT NULL)')
        if market:
            conditions.append('t.market ILIKE %s')
            params.append(f'%{market}%')

        if lat and lng and not market:
            # Map lat/lng to nearest market for fast indexed query
            nearest_market = find_nearest_market(lat, lng, radius * 1.5)
            if nearest_market:
                conditions.append('t.market = %s')
                params.append(nearest_market)
        where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
        if conditions:
            query = f"""
                SELECT t.id, t.owner, t.voltage_kv, t.volt_class, t.sub_1, t.sub_2, t.status, t.market,
                       NULL as distance_miles
                FROM discovered_transmission_lines t
                {where}
                ORDER BY t.voltage_kv DESC NULLS LAST
                LIMIT %s
            """
            params_full = params + [limit]
        else:
            # No location - filter by market or state
            where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
            query = f"""
                SELECT t.id, t.owner, t.voltage_kv, t.volt_class, t.sub_1, t.sub_2, t.status, t.market,
                       NULL as sub1_lat, NULL as sub1_lng, t.sub_1 as sub1_name,
                       NULL as sub2_lat, NULL as sub2_lng, t.sub_2 as sub2_name,
                       NULL as distance_miles
                FROM discovered_transmission_lines t
                {where}
                ORDER BY t.voltage_kv DESC NULLS LAST
                LIMIT %s
            """
            params_full = params + [limit]

        c.execute(query, params_full)
        cols = [desc[0] for desc in c.description]
        rows = c.fetchall()

        lines = []
        for row in rows:
            r = dict(zip(cols, row))
            line = {
                'id': r.get('id'),
                'voltage_kv': r.get('voltage_kv'),
                'volt_class': r.get('volt_class'),
                'owner': r.get('owner'),
                'status': r.get('status'),
                'market': r.get('market'),
                'sub_1': r.get('sub1_name') or r.get('sub_1'),
                'sub_2': r.get('sub2_name') or r.get('sub_2'),
                'distance_miles': float(r['distance_miles']) if r.get('distance_miles') is not None else None,
                'source': 'HIFLD/Neon'
            }
            # Include substation coordinates for rendering
            if r.get('sub1_lat') and r.get('sub2_lat'):
                line['paths'] = [[[r['sub1_lng'], r['sub1_lat']], [r['sub2_lng'], r['sub2_lat']]]]
            elif r.get('sub1_lat'):
                line['lat'] = r['sub1_lat']
                line['lng'] = r['sub1_lng']
            lines.append(line)

        return jsonify({
            'success': True,
            'count': len(lines),
            'total_available': '2,821,162 in Neon',
            'source': 'HIFLD/DHS (Neon PostgreSQL)',
            'transmission_lines': lines
        })
    except Exception as e:
        import traceback
        print(f"HIFLD transmission Neon error: {traceback.format_exc()}")
        return jsonify({
            'success': False,
            'error': str(e),
            'count': 0,
            'transmission_lines': []
        }), 500


@expanded_infra_bp.route('/api/v2/infrastructure/hifld/gas-pipelines', methods=['GET'])
def get_hifld_gas_pipelines():
    """Query gas pipelines from Neon PostgreSQL (with ArcGIS fallback)."""
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    radius = request.args.get('radius', 50, type=float)
    state = request.args.get('state')
    limit = min(request.args.get('limit', 100, type=int), 500)

    try:
        from db_utils import get_db
        conn = get_db()
        c = conn.cursor()

        conditions = []
        params = []

        if state:
            conditions.append('state = %s')
            params.append(state.upper())

        if lat and lng:
            lat_range = radius / 69.0
            lng_range = radius / (69.0 * max(0.1, abs(__import__('math').cos(__import__('math').radians(lat)))))
            conditions.append('lat BETWEEN %s AND %s')
            params.extend([lat - lat_range, lat + lat_range])
            conditions.append('lng BETWEEN %s AND %s')
            params.extend([lng - lng_range, lng + lng_range])

            where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
            query = f"""
                SELECT name, operator, pipeline_type, diameter_inches, capacity_mcf, status, lat, lng, city, state,
                    ROUND((3959 * acos(LEAST(1.0, cos(radians(%s)) * cos(radians(lat)) * cos(radians(lng) - radians(%s)) + sin(radians(%s)) * sin(radians(lat)))))::numeric, 2) as distance_miles
                FROM gas_pipelines
                {where}
                AND lat IS NOT NULL AND lng IS NOT NULL
                ORDER BY distance_miles
                LIMIT %s
            """
            params_full = [lat, lng, lat] + params + [limit]
        else:
            where = 'WHERE ' + ' AND '.join(conditions) if conditions else ''
            query = f"""
                SELECT name, operator, pipeline_type, diameter_inches, capacity_mcf, status, lat, lng, city, state,
                    NULL as distance_miles
                FROM gas_pipelines
                {where}
                AND lat IS NOT NULL AND lng IS NOT NULL
                LIMIT %s
            """
            params_full = params + [limit]

        c.execute(query, params_full)
        cols = [desc[0] for desc in c.description]
        rows = c.fetchall()

        pipelines = []
        for row in rows:
            r = dict(zip(cols, row))
            pipelines.append({
                'name': r.get('name'),
                'operator': r.get('operator'),
                'pipeline_type': r.get('pipeline_type'),
                'diameter_inches': r.get('diameter_inches'),
                'capacity_mcf': r.get('capacity_mcf'),
                'status': r.get('status'),
                'lat': r.get('lat'),
                'lng': r.get('lng'),
                'city': r.get('city'),
                'state': r.get('state'),
                'distance_miles': float(r['distance_miles']) if r.get('distance_miles') is not None else None,
                'source': 'HIFLD/Neon'
            })

        return jsonify({
            'success': True,
            'count': len(pipelines),
            'total_available': '37,705 in Neon',
            'source': 'HIFLD/DOT (Neon PostgreSQL)',
            'pipelines': pipelines
        })
    except Exception as e:
        import traceback
        print(f"HIFLD gas pipelines Neon error: {traceback.format_exc()}")
        return jsonify({
            'success': False,
            'error': str(e),
            'count': 0,
            'pipelines': []
        }), 500


@expanded_infra_bp.route('/api/v2/infrastructure/railroads', methods=['GET'])
def get_railroads():
    lat = request.args.get('lat')
    lng = request.args.get('lng')
    radius = request.args.get('radius', 50, type=float)
    
    params = {
        'where': '1=1',
        'outFields': 'RROWNER1,RROWNER2,RROWNER3,TRACKS,STFIPS,CNTYFIPS',
        'resultRecordCount': '500',
        'f': 'json'
    }
    
    if lat and lng:
        bbox = bbox_from_params(lat, lng, radius)
        params['geometry'] = bbox
        params['geometryType'] = 'esriGeometryEnvelope'
        params['spatialRel'] = 'esriSpatialRelIntersects'
    
    url = f"{FRA_BASE}/NTAD_North_American_Rail_Network_Lines/FeatureServer/0"
    result = query_arcgis(url, params)
    
    railroads = []
    for f in result.get('features', []):
        attr = f.get('attributes', {})
        geom = f.get('geometry', {})
        railroads.append({
            'owner': attr.get('RROWNER1'),
            'owner_2': attr.get('RROWNER2'),
            'tracks': attr.get('TRACKS'),
            'state_fips': attr.get('STFIPS'),
            'paths': geom.get('paths', []),
            'source': 'FRA/DOT'
        })
    
    return jsonify({
        'success': True,
        'count': len(railroads),
        'total_available': '140,000+ miles',
        'source': 'FRA/DOT',
        'railroads': railroads
    })


@expanded_infra_bp.route('/api/v2/infrastructure/airports', methods=['GET'])
def get_airports():
    lat = request.args.get('lat')
    lng = request.args.get('lng')
    radius = request.args.get('radius', 100, type=float)
    airport_type = request.args.get('type')
    
    params = {
        'outFields': 'IDENT,NAME,TYPE,CITY,STATE,LATITUDE,LONGITUDE,ELEVATION',
        'resultRecordCount': '500',
        'f': 'json',
        'inSR': '4326',
        'outSR': '4326'
    }
    
    where_clauses = ['1=1']
    if airport_type:
        where_clauses.append(f"TYPE='{airport_type}'")
    params['where'] = ' AND '.join(where_clauses)
    
    if lat and lng:
        bbox = bbox_from_params(lat, lng, radius)
        params['geometry'] = bbox
        params['geometryType'] = 'esriGeometryEnvelope'
        params['spatialRel'] = 'esriSpatialRelIntersects'
    
    url = f"{HIFLD_BASE}/Public_Airports/FeatureServer/0"
    result = query_arcgis(url, params)
    
    airports = []
    for f in result.get('features', []):
        attr = f.get('attributes', {})
        airports.append({
            'ident': attr.get('IDENT'),
            'name': attr.get('NAME'),
            'type': attr.get('TYPE'),
            'city': attr.get('CITY'),
            'state': attr.get('STATE'),
            'lat': attr.get('LATITUDE'),
            'lng': attr.get('LONGITUDE'),
            'elevation_ft': attr.get('ELEVATION'),
            'source': 'HIFLD/FAA'
        })
    
    return jsonify({
        'success': True,
        'count': len(airports),
        'total_available': '19,000+',
        'source': 'HIFLD/FAA',
        'airports': airports
    })


@expanded_infra_bp.route('/api/v2/infrastructure/aquifers', methods=['GET'])
def get_aquifers():
    lat = request.args.get('lat')
    lng = request.args.get('lng')
    radius = request.args.get('radius', 100, type=float)
    
    params = {
        'where': '1=1',
        'outFields': '*',
        'resultRecordCount': '100',
        'f': 'json'
    }
    
    if lat and lng:
        bbox = bbox_from_params(lat, lng, radius)
        params['geometry'] = bbox
        params['geometryType'] = 'esriGeometryEnvelope'
        params['spatialRel'] = 'esriSpatialRelIntersects'
    
    result = query_arcgis(USGS_AQUIFERS, params)
    
    aquifers = []
    for f in result.get('features', []):
        attr = f.get('attributes', {})
        aquifers.append({
            'name': attr.get('AQ_NAME') or attr.get('NAT_AQFR_CD'),
            'rock_type': attr.get('ROCK_TYPE'),
            'rock_name': attr.get('ROCK_NAME'),
            'source': 'USGS'
        })
    
    return jsonify({
        'success': True,
        'count': len(aquifers),
        'total_available': '66 Principal Aquifers',
        'source': 'USGS',
        'aquifers': aquifers
    })


@expanded_infra_bp.route('/api/v2/infrastructure/internet-exchanges', methods=['GET'])
def get_internet_exchanges():
    lat = request.args.get('lat')
    lng = request.args.get('lng')
    radius = request.args.get('radius', 100, type=float)
    
    params = {
        'where': '1=1',
        'outFields': '*',
        'resultRecordCount': '200',
        'f': 'json',
        'inSR': '4326',
        'outSR': '4326'
    }
    
    if lat and lng:
        bbox = bbox_from_params(lat, lng, radius)
        params['geometry'] = bbox
        params['geometryType'] = 'esriGeometryEnvelope'
        params['spatialRel'] = 'esriSpatialRelIntersects'
    
    url = f"{HIFLD_BASE}/Internet_Exchange_Points/FeatureServer/0"
    result = query_arcgis(url, params)
    
    exchanges = []
    for f in result.get('features', []):
        attr = f.get('attributes', {})
        geom = f.get('geometry', {})
        exchanges.append({
            'name': attr.get('NAME'),
            'city': attr.get('CITY'),
            'state': attr.get('STATE'),
            'lat': geom.get('y'),
            'lng': geom.get('x'),
            'source': 'HIFLD'
        })
    
    return jsonify({
        'success': True,
        'count': len(exchanges),
        'total_available': '100+',
        'source': 'HIFLD',
        'internet_exchanges': exchanges
    })


@expanded_infra_bp.route('/api/v2/infrastructure/summary', methods=['GET'])
def infrastructure_summary():
    lat = request.args.get('lat')
    lng = request.args.get('lng')
    radius = request.args.get('radius', 50, type=float)
    
    summary = {
        'power': {
            'substations': {'available': True, 'source': 'HIFLD', 'count': '70k+'},
            'transmission': {'available': True, 'source': 'HIFLD', 'count': '300k+ mi'},
            'power_plants': {'available': True, 'source': 'HIFLD', 'count': '10k+'}
        },
        'gas': {
            'pipelines': {'available': True, 'source': 'HIFLD', 'count': '300k+ mi'},
            'compressors': {'available': True, 'source': 'HIFLD', 'count': '1.5k+'},
            'lng_terminals': {'available': True, 'source': 'DC Hub', 'count': '10'},
            'storage': {'available': True, 'source': 'HIFLD', 'count': '400+'},
            'processing': {'available': True, 'source': 'HIFLD', 'count': '500+'},
            'market_hubs': {'available': True, 'source': 'DC Hub', 'count': '15'}
        },
        'transportation': {
            'railroads': {'available': True, 'source': 'FRA', 'count': '140k+ mi'},
            'airports': {'available': True, 'source': 'HIFLD/FAA', 'count': '19k+'},
            'submarine_cables': {'available': True, 'source': 'DC Hub', 'count': '18'}
        },
        'water': {
            'aquifers': {'available': True, 'source': 'USGS', 'count': '66'},
            'rivers': {'available': True, 'source': 'DC Hub', 'count': '7'}
        },
        'environmental': {
            'flood_zones': {'available': True, 'source': 'FEMA', 'count': 'Live'},
            'seismic': {'available': True, 'source': 'USGS', 'count': '681+'}
        },
        'fiber': {
            'internet_exchanges': {'available': True, 'source': 'HIFLD', 'count': '100+'},
            'long_haul_fiber': {'available': True, 'source': 'DC Hub', 'count': '12 providers'}
        },
        'utilities': {
            'iso_rto': {'available': True, 'source': 'DC Hub', 'count': '7'},
            'utility_territories': {'available': True, 'source': 'HIFLD', 'count': '3k+'}
        },
        'query': {'lat': lat, 'lng': lng, 'radius_miles': radius}
    }
    
    return jsonify({
        'success': True,
        'summary': summary,
        'total_layer_types': sum(len(cat) for cat in summary.values() if isinstance(cat, dict)),
        'generated_at': datetime.now().isoformat()
    })


def register_expanded_infrastructure(app):
    app.register_blueprint(expanded_infra_bp)
    print("✅ Expanded Infrastructure API v2 registered:")
    print("   GET /api/v2/infrastructure/layers - List all 40+ layers")
    print("   GET /api/v2/infrastructure/<layer_id> - Query any layer")
    print("   GET /api/v2/infrastructure/hifld/substations - 70k+ substations")
    print("   GET /api/v2/infrastructure/hifld/transmission - 300k+ transmission")
    print("   GET /api/v2/infrastructure/hifld/gas-pipelines - 300k+ gas")
    print("   GET /api/v2/infrastructure/railroads - FRA rail network")
    print("   GET /api/v2/infrastructure/airports - 19k+ airports")
    print("   GET /api/v2/infrastructure/aquifers - USGS aquifers")
    print("   GET /api/v2/infrastructure/internet-exchanges - IXPs")
    print("   GET /api/v2/infrastructure/static/<layer> - Static datasets")
    print("   GET /api/v2/infrastructure/summary - Full infrastructure summary")
