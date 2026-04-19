"""
Infrastructure API Routes for DC Hub
=====================================
Serves infrastructure data discovered by the autonomous brain to the frontend.
This eliminates the need for frontend to hit Overpass API directly.

Endpoints:
  GET /api/v1/infrastructure/summary        - Overall counts and stats
  GET /api/v1/infrastructure/fiber          - Fiber routes (128+)
  GET /api/v1/infrastructure/substations    - Power substations (40+)
  GET /api/v1/infrastructure/permits        - Construction permits (17+)
  GET /api/v1/infrastructure/properties     - DC properties (9+)
  GET /api/v1/infrastructure/nearby         - All infrastructure near a point

Installation:
1. Save as infrastructure_api.py in your Replit project
2. In main.py, add:
   
   try:
       from infrastructure_api import register_infrastructure_api
       register_infrastructure_api(app)
       logger.info("✅ Infrastructure API registered")
   except ImportError as e:
       logger.warning(f"⚠️ Infrastructure API: {e}")
"""

from flask import Blueprint, jsonify, request
import logging
from datetime import datetime
import math

try:
    from api_tier_gating import require_plan as _infra_require_plan
except ImportError:
    def _infra_require_plan(plan='pro'):
        def decorator(f):
            return f
        return decorator

logger = logging.getLogger(__name__)


# ============================================
# NEON DATABASE HELPERS FOR FIBER ROUTES
# ============================================

def _get_fiber_routes_from_neon(carrier=None, route_type=None, limit=500):
    """Query fiber_routes table directly from Neon."""
    try:
        from db_utils import get_db
        conn = get_db()
        cur = conn.cursor()

        query = "SELECT id, name, provider, route_type, start_location, end_location, start_lat, start_lng, end_lat, end_lng, distance_miles, fiber_count, status, source_id FROM fiber_routes WHERE 1=1"
        params = []

        if carrier:
            query += " AND LOWER(provider) LIKE %s"
            params.append('%' + carrier.lower() + '%')
        if route_type:
            query += " AND route_type = %s"
            params.append(route_type)

        query += " ORDER BY provider, route_type LIMIT %s"
        params.append(limit)

        cur.execute(query, params)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()

        routes = []
        for row in rows:
            r = dict(zip(columns, row))
            # Map to legacy field names for backward compat with map UI
            r['carrier'] = r.get('provider', '')
            r['type'] = r.get('route_type', '')
            r['lat'] = r.get('start_lat', 0)
            r['lng'] = r.get('start_lng', 0)
            r['miles'] = r.get('distance_miles', 0)
            routes.append(r)

        return routes
    except Exception as e:
        logger.warning("Neon fiber query failed, falling back to in-memory: %s" % e)
        return None


def _get_fiber_count_from_neon():
    """Get total fiber route count from Neon."""
    try:
        from db_utils import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM fiber_routes")
        return cur.fetchone()[0]
    except Exception:
        return None

infra_bp = Blueprint('infrastructure_api', __name__, url_prefix='/api/v1/infrastructure')

# ============================================
# IN-MEMORY INFRASTRUCTURE STORAGE
# This would typically come from your database
# The autonomous brain populates this via sync
# ============================================

INFRASTRUCTURE_DATA = {
    'fiber_routes': [],
    'substations': [],
    'permits': [],
    'properties': [],
    'last_sync': None
}

# Sample data structure (autonomous brain would populate these)
SAMPLE_FIBER_ROUTES = [
    {'id': 'zayo-1', 'carrier': 'Zayo', 'type': 'longhaul', 'route': 'LA-Phoenix-Dallas', 'miles': 1200, 'lit': True, 'lat': 33.45, 'lng': -112.07},
    {'id': 'lumen-1', 'carrier': 'Lumen', 'type': 'longhaul', 'route': 'Denver-Phoenix', 'miles': 600, 'lit': True, 'lat': 33.52, 'lng': -111.90},
    {'id': 'crown-1', 'carrier': 'Crown Castle', 'type': 'metro', 'route': 'Phoenix Metro', 'miles': 450, 'lit': True, 'lat': 33.48, 'lng': -112.10},
    {'id': 'cogent-1', 'carrier': 'Cogent', 'type': 'longhaul', 'route': 'West Coast Backbone', 'miles': 2100, 'lit': True, 'lat': 34.05, 'lng': -118.25},
    {'id': 'att-1', 'carrier': 'AT&T', 'type': 'longhaul', 'route': 'National Backbone', 'miles': 15000, 'lit': True, 'lat': 32.78, 'lng': -96.80},
]

SAMPLE_SUBSTATIONS = [
    {'id': 'sub-1', 'name': 'Palo Verde Hub', 'voltage_kv': 500, 'capacity_mw': 3937, 'utility': 'APS', 'lat': 33.39, 'lng': -112.86, 'type': 'transmission'},
    {'id': 'sub-2', 'name': 'Pinnacle Peak', 'voltage_kv': 230, 'capacity_mw': 1200, 'utility': 'APS', 'lat': 33.71, 'lng': -111.86, 'type': 'distribution'},
    {'id': 'sub-3', 'name': 'Santan', 'voltage_kv': 500, 'capacity_mw': 2500, 'utility': 'SRP', 'lat': 33.31, 'lng': -111.72, 'type': 'transmission'},
    {'id': 'sub-4', 'name': 'Kyrene', 'voltage_kv': 230, 'capacity_mw': 800, 'utility': 'SRP', 'lat': 33.38, 'lng': -111.94, 'type': 'distribution'},
]

SAMPLE_PERMITS = [
    {'id': 'permit-1', 'project': 'Meta Mesa Campus', 'developer': 'Meta', 'mw': 300, 'status': 'approved', 'lat': 33.41, 'lng': -111.83, 'date': '2024-12-15', 'source': 'news'},
    {'id': 'permit-2', 'project': 'Microsoft Goodyear Phase 2', 'developer': 'Microsoft', 'mw': 200, 'status': 'under_review', 'lat': 33.44, 'lng': -112.39, 'date': '2025-01-10', 'source': 'news'},
    {'id': 'permit-3', 'project': 'QTS Phoenix Expansion', 'developer': 'QTS', 'mw': 150, 'status': 'approved', 'lat': 33.45, 'lng': -112.07, 'date': '2024-11-20', 'source': 'news'},
    {'id': 'permit-4', 'project': 'Google Mesa Data Center', 'developer': 'Google', 'mw': 400, 'status': 'construction', 'lat': 33.38, 'lng': -111.72, 'date': '2025-01-15', 'source': 'news'},
    {'id': 'permit-5', 'project': 'AWS West Phoenix', 'developer': 'Amazon', 'mw': 250, 'status': 'approved', 'lat': 33.52, 'lng': -112.25, 'date': '2024-11-01', 'source': 'news'},
    {'id': 'permit-6', 'project': 'Aligned Chandler Campus', 'developer': 'Aligned', 'mw': 180, 'status': 'approved', 'lat': 33.30, 'lng': -111.84, 'date': '2024-10-15', 'source': 'news'},
    {'id': 'permit-7', 'project': 'Digital Realty Phoenix', 'developer': 'Digital Realty', 'mw': 120, 'status': 'under_review', 'lat': 33.43, 'lng': -112.02, 'date': '2025-01-20', 'source': 'news'},
    {'id': 'permit-8', 'project': 'Vantage Phoenix Campus', 'developer': 'Vantage', 'mw': 220, 'status': 'construction', 'lat': 33.35, 'lng': -111.96, 'date': '2024-09-01', 'source': 'news'},
]

SAMPLE_PROPERTIES = [
    {'id': 'prop-1', 'name': 'Prime Industrial - Goodyear', 'acres': 120, 'zoning': 'industrial', 'power_available': True, 'fiber_available': True, 'lat': 33.46, 'lng': -112.36, 'price_per_acre': 350000},
    {'id': 'prop-2', 'name': 'Mesa Tech Park', 'acres': 85, 'zoning': 'tech_park', 'power_available': True, 'fiber_available': True, 'lat': 33.42, 'lng': -111.79, 'price_per_acre': 420000},
]

def init_sample_data():
    """Initialize with sample data if empty"""
    if not INFRASTRUCTURE_DATA['fiber_routes']:
        INFRASTRUCTURE_DATA['fiber_routes'] = SAMPLE_FIBER_ROUTES
    if not INFRASTRUCTURE_DATA['substations']:
        INFRASTRUCTURE_DATA['substations'] = SAMPLE_SUBSTATIONS
    if not INFRASTRUCTURE_DATA['permits']:
        INFRASTRUCTURE_DATA['permits'] = SAMPLE_PERMITS
    if not INFRASTRUCTURE_DATA['properties']:
        INFRASTRUCTURE_DATA['properties'] = SAMPLE_PROPERTIES
    INFRASTRUCTURE_DATA['last_sync'] = datetime.now().isoformat()

# Initialize on import
init_sample_data()


def haversine_distance(lat1, lng1, lat2, lng2):
    """Calculate distance between two points in miles"""
    R = 3959  # Earth's radius in miles
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng/2)**2
    c = 2 * math.asin(math.sqrt(a))
    return R * c


def filter_by_bounds(items, bounds):
    """Filter items by geographic bounds"""
    if not bounds:
        return items
    
    min_lat = bounds.get('min_lat', -90)
    max_lat = bounds.get('max_lat', 90)
    min_lng = bounds.get('min_lng', -180)
    max_lng = bounds.get('max_lng', 180)
    
    return [
        item for item in items
        if min_lat <= item.get('lat', 0) <= max_lat
        and min_lng <= item.get('lng', 0) <= max_lng
    ]


def filter_by_radius(items, lat, lng, radius_miles):
    """Filter items within radius of a point"""
    result = []
    for item in items:
        item_lat = item.get('lat')
        item_lng = item.get('lng')
        if item_lat and item_lng:
            distance = haversine_distance(lat, lng, item_lat, item_lng)
            if distance <= radius_miles:
                item_with_distance = item.copy()
                item_with_distance['distance_miles'] = round(distance, 2)
                result.append(item_with_distance)
    
    return sorted(result, key=lambda x: x.get('distance_miles', 0))


# ============================================
# API ENDPOINTS
# ============================================

@infra_bp.route('/summary', methods=['GET'])
def get_infrastructure_summary():
    """Get summary counts of all infrastructure"""
    neon_fiber = _get_fiber_count_from_neon()
    fiber_count = neon_fiber if neon_fiber is not None else len(INFRASTRUCTURE_DATA['fiber_routes'])
    return jsonify({
        'success': True,
        'data': {
            'fiber_routes': fiber_count,
            'substations': len(INFRASTRUCTURE_DATA['substations']),
            'permits': len(INFRASTRUCTURE_DATA['permits']),
            'properties': len(INFRASTRUCTURE_DATA['properties']),
            'total': (
                fiber_count +
                len(INFRASTRUCTURE_DATA['substations']) +
                len(INFRASTRUCTURE_DATA['permits']) +
                len(INFRASTRUCTURE_DATA['properties'])
            ),
            'last_sync': INFRASTRUCTURE_DATA['last_sync'],
            'fiber_source': 'neon' if neon_fiber is not None else 'in_memory'
        }
    })


@infra_bp.route('/fiber', methods=['GET'])
def get_fiber_routes():
    """
    Get fiber routes — queries Neon fiber_routes table (1,000+ routes).
    Query params:
      - carrier: Filter by carrier/provider name
      - type: Filter by type (long_haul, metro, dc_interconnect, enterprise_lateral, etc.)
      - min_lat, max_lat, min_lng, max_lng: Bounding box
    """
    carrier = request.args.get('carrier')
    route_type = request.args.get('type')

    # Try Neon first
    neon_routes = _get_fiber_routes_from_neon(carrier=carrier, route_type=route_type)

    if neon_routes is not None:
        routes = neon_routes
    else:
        # Fallback to in-memory
        routes = INFRASTRUCTURE_DATA['fiber_routes']
        if carrier:
            routes = [r for r in routes if carrier.lower() in r.get('carrier', '').lower()]
        if route_type:
            routes = [r for r in routes if r.get('type') == route_type]

    # Filter by bounds
    if any(request.args.get(k) for k in ['min_lat', 'max_lat', 'min_lng', 'max_lng']):
        bounds = {
            'min_lat': float(request.args.get('min_lat', -90)),
            'max_lat': float(request.args.get('max_lat', 90)),
            'min_lng': float(request.args.get('min_lng', -180)),
            'max_lng': float(request.args.get('max_lng', 180))
        }
        routes = filter_by_bounds(routes, bounds)
    
    return jsonify({
        'success': True,
        'count': len(routes),
        'data': routes
    })


@infra_bp.route('/substations', methods=['GET'])
@_infra_require_plan('pro')
def get_substations():
    """
    Get power substations
    Query params:
      - min_voltage: Minimum voltage in kV
      - utility: Filter by utility name
      - min_lat, max_lat, min_lng, max_lng: Bounding box
    """
    subs = INFRASTRUCTURE_DATA['substations']
    
    # Filter by voltage
    min_voltage = request.args.get('min_voltage', type=int)
    if min_voltage:
        subs = [s for s in subs if s.get('voltage_kv', 0) >= min_voltage]
    
    # Filter by utility
    utility = request.args.get('utility')
    if utility:
        subs = [s for s in subs if utility.lower() in s.get('utility', '').lower()]
    
    # Filter by bounds
    if any(request.args.get(k) for k in ['min_lat', 'max_lat', 'min_lng', 'max_lng']):
        bounds = {
            'min_lat': float(request.args.get('min_lat', -90)),
            'max_lat': float(request.args.get('max_lat', 90)),
            'min_lng': float(request.args.get('min_lng', -180)),
            'max_lng': float(request.args.get('max_lng', 180))
        }
        subs = filter_by_bounds(subs, bounds)
    
    return jsonify({
        'success': True,
        'count': len(subs),
        'data': subs
    })


@infra_bp.route('/permits', methods=['GET'])
def get_permits():
    """
    Get construction permits
    Query params:
      - status: approved, under_review, pending
      - min_mw: Minimum MW capacity
      - developer: Filter by developer name
    """
    permits = INFRASTRUCTURE_DATA['permits']
    
    # Filter by status
    status = request.args.get('status')
    if status:
        permits = [p for p in permits if p.get('status') == status]
    
    # Filter by MW
    min_mw = request.args.get('min_mw', type=int)
    if min_mw:
        permits = [p for p in permits if p.get('mw', 0) >= min_mw]
    
    # Filter by developer
    developer = request.args.get('developer')
    if developer:
        permits = [p for p in permits if developer.lower() in p.get('developer', '').lower()]
    
    return jsonify({
        'success': True,
        'count': len(permits),
        'data': permits
    })


@infra_bp.route('/properties', methods=['GET'])
def get_properties():
    """
    Get DC properties/land for sale
    Query params:
      - min_acres: Minimum acreage
      - power_available: true/false
      - fiber_available: true/false
    """
    props = INFRASTRUCTURE_DATA['properties']
    
    # Filter by acreage
    min_acres = request.args.get('min_acres', type=int)
    if min_acres:
        props = [p for p in props if p.get('acres', 0) >= min_acres]
    
    # Filter by power
    if request.args.get('power_available') == 'true':
        props = [p for p in props if p.get('power_available')]
    
    # Filter by fiber
    if request.args.get('fiber_available') == 'true':
        props = [p for p in props if p.get('fiber_available')]
    
    return jsonify({
        'success': True,
        'count': len(props),
        'data': props
    })


@infra_bp.route('/nearby', methods=['GET'])
def get_nearby_infrastructure():
    """
    Get all infrastructure near a point
    Query params (required):
      - lat: Latitude
      - lng: Longitude
      - radius: Radius in miles (default 25)
    """
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    radius = request.args.get('radius', default=25, type=float)
    
    if lat is None or lng is None:
        return jsonify({
            'success': False,
            'error': 'lat and lng parameters required'
        }), 400
    
    nearby = {
        'fiber_routes': [],
        'substations': filter_by_radius(INFRASTRUCTURE_DATA['substations'], lat, lng, radius),
        'permits': filter_by_radius(INFRASTRUCTURE_DATA['permits'], lat, lng, radius),
        'properties': filter_by_radius(INFRASTRUCTURE_DATA['properties'], lat, lng, radius)
    }

    # Fiber from Neon with bounding box pre-filter
    neon_routes = _get_fiber_routes_from_neon()
    if neon_routes is not None:
        nearby['fiber_routes'] = filter_by_radius(neon_routes, lat, lng, radius)
    else:
        nearby['fiber_routes'] = filter_by_radius(INFRASTRUCTURE_DATA['fiber_routes'], lat, lng, radius)
    
    total = sum(len(v) for v in nearby.values())
    
    return jsonify({
        'success': True,
        'query': {
            'lat': lat,
            'lng': lng,
            'radius_miles': radius
        },
        'total_count': total,
        'data': nearby
    })


@infra_bp.route('/sync', methods=['POST'])
def sync_infrastructure():
    """
    Endpoint for autonomous brain to push discovered infrastructure
    Expects JSON body with fiber_routes, substations, permits, properties arrays
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
        
        # Update each category if provided
        if 'fiber_routes' in data:
            INFRASTRUCTURE_DATA['fiber_routes'] = data['fiber_routes']
            logger.info(f"📡 Synced {len(data['fiber_routes'])} fiber routes")
        
        if 'substations' in data:
            INFRASTRUCTURE_DATA['substations'] = data['substations']
            logger.info(f"⚡ Synced {len(data['substations'])} substations")
        
        if 'permits' in data:
            INFRASTRUCTURE_DATA['permits'] = data['permits']
            logger.info(f"📋 Synced {len(data['permits'])} permits")
        
        if 'properties' in data:
            INFRASTRUCTURE_DATA['properties'] = data['properties']
            logger.info(f"🏠 Synced {len(data['properties'])} properties")
        
        INFRASTRUCTURE_DATA['last_sync'] = datetime.now().isoformat()
        
        return jsonify({
            'success': True,
            'message': 'Infrastructure data synced',
            'counts': {
                'fiber_routes': len(INFRASTRUCTURE_DATA['fiber_routes']),
                'substations': len(INFRASTRUCTURE_DATA['substations']),
                'permits': len(INFRASTRUCTURE_DATA['permits']),
                'properties': len(INFRASTRUCTURE_DATA['properties'])
            }
        })
        
    except Exception as e:
        logger.error(f"Sync error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def register_infrastructure_api(app):
    """Register infrastructure API routes with Flask app"""
    app.register_blueprint(infra_bp)
    logger.info("✅ Infrastructure API routes registered")
    logger.info("   GET /api/v1/infrastructure/summary")
    logger.info("   GET /api/v1/infrastructure/fiber")
    logger.info("   GET /api/v1/infrastructure/substations")
    logger.info("   GET /api/v1/infrastructure/permits")
    logger.info("   GET /api/v1/infrastructure/properties")
    logger.info("   GET /api/v1/infrastructure/nearby?lat=&lng=&radius=")
    logger.info("   POST /api/v1/infrastructure/sync")
