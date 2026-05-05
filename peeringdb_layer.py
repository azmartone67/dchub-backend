"""
DC Hub — PeeringDB Interconnection Layer
==========================================
Data center facilities, Internet Exchange Points, and network presence.
Free API, no key needed for read access.

Endpoints:
  /api/v2/connectivity/facilities  — DC facilities with network counts
  /api/v2/connectivity/ixps        — Internet Exchange Points
  /api/v2/connectivity/networks    — Networks at a specific facility
"""

import time
import logging
import requests
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

peeringdb_bp = Blueprint('peeringdb', __name__)

PEERINGDB_API = 'https://www.peeringdb.com/api'

# Cache
_pdb_cache = {}
CACHE_TTL = 86400  # 24 hours — PeeringDB updates daily


def _get_cache(key):
    if key in _pdb_cache:
        data, ts = _pdb_cache[key]
        if time.time() - ts < CACHE_TTL:
            return data
        del _pdb_cache[key]
    return None


def _set_cache(key, data):
    if len(_pdb_cache) > 100:
        oldest = min(_pdb_cache, key=lambda k: _pdb_cache[k][1])
        del _pdb_cache[oldest]
    _pdb_cache[key] = (data, time.time())


def query_peeringdb(endpoint, params=None, timeout=30):
    """Query PeeringDB API with caching"""
    cache_key = f"pdb:{endpoint}:{str(sorted(params.items()) if params else '')}"
    cached = _get_cache(cache_key)
    if cached:
        return cached

    url = f"{PEERINGDB_API}/{endpoint}"
    try:
        resp = requests.get(url, params=params, timeout=timeout, headers={
            'Accept': 'application/json',
            'User-Agent': 'DCHub/1.0 (https://dchub.cloud)'
        })
        resp.raise_for_status()
        data = resp.json().get('data', [])
        _set_cache(cache_key, data)
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"PeeringDB API error: {e}")
        return []


@peeringdb_bp.route('/api/v2/connectivity/facilities', methods=['GET'])
def get_peeringdb_facilities():
    """
    Get data center facilities from PeeringDB within bounding box.
    
    Query params:
        minLat, maxLat, minLng, maxLng: Bounding box (required)
        limit: Max results (default 100)
    """
    min_lat = request.args.get('minLat', type=float)
    max_lat = request.args.get('maxLat', type=float)
    min_lng = request.args.get('minLng', type=float)
    max_lng = request.args.get('maxLng', type=float)
    limit = min(500, request.args.get('limit', 100, type=int))

    if not all(v is not None for v in [min_lat, max_lat, min_lng, max_lng]):
        return jsonify({'success': False, 'error': 'Bounds required'}), 400

    params = {
        'latitude__gte': min_lat,
        'latitude__lte': max_lat,
        'longitude__gte': min_lng,
        'longitude__lte': max_lng,
        'limit': limit
    }

    facilities = query_peeringdb('fac', params)
    
    features = []
    for fac in facilities:
        lat = fac.get('latitude')
        lng = fac.get('longitude')
        if not lat or not lng:
            continue
            
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [float(lng), float(lat)]
            },
            'properties': {
                'id': fac.get('id'),
                'name': fac.get('name', ''),
                'org_name': fac.get('org', {}).get('name', '') if isinstance(fac.get('org'), dict) else str(fac.get('org_id', '')),
                'city': fac.get('city', ''),
                'state': fac.get('state', ''),
                'country': fac.get('country', ''),
                'zipcode': fac.get('zipcode', ''),
                'website': fac.get('website', ''),
                'net_count': fac.get('net_count', 0),
                'ix_count': fac.get('ix_count', 0),
                'floor_space': fac.get('available_voltage_services', ''),
                'diverse_serving_substations': fac.get('diverse_serving_substations', False),
                'property': fac.get('property', ''),
                'notes': fac.get('notes', '')[:200] if fac.get('notes') else '',
                'source': 'PeeringDB'
            }
        })

    return jsonify({
        'success': True,
        'type': 'FeatureCollection',
        'features': features,
        'count': len(features),
        'source': 'PeeringDB'
    })


@peeringdb_bp.route('/api/v2/connectivity/ixps', methods=['GET'])
def get_ixps():
    """
    Get Internet Exchange Points from PeeringDB.
    
    Query params:
        country: Country code filter (e.g., 'US')
        city: City name filter
        limit: Max results (default 100)
    """
    country = request.args.get('country', '')
    city = request.args.get('city', '')
    limit = min(500, request.args.get('limit', 100, type=int))

    params = {'limit': limit}
    if country:
        params['country'] = country
    if city:
        params['city__contains'] = city

    ixps = query_peeringdb('ix', params)
    
    results = []
    for ix in ixps:
        results.append({
            'id': ix.get('id'),
            'name': ix.get('name', ''),
            'name_long': ix.get('name_long', ''),
            'city': ix.get('city', ''),
            'country': ix.get('country', ''),
            'region_continent': ix.get('region_continent', ''),
            'media': ix.get('media', ''),
            'proto_unicast': ix.get('proto_unicast', False),
            'proto_multicast': ix.get('proto_multicast', False),
            'proto_ipv6': ix.get('proto_ipv6', False),
            'net_count': ix.get('net_count', 0),
            'fac_count': ix.get('fac_count', 0),
            'website': ix.get('website', ''),
            'tech_email': ix.get('tech_email', ''),
            'policy_email': ix.get('policy_email', ''),
            'notes': ix.get('notes', '')[:200] if ix.get('notes') else '',
            'source': 'PeeringDB'
        })

    return jsonify({
        'success': True,
        'data': results,
        'count': len(results),
        'source': 'PeeringDB'
    })


@peeringdb_bp.route('/api/v2/connectivity/networks', methods=['GET'])
def get_networks_at_facility():
    """
    Get all networks present at a specific facility.
    
    Query params:
        fac_id: PeeringDB facility ID (required)
    """
    fac_id = request.args.get('fac_id', type=int)
    if not fac_id:
        return jsonify({'success': False, 'error': 'fac_id required'}), 400

    netfacs = query_peeringdb('netfac', {'fac_id': fac_id, 'limit': 500})
    
    networks = []
    for nf in netfacs:
        networks.append({
            'net_id': nf.get('net_id'),
            'name': nf.get('name', ''),
            'asn': nf.get('local_asn', nf.get('asn', 0)),
            'speed': nf.get('speed', 0),
            'status': nf.get('status', ''),
            'city': nf.get('city', ''),
            'country': nf.get('country', ''),
        })

    return jsonify({
        'success': True,
        'fac_id': fac_id,
        'networks': networks,
        'count': len(networks),
        'source': 'PeeringDB'
    })


@peeringdb_bp.route('/api/v2/connectivity/summary', methods=['GET'])
def connectivity_summary():
    """
    Get connectivity summary near a location — combines facilities + IXPs.
    
    Query params:
        lat, lng: Center point (required)
        radius: Radius in degrees (default 0.5 = ~55km)
    """
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    radius = request.args.get('radius', 0.5, type=float)

    if lat is None or lng is None:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400

    # Get nearby facilities
    fac_params = {
        'latitude__gte': lat - radius,
        'latitude__lte': lat + radius,
        'longitude__gte': lng - radius,
        'longitude__lte': lng + radius,
        'limit': 50
    }
    facilities = query_peeringdb('fac', fac_params)

    total_networks = sum(f.get('net_count', 0) for f in facilities)
    total_ixs = sum(f.get('ix_count', 0) for f in facilities)
    
    # Connectivity score (0-100)
    if len(facilities) == 0:
        score = 0
        level = 'None'
    elif total_networks < 10:
        score = 20
        level = 'Low'
    elif total_networks < 50:
        score = 40
        level = 'Moderate'
    elif total_networks < 200:
        score = 70
        level = 'Good'
    else:
        score = min(100, 70 + total_networks // 10)
        level = 'Excellent'

    return jsonify({
        'success': True,
        'location': {'lat': lat, 'lng': lng, 'radius_deg': radius},
        'connectivity': {
            'score': score,
            'level': level,
            'facilities_nearby': len(facilities),
            'total_networks': total_networks,
            'total_ix_presence': total_ixs,
            'top_facilities': [
                {
                    'name': f.get('name', ''),
                    'city': f.get('city', ''),
                    'net_count': f.get('net_count', 0),
                    'ix_count': f.get('ix_count', 0)
                }
                for f in sorted(facilities, key=lambda x: x.get('net_count', 0), reverse=True)[:5]
            ]
        },
        'source': 'PeeringDB'
    })


def register_peeringdb_routes(app):
    """Register PeeringDB blueprint with Flask app"""
    app.register_blueprint(peeringdb_bp)
    logger.info("🔗 PeeringDB interconnection routes registered")
