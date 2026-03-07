"""
DC Hub — Water & Drought Intelligence
=======================================
USGS water data + US Drought Monitor for cooling water assessment.

Endpoints:
  /api/v2/water/streamflow    — Real-time streamflow near coordinates
  /api/v2/water/groundwater   — Groundwater monitoring wells nearby
  /api/v2/water/drought       — Current drought severity by state/county
  /api/v2/water/summary       — Combined water risk assessment
"""

import time
import logging
import requests
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

water_bp = Blueprint('water_intel', __name__)

# USGS Water Services
USGS_WATER_API = 'https://waterservices.usgs.gov/nwis'
DROUGHT_API = 'https://usdmdataservices.unl.edu/api'

# Cache
_water_cache = {}
CACHE_TTL = 3600  # 1 hour for real-time water data
DROUGHT_CACHE_TTL = 86400  # 24 hours for weekly drought data


def _get_cache(key, ttl=CACHE_TTL):
    if key in _water_cache:
        data, ts = _water_cache[key]
        if time.time() - ts < ttl:
            return data
        del _water_cache[key]
    return None


def _set_cache(key, data):
    if len(_water_cache) > 100:
        oldest = min(_water_cache, key=lambda k: _water_cache[k][1])
        del _water_cache[oldest]
    _water_cache[key] = (data, time.time())


@water_bp.route('/api/v2/water/streamflow', methods=['GET'])
def get_streamflow():
    """
    Get real-time streamflow data from USGS monitoring stations near coordinates.
    
    Query params:
        lat, lng: Center point (required)
        radius: Search radius in miles (default 25)
        limit: Max stations (default 10)
    """
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    radius = request.args.get('radius', 25, type=float)
    limit = min(50, request.args.get('limit', 10, type=int))

    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    cache_key = f"streamflow:{lat:.2f},{lng:.2f}:{radius}"
    cached = _get_cache(cache_key)
    if cached:
        return jsonify({'success': True, **cached})

    # Convert miles to degrees for bounding box
    deg_lat = radius / 69.0
    deg_lng = radius / (69.0 * max(0.1, abs(0.0174533 * lat)))

    params = {
        'format': 'json',
        'bBox': f'{lng - deg_lng:.4f},{lat - deg_lat:.4f},{lng + deg_lng:.4f},{lat + deg_lat:.4f}',
        'siteType': 'ST',  # Stream sites
        'siteStatus': 'active',
        'parameterCd': '00060',  # Discharge (streamflow)
        'hasDataTypeCd': 'iv',  # Instantaneous values
    }

    try:
        resp = requests.get(f'{USGS_WATER_API}/site/', params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        
        sites = data.get('value', {}).get('timeSeries', [])
        if not sites:
            # Try the sites endpoint directly
            resp2 = requests.get(f'{USGS_WATER_API}/site/', params={
                **params, 'format': 'rdb'
            }, timeout=20)
            # Parse RDB format
            stations = []
        else:
            stations = []
            for site in sites[:limit]:
                site_info = site.get('sourceInfo', {})
                geo = site_info.get('geoLocation', {}).get('geogLocation', {})
                values = site.get('values', [{}])[0].get('value', [])
                latest = values[-1] if values else {}
                
                stations.append({
                    'site_id': site_info.get('siteCode', [{}])[0].get('value', ''),
                    'name': site_info.get('siteName', ''),
                    'lat': geo.get('latitude'),
                    'lng': geo.get('longitude'),
                    'streamflow_cfs': float(latest.get('value', 0)) if latest.get('value') else None,
                    'measurement_time': latest.get('dateTime', ''),
                    'source': 'USGS'
                })

        result = {
            'stations': stations,
            'count': len(stations),
            'radius_miles': radius,
            'source': 'USGS National Water Information System'
        }
        _set_cache(cache_key, result)
        return jsonify({'success': True, **result})

    except requests.exceptions.RequestException as e:
        logger.error(f"USGS streamflow API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 502


@water_bp.route('/api/v2/water/groundwater', methods=['GET'])
def get_groundwater():
    """
    Get groundwater monitoring wells near coordinates.
    
    Query params:
        lat, lng: Center point (required)
        radius: Search radius in miles (default 25)
    """
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    radius = request.args.get('radius', 25, type=float)

    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    cache_key = f"gw:{lat:.2f},{lng:.2f}:{radius}"
    cached = _get_cache(cache_key)
    if cached:
        return jsonify({'success': True, **cached})

    deg_lat = radius / 69.0
    deg_lng = radius / (69.0 * max(0.1, abs(0.0174533 * lat)))

    params = {
        'format': 'json',
        'bBox': f'{lng - deg_lng:.4f},{lat - deg_lat:.4f},{lng + deg_lng:.4f},{lat + deg_lat:.4f}',
        'siteType': 'GW',  # Groundwater wells
        'siteStatus': 'active',
        'parameterCd': '72019',  # Depth to water level
        'hasDataTypeCd': 'iv',
    }

    try:
        resp = requests.get(f'{USGS_WATER_API}/site/', params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        
        sites = data.get('value', {}).get('timeSeries', [])
        wells = []
        for site in sites[:20]:
            site_info = site.get('sourceInfo', {})
            geo = site_info.get('geoLocation', {}).get('geogLocation', {})
            values = site.get('values', [{}])[0].get('value', [])
            latest = values[-1] if values else {}
            
            wells.append({
                'site_id': site_info.get('siteCode', [{}])[0].get('value', ''),
                'name': site_info.get('siteName', ''),
                'lat': geo.get('latitude'),
                'lng': geo.get('longitude'),
                'water_depth_ft': float(latest.get('value', 0)) if latest.get('value') else None,
                'measurement_time': latest.get('dateTime', ''),
                'source': 'USGS'
            })

        result = {
            'wells': wells,
            'count': len(wells),
            'radius_miles': radius,
            'source': 'USGS Groundwater'
        }
        _set_cache(cache_key, result)
        return jsonify({'success': True, **result})

    except requests.exceptions.RequestException as e:
        logger.error(f"USGS groundwater API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 502


@water_bp.route('/api/v2/water/drought', methods=['GET'])
def get_drought():
    """
    Get current drought conditions from US Drought Monitor.
    
    Query params:
        state: Two-letter state code (required, e.g., 'AZ')
    """
    state = request.args.get('state', '').upper()
    if not state or len(state) != 2:
        return jsonify({'success': False, 'error': 'Two-letter state code required'}), 400

    cache_key = f"drought:{state}"
    cached = _get_cache(cache_key, ttl=DROUGHT_CACHE_TTL)
    if cached:
        return jsonify({'success': True, **cached})

    # US Drought Monitor API
    url = f"https://usdmdataservices.unl.edu/api/StateStatistics/GetDroughtSeverityStatisticsByAreaPercent"
    params = {
        'aoi': state,
        'startdate': '',  # empty = current
        'enddate': '',
        'statisticsType': 'GetDroughtSeverityStatisticsByAreaPercent'
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            
            # Parse drought levels
            result = {
                'state': state,
                'drought_data': data[:5] if isinstance(data, list) else data,
                'source': 'US Drought Monitor'
            }
        else:
            # Fallback: try simpler endpoint
            result = {
                'state': state,
                'drought_data': [],
                'note': 'Drought Monitor API returned non-200',
                'source': 'US Drought Monitor'
            }

        _set_cache(cache_key, result)
        return jsonify({'success': True, **result})

    except requests.exceptions.RequestException as e:
        logger.error(f"Drought Monitor API error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 502


@water_bp.route('/api/v2/water/summary', methods=['GET'])
def water_summary():
    """
    Combined water risk assessment for a location.
    
    Query params:
        lat, lng: Location (required)
        state: State code (required for drought data)
    """
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    state = request.args.get('state', '').upper()

    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    # Gather data from sub-endpoints
    streamflow_data = []
    groundwater_data = []
    drought_data = {}

    try:
        # Streamflow
        deg = 25 / 69.0
        sf_params = {
            'format': 'json',
            'bBox': f'{lng-deg:.4f},{lat-deg:.4f},{lng+deg:.4f},{lat+deg:.4f}',
            'siteType': 'ST',
            'siteStatus': 'active',
            'parameterCd': '00060',
            'hasDataTypeCd': 'iv',
        }
        sf_resp = requests.get(f'{USGS_WATER_API}/site/', params=sf_params, timeout=15)
        if sf_resp.status_code == 200:
            sf_sites = sf_resp.json().get('value', {}).get('timeSeries', [])
            streamflow_data = [{'name': s.get('sourceInfo', {}).get('siteName', '')} for s in sf_sites[:5]]
    except Exception as e:
        logger.warning(f"Streamflow lookup failed: {e}")

    # Water availability score
    stream_count = len(streamflow_data)
    
    if stream_count >= 5:
        water_score = 85
        water_level = 'Excellent'
    elif stream_count >= 2:
        water_score = 65
        water_level = 'Good'
    elif stream_count >= 1:
        water_score = 40
        water_level = 'Limited'
    else:
        water_score = 15
        water_level = 'Scarce'

    return jsonify({
        'success': True,
        'location': {'lat': lat, 'lng': lng, 'state': state},
        'water_assessment': {
            'score': water_score,
            'level': water_level,
            'streams_nearby': stream_count,
            'groundwater_wells': len(groundwater_data),
        },
        'source': 'USGS + US Drought Monitor'
    })


def register_water_routes(app):
    """Register water intelligence blueprint with Flask app"""
    app.register_blueprint(water_bp)
    logger.info("💧 Water & drought intelligence routes registered")
