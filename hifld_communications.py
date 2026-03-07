"""
DC Hub — HIFLD Communications Infrastructure
===============================================
Cell towers, microwave backhaul, antenna structures from NASA HIFLD mirror.

Endpoints:
  /api/v2/comms/cellular-towers     — Cell tower locations
  /api/v2/comms/microwave-towers    — Microwave backhaul towers
  /api/v2/comms/antenna-structures  — FCC registered antenna structures
"""

import time
import logging
import requests
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

comms_bp = Blueprint('hifld_comms', __name__)

HIFLD_COMMS_BASE = 'https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/communications/FeatureServer'

# Layer indices from HIFLD Communications FeatureServer
COMMS_LAYERS = {
    'cellular_towers': 5,
    'microwave_service_towers': 11,
    'antenna_structure_registrate': 1,
    'fm_transmission_towers': 7,
    'land_mobile_commercial': 9,
}

# Cache
_comms_cache = {}
CACHE_TTL = 3600  # 1 hour


def _get_cache(key):
    if key in _comms_cache:
        data, ts = _comms_cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
        del _comms_cache[key]
    return None


def _set_cache(key, data):
    if len(_comms_cache) > 50:
        oldest = min(_comms_cache, key=lambda k: _comms_cache[k][1])
        del _comms_cache[oldest]
    _comms_cache[key] = (data, time.time())


def query_hifld_comms(layer_index, bbox, max_results=500):
    """Query HIFLD Communications FeatureServer"""
    cache_key = f"comms:{layer_index}:{bbox}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    url = f"{HIFLD_COMMS_BASE}/{layer_index}/query"
    params = {
        'where': '1=1',
        'geometry': bbox,
        'geometryType': 'esriGeometryEnvelope',
        'spatialRel': 'esriSpatialRelIntersects',
        'outFields': '*',
        'inSR': '4326',
        'outSR': '4326',
        'f': 'json',
        'resultRecordCount': max_results
    }

    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        features = []
        for feat in data.get('features', []):
            geom = feat.get('geometry', {})
            attrs = feat.get('attributes', {})
            
            if geom.get('x') and geom.get('y'):
                features.append({
                    'type': 'Feature',
                    'geometry': {
                        'type': 'Point',
                        'coordinates': [geom['x'], geom['y']]
                    },
                    'properties': attrs
                })

        result = {
            'type': 'FeatureCollection',
            'features': features,
            'count': len(features)
        }
        _set_cache(cache_key, result)
        return result

    except requests.exceptions.RequestException as e:
        logger.error(f"HIFLD Comms query error (layer {layer_index}): {e}")
        return {'type': 'FeatureCollection', 'features': [], 'count': 0, 'error': str(e)}


def _make_bbox(min_lat, max_lat, min_lng, max_lng):
    return f"{min_lng},{min_lat},{max_lng},{max_lat}"


def _parse_bounds(req):
    min_lat = req.args.get('minLat', type=float)
    max_lat = req.args.get('maxLat', type=float)
    min_lng = req.args.get('minLng', type=float)
    max_lng = req.args.get('maxLng', type=float)
    if not all(v is not None for v in [min_lat, max_lat, min_lng, max_lng]):
        return None, None
    return _make_bbox(min_lat, max_lat, min_lng, max_lng), {'minLat': min_lat, 'maxLat': max_lat, 'minLng': min_lng, 'maxLng': max_lng}


@comms_bp.route('/api/v2/comms/cellular-towers', methods=['GET'])
def get_cellular_towers():
    """Get cell tower locations from HIFLD within bounding box."""
    bbox, bounds = _parse_bounds(request)
    if not bbox:
        return jsonify({'success': False, 'error': 'Bounds required: minLat, maxLat, minLng, maxLng'}), 400

    result = query_hifld_comms(COMMS_LAYERS['cellular_towers'], bbox)
    return jsonify({'success': True, 'source': 'HIFLD Communications', **result})


@comms_bp.route('/api/v2/comms/microwave-towers', methods=['GET'])
def get_microwave_towers():
    """Get microwave backhaul tower locations from HIFLD."""
    bbox, bounds = _parse_bounds(request)
    if not bbox:
        return jsonify({'success': False, 'error': 'Bounds required'}), 400

    result = query_hifld_comms(COMMS_LAYERS['microwave_service_towers'], bbox)
    return jsonify({'success': True, 'source': 'HIFLD Communications', **result})


@comms_bp.route('/api/v2/comms/antenna-structures', methods=['GET'])
def get_antenna_structures():
    """Get FCC registered antenna structures from HIFLD."""
    bbox, bounds = _parse_bounds(request)
    if not bbox:
        return jsonify({'success': False, 'error': 'Bounds required'}), 400

    result = query_hifld_comms(COMMS_LAYERS['antenna_structure_registrate'], bbox)
    return jsonify({'success': True, 'source': 'HIFLD Communications', **result})


@comms_bp.route('/api/v2/comms/summary', methods=['GET'])
def comms_summary():
    """Get communications infrastructure summary near a location."""
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    radius = request.args.get('radius', 0.25, type=float)

    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    bbox = _make_bbox(lat - radius, lat + radius, lng - radius, lng + radius)
    
    cell_data = query_hifld_comms(COMMS_LAYERS['cellular_towers'], bbox, max_results=100)
    microwave_data = query_hifld_comms(COMMS_LAYERS['microwave_service_towers'], bbox, max_results=100)

    cell_count = cell_data.get('count', 0)
    mw_count = microwave_data.get('count', 0)
    
    if cell_count >= 20:
        connectivity_level = 'Excellent'
        score = 90
    elif cell_count >= 10:
        connectivity_level = 'Good'
        score = 70
    elif cell_count >= 3:
        connectivity_level = 'Moderate'
        score = 45
    else:
        connectivity_level = 'Limited'
        score = 20

    return jsonify({
        'success': True,
        'location': {'lat': lat, 'lng': lng},
        'communications': {
            'score': score,
            'level': connectivity_level,
            'cellular_towers': cell_count,
            'microwave_towers': mw_count,
            'total_infrastructure': cell_count + mw_count,
        },
        'source': 'HIFLD Communications'
    })


def register_comms_routes(app):
    """Register HIFLD communications blueprint with Flask app"""
    app.register_blueprint(comms_bp)
    logger.info("📡 HIFLD Communications routes registered")
