"""
EIA (Energy Information Administration) API Integration
Provides electricity retail prices, power plants data, and generation by source
"""

import os
import requests
from flask import Blueprint, jsonify, request

eia_bp = Blueprint('eia', __name__)

EIA_API_KEY = os.environ.get('EIA_API_KEY')
EIA_BASE_URL = 'https://api.eia.gov/v2'

def make_eia_request(endpoint, params=None):
    """Make authenticated request to EIA API"""
    if not EIA_API_KEY:
        return None, "EIA_API_KEY not configured"
    
    if params is None:
        params = {}
    params['api_key'] = EIA_API_KEY
    
    try:
        url = f"{EIA_BASE_URL}/{endpoint}"
        response = requests.get(url, params=params, timeout=30)
        if response.status_code == 200:
            return response.json(), None
        else:
            return None, f"EIA API error: {response.status_code}"
    except Exception as e:
        return None, str(e)


@eia_bp.route('/api/eia/prices', methods=['GET'])
def get_electricity_prices():
    """
    Get electricity retail prices by state
    Query params:
      - state: State abbreviation (e.g., TX, CA, VA)
      - sector: residential, commercial, industrial, all (default: all)
      - year: Year (default: latest)
    """
    state = request.args.get('state')
    sector = request.args.get('sector', 'ALL')
    year = request.args.get('year')
    
    sector_map = {
        'residential': 'RES',
        'commercial': 'COM', 
        'industrial': 'IND',
        'all': 'ALL'
    }
    sector_id = sector_map.get(sector.lower(), 'ALL') if sector else 'ALL'
    
    params = {
        'frequency': 'annual',
        'data[0]': 'price',
        'sort[0][column]': 'period',
        'sort[0][direction]': 'desc',
        'length': 200
    }
    
    if state:
        params['facets[stateid][]'] = state.upper()
    if sector_id != 'ALL':
        params['facets[sectorid][]'] = sector_id
    
    data, error = make_eia_request('electricity/retail-sales/data', params)
    
    if error:
        return jsonify({'success': False, 'error': error}), 500
    
    results = []
    seen_states = set()
    
    if data and 'response' in data and 'data' in data['response']:
        for item in data['response']['data']:
            state_id = item.get('stateid')
            item_sector = item.get('sectorid')
            
            if state and state_id != state.upper():
                continue
            if sector_id != 'ALL' and item_sector != sector_id:
                continue
            
            key = f"{state_id}-{item.get('period')}"
            if key in seen_states:
                continue
            seen_states.add(key)
            
            results.append({
                'state': state_id,
                'state_name': item.get('stateDescription'),
                'sector': item.get('sectorName'),
                'year': item.get('period'),
                'price_cents_kwh': float(item.get('price', 0)),
                'price_dollars_mwh': float(item.get('price', 0)) * 10
            })
            
            if year and item.get('period') == year:
                break
    
    results = results[:50]
    
    return jsonify({
        'success': True,
        'source': 'EIA - U.S. Energy Information Administration',
        'count': len(results),
        'data': results
    })


@eia_bp.route('/api/eia/power-plants', methods=['GET'])
def get_power_plants():
    """
    Get power plants data
    Query params:
      - state: State abbreviation
      - fuel: coal, natural_gas, nuclear, hydro, wind, solar
      - min_capacity_mw: Minimum capacity in MW
    """
    state = request.args.get('state')
    fuel = request.args.get('fuel')
    min_capacity = request.args.get('min_capacity_mw', type=float)
    
    params = {
        'frequency': 'annual',
        'data[0]': 'total-consumption-btu',
        'data[1]': 'generation',
        'sort[0][column]': 'generation',
        'sort[0][direction]': 'desc',
        'length': 500
    }
    
    if state:
        params['facets[state][]'] = state.upper()
    
    data, error = make_eia_request('electricity/facility-fuel/data', params)
    
    if error:
        return jsonify({'success': False, 'error': error}), 500
    
    fuel_map = {
        'coal': ['COL', 'COW', 'RC'],
        'natural_gas': ['NG', 'OG'],
        'nuclear': ['NUC'],
        'hydro': ['HYC', 'HPS'],
        'wind': ['WND'],
        'solar': ['SUN', 'DPV']
    }
    
    results = []
    seen_plants = set()
    
    if data and 'response' in data and 'data' in data['response']:
        for item in data['response']['data']:
            plant_id = item.get('plantid')
            plant_state = item.get('state')
            plant_fuel = item.get('fuel2002')
            generation = float(item.get('generation', 0) or 0)
            
            if plant_id in seen_plants:
                continue
            
            if state and plant_state != state.upper():
                continue
            
            if fuel and fuel in fuel_map:
                if plant_fuel not in fuel_map[fuel]:
                    continue
            
            capacity_estimate = generation / 8760 * 1.3 if generation > 0 else 0
            
            if min_capacity and capacity_estimate < min_capacity:
                continue
            
            seen_plants.add(plant_id)
            
            results.append({
                'plant_id': plant_id,
                'plant_name': item.get('plantName'),
                'state': plant_state,
                'fuel_type': plant_fuel,
                'fuel_description': item.get('fuel-units'),
                'year': item.get('period'),
                'generation_mwh': generation,
                'capacity_estimate_mw': round(capacity_estimate, 1)
            })
    
    results = results[:100]
    
    return jsonify({
        'success': True,
        'source': 'EIA - U.S. Energy Information Administration',
        'count': len(results),
        'data': results
    })


@eia_bp.route('/api/eia/generation', methods=['GET'])
def get_electricity_generation():
    """
    Get electricity generation by source
    Query params:
      - state: State abbreviation
      - source: coal, natural_gas, nuclear, hydro, wind, solar, all
      - year: Year (default: latest)
    """
    state = request.args.get('state')
    source = request.args.get('source', 'all')
    year = request.args.get('year')
    
    params = {
        'frequency': 'annual',
        'data[0]': 'generation',
        'sort[0][column]': 'period',
        'sort[0][direction]': 'desc',
        'length': 500
    }
    
    if state:
        params['facets[location][]'] = state.upper()
    
    data, error = make_eia_request('electricity/electric-power-operational-data/data', params)
    
    if error:
        return jsonify({'success': False, 'error': error}), 500
    
    source_map = {
        'coal': ['COW'],
        'natural_gas': ['NG'],
        'nuclear': ['NUC'],
        'hydro': ['HYC', 'HPS'],
        'wind': ['WND'],
        'solar': ['SUN', 'TSN'],
        'all': None
    }
    
    results = []
    state_totals = {}
    
    if data and 'response' in data and 'data' in data['response']:
        for item in data['response']['data']:
            item_state = item.get('location')
            item_source = item.get('fueltypeid')
            item_year = item.get('period')
            generation = float(item.get('generation', 0) or 0)
            
            if len(item_state or '') > 2:
                continue
            
            if state and item_state != state.upper():
                continue
            
            if year and item_year != year:
                continue
            
            if source != 'all' and source in source_map:
                if item_source not in source_map[source]:
                    continue
            
            key = f"{item_state}-{item_year}"
            if key not in state_totals:
                state_totals[key] = {
                    'state': item_state,
                    'year': item_year,
                    'sources': {},
                    'total_generation_mwh': 0
                }
            
            source_name = item.get('fueltypeid', 'other')
            state_totals[key]['sources'][source_name] = generation
            state_totals[key]['total_generation_mwh'] += generation
    
    for key, data in sorted(state_totals.items(), key=lambda x: -x[1]['total_generation_mwh']):
        total = data['total_generation_mwh']
        sources_pct = {}
        for src, gen in data['sources'].items():
            if gen > 0:
                sources_pct[src] = {
                    'generation_mwh': round(gen, 0),
                    'percentage': round(gen / total * 100, 1) if total > 0 else 0
                }
        
        results.append({
            'state': data['state'],
            'year': data['year'],
            'total_generation_mwh': round(total, 0),
            'total_generation_twh': round(total / 1_000_000, 2),
            'sources': sources_pct
        })
    
    results = results[:50]
    
    return jsonify({
        'success': True,
        'source': 'EIA - U.S. Energy Information Administration',
        'count': len(results),
        'data': results
    })


@eia_bp.route('/api/eia/status', methods=['GET'])
def get_eia_status():
    """Check EIA API status and available data"""
    has_key = bool(EIA_API_KEY)
    
    test_result = None
    if has_key:
        data, error = make_eia_request('electricity/retail-sales/data', {
            'frequency': 'annual',
            'length': 1
        })
        test_result = 'connected' if data else f'error: {error}'
    
    return jsonify({
        'success': True,
        'api_key_configured': has_key,
        'connection_status': test_result,
        'endpoints': {
            '/api/eia/prices': 'Electricity retail prices by state',
            '/api/eia/power-plants': 'Power plants data with generation',
            '/api/eia/generation': 'Electricity generation by source'
        },
        'data_source': 'U.S. Energy Information Administration (EIA)',
        'documentation': 'https://www.eia.gov/opendata/'
    })

# === phase 92: source-registry heartbeat (auto-fires on clean module exit) ===
# Non-invasive: never crashes the script if the registry is unreachable.
# Source ID: backend-eia-api
_phase92_heartbeat_registered = True
try:
    import atexit as _phase92_atexit
    from dchub_heartbeat import heartbeat as _phase92_heartbeat
    def _phase92_emit():
        try:
            _phase92_heartbeat("backend-eia-api", status="success",
                              metadata={"trigger": "atexit"})
        except Exception:
            pass
    _phase92_atexit.register(_phase92_emit)
except Exception:
    pass  # heartbeat module unavailable; extractor continues normally
