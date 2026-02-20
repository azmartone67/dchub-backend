"""
Water & Drought Routes for DC Hub Backend
==========================================
Proxies USDM (US Drought Monitor) API calls to avoid CORS issues

Endpoints:
  GET /api/water/drought/state/<state>     - State drought data
  GET /api/water/drought/compare           - Multi-state comparison  
  GET /api/water/drought/point             - Point-based query

Installation:
1. Save this file as water_drought_routes.py in your Replit project
2. In main.py, add at the top with other imports:
   
   try:
       from water_drought_routes import register_water_routes
       logger.info("  ✅ water_drought_routes")
   except ImportError as e:
       register_water_routes = None
       logger.warning(f"  ⚠️ water_drought_routes: {e}")

3. At the bottom of main.py (around line 8280), the registration should work:
   
   if register_water_routes:
       register_water_routes(app)
       logger.info("✅ Water & Drought API registered")
"""

from flask import Blueprint, jsonify, request
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Try to import requests, handle if not available
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    logger.warning("⚠️ requests library not available - using fallback data only")

water_bp = Blueprint('water', __name__, url_prefix='/api/water')

# Cache for API responses
_drought_cache = {}
CACHE_TTL = 3600  # 1 hour

def get_cached(key):
    """Get cached data if still valid"""
    if key in _drought_cache:
        cached = _drought_cache[key]
        if datetime.now().timestamp() - cached['timestamp'] < CACHE_TTL:
            return cached['data']
    return None

def set_cached(key, data):
    """Cache data with timestamp"""
    _drought_cache[key] = {
        'data': data,
        'timestamp': datetime.now().timestamp()
    }

# Hardcoded fallback data (updated periodically)
FALLBACK_STATE_DATA = {
    'AZ': {'none_pct': 15, 'd0_pct': 25, 'd1_pct': 30, 'd2_pct': 20, 'd3_pct': 8, 'd4_pct': 2},
    'TX': {'none_pct': 35, 'd0_pct': 25, 'd1_pct': 20, 'd2_pct': 12, 'd3_pct': 6, 'd4_pct': 2},
    'VA': {'none_pct': 85, 'd0_pct': 10, 'd1_pct': 5, 'd2_pct': 0, 'd3_pct': 0, 'd4_pct': 0},
    'OH': {'none_pct': 90, 'd0_pct': 8, 'd1_pct': 2, 'd2_pct': 0, 'd3_pct': 0, 'd4_pct': 0},
    'GA': {'none_pct': 75, 'd0_pct': 15, 'd1_pct': 8, 'd2_pct': 2, 'd3_pct': 0, 'd4_pct': 0},
    'NV': {'none_pct': 10, 'd0_pct': 20, 'd1_pct': 30, 'd2_pct': 25, 'd3_pct': 10, 'd4_pct': 5},
    'CA': {'none_pct': 40, 'd0_pct': 20, 'd1_pct': 15, 'd2_pct': 15, 'd3_pct': 7, 'd4_pct': 3},
    'WA': {'none_pct': 70, 'd0_pct': 15, 'd1_pct': 10, 'd2_pct': 5, 'd3_pct': 0, 'd4_pct': 0},
    'OR': {'none_pct': 60, 'd0_pct': 20, 'd1_pct': 12, 'd2_pct': 6, 'd3_pct': 2, 'd4_pct': 0},
    'CO': {'none_pct': 30, 'd0_pct': 25, 'd1_pct': 25, 'd2_pct': 15, 'd3_pct': 4, 'd4_pct': 1},
    'IL': {'none_pct': 85, 'd0_pct': 10, 'd1_pct': 4, 'd2_pct': 1, 'd3_pct': 0, 'd4_pct': 0},
    'NJ': {'none_pct': 90, 'd0_pct': 7, 'd1_pct': 3, 'd2_pct': 0, 'd3_pct': 0, 'd4_pct': 0},
    'NC': {'none_pct': 80, 'd0_pct': 12, 'd1_pct': 6, 'd2_pct': 2, 'd3_pct': 0, 'd4_pct': 0},
    'IA': {'none_pct': 80, 'd0_pct': 12, 'd1_pct': 6, 'd2_pct': 2, 'd3_pct': 0, 'd4_pct': 0},
    'UT': {'none_pct': 20, 'd0_pct': 25, 'd1_pct': 30, 'd2_pct': 18, 'd3_pct': 5, 'd4_pct': 2},
}


@water_bp.route('/drought/state/<state>', methods=['GET'])
def get_state_drought(state):
    """
    Get drought data for a specific state
    Proxies USDM API to avoid CORS issues
    """
    state = state.upper()
    
    # Check cache first
    cache_key = f'drought_state_{state}'
    cached = get_cached(cache_key)
    if cached:
        return jsonify({'success': True, 'data': cached, 'source': 'cache'})
    
    # Try live USDM API if requests library is available
    if REQUESTS_AVAILABLE:
        try:
            today = datetime.now()
            date_str = today.strftime('%Y%m%d')
            
            url = 'https://usdmdataservices.unl.edu/api/StateStatistics/GetDroughtSeverityStatisticsByAreaPercent'
            params = {
                'aoi': state,
                'startdate': date_str,
                'enddate': date_str,
                'statisticsType': '1'
            }
            
            response = requests.get(url, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if data and len(data) > 0:
                    latest = data[0]
                    state_data = {
                        'state': state,
                        'none_pct': 100 - (latest.get('D0', 0) or 0),
                        'd0_pct': latest.get('D0', 0) or 0,
                        'd1_pct': latest.get('D1', 0) or 0,
                        'd2_pct': latest.get('D2', 0) or 0,
                        'd3_pct': latest.get('D3', 0) or 0,
                        'd4_pct': latest.get('D4', 0) or 0,
                        'date': latest.get('MapDate', today.isoformat()),
                        'source': 'usdm_live'
                    }
                    
                    set_cached(cache_key, state_data)
                    logger.info(f"✅ Fetched live drought data for {state}")
                    return jsonify({'success': True, 'data': state_data})
            
            logger.warning(f"USDM API returned {response.status_code} for {state}")
            
        except requests.exceptions.Timeout:
            logger.warning(f"USDM API timeout for {state}")
        except requests.exceptions.RequestException as e:
            logger.error(f"USDM API request error for {state}: {e}")
        except Exception as e:
            logger.error(f"USDM API error for {state}: {e}")
    
    # Fallback to hardcoded data
    fallback = FALLBACK_STATE_DATA.get(state, {
        'none_pct': 70, 'd0_pct': 15, 'd1_pct': 10, 'd2_pct': 4, 'd3_pct': 1, 'd4_pct': 0
    }).copy()
    fallback['state'] = state
    fallback['date'] = datetime.now().isoformat()
    fallback['source'] = 'fallback'
    
    set_cached(cache_key, fallback)
    logger.info(f"📊 Using fallback drought data for {state}")
    return jsonify({'success': True, 'data': fallback})


@water_bp.route('/drought/compare', methods=['GET'])
def compare_states_drought():
    """
    Compare drought data across multiple states
    Query: ?states=AZ,TX,VA,OH
    """
    states_str = request.args.get('states', 'AZ,TX,VA,OH,NV,GA')
    states = [s.strip().upper() for s in states_str.split(',')]
    
    results = []
    for state in states:
        # Get state data (uses cache if available)
        cache_key = f'drought_state_{state}'
        cached = get_cached(cache_key)
        
        if cached:
            state_data = cached
        else:
            # Quick fallback for comparison (don't wait for API)
            state_data = FALLBACK_STATE_DATA.get(state, {
                'none_pct': 70, 'd0_pct': 15, 'd1_pct': 10, 'd2_pct': 4, 'd3_pct': 1, 'd4_pct': 0
            })
            state_data['state'] = state
            state_data['source'] = 'fallback'
        
        # Calculate water risk score
        score = 100
        score -= (state_data.get('d2_pct', 0) + state_data.get('d3_pct', 0) + state_data.get('d4_pct', 0)) * 0.5
        score -= state_data.get('d1_pct', 0) * 0.2
        score -= state_data.get('d0_pct', 0) * 0.1
        score = max(0, min(100, score))
        
        results.append({
            'state': state,
            'water_risk_score': round(score),
            'drought_coverage': state_data,
            'severe_drought_pct': (state_data.get('d2_pct', 0) + state_data.get('d3_pct', 0) + state_data.get('d4_pct', 0))
        })
    
    # Sort by water risk score (highest = best)
    results.sort(key=lambda x: x['water_risk_score'], reverse=True)
    
    return jsonify({
        'success': True,
        'comparison': results,
        'timestamp': datetime.now().isoformat()
    })


@water_bp.route('/drought/point', methods=['GET'])
def get_point_drought():
    """
    Get drought data for a specific lat/lng point
    Query: ?lat=33.45&lng=-112.07
    """
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    
    if not lat or not lng:
        return jsonify({'success': False, 'error': 'lat and lng required'}), 400
    
    try:
        # Query ArcGIS point service
        url = 'https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_Drought_Intensity/FeatureServer/0/query'
        params = {
            'geometry': f'{lng},{lat}',
            'geometryType': 'esriGeometryPoint',
            'inSR': '4326',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': '*',
            'returnGeometry': 'false',
            'f': 'json'
        }
        
        response = requests.get(url, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get('features') and len(data['features']) > 0:
                attrs = data['features'][0].get('attributes', {})
                dm = attrs.get('DM', attrs.get('dm', -1))
                level = f'D{dm}' if dm >= 0 and dm <= 4 else 'None'
                
                return jsonify({
                    'success': True,
                    'data': {
                        'lat': lat,
                        'lng': lng,
                        'drought_level': level,
                        'raw': attrs,
                        'source': 'arcgis'
                    }
                })
        
        # No data found
        return jsonify({
            'success': True,
            'data': {
                'lat': lat,
                'lng': lng,
                'drought_level': 'None',
                'source': 'default'
            }
        })
        
    except Exception as e:
        logger.error(f"Point drought query error: {e}")
        return jsonify({
            'success': True,
            'data': {
                'lat': lat,
                'lng': lng,
                'drought_level': 'Unknown',
                'source': 'error'
            }
        })


def register_water_routes(app):
    """Register water/drought routes with Flask app"""
    app.register_blueprint(water_bp)
    logger.info("✅ Water & Drought API routes registered")
    logger.info("   GET /api/water/drought/state/<state>")
    logger.info("   GET /api/water/drought/compare?states=AZ,TX,VA")
    logger.info("   GET /api/water/drought/point?lat=33.45&lng=-112.07")
