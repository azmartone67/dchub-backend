"""
NOAA Weather Risk & FEMA Disaster Zone APIs
Adds climate risk and disaster assessment to site scoring
"""

import requests
from flask import Blueprint, jsonify, request
from datetime import datetime, timedelta
import json

risk_bp = Blueprint('risk_assessment', __name__)

CACHE = {}
CACHE_DURATION = 3600

def get_cached(key, fetch_func):
    now = datetime.now()
    if key in CACHE:
        data, timestamp = CACHE[key]
        if (now - timestamp).seconds < CACHE_DURATION:
            return data
    try:
        data = fetch_func()
        CACHE[key] = (data, now)
        return data
    except Exception as e:
        print(f"Risk API error for {key}: {e}")
        return None

NOAA_BASE = "https://api.weather.gov"
FEMA_BASE = "https://hazards.fema.gov/gis/nfhl/rest/services"

def get_noaa_alerts(lat, lng):
    """Get active weather alerts for a location"""
    try:
        url = f"{NOAA_BASE}/alerts/active?point={lat},{lng}"
        headers = {'User-Agent': 'DCHub/1.0 (dchub.cloud)'}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            alerts = data.get('features', [])
            return {
                'active_alerts': len(alerts),
                'alerts': [{
                    'event': a['properties'].get('event'),
                    'severity': a['properties'].get('severity'),
                    'urgency': a['properties'].get('urgency'),
                    'headline': a['properties'].get('headline'),
                    'expires': a['properties'].get('expires')
                } for a in alerts[:5]]
            }
        return {'active_alerts': 0, 'alerts': []}
    except Exception as e:
        return {'active_alerts': 0, 'alerts': [], 'error': str(e)}

def get_noaa_forecast(lat, lng):
    """Get weather forecast grid for location"""
    try:
        url = f"{NOAA_BASE}/points/{lat},{lng}"
        headers = {'User-Agent': 'DCHub/1.0 (dchub.cloud)'}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            props = data.get('properties', {})
            return {
                'grid_id': props.get('gridId'),
                'grid_x': props.get('gridX'),
                'grid_y': props.get('gridY'),
                'timezone': props.get('timeZone'),
                'county': props.get('county'),
                'forecast_url': props.get('forecast')
            }
        return None
    except:
        return None

def get_historical_weather_risk(lat, lng):
    """Calculate historical weather risk score based on location"""
    high_risk_zones = {
        'hurricane': [(25, 32, -100, -75)],
        'tornado': [(30, 45, -105, -85)],
        'earthquake': [(32, 42, -125, -115), (18, 20, -156, -154)],
        'wildfire': [(32, 42, -125, -115)],
        'flood': [(25, 35, -100, -80), (38, 45, -95, -75)]
    }
    
    risks = {}
    total_score = 100
    
    for risk_type, zones in high_risk_zones.items():
        for min_lat, max_lat, min_lng, max_lng in zones:
            if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
                if risk_type == 'hurricane':
                    risks['hurricane'] = 'high'
                    total_score -= 15
                elif risk_type == 'tornado':
                    risks['tornado'] = 'moderate'
                    total_score -= 10
                elif risk_type == 'earthquake':
                    risks['earthquake'] = 'high'
                    total_score -= 20
                elif risk_type == 'wildfire':
                    risks['wildfire'] = 'moderate'
                    total_score -= 10
                elif risk_type == 'flood':
                    risks['flood'] = 'moderate'
                    total_score -= 8
    
    return {
        'risk_score': max(0, total_score),
        'risk_factors': risks,
        'risk_level': 'low' if total_score >= 80 else 'moderate' if total_score >= 60 else 'high'
    }

def get_fema_flood_zone(lat, lng):
    """Get FEMA flood zone for a location"""
    try:
        url = f"{FEMA_BASE}/public/NFHL/MapServer/28/query"
        params = {
            'geometry': f'{lng},{lat}',
            'geometryType': 'esriGeometryPoint',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'FLD_ZONE,ZONE_SUBTY,SFHA_TF',
            'returnGeometry': 'false',
            'f': 'json'
        }
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            features = data.get('features', [])
            if features:
                attrs = features[0].get('attributes', {})
                zone = attrs.get('FLD_ZONE', 'Unknown')
                is_sfha = attrs.get('SFHA_TF', 'F') == 'T'
                return {
                    'flood_zone': zone,
                    'special_flood_hazard_area': is_sfha,
                    'zone_description': get_flood_zone_description(zone),
                    'flood_risk': 'high' if is_sfha else 'low' if zone in ['X', 'C'] else 'moderate'
                }
            return {'flood_zone': 'X', 'special_flood_hazard_area': False, 'flood_risk': 'low'}
        return {'flood_zone': 'Unknown', 'error': 'FEMA API unavailable'}
    except Exception as e:
        return {'flood_zone': 'Unknown', 'error': str(e)}

def get_flood_zone_description(zone):
    """Get description for FEMA flood zone codes"""
    descriptions = {
        'A': 'High risk - 1% annual flood chance',
        'AE': 'High risk - 1% annual flood chance with BFE',
        'AH': 'High risk - 1% shallow flooding',
        'AO': 'High risk - 1% sheet flow flooding',
        'V': 'High risk - Coastal flood with wave action',
        'VE': 'High risk - Coastal flood with wave action and BFE',
        'X': 'Minimal risk - Outside 500-year floodplain',
        'B': 'Moderate risk - 500-year floodplain',
        'C': 'Minimal risk - Outside 500-year floodplain',
        'D': 'Undetermined risk - No analysis performed'
    }
    return descriptions.get(zone, 'Unknown flood zone')

def get_fema_disaster_declarations(state):
    """Get recent disaster declarations for a state"""
    try:
        url = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
        params = {
            '$filter': f"state eq '{state}'",
            '$orderby': 'declarationDate desc',
            '$top': 10
        }
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            declarations = data.get('DisasterDeclarationsSummaries', [])
            return {
                'state': state,
                'recent_disasters': len(declarations),
                'declarations': [{
                    'disaster_number': d.get('disasterNumber'),
                    'declaration_date': d.get('declarationDate'),
                    'incident_type': d.get('incidentType'),
                    'title': d.get('declarationTitle'),
                    'designated_area': d.get('designatedArea')
                } for d in declarations]
            }
        return {'state': state, 'recent_disasters': 0, 'declarations': []}
    except Exception as e:
        return {'state': state, 'error': str(e)}

def calculate_comprehensive_risk(lat, lng, state=None):
    """Calculate comprehensive risk assessment for data center site"""
    weather_risk = get_historical_weather_risk(lat, lng)
    flood_data = get_fema_flood_zone(lat, lng)
    alerts = get_noaa_alerts(lat, lng)
    
    base_score = weather_risk['risk_score']
    
    if flood_data.get('special_flood_hazard_area'):
        base_score -= 15
    elif flood_data.get('flood_risk') == 'moderate':
        base_score -= 5
    
    if alerts.get('active_alerts', 0) > 0:
        base_score -= min(10, alerts['active_alerts'] * 3)
    
    final_score = max(0, min(100, base_score))
    
    return {
        'location': {'lat': lat, 'lng': lng},
        'overall_risk_score': final_score,
        'risk_grade': 'A' if final_score >= 90 else 'B' if final_score >= 75 else 'C' if final_score >= 60 else 'D' if final_score >= 40 else 'F',
        'weather_risk': weather_risk,
        'flood_assessment': flood_data,
        'active_weather_alerts': alerts,
        'data_center_suitability': 'excellent' if final_score >= 85 else 'good' if final_score >= 70 else 'fair' if final_score >= 50 else 'poor',
        'recommendations': get_recommendations(weather_risk, flood_data, final_score)
    }

def get_recommendations(weather_risk, flood_data, score):
    """Generate site recommendations based on risk assessment"""
    recs = []
    
    if weather_risk.get('risk_factors', {}).get('earthquake'):
        recs.append("Consider seismic-resistant construction and equipment mounting")
    
    if weather_risk.get('risk_factors', {}).get('hurricane'):
        recs.append("Implement hurricane-rated building envelope and backup power")
    
    if weather_risk.get('risk_factors', {}).get('tornado'):
        recs.append("Design for tornado wind loads and consider underground facilities")
    
    if flood_data.get('special_flood_hazard_area'):
        recs.append("Elevate critical equipment above base flood elevation")
        recs.append("Consider flood insurance requirements")
    
    if weather_risk.get('risk_factors', {}).get('wildfire'):
        recs.append("Maintain defensible space and fire-resistant landscaping")
    
    if score >= 85:
        recs.append("Location has excellent natural disaster resilience")
    
    return recs if recs else ["No significant risk factors identified"]

@risk_bp.route('/api/risk/assessment', methods=['GET'])
def risk_assessment():
    """GET /api/risk/assessment - Comprehensive risk assessment for a location"""
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    
    if not lat or not lng:
        return jsonify({'error': 'lat and lng parameters required'}), 400
    
    cache_key = f"risk_{lat}_{lng}"
    result = get_cached(cache_key, lambda: calculate_comprehensive_risk(lat, lng))
    
    return jsonify({
        'success': True,
        'assessment': result,
        'source': 'NOAA + FEMA',
        'cached': cache_key in CACHE
    })

@risk_bp.route('/api/risk/weather-alerts', methods=['GET'])
def weather_alerts():
    """GET /api/risk/weather-alerts - Active weather alerts for location"""
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    
    if not lat or not lng:
        return jsonify({'error': 'lat and lng parameters required'}), 400
    
    alerts = get_noaa_alerts(lat, lng)
    return jsonify({'success': True, 'data': alerts, 'source': 'NOAA Weather API'})

@risk_bp.route('/api/risk/flood-zone', methods=['GET'])
def flood_zone():
    """GET /api/risk/flood-zone - FEMA flood zone information"""
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    
    if not lat or not lng:
        return jsonify({'error': 'lat and lng parameters required'}), 400
    
    flood_data = get_fema_flood_zone(lat, lng)
    return jsonify({'success': True, 'data': flood_data, 'source': 'FEMA NFHL'})

@risk_bp.route('/api/risk/disasters', methods=['GET'])
def disaster_history():
    """GET /api/risk/disasters - Recent disaster declarations by state"""
    state = request.args.get('state', '').upper()
    
    if not state or len(state) != 2:
        return jsonify({'error': 'Valid 2-letter state code required'}), 400
    
    disasters = get_fema_disaster_declarations(state)
    return jsonify({'success': True, 'data': disasters, 'source': 'FEMA OpenFEMA API'})

@risk_bp.route('/api/risk/summary', methods=['GET'])
def risk_summary():
    """GET /api/risk/summary - API summary and capabilities"""
    return jsonify({
        'success': True,
        'service': 'DC Hub Risk Assessment API',
        'version': '1.0',
        'endpoints': {
            '/api/risk/assessment': 'Comprehensive site risk assessment (lat, lng)',
            '/api/risk/weather-alerts': 'Active NOAA weather alerts (lat, lng)',
            '/api/risk/flood-zone': 'FEMA flood zone lookup (lat, lng)',
            '/api/risk/disasters': 'Recent disaster declarations (state)'
        },
        'data_sources': [
            {'name': 'NOAA Weather API', 'type': 'weather_alerts', 'status': 'active'},
            {'name': 'FEMA NFHL', 'type': 'flood_zones', 'status': 'active'},
            {'name': 'FEMA OpenFEMA', 'type': 'disaster_declarations', 'status': 'active'}
        ],
        'risk_factors_assessed': [
            'Hurricane risk',
            'Tornado risk', 
            'Earthquake risk',
            'Wildfire risk',
            'Flood zone status',
            'Active weather alerts'
        ]
    })

def register_risk_routes(app):
    """Register risk assessment routes with Flask app"""
    app.register_blueprint(risk_bp)
    print("🌪️ Risk Assessment API registered:")
    print("   GET /api/risk/assessment - Comprehensive site risk")
    print("   GET /api/risk/weather-alerts - NOAA weather alerts")
    print("   GET /api/risk/flood-zone - FEMA flood zones")
    print("   GET /api/risk/disasters - Disaster declarations")
