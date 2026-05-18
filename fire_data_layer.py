"""
DC Hub — NASA FIRMS Fire Data Layer
====================================
Near real-time active fire/hotspot data from VIIRS satellite.
Free API with Earthdata MAP_KEY.

Endpoints:
  /api/v2/risk/active-fires  — Active fires within bounding box
  /api/v2/risk/fire-history  — Fire history for site risk assessment
"""

import os
import time
import logging
import requests
from flask import Blueprint, request, jsonify
from functools import lru_cache

logger = logging.getLogger(__name__)

fire_bp = Blueprint('fire_data', __name__)

# NASA FIRMS API - get key at https://firms.modaps.eosdis.nasa.gov/api/map_key/
FIRMS_MAP_KEY = os.environ.get('FIRMS_MAP_KEY', '')
FIRMS_BASE = 'https://firms.modaps.eosdis.nasa.gov/api/area/csv'

# Simple in-memory cache
_fire_cache = {}
CACHE_TTL = 10800  # 3 hours


def _get_cache(key):
    if key in _fire_cache:
        data, ts = _fire_cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
        del _fire_cache[key]
    return None


def _set_cache(key, data):
    # Keep cache small
    if len(_fire_cache) > 50:
        oldest = min(_fire_cache, key=lambda k: _fire_cache[k][1])
        del _fire_cache[oldest]
    _fire_cache[key] = (data, time.time())


def query_firms(west, south, east, north, days=1, source='VIIRS_NOAA20_NRT'):
    """Query NASA FIRMS API for active fires in bounding box"""
    if not FIRMS_MAP_KEY:
        return {'error': 'FIRMS_MAP_KEY not configured', 'features': []}

    cache_key = f"firms:{west:.1f},{south:.1f},{east:.1f},{north:.1f}:{days}:{source}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    url = f"{FIRMS_BASE}/{FIRMS_MAP_KEY}/{source}/{west},{south},{east},{north}/{days}"
    
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        
        lines = resp.text.strip().split('\n')
        if len(lines) < 2:
            result = {'features': [], 'count': 0, 'source': source}
            _set_cache(cache_key, result)
            return result

        headers = lines[0].split(',')
        features = []
        
        for line in lines[1:]:
            vals = line.split(',')
            if len(vals) < len(headers):
                continue
            row = dict(zip(headers, vals))
            
            try:
                lat = float(row.get('latitude', 0))
                lng = float(row.get('longitude', 0))
                if lat == 0 and lng == 0:
                    continue
                    
                features.append({
                    'type': 'Feature',
                    'geometry': {
                        'type': 'Point',
                        'coordinates': [lng, lat]
                    },
                    'properties': {
                        'latitude': lat,
                        'longitude': lng,
                        'brightness': float(row.get('bright_ti4', row.get('brightness', 0))),
                        'confidence': row.get('confidence', 'unknown'),
                        'frp': float(row.get('frp', 0)),
                        'acq_date': row.get('acq_date', ''),
                        'acq_time': row.get('acq_time', ''),
                        'satellite': row.get('satellite', source),
                        'daynight': row.get('daynight', ''),
                    }
                })
            except (ValueError, TypeError):
                continue

        result = {
            'type': 'FeatureCollection',
            'features': features,
            'count': len(features),
            'source': source,
            'days_queried': days
        }
        _set_cache(cache_key, result)
        return result

    except requests.exceptions.RequestException as e:
        logger.error(f"FIRMS API error: {e}")
        return {'error': str(e), 'features': [], 'count': 0}


# AUTO-REPAIR: duplicate route '/api/v2/risk/active-fires' also in main.py:15643 — review and remove one
@fire_bp.route('/api/v2/risk/active-fires', methods=['GET'])
def get_active_fires():
    """
    Get active fires within bounding box from NASA FIRMS.
    
    Query params:
        minLat, maxLat, minLng, maxLng: Bounding box (required)
        days: Number of days to look back (1-10, default 1)
        source: VIIRS_NOAA20_NRT, VIIRS_SNPP_NRT, MODIS_NRT (default: VIIRS_NOAA20_NRT)
    """
    min_lat = request.args.get('minLat', type=float)
    max_lat = request.args.get('maxLat', type=float)
    min_lng = request.args.get('minLng', type=float)
    max_lng = request.args.get('maxLng', type=float)
    days = min(10, max(1, request.args.get('days', 1, type=int)))
    source = request.args.get('source', 'VIIRS_NOAA20_NRT')

    if not all(v is not None for v in [min_lat, max_lat, min_lng, max_lng]):
        return jsonify({'success': False, 'error': 'Bounds required: minLat, maxLat, minLng, maxLng'}), 400

    if not FIRMS_MAP_KEY:
        return jsonify({
            'success': False, 
            'error': 'NASA FIRMS API key not configured. Set FIRMS_MAP_KEY environment variable.',
            'setup_url': 'https://firms.modaps.eosdis.nasa.gov/api/map_key/'
        }), 503

    result = query_firms(min_lng, min_lat, max_lng, max_lat, days, source)
    
    if 'error' in result and result.get('count', 0) == 0:
        return jsonify({'success': False, 'error': result['error']}), 502

    return jsonify({
        'success': True,
        'data': result,
        'count': result.get('count', 0),
        'source': 'NASA FIRMS',
        'satellite': source,
        'days': days
    })


@fire_bp.route('/api/v2/risk/fire-history', methods=['GET'])
def get_fire_history():
    """
    Get 10-day fire history for a specific location (site risk assessment).
    
    Query params:
        lat, lng: Center point (required)
        radius: Radius in degrees (default 0.5 = ~55km)
    """
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    radius = request.args.get('radius', 0.5, type=float)

    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    if not FIRMS_MAP_KEY:
        return jsonify({'success': False, 'error': 'FIRMS_MAP_KEY not configured'}), 503

    result = query_firms(
        lng - radius, lat - radius, 
        lng + radius, lat + radius, 
        days=10, source='VIIRS_NOAA20_NRT'
    )

    # Calculate risk metrics
    fire_count = result.get('count', 0)
    max_frp = max((f['properties']['frp'] for f in result.get('features', [])), default=0)
    high_confidence = len([f for f in result.get('features', []) 
                          if f['properties'].get('confidence') in ('high', 'h', 'H')])

    risk_score = min(100, fire_count * 5 + (max_frp / 10))
    if fire_count == 0:
        risk_level = 'Low'
    elif fire_count <= 5:
        risk_level = 'Moderate'
    elif fire_count <= 20:
        risk_level = 'High'
    else:
        risk_level = 'Extreme'

    return jsonify({
        'success': True,
        'location': {'lat': lat, 'lng': lng, 'radius_deg': radius},
        'fire_risk': {
            'score': round(risk_score, 1),
            'level': risk_level,
            'fires_10day': fire_count,
            'high_confidence_fires': high_confidence,
            'max_frp': round(max_frp, 1),
        },
        'fires': result.get('features', [])[:50],  # Limit response size
        'source': 'NASA FIRMS VIIRS'
    })


def register_fire_routes(app):
    """Register fire data blueprint with Flask app"""
    app.register_blueprint(fire_bp)
    logger.info("🔥 NASA FIRMS fire data routes registered")
