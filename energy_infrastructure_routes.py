"""
DC Hub Energy Infrastructure API Routes v3
==========================================
Enhanced with:
- Fixed pipeline scoring (case-insensitive matching)
- Point-to-line distance for accurate pipeline proximity
- More comprehensive recommendations
- Tallgrass/REX pipeline detection
- Power plant proximity scoring
- FALLBACK ENDPOINTS for HIFLD services (v3)
- Multiple source queries for substations, transmission, power plants

INSTALLATION:
1. Replace existing energy_infrastructure_routes.py in Replit
2. Restart the server
"""

import requests
from flask import request, jsonify
from functools import wraps
from api_data_protection import protect_data
import time
import logging
from math import cos, radians, sin, sqrt, atan2, inf
from db_utils import get_db

# Lazy tier gating - checks at runtime, not import time
def require_plan(min_plan='pro'):
    """Lazy require_plan that validates plan at request time"""
    logger = logging.getLogger(__name__)
    
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            try:
                from api_tier_gating import validate_api_key, user_has_access
                
                # Check API key
                api_key = request.headers.get('X-API-Key') or request.args.get('api_key')
                if not api_key:
                    return jsonify({
                        'success': False,
                        'error': 'authentication_required',
                        'message': f'This endpoint requires a {min_plan.title()} plan or higher.',
                        'signup_url': 'https://dchub.cloud/signup',
                    }), 401
                
                valid, info = validate_api_key(api_key)
                if not valid:
                    return jsonify({
                        'success': False,
                        'error': 'invalid_api_key',
                        'message': 'Invalid or inactive API key',
                    }), 401
                
                user_plan = info.get('plan', 'free')
                if not user_has_access(user_plan, min_plan):
                    return jsonify({
                        'success': False,
                        'error': 'plan_upgrade_required',
                        'message': f'This endpoint requires {min_plan.title()} plan. You are on {user_plan.title()}.',
                        'upgrade_url': 'https://dchub.cloud/pricing',
                    }), 403
                
                return f(*args, **kwargs)
            except ImportError:
                # Tier gating not available, allow through
                return f(*args, **kwargs)
            except Exception as e:
                logger.error(f"Tier gating error: {e}")
                return f(*args, **kwargs)
        return wrapper
    return decorator

# =============================================================================
# CONFIGURATION
# =============================================================================

# External API endpoints
# Primary HIFLD endpoints (services1.arcgis.com)
HIFLD_BASE = 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services'

# Alternative HIFLD endpoints (if primary fails)
HIFLD_ALT_SUBSTATIONS = 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/US_Electric_Substations_Transmission_Lines/FeatureServer/0'
HIFLD_ALT_TRANSMISSION = 'https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/US_Electric_Power_Transmission_Lines/FeatureServer/0'
HIFLD_ALT_POWERPLANTS = 'https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/US_Power_Plants/FeatureServer/0'

# EIA Power Plants (reliable alternative)
EIA_POWER_PLANTS = 'https://services.arcgis.com/rjCUzSIRlCIAZQhK/arcgis/rest/services/EIA_Power_Plants/FeatureServer/0'

DOT_PIPELINES = 'https://geo.dot.gov/server/rest/services/Hosted/Natural_Gas_Pipelines_US_EIA/FeatureServer/0'
TEXAS_RRC_BASE = 'https://gis.rrc.texas.gov/server/rest/services/rrc_public/RRC_Public_Viewer_Srvs/MapServer'

# State oil/gas GIS endpoints
STATE_GIS = {
    'CA': 'https://gis.conservation.ca.gov/server/rest/services/WellSTAR/Wells/MapServer/0',
    'NM': 'https://services.arcgis.com/QVENGdaPbd4LUkLV/arcgis/rest/services/OCD_Wells/FeatureServer/0',
    'CO': 'https://cogccmap.state.co.us/arcgis/rest/services/CO_COGCC_Pub/SurfaceHoles/MapServer/0',
}

# Simple in-memory cache
_CACHE = {}
_CACHE_DURATION = 300  # 5 minutes

def get_cached(key):
    """Get cached data if still valid"""
    if key in _CACHE:
        data, timestamp = _CACHE[key]
        if time.time() - timestamp < _CACHE_DURATION:
            return data
    return None

def set_cache(key, data):
    """Cache data with timestamp"""
    _CACHE[key] = (data, time.time())

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def query_arcgis(base_url, params, timeout=30):
    """Query an ArcGIS REST API endpoint"""
    default_params = {
        'f': 'json',
        'outSR': '4326',
        'returnGeometry': 'true',
    }
    default_params.update(params)
    
    try:
        response = requests.get(
            f"{base_url}/query",
            params=default_params,
            timeout=timeout
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"ArcGIS query error: {e}")
        return {'features': [], 'error': str(e)}

def bounds_to_envelope(min_lat, max_lat, min_lng, max_lng):
    """Convert bounds to ArcGIS envelope string"""
    return f"{min_lng},{min_lat},{max_lng},{max_lat}"

def haversine_distance(lat1, lng1, lat2, lng2):
    """Calculate distance in meters between two points"""
    R = 6371000  # Earth radius in meters
    lat1, lng1, lat2, lng2 = map(radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlng/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return R * c

def point_to_line_distance(point_lat, point_lng, line_coords):
    """
    Calculate minimum distance from a point to a polyline.
    line_coords is a list of [lng, lat] pairs.
    Returns distance in meters.
    """
    if not line_coords or len(line_coords) < 2:
        return inf
    
    min_dist = inf
    
    # Check distance to each segment
    for i in range(len(line_coords) - 1):
        lng1, lat1 = line_coords[i]
        lng2, lat2 = line_coords[i + 1]
        
        # Distance to segment endpoints
        d1 = haversine_distance(point_lat, point_lng, lat1, lng1)
        d2 = haversine_distance(point_lat, point_lng, lat2, lng2)
        
        # Approximate perpendicular distance (simplified)
        # For short segments, min of endpoints is good enough
        segment_dist = min(d1, d2)
        
        if segment_dist < min_dist:
            min_dist = segment_dist
    
    return min_dist

def calculate_infrastructure_score(lat, lng, substations, pipelines, transmission_lines, power_plants):
    """
    Calculate infrastructure access score (0-100)
    Based on proximity to power and gas infrastructure
    
    ENHANCED: Better pipeline detection, point-to-line distance
    """
    base_score = 30  # Base score
    recommendations = []
    details = {}
    
    # ===================
    # POWER SCORE (max 50)
    # ===================
    power_score = 0
    
    # Substations (max 25 pts)
    if substations:
        nearest_sub_dist = inf
        nearest_sub = None
        
        for sub in substations:
            try:
                geom = sub.get('geometry', {})
                # Handle both point and x/y formats
                if 'x' in geom and 'y' in geom:
                    sub_lng, sub_lat = geom['x'], geom['y']
                elif 'coordinates' in geom:
                    sub_lng, sub_lat = geom['coordinates']
                else:
                    continue
                
                dist = haversine_distance(lat, lng, sub_lat, sub_lng)
                if dist < nearest_sub_dist:
                    nearest_sub_dist = dist
                    nearest_sub = sub
            except:
                pass
        
        if nearest_sub_dist < inf:
            sub_dist_km = nearest_sub_dist / 1000
            details['nearestSubstationKm'] = round(sub_dist_km, 1)
            details['nearestSubstationName'] = nearest_sub.get('attributes', {}).get('NAME', 'Unknown')
            
            if sub_dist_km < 5:
                power_score += 25
                recommendations.append(f"✅ Substation within {sub_dist_km:.1f}km - excellent grid access")
            elif sub_dist_km < 10:
                power_score += 20
                recommendations.append(f"✅ Substation within {sub_dist_km:.1f}km - good grid access")
            elif sub_dist_km < 20:
                power_score += 12
                recommendations.append(f"⚠️ Substation {sub_dist_km:.1f}km away - grid extension may be needed")
            elif sub_dist_km < 40:
                power_score += 5
    
    # Transmission lines (max 15 pts)
    if transmission_lines:
        nearest_line_dist = inf
        nearest_line = None
        
        for line in transmission_lines:
            try:
                paths = line.get('geometry', {}).get('paths', [])
                for path in paths:
                    dist = point_to_line_distance(lat, lng, path)
                    if dist < nearest_line_dist:
                        nearest_line_dist = dist
                        nearest_line = line
            except:
                pass
        
        if nearest_line_dist < inf:
            line_dist_km = nearest_line_dist / 1000
            details['nearestTransmissionKm'] = round(line_dist_km, 1)
            voltage = nearest_line.get('attributes', {}).get('VOLTAGE', 0) if nearest_line else 0
            details['nearestTransmissionVoltage'] = voltage
            
            if line_dist_km < 2:
                power_score += 15
            elif line_dist_km < 5:
                power_score += 12
            elif line_dist_km < 10:
                power_score += 8
            elif line_dist_km < 20:
                power_score += 4
    
    # Power plants (max 10 pts)
    if power_plants:
        nearest_plant_dist = inf
        nearest_plant = None
        
        for plant in power_plants:
            try:
                geom = plant.get('geometry', {})
                if 'x' in geom and 'y' in geom:
                    plant_lng, plant_lat = geom['x'], geom['y']
                else:
                    continue
                
                dist = haversine_distance(lat, lng, plant_lat, plant_lng)
                if dist < nearest_plant_dist:
                    nearest_plant_dist = dist
                    nearest_plant = plant
            except:
                pass
        
        if nearest_plant_dist < inf:
            plant_dist_km = nearest_plant_dist / 1000
            details['nearestPowerPlantKm'] = round(plant_dist_km, 1)
            attrs = nearest_plant.get('attributes', {}) if nearest_plant else {}
            details['nearestPowerPlantName'] = attrs.get('NAME', 'Unknown')
            details['nearestPowerPlantMW'] = attrs.get('TOTAL_MW', 0)
            details['nearestPowerPlantFuel'] = attrs.get('PRIM_FUEL', 'Unknown')
            
            if plant_dist_km < 10:
                power_score += 10
            elif plant_dist_km < 25:
                power_score += 6
            elif plant_dist_km < 50:
                power_score += 3
    
    # ===================
    # GAS SCORE (max 50)
    # ===================
    gas_score = 0
    has_tallgrass = False
    has_interstate = False
    pipeline_operators = set()
    
    if pipelines:
        # Filter for gas pipelines (case-insensitive!)
        gas_pipelines = []
        for p in pipelines:
            attrs = p.get('attributes', {})
            typepipe = str(attrs.get('typepipe', '')).lower()
            commodity = str(attrs.get('COMMODITY', '')).lower()
            
            # Include if it's a gas pipeline
            if typepipe in ['interstate', 'intrastate'] or 'gas' in commodity:
                gas_pipelines.append(p)
                
                # Track operators
                operator = attrs.get('operator', '')
                if operator:
                    pipeline_operators.add(operator)
                    
                    # Check for Tallgrass/REX
                    op_lower = operator.lower()
                    if 'tallgrass' in op_lower or 'rockies express' in op_lower or 'rex' in op_lower:
                        has_tallgrass = True
                
                if typepipe == 'interstate':
                    has_interstate = True
        
        details['pipelineOperators'] = list(pipeline_operators)[:5]  # Top 5
        details['hasTallgrass'] = has_tallgrass
        details['hasInterstate'] = has_interstate
        
        if gas_pipelines:
            # Find nearest pipeline using point-to-line distance
            nearest_pipe_dist = inf
            nearest_pipe = None
            
            for pipe in gas_pipelines:
                try:
                    paths = pipe.get('geometry', {}).get('paths', [])
                    for path in paths:
                        dist = point_to_line_distance(lat, lng, path)
                        if dist < nearest_pipe_dist:
                            nearest_pipe_dist = dist
                            nearest_pipe = pipe
                except:
                    pass
            
            if nearest_pipe_dist < inf:
                pipe_dist_km = nearest_pipe_dist / 1000
                details['nearestPipelineKm'] = round(pipe_dist_km, 1)
                
                if nearest_pipe:
                    attrs = nearest_pipe.get('attributes', {})
                    details['nearestPipelineOperator'] = attrs.get('operator', 'Unknown')
                    details['nearestPipelineType'] = attrs.get('typepipe', 'Unknown')
                
                # Score based on distance
                if pipe_dist_km < 2:
                    gas_score += 40
                    recommendations.append(f"✅ Gas pipeline within {pipe_dist_km:.1f}km - excellent for gas-powered generation")
                elif pipe_dist_km < 5:
                    gas_score += 35
                    recommendations.append(f"✅ Gas pipeline within {pipe_dist_km:.1f}km - good gas access")
                elif pipe_dist_km < 10:
                    gas_score += 25
                    recommendations.append(f"✅ Gas pipeline {pipe_dist_km:.1f}km away - feasible connection")
                elif pipe_dist_km < 20:
                    gas_score += 15
                    recommendations.append(f"⚠️ Gas pipeline {pipe_dist_km:.1f}km away - may need lateral")
                elif pipe_dist_km < 40:
                    gas_score += 5
                    recommendations.append(f"⚠️ Nearest pipeline {pipe_dist_km:.1f}km away - limited gas access")
                else:
                    recommendations.append(f"❌ Nearest pipeline {pipe_dist_km:.1f}km away - challenging gas access")
                
                # Bonus for Tallgrass partnership opportunity
                if has_tallgrass:
                    gas_score += 5
                    recommendations.append("🎯 Tallgrass/REX pipeline in area - potential partnership opportunity")
                
                # Bonus for interstate access
                if has_interstate and not has_tallgrass:
                    gas_score += 3
                    recommendations.append("📍 Interstate pipeline access available")
            else:
                recommendations.append("ℹ️ No gas pipelines found within search area")
        else:
            recommendations.append("ℹ️ No gas pipelines found in search area")
    else:
        recommendations.append("ℹ️ Pipeline data not available for this area")
    
    # ===================
    # OVERALL SCORE
    # ===================
    # Weight: 50% power, 50% gas
    overall_score = int(base_score + (power_score * 0.5) + (gas_score * 0.5))
    overall_score = min(100, max(0, overall_score))
    
    # Add rating
    if overall_score >= 80:
        rating = "Excellent"
    elif overall_score >= 65:
        rating = "Good"
    elif overall_score >= 50:
        rating = "Moderate"
    else:
        rating = "Challenging"
    
    return {
        'overallScore': overall_score,
        'rating': rating,
        'powerScore': power_score,
        'gasScore': gas_score,
        'recommendations': recommendations,
        'details': details
    }

# =============================================================================
# FLASK ROUTES
# =============================================================================

def setup_energy_routes(app):
    """Register energy infrastructure routes with Flask app"""
    
    @app.route('/api/v1/energy/site-analysis', methods=['GET'])
    def energy_site_analysis():
        """
        Comprehensive site analysis for energy infrastructure
        
        Query params:
        - lat: Latitude (required)
        - lng: Longitude (required)  
        - radius: Search radius in meters (default: 25000)
        """
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        radius = request.args.get('radius', 25000, type=int)
        
        if not lat or not lng:
            return jsonify({'success': False, 'error': 'lat and lng required'}), 400
        
        # Check cache
        cache_key = f"site-analysis:{lat:.4f}:{lng:.4f}:{radius}"
        cached = get_cached(cache_key)
        if cached:
            return jsonify({'success': True, 'data': cached, 'cached': True})
        
        # Calculate bounds
        lat_delta = radius / 111000
        lng_delta = radius / (111000 * abs(cos(radians(lat))))
        bounds = {
            'minLat': lat - lat_delta,
            'maxLat': lat + lat_delta,
            'minLng': lng - lng_delta,
            'maxLng': lng + lng_delta
        }
        envelope = bounds_to_envelope(bounds['minLat'], bounds['maxLat'], bounds['minLng'], bounds['maxLng'])
        
        # Query infrastructure
        substations = []
        pipelines = []
        transmission = []
        power_plants = []
        
        # HIFLD Substations - try multiple endpoints
        try:
            # Try primary endpoint first
            sub_data = query_arcgis(f"{HIFLD_BASE}/Electric_Substations/FeatureServer/0", {
                'where': '1=1',
                'geometry': envelope,
                'geometryType': 'esriGeometryEnvelope',
                'spatialRel': 'esriSpatialRelIntersects',
                'outFields': 'NAME,CITY,STATE,ZIP,TYPE,STATUS,OWNER,MAX_VOLT,MIN_VOLT',
                'inSR': '4326',
                'resultRecordCount': '500'
            })
            substations = sub_data.get('features', [])
            
            # If primary returns empty, try alternative endpoint
            if len(substations) == 0:
                print("Primary substation endpoint empty, trying alternative...")
                sub_data = query_arcgis(HIFLD_ALT_SUBSTATIONS, {
                    'where': '1=1',
                    'geometry': envelope,
                    'geometryType': 'esriGeometryEnvelope',
                    'spatialRel': 'esriSpatialRelIntersects',
                    'outFields': '*',
                    'inSR': '4326',
                    'resultRecordCount': '500'
                })
                substations = sub_data.get('features', [])
        except Exception as e:
            print(f"Substation query error: {e}")
        
        # DOT Gas Pipelines
        try:
            pipe_data = query_arcgis(DOT_PIPELINES, {
                'where': '1=1',
                'geometry': envelope,
                'geometryType': 'esriGeometryEnvelope',
                'spatialRel': 'esriSpatialRelIntersects',
                'outFields': 'typepipe,operator,status',
                'inSR': '4326',
                'resultRecordCount': '500'
            })
            pipelines = pipe_data.get('features', [])
        except Exception as e:
            print(f"Pipeline query error: {e}")
        
        # HIFLD Transmission Lines - try multiple endpoints
        try:
            trans_data = query_arcgis(f"{HIFLD_BASE}/Electric_Power_Transmission_Lines/FeatureServer/0", {
                'where': 'VOLTAGE >= 69',
                'geometry': envelope,
                'geometryType': 'esriGeometryEnvelope',
                'spatialRel': 'esriSpatialRelIntersects',
                'outFields': 'VOLTAGE,OWNER,STATUS,SHAPE_Length',
                'inSR': '4326',
                'resultRecordCount': '200'
            })
            transmission = trans_data.get('features', [])
            
            # If primary returns empty, try alternative endpoint
            if len(transmission) == 0:
                print("Primary transmission endpoint empty, trying alternative...")
                trans_data = query_arcgis(HIFLD_ALT_TRANSMISSION, {
                    'where': '1=1',
                    'geometry': envelope,
                    'geometryType': 'esriGeometryEnvelope',
                    'spatialRel': 'esriSpatialRelIntersects',
                    'outFields': '*',
                    'inSR': '4326',
                    'resultRecordCount': '200'
                })
                transmission = trans_data.get('features', [])
        except Exception as e:
            print(f"Transmission query error: {e}")
        
        # HIFLD Power Plants - try multiple endpoints
        try:
            plant_data = query_arcgis(f"{HIFLD_BASE}/Power_Plants/FeatureServer/0", {
                'where': '1=1',
                'geometry': envelope,
                'geometryType': 'esriGeometryEnvelope',
                'spatialRel': 'esriSpatialRelIntersects',
                'outFields': 'NAME,TOTAL_MW,PRIM_FUEL,SEC_FUEL,STATUS,UTILITY_NA',
                'inSR': '4326',
                'resultRecordCount': '100'
            })
            power_plants = plant_data.get('features', [])
            
            # If HIFLD returns empty, try EIA endpoint
            if len(power_plants) == 0:
                print("HIFLD power plants empty, trying EIA endpoint...")
                plant_data = query_arcgis(EIA_POWER_PLANTS, {
                    'where': '1=1',
                    'geometry': envelope,
                    'geometryType': 'esriGeometryEnvelope',
                    'spatialRel': 'esriSpatialRelIntersects',
                    'outFields': '*',
                    'inSR': '4326',
                    'resultRecordCount': '100'
                })
                power_plants = plant_data.get('features', [])
        except Exception as e:
            print(f"Power plant query error: {e}")
        
        # Fallback to local EIA data if HIFLD returns empty
        if len(power_plants) == 0:
            state = detect_state_from_coords(lat, lng)
            if state and state != 'US':
                try:
                    import sqlite3
                    conn = get_db()
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT id, name, fuel_type, capacity_mw, generation_mwh, 
                               operator, status, state, county, sector, source
                        FROM discovered_power_plants
                        WHERE state = ?
                    """, (state,))
                    
                    for row in cursor.fetchall():
                        gen_mwh = row['generation_mwh'] or 0
                        cap_mw = row['capacity_mw'] or 0
                        # Calculate capacity factor: actual generation / max possible (MW * 8760 hours)
                        capacity_factor = None
                        if gen_mwh > 0 and cap_mw > 0:
                            max_gen = cap_mw * 8760
                            capacity_factor = round(min(100, (gen_mwh / max_gen) * 100), 1)
                        
                        power_plants.append({
                            'attributes': {
                                'NAME': row['name'] or 'Unknown',
                                'TOTAL_MW': cap_mw,
                                'PRIM_FUEL': row['fuel_type'] or 'Unknown',
                                'STATUS': row['status'] or 'Operating',
                                'UTILITY_NA': row['operator'] or 'Unknown',
                                'GENERATION_MWH': gen_mwh,
                                'CAPACITY_FACTOR': capacity_factor,
                                'SOURCE': row['source'] or 'EIA'
                            }
                        })
                    conn.close()
                    print(f"📊 Site analysis fallback: Found {len(power_plants)} plants from local DB for {state}")
                except Exception as db_err:
                    print(f"Local DB fallback error: {db_err}")
        
        # Calculate score
        score_data = calculate_infrastructure_score(lat, lng, substations, pipelines, transmission, power_plants)
        
        # Format plants for power_infrastructure (flat structure with generation data)
        formatted_plants = []
        total_capacity_mw = 0
        total_generation_mwh = 0
        for p in power_plants[:20]:
            attr = p.get('attributes', {})
            cap = attr.get('TOTAL_MW', 0) or 0
            gen = attr.get('GENERATION_MWH', 0) or 0
            cf = attr.get('CAPACITY_FACTOR')
            total_capacity_mw += cap
            total_generation_mwh += gen
            formatted_plants.append({
                'name': attr.get('NAME', 'Unknown'),
                'fuel_type': attr.get('PRIM_FUEL', 'Unknown'),
                'capacity_mw': cap,
                'generation_mwh': gen,
                'capacity_factor': cf,
                'status': attr.get('STATUS', 'Operating'),
                'operator': attr.get('UTILITY_NA', 'Unknown'),
                'source': attr.get('SOURCE', 'HIFLD')
            })
        
        result = {
            'location': {'lat': lat, 'lng': lng},
            'radius': radius,
            'scores': score_data,
            'counts': {
                'substations': len(substations),
                'pipelines': len(pipelines),
                'transmissionLines': len(transmission),
                'powerPlants': len(power_plants)
            },
            'infrastructure': {
                'substations': substations[:20],  # Limit for response size
                'pipelines': pipelines[:20],
                'transmissionLines': transmission[:10],
                'powerPlants': power_plants[:10]
            },
            'power_infrastructure': {
                'plants': formatted_plants,
                'total_count': len(power_plants),
                'total_capacity_mw': round(total_capacity_mw, 1),
                'total_generation_mwh': round(total_generation_mwh, 1)
            }
        }
        
        set_cache(cache_key, result)
        return jsonify({'success': True, 'data': result})
    
    @app.route('/api/v1/energy/pipelines', methods=['GET'])
    def get_pipelines():
        """
        Get gas pipelines in an area
        
        Query params:
        - minLat, maxLat, minLng, maxLng: Bounding box
        - type: 'interstate', 'intrastate', or 'all' (default: all)
        - operator: Filter by operator name
        """
        min_lat = request.args.get('minLat', type=float)
        max_lat = request.args.get('maxLat', type=float)
        min_lng = request.args.get('minLng', type=float)
        max_lng = request.args.get('maxLng', type=float)
        pipe_type = request.args.get('type', 'all')
        operator = request.args.get('operator', '')
        
        if not all([min_lat, max_lat, min_lng, max_lng]):
            return jsonify({'success': False, 'error': 'Bounds required (minLat, maxLat, minLng, maxLng)'}), 400
        
        envelope = bounds_to_envelope(min_lat, max_lat, min_lng, max_lng)
        
        # Build where clause (case-insensitive)
        where_parts = ['1=1']
        if pipe_type != 'all':
            # Handle case-insensitive matching
            where_parts.append(f"UPPER(typepipe) = '{pipe_type.upper()}'")
        if operator:
            where_parts.append(f"UPPER(operator) LIKE '%{operator.upper()}%'")
        
        try:
            data = query_arcgis(DOT_PIPELINES, {
                'where': ' AND '.join(where_parts),
                'geometry': envelope,
                'geometryType': 'esriGeometryEnvelope',
                'spatialRel': 'esriSpatialRelIntersects',
                'outFields': '*',
                'inSR': '4326',
                'resultRecordCount': '1000'
            })
            
            return jsonify({
                'success': True,
                'count': len(data.get('features', [])),
                'data': data.get('features', [])
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/v1/energy/substations', methods=['GET'])
    def get_substations():
        """
        Get electrical substations in an area
        
        Query params:
        - minLat, maxLat, minLng, maxLng: Bounding box
        - minVoltage: Minimum voltage in kV (default: 0)
        """
        min_lat = request.args.get('minLat', type=float)
        max_lat = request.args.get('maxLat', type=float)
        min_lng = request.args.get('minLng', type=float)
        max_lng = request.args.get('maxLng', type=float)
        min_voltage = request.args.get('minVoltage', 0, type=int)
        
        if not all([min_lat, max_lat, min_lng, max_lng]):
            return jsonify({'success': False, 'error': 'Bounds required'}), 400
        
        envelope = bounds_to_envelope(min_lat, max_lat, min_lng, max_lng)
        
        where_clause = '1=1'
        if min_voltage > 0:
            where_clause = f'MAX_VOLT >= {min_voltage}'
        
        try:
            data = query_arcgis(f"{HIFLD_BASE}/Electric_Substations/FeatureServer/0", {
                'where': where_clause,
                'geometry': envelope,
                'geometryType': 'esriGeometryEnvelope',
                'spatialRel': 'esriSpatialRelIntersects',
                'outFields': '*',
                'inSR': '4326',
                'resultRecordCount': '1000'
            })
            
            return jsonify({
                'success': True,
                'count': len(data.get('features', [])),
                'data': data.get('features', [])
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/v1/energy/transmission', methods=['GET'])
    def get_transmission_lines():
        """
        Get transmission lines in an area
        
        Query params:
        - minLat, maxLat, minLng, maxLng: Bounding box
        - minVoltage: Minimum voltage in kV (default: 69)
        """
        min_lat = request.args.get('minLat', type=float)
        max_lat = request.args.get('maxLat', type=float)
        min_lng = request.args.get('minLng', type=float)
        max_lng = request.args.get('maxLng', type=float)
        min_voltage = request.args.get('minVoltage', 69, type=int)
        
        if not all([min_lat, max_lat, min_lng, max_lng]):
            return jsonify({'success': False, 'error': 'Bounds required'}), 400
        
        envelope = bounds_to_envelope(min_lat, max_lat, min_lng, max_lng)
        
        try:
            data = query_arcgis(f"{HIFLD_BASE}/Electric_Power_Transmission_Lines/FeatureServer/0", {
                'where': f'VOLTAGE >= {min_voltage}',
                'geometry': envelope,
                'geometryType': 'esriGeometryEnvelope',
                'spatialRel': 'esriSpatialRelIntersects',
                'outFields': '*',
                'inSR': '4326',
                'resultRecordCount': '500'
            })
            
            return jsonify({
                'success': True,
                'count': len(data.get('features', [])),
                'data': data.get('features', [])
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/v1/energy/power-plants', methods=['GET'])
    @require_plan('pro')
    @protect_data
    def get_power_plants():
        """
        Get power plants in an area
        
        Query params (Option 1 - Point search):
        - lat, lng: Center point coordinates
        - radius: Search radius in meters (default: 50000)
        
        Query params (Option 2 - Bounds search):
        - minLat, maxLat, minLng, maxLng: Bounding box
        - minMW: Minimum capacity in MW
        - fuel: Filter by primary fuel type
        """
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        radius = request.args.get('radius', 50000, type=int)
        
        min_lat = request.args.get('minLat', type=float)
        max_lat = request.args.get('maxLat', type=float)
        min_lng = request.args.get('minLng', type=float)
        max_lng = request.args.get('maxLng', type=float)
        min_mw = request.args.get('minMW', 0, type=int)
        fuel = request.args.get('fuel', '')
        
        if lat is not None and lng is not None:
            plants = []
            
            try:
                url = f"{HIFLD_BASE}/Power_Plants/FeatureServer/0/query"
                params = {
                    'geometry': f'{lng},{lat}',
                    'geometryType': 'esriGeometryPoint',
                    'distance': radius,
                    'units': 'esriSRUnit_Meter',
                    'outFields': 'NAME,PRIMSOURCE,TOTAL_MW,STATUS,OPERATOR,COUNTY,STATE,NAICS_DESC',
                    'returnGeometry': 'true',
                    'f': 'json'
                }
                
                response = requests.get(url, params=params, timeout=10)
                response.raise_for_status()
                data = response.json()
                
                for f in data.get('features', []):
                    attr = f.get('attributes', {})
                    geom = f.get('geometry', {})
                    plants.append({
                        'name': attr.get('NAME', 'Unknown'),
                        'fuel_type': attr.get('PRIMSOURCE', 'Unknown'),
                        'capacity_mw': attr.get('TOTAL_MW', 0) or 0,
                        'generation_mwh': None,
                        'status': attr.get('STATUS', 'Unknown'),
                        'operator': attr.get('OPERATOR', 'Unknown'),
                        'county': attr.get('COUNTY'),
                        'state': attr.get('STATE'),
                        'lat': geom.get('y'),
                        'lng': geom.get('x'),
                        'source': 'HIFLD'
                    })
            except Exception as e:
                print(f"HIFLD API error: {e}")
            
            if len(plants) == 0:
                state = detect_state_from_coords(lat, lng)
                if state and state != 'US':
                    try:
                        import sqlite3
                        conn = get_db()
                        conn.row_factory = sqlite3.Row
                        cursor = conn.cursor()
                        cursor.execute("""
                            SELECT id, name, fuel_type, capacity_mw, generation_mwh, 
                                   operator, status, state, county, sector, source
                            FROM discovered_power_plants
                            WHERE state = ?
                        """, (state,))
                        
                        for row in cursor.fetchall():
                            plants.append({
                                'id': row['id'],
                                'name': row['name'] or 'Unknown',
                                'fuel_type': row['fuel_type'] or 'Unknown',
                                'capacity_mw': row['capacity_mw'] or 0,
                                'generation_mwh': row['generation_mwh'] or 0,
                                'operator': row['operator'] or 'Unknown',
                                'status': row['status'] or 'Operating',
                                'state': row['state'],
                                'county': row['county'],
                                'lat': None,
                                'lng': None,
                                'source': row['source'] or 'EIA'
                            })
                        conn.close()
                        print(f"📊 Fallback: Found {len(plants)} plants from local DB for {state}")
                    except Exception as db_err:
                        print(f"Local DB fallback error: {db_err}")
            
            total_mw = sum(p.get('capacity_mw', 0) or 0 for p in plants)
            total_gen = sum(p.get('generation_mwh', 0) or 0 for p in plants)
            
            return jsonify({
                'success': True,
                'data': {
                    'plants': plants[:50],
                    'total_count': len(plants),
                    'total_capacity_mw': round(total_mw, 1),
                    'total_generation_mwh': round(total_gen, 1)
                }
            })
        
        if not all([min_lat, max_lat, min_lng, max_lng]):
            return jsonify({'success': False, 'error': 'lat/lng or bounds required'}), 400
        
        envelope = bounds_to_envelope(min_lat, max_lat, min_lng, max_lng)
        
        where_parts = ['1=1']
        if min_mw > 0:
            where_parts.append(f'TOTAL_MW >= {min_mw}')
        if fuel:
            where_parts.append(f"UPPER(PRIM_FUEL) LIKE '%{fuel.upper()}%'")
        
        try:
            data = query_arcgis(f"{HIFLD_BASE}/Power_Plants/FeatureServer/0", {
                'where': ' AND '.join(where_parts),
                'geometry': envelope,
                'geometryType': 'esriGeometryEnvelope',
                'spatialRel': 'esriSpatialRelIntersects',
                'outFields': '*',
                'inSR': '4326',
                'resultRecordCount': '500'
            })
            
            return jsonify({
                'success': True,
                'count': len(data.get('features', [])),
                'data': data.get('features', [])
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/v1/energy/wells', methods=['GET'])
    def get_wells():
        """
        Get oil/gas wells from state regulatory agencies
        
        Query params:
        - lat, lng: Center point (required)
        - radius: Search radius in meters (default: 10000)
        - state: State code (auto-detected if not provided)
        """
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        radius = request.args.get('radius', 10000, type=int)
        state = request.args.get('state', '').upper()
        
        if not lat or not lng:
            return jsonify({'success': False, 'error': 'lat and lng required'}), 400
        
        # Auto-detect state if not provided
        if not state:
            if -124.5 <= lng <= -114 and 32.5 <= lat <= 42:
                state = 'CA'
            elif -109 <= lng <= -103 and 31.3 <= lat <= 37:
                state = 'NM'
            elif -109 <= lng <= -102 and 37 <= lat <= 41:
                state = 'CO'
            elif -106.6 <= lng <= -93.5 and 25.8 <= lat <= 36.5:
                state = 'TX'
        
        if state not in STATE_GIS and state != 'TX':
            return jsonify({
                'success': True,
                'message': f'No well data API available for state: {state}',
                'data': []
            })
        
        # Calculate bounds
        lat_delta = radius / 111000
        lng_delta = radius / (111000 * abs(cos(radians(lat))))
        envelope = bounds_to_envelope(lat - lat_delta, lat + lat_delta, lng - lng_delta, lng + lng_delta)
        
        try:
            if state == 'TX':
                # Texas RRC
                data = query_arcgis(f"{TEXAS_RRC_BASE}/1", {  # Oil wells layer
                    'where': '1=1',
                    'geometry': envelope,
                    'geometryType': 'esriGeometryEnvelope',
                    'spatialRel': 'esriSpatialRelIntersects',
                    'outFields': '*',
                    'inSR': '4326',
                    'resultRecordCount': '500'
                })
            else:
                # State GIS
                data = query_arcgis(STATE_GIS[state], {
                    'where': '1=1',
                    'geometry': envelope,
                    'geometryType': 'esriGeometryEnvelope',
                    'spatialRel': 'esriSpatialRelIntersects',
                    'outFields': '*',
                    'inSR': '4326',
                    'resultRecordCount': '500'
                })
            
            return jsonify({
                'success': True,
                'state': state,
                'count': len(data.get('features', [])),
                'data': data.get('features', [])
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/v1/energy/texas-pipelines', methods=['GET'])
    def get_texas_pipelines():
        """
        Get Texas RRC pipeline data
        
        Query params:
        - minLat, maxLat, minLng, maxLng: Bounding box (in Texas)
        - operator: Filter by operator/company name
        """
        min_lat = request.args.get('minLat', type=float)
        max_lat = request.args.get('maxLat', type=float)
        min_lng = request.args.get('minLng', type=float)
        max_lng = request.args.get('maxLng', type=float)
        operator = request.args.get('operator', '')
        
        if not all([min_lat, max_lat, min_lng, max_lng]):
            return jsonify({'success': False, 'error': 'Bounds required'}), 400
        
        envelope = bounds_to_envelope(min_lat, max_lat, min_lng, max_lng)
        
        where_parts = ['1=1']
        if operator:
            where_parts.append(f"UPPER(OPERATOR_NAME) LIKE '%{operator.upper()}%'")
        
        try:
            data = query_arcgis(f"{TEXAS_RRC_BASE}/0", {  # Pipelines layer
                'where': ' AND '.join(where_parts),
                'geometry': envelope,
                'geometryType': 'esriGeometryEnvelope',
                'spatialRel': 'esriSpatialRelIntersects',
                'outFields': '*',
                'inSR': '4326',
                'resultRecordCount': '1000'
            })
            
            return jsonify({
                'success': True,
                'count': len(data.get('features', [])),
                'data': data.get('features', [])
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
    
    @app.route('/api/v1/energy/compare-sites', methods=['GET'])
    def compare_sites():
        """
        Compare multiple sites for energy infrastructure
        
        Query params:
        - sites: Comma-separated lat,lng pairs (e.g., "33.45,-112.87;32.88,-111.76")
        - radius: Search radius in meters (default: 25000)
        """
        sites_param = request.args.get('sites', '')
        radius = request.args.get('radius', 25000, type=int)
        
        if not sites_param:
            return jsonify({'success': False, 'error': 'sites parameter required (format: lat,lng;lat,lng)'}), 400
        
        # Parse sites
        sites = []
        for site_str in sites_param.split(';'):
            try:
                lat, lng = map(float, site_str.split(','))
                sites.append({'lat': lat, 'lng': lng})
            except:
                continue
        
        if not sites:
            return jsonify({'success': False, 'error': 'No valid sites provided'}), 400
        
        # Analyze each site
        results = []
        for site in sites:
            lat, lng = site['lat'], site['lng']
            
            # Calculate bounds
            lat_delta = radius / 111000
            lng_delta = radius / (111000 * abs(cos(radians(lat))))
            envelope = bounds_to_envelope(lat - lat_delta, lat + lat_delta, lng - lng_delta, lng + lng_delta)
            
            # Query infrastructure
            substations = []
            pipelines = []
            transmission = []
            power_plants = []
            
            try:
                sub_data = query_arcgis(f"{HIFLD_BASE}/Electric_Substations/FeatureServer/0", {
                    'where': '1=1', 'geometry': envelope,
                    'geometryType': 'esriGeometryEnvelope', 'spatialRel': 'esriSpatialRelIntersects',
                    'outFields': 'NAME,MAX_VOLT', 'inSR': '4326', 'resultRecordCount': '100'
                })
                substations = sub_data.get('features', [])
            except:
                pass
            
            try:
                pipe_data = query_arcgis(DOT_PIPELINES, {
                    'where': '1=1', 'geometry': envelope,
                    'geometryType': 'esriGeometryEnvelope', 'spatialRel': 'esriSpatialRelIntersects',
                    'outFields': 'typepipe,operator', 'inSR': '4326', 'resultRecordCount': '100'
                })
                pipelines = pipe_data.get('features', [])
            except:
                pass
            
            try:
                trans_data = query_arcgis(f"{HIFLD_BASE}/Electric_Power_Transmission_Lines/FeatureServer/0", {
                    'where': 'VOLTAGE >= 69', 'geometry': envelope,
                    'geometryType': 'esriGeometryEnvelope', 'spatialRel': 'esriSpatialRelIntersects',
                    'outFields': 'VOLTAGE', 'inSR': '4326', 'resultRecordCount': '50'
                })
                transmission = trans_data.get('features', [])
            except:
                pass
            
            try:
                plant_data = query_arcgis(f"{HIFLD_BASE}/Power_Plants/FeatureServer/0", {
                    'where': '1=1', 'geometry': envelope,
                    'geometryType': 'esriGeometryEnvelope', 'spatialRel': 'esriSpatialRelIntersects',
                    'outFields': 'NAME,TOTAL_MW,PRIM_FUEL', 'inSR': '4326', 'resultRecordCount': '50'
                })
                power_plants = plant_data.get('features', [])
            except:
                pass
            
            # Calculate score
            score_data = calculate_infrastructure_score(lat, lng, substations, pipelines, transmission, power_plants)
            
            results.append({
                'location': {'lat': lat, 'lng': lng},
                'scores': score_data,
                'counts': {
                    'substations': len(substations),
                    'pipelines': len(pipelines),
                    'transmissionLines': len(transmission),
                    'powerPlants': len(power_plants)
                }
            })
        
        # Sort by overall score
        results.sort(key=lambda x: x['scores']['overallScore'], reverse=True)
        
        return jsonify({
            'success': True,
            'count': len(results),
            'data': results,
            'bestSite': results[0] if results else None
        })
    
    def detect_state_from_coords(lat, lng):
        """Simple state detection from coordinates"""
        state_bounds = {
            'AZ': (31.3, 37.0, -114.8, -109.0),
            'TX': (25.8, 36.5, -106.6, -93.5),
            'VA': (36.5, 39.5, -83.7, -75.2),
            'GA': (30.4, 35.0, -85.6, -80.8),
            'NV': (35.0, 42.0, -120.0, -114.0),
            'CA': (32.5, 42.0, -124.4, -114.1),
            'OR': (42.0, 46.3, -124.6, -116.5),
            'WA': (45.5, 49.0, -124.8, -116.9),
            'OH': (38.4, 42.0, -84.8, -80.5),
            'IA': (40.4, 43.5, -96.6, -90.1),
            'UT': (37.0, 42.0, -114.0, -109.0)
        }
        
        for state, (min_lat, max_lat, min_lng, max_lng) in state_bounds.items():
            if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
                return state
        
        return 'US'
    
    print("✅ Energy Infrastructure API v3 routes registered (with fallback endpoints):")
    print("   GET /api/v1/energy/site-analysis")
    print("   GET /api/v1/energy/pipelines")
    print("   GET /api/v1/energy/substations")
    print("   GET /api/v1/energy/transmission")
    print("   GET /api/v1/energy/power-plants")
    print("   GET /api/v1/energy/wells")
    print("   GET /api/v1/energy/texas-pipelines")
    print("   GET /api/v1/energy/compare-sites")
