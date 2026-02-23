"""
Power Plant Intelligence Module
================================
Integrates EIA Form 860/923 plant-level data and EPA ECHO discharge permits
for DC Hub's Land & Power and site evaluation features.

Data Sources:
  - EIA API v2: /electricity/operating-generator-capacity (Form 860 data)
  - EIA API v2: /electricity/facility-fuel (Form 923 generation/fuel data)
  - EPA ECHO: CWA facility search with NPDES discharge permits

Endpoints:
  GET /api/v1/power-plants/nearby       — Power plants near coordinates
  GET /api/v1/power-plants/detail/<id>  — Single plant detail (EIA plant ID)
  GET /api/v1/power-plants/cooling      — Plants with cooling system data (water risk)
  GET /api/v1/power-plants/generation   — Generation & fuel data (Form 923)
  GET /api/v1/power-plants/permits      — EPA ECHO discharge permits nearby
  GET /api/v1/power-plants/water-risk   — Combined water risk assessment
  GET /api/v1/power-plants/summary      — API info and available endpoints
"""

import os
import json
import logging
import time
from datetime import datetime
from functools import lru_cache
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

power_plant_bp = Blueprint('power_plant_intel', __name__)

EIA_API_KEY = os.environ.get('EIA_API_KEY', '')
EIA_BASE = 'https://api.eia.gov/v2'

# Simple bounded cache
_cache = {}
_cache_ttl = {}
CACHE_DURATION = 1800  # 30 minutes


def _get_cached(key):
    if key in _cache and time.time() - _cache_ttl.get(key, 0) < CACHE_DURATION:
        return _cache[key]
    return None


def _set_cached(key, value):
    _cache[key] = value
    _cache_ttl[key] = time.time()
    # Evict old entries if cache grows too large
    if len(_cache) > 200:
        oldest = sorted(_cache_ttl, key=_cache_ttl.get)[:50]
        for k in oldest:
            _cache.pop(k, None)
            _cache_ttl.pop(k, None)


def _eia_request(path, params=None):
    """Make a request to EIA API v2."""
    import urllib.request
    import urllib.parse
    import urllib.error

    if not EIA_API_KEY:
        return {'error': 'EIA_API_KEY not configured'}

    base_params = {'api_key': EIA_API_KEY}
    if params:
        base_params.update(params)

    url = f"{EIA_BASE}/{path}"
    if base_params:
        url += '?' + urllib.parse.urlencode(base_params, doseq=True)

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'DCHub/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return {'error': f'EIA API HTTP {e.code}', 'detail': e.read().decode('utf-8', errors='replace')[:500]}
    except Exception as e:
        return {'error': str(e)}


def _echo_request(endpoint, params=None):
    """Make a request to EPA ECHO REST API."""
    import urllib.request
    import urllib.parse
    import urllib.error

    base_url = f"https://echo.epa.gov/api/{endpoint}"
    if params:
        base_url += '?' + urllib.parse.urlencode(params, doseq=True)

    logger.info(f"ECHO request: {base_url[:200]}")
    try:
        req = urllib.request.Request(base_url, headers={'User-Agent': 'DCHub/1.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode('utf-8')
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {'error': 'Invalid JSON response', 'raw': body[:500]}
    except Exception as e:
        logger.warning(f"ECHO API error: {e}")
        return {'error': str(e)}


# =============================================================================
# EIA Form 860 — Operating Generator Capacity (Plant-Level Data)
# =============================================================================

@power_plant_bp.route('/api/v1/power-plants/nearby')
def power_plants_nearby():
    """Find power plants near coordinates using EIA operating generator capacity data.

    Query params:
        lat (float): Latitude (required)
        lng (float): Longitude (required)
        radius (float): Search radius in miles (default: 25)
        fuel (str): Filter by fuel type — NG, NUC, SUN, WND, WAT, COL, etc.
        min_mw (float): Minimum capacity in MW (default: 1)
        status (str): Operating status — OP (operating), SB (standby), OS (out of service)
        limit (int): Max results (default: 50, max: 200)
    """
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if lat is None or lng is None:
        return jsonify({'error': 'lat and lng parameters required'}), 400

    radius = request.args.get('radius', 25, type=float)
    fuel = request.args.get('fuel', '')
    min_mw = request.args.get('min_mw', 1, type=float)
    status = request.args.get('status', 'OP')
    limit = min(request.args.get('limit', 50, type=int), 200)

    cache_key = f"eia_plants_{lat:.2f}_{lng:.2f}_{radius}_{fuel}_{min_mw}_{status}"
    cached = _get_cached(cache_key)
    if cached:
        return jsonify(cached)

    # EIA API v2: operating-generator-capacity provides plant-level data
    # Note: EIA API doesn't return lat/lng — we supplement with HIFLD or return state-level
    # We use length=1 per generator to get latest month only (avoids monthly duplication)
    state = _coords_to_state(lat, lng)
    if not state:
        return jsonify({'error': 'Could not determine state from coordinates'}), 400

    params = {
        'frequency': 'monthly',
        'data[0]': 'nameplate-capacity-mw',
        'data[1]': 'county',
        'data[2]': 'latitude',
        'data[3]': 'longitude',
        'facets[stateid][]': state,
        'sort[0][column]': 'period',
        'sort[0][direction]': 'desc',
        'length': 5000,
    }
    if status:
        params['facets[status][]'] = status
    if fuel:
        params['facets[energy_source_code][]'] = fuel

    data = _eia_request('electricity/operating-generator-capacity/data/', params)
    if 'error' in data:
        return jsonify({'success': False, 'error': data['error']}), 500

    # Deduplicate: only keep the most recent period per plant+generator combo
    seen_generators = set()
    plants = {}
    for row in data.get('response', {}).get('data', []):
        plant_id = row.get('plantid') or row.get('plantCode')
        gen_id = row.get('generatorid', row.get('generator_id', ''))
        if not plant_id:
            continue

        # Skip if we already saw this generator (we sorted by period desc, so first = newest)
        dedup_key = f"{plant_id}:{gen_id}"
        if dedup_key in seen_generators:
            continue
        seen_generators.add(dedup_key)

        # Try multiple possible field names for coordinates
        plant_lat = _safe_float(row.get('latitude') or row.get('lat') or row.get('plantLatitude'))
        plant_lng = _safe_float(row.get('longitude') or row.get('lon') or row.get('long') or row.get('plantLongitude'))
        capacity = _safe_float(row.get('nameplate-capacity-mw', 0))

        if capacity < min_mw:
            continue

        if plant_lat and plant_lng:
            dist = _haversine(lat, lng, plant_lat, plant_lng)
            if dist > radius:
                continue
        else:
            dist = None

        # Aggregate by plant (each generator counted once due to dedup above)
        if plant_id not in plants:
            plants[plant_id] = {
                'plant_id': plant_id,
                'plant_name': row.get('plantName', row.get('plant_name', '')),
                'latitude': plant_lat,
                'longitude': plant_lng,
                'state': row.get('stateid', state),
                'county': row.get('county', ''),
                'distance_miles': round(dist, 1) if dist else None,
                'total_capacity_mw': 0,
                'fuel_types': set(),
                'prime_movers': set(),
                'generators': 0,
                'status': row.get('status', ''),
                'balancing_authority': row.get('balancing_authority_code', ''),
            }
        plants[plant_id]['total_capacity_mw'] += capacity
        plants[plant_id]['fuel_types'].add(row.get('energy_source_code', row.get('energy-source-code', '')))
        plants[plant_id]['prime_movers'].add(row.get('prime_mover_code', row.get('prime-mover-code', '')))
        plants[plant_id]['generators'] += 1
        # Update lat/lng if we got it for this row but not previous
        if plant_lat and not plants[plant_id].get('latitude'):
            plants[plant_id]['latitude'] = plant_lat
            plants[plant_id]['longitude'] = plant_lng

    # Convert sets to lists, sort by distance
    result_list = []
    for p in plants.values():
        p['fuel_types'] = sorted(list(p['fuel_types'] - {''}))
        p['prime_movers'] = sorted(list(p['prime_movers'] - {''}))
        p['total_capacity_mw'] = round(p['total_capacity_mw'], 1)
        result_list.append(p)

    result_list.sort(key=lambda x: x.get('distance_miles') or 9999)
    result_list = result_list[:limit]

    result = {
        'success': True,
        'source': 'EIA Form 860 (Operating Generator Capacity)',
        'query': {'lat': lat, 'lng': lng, 'radius_miles': radius, 'state': state, 'fuel': fuel or 'all'},
        'total_plants': len(result_list),
        'total_capacity_mw': round(sum(p['total_capacity_mw'] for p in result_list), 1),
        'plants': result_list,
        'queried_at': datetime.utcnow().isoformat()
    }

    _set_cached(cache_key, result)
    return jsonify(result)


@power_plant_bp.route('/api/v1/power-plants/detail/<plant_id>')
def power_plant_detail(plant_id):
    """Get detailed info for a specific EIA plant ID."""
    cache_key = f"eia_plant_detail_{plant_id}"
    cached = _get_cached(cache_key)
    if cached:
        return jsonify(cached)

    params = {
        'frequency': 'monthly',
        'data[0]': 'nameplate-capacity-mw',
        'facets[plantid][]': plant_id,
        'sort[0][column]': 'period',
        'sort[0][direction]': 'desc',
        'length': 100,
    }

    data = _eia_request('electricity/operating-generator-capacity/data/', params)
    if 'error' in data:
        # Fallback: try plantCode facet name
        params2 = dict(params)
        del params2['facets[plantid][]']
        params2['facets[plantCode][]'] = plant_id
        data = _eia_request('electricity/operating-generator-capacity/data/', params2)
        if 'error' in data:
            return jsonify({'success': False, 'error': data['error'], 'detail': data.get('detail', '')}), 500

    rows = data.get('response', {}).get('data', [])
    if not rows:
        return jsonify({'success': False, 'error': f'No data found for plant ID {plant_id}'}), 404

    first = rows[0]
    generators = []
    total_mw = 0

    for row in rows:
        cap = _safe_float(row.get('nameplate-capacity-mw', 0))
        total_mw += cap
        generators.append({
            'generator_id': row.get('generatorid', row.get('generator_id', '')),
            'capacity_mw': cap,
            'fuel_type': row.get('energy_source_code', row.get('energy-source-code', '')),
            'prime_mover': row.get('prime_mover_code', row.get('prime-mover-code', '')),
            'status': row.get('status', ''),
            'operating_month': row.get('operating_year_month', ''),
            'technology': row.get('technology', ''),
        })

    result = {
        'success': True,
        'source': 'EIA Form 860',
        'plant': {
            'plant_id': plant_id,
            'plant_name': first.get('plantName', first.get('plant_name', '')),
            'latitude': _safe_float(first.get('latitude')),
            'longitude': _safe_float(first.get('longitude')),
            'state': first.get('stateid', ''),
            'county': first.get('county', ''),
            'balancing_authority': first.get('balancing_authority_code', ''),
            'sector': first.get('sector', first.get('sector_name', '')),
            'total_capacity_mw': round(total_mw, 1),
            'generator_count': len(generators),
            'generators': generators,
        },
        'queried_at': datetime.utcnow().isoformat()
    }

    _set_cached(cache_key, result)
    return jsonify(result)


# =============================================================================
# EIA Form 923 — Generation & Fuel Consumption
# =============================================================================

@power_plant_bp.route('/api/v1/power-plants/generation')
def power_plant_generation():
    """Get generation and fuel consumption data for plants (Form 923 / facility-fuel).

    Query params:
        state (str): State code (required, e.g. 'AZ', 'VA')
        fuel (str): Filter by fuel type
        year (int): Year (default: most recent)
        limit (int): Max results (default: 50)
    """
    state = request.args.get('state', '').upper()
    if not state or len(state) != 2:
        return jsonify({'error': 'state parameter required (2-letter code)'}), 400

    fuel = request.args.get('fuel', '')
    year = request.args.get('year', '', type=str)
    limit = min(request.args.get('limit', 50, type=int), 200)

    cache_key = f"eia_generation_{state}_{fuel}_{year}"
    cached = _get_cached(cache_key)
    if cached:
        return jsonify(cached)

    params = {
        'frequency': 'annual',
        'data[0]': 'generation',
        'facets[stateid][]': state,
        'sort[0][column]': 'generation',
        'sort[0][direction]': 'desc',
        'length': limit,
    }
    if fuel:
        params['facets[fueltypeid][]'] = fuel

    data = _eia_request('electricity/facility-fuel/data/', params)
    if 'error' in data:
        # Fallback: try electric-power-operational-data route
        params2 = {
            'frequency': 'annual',
            'data[0]': 'generation',
            'facets[location][]': state,
            'sort[0][column]': 'generation',
            'sort[0][direction]': 'desc',
            'length': limit,
        }
        if fuel:
            params2['facets[fueltypeid][]'] = fuel
        data = _eia_request('electricity/electric-power-operational-data/data/', params2)
        if 'error' in data:
            return jsonify({'success': False, 'error': data['error'], 'detail': data.get('detail', '')}), 500

    plants = []
    for row in data.get('response', {}).get('data', []):
        plants.append({
            'plant_id': row.get('plantCode', row.get('plantid', '')),
            'plant_name': row.get('plantName', row.get('plant_name', '')),
            'state': row.get('stateid', state),
            'fuel_type': row.get('fuel2002', row.get('fueltypeid', '')),
            'fuel_name': row.get('fuelTypeDescription', ''),
            'generation_mwh': _safe_float(row.get('generation', 0)),
            'consumption_btu': _safe_float(row.get('total-consumption-btu', 0)),
            'period': row.get('period', ''),
        })

    result = {
        'success': True,
        'source': 'EIA Form 923 (Facility Fuel)',
        'query': {'state': state, 'fuel': fuel or 'all'},
        'total_plants': len(plants),
        'total_generation_mwh': round(sum(p['generation_mwh'] for p in plants), 0),
        'plants': plants,
        'queried_at': datetime.utcnow().isoformat()
    }

    _set_cached(cache_key, result)
    return jsonify(result)


# =============================================================================
# EPA ECHO — Discharge Permits (NPDES)
# =============================================================================

@power_plant_bp.route('/api/v1/power-plants/permits')
def discharge_permits_nearby():
    """Find EPA ECHO facilities with NPDES discharge permits near coordinates.

    Query params:
        lat (float): Latitude (required)
        lng (float): Longitude (required)
        radius (float): Search radius in miles (default: 10)
        limit (int): Max results (default: 25)
    """
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if lat is None or lng is None:
        return jsonify({'error': 'lat and lng parameters required'}), 400

    radius = request.args.get('radius', 10, type=float)
    limit = min(request.args.get('limit', 25, type=int), 100)

    cache_key = f"echo_permits_{lat:.2f}_{lng:.2f}_{radius}"
    cached = _get_cached(cache_key)
    if cached:
        return jsonify(cached)

    # EPA ECHO CWA facility search — use get_facility_info (self-contained endpoint)
    params = {
        'output': 'JSON',
        'p_lat': str(lat),
        'p_long': str(lng),
        'p_radius': str(radius),
        'responseset': str(limit),
    }

    data = _echo_request('cwa_rest_services.get_facility_info', params)
    if 'error' in data:
        # Try alternate base URL format
        import urllib.request, urllib.parse, urllib.error
        alt_url = f"https://echo.epa.gov/api/cwa_rest_services.get_facility_info?{urllib.parse.urlencode(params)}"
        logger.info(f"ECHO fallback: trying {alt_url[:200]}")
        try:
            req = urllib.request.Request(alt_url, headers={'User-Agent': 'DCHub/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except Exception as e2:
            return jsonify({'success': False, 'error': str(data.get('error', '')), 'fallback_error': str(e2), 'note': 'EPA ECHO API may be temporarily unavailable'}), 502

    facilities = []
    # Parse ECHO response — get_facility_info returns nested Results.Facilities
    results = data.get('Results', data.get('results', data))
    facility_list = results.get('Facilities', results.get('FacilityInfo', results.get('ClusterOutput', [])))

    # If we got a cluster response, look for individual facilities
    if not isinstance(facility_list, list):
        facility_list = []

    logger.info(f"ECHO returned {len(facility_list)} facilities")

    if isinstance(facility_list, list):
        for fac in facility_list:
            fac_lat = _safe_float(fac.get('FacLat', fac.get('Latitude')))
            fac_lng = _safe_float(fac.get('FacLong', fac.get('Longitude')))
            dist = _haversine(lat, lng, fac_lat, fac_lng) if fac_lat and fac_lng else None

            facilities.append({
                'registry_id': fac.get('RegistryID', fac.get('FacFRSID', '')),
                'npdes_id': fac.get('CWPSourceID', fac.get('SourceID', fac.get('CWPPermitNumber', ''))),
                'facility_name': fac.get('FacName', fac.get('CWPName', '')),
                'address': fac.get('FacStreet', ''),
                'city': fac.get('FacCity', ''),
                'state': fac.get('FacState', ''),
                'zip': fac.get('FacZip', ''),
                'latitude': fac_lat,
                'longitude': fac_lng,
                'distance_miles': round(dist, 1) if dist else None,
                'permit_status': fac.get('CWPPermitStatusDesc', fac.get('PermitStatus', '')),
                'permit_type': fac.get('CWPPermitTypeDesc', ''),
                'facility_type': fac.get('CWPFacilityTypeIndicator', ''),
                'sic_code': fac.get('CWPSICCodes', fac.get('SICCodes', '')),
                'naics_code': fac.get('CWPNAICCodes', fac.get('NAICSCodes', '')),
                'compliance_status': fac.get('CWPSNCStatus', fac.get('ComplianceStatus', '')),
                'major_minor': fac.get('CWPMajorMinorStatusFlag', ''),
                'receiving_water': fac.get('CWPReceivingWaters', ''),
                'total_penalties': fac.get('CWPTotalPenalties', ''),
                'last_inspection': fac.get('CWPLastInspDate', ''),
                'is_power_plant': _is_power_sic(fac.get('CWPSICCodes', '')),
            })

    facilities.sort(key=lambda x: x.get('distance_miles') or 9999)

    result = {
        'success': True,
        'source': 'EPA ECHO (NPDES Discharge Permits)',
        'query': {'lat': lat, 'lng': lng, 'radius_miles': radius},
        'total_facilities': len(facilities),
        'power_plants': sum(1 for f in facilities if f.get('is_power_plant')),
        'facilities': facilities,
        'queried_at': datetime.utcnow().isoformat()
    }

    _set_cached(cache_key, result)
    return jsonify(result)


# =============================================================================
# Combined Water Risk Assessment
# =============================================================================

@power_plant_bp.route('/api/v1/power-plants/water-risk')
def water_risk_assessment():
    """Combined water risk assessment for a location — power plants + discharge permits.

    Query params:
        lat (float): Latitude (required)
        lng (float): Longitude (required)
        radius (float): Search radius in miles (default: 15)
    """
    lat = request.args.get('lat', type=float)
    lng = request.args.get('lng', type=float)
    if lat is None or lng is None:
        return jsonify({'error': 'lat and lng parameters required'}), 400

    radius = request.args.get('radius', 15, type=float)

    cache_key = f"water_risk_{lat:.2f}_{lng:.2f}_{radius}"
    cached = _get_cached(cache_key)
    if cached:
        return jsonify(cached)

    # Get power plants nearby
    state = _coords_to_state(lat, lng)
    plants_data = []
    if state and EIA_API_KEY:
        params = {
            'frequency': 'monthly',
            'data[0]': 'nameplate-capacity-mw',
            'facets[stateid][]': state,
            'facets[status][]': 'OP',
            'sort[0][column]': 'nameplate-capacity-mw',
            'sort[0][direction]': 'desc',
            'length': 2000,
        }
        eia_data = _eia_request('electricity/operating-generator-capacity/data/', params)
        for row in eia_data.get('response', {}).get('data', []):
            plant_lat = _safe_float(row.get('latitude'))
            plant_lng = _safe_float(row.get('longitude'))
            if plant_lat and plant_lng:
                dist = _haversine(lat, lng, plant_lat, plant_lng)
                if dist <= radius:
                    plants_data.append({
                        'plant_id': row.get('plantid', row.get('plantCode', '')),
                        'plant_name': row.get('plantName', ''),
                        'capacity_mw': _safe_float(row.get('nameplate-capacity-mw', 0)),
                        'fuel': row.get('energy_source_code', ''),
                        'latitude': plant_lat,
                        'longitude': plant_lng,
                        'distance_miles': round(dist, 1),
                    })

    # Get NPDES permits nearby
    permits_data = []
    echo_params = {
        'output': 'JSON',
        'p_lat': str(lat),
        'p_long': str(lng),
        'p_radius': str(radius),
        'responseset': '50',
    }
    echo_data = _echo_request('cwa_rest_services.get_facility_info', echo_params)
    results = echo_data.get('Results', echo_data.get('results', echo_data))
    fac_list = results.get('Facilities', results.get('FacilityInfo', []))
    if not isinstance(fac_list, list):
        fac_list = []
    for fac in fac_list:
        fac_lat = _safe_float(fac.get('FacLat'))
        fac_lng = _safe_float(fac.get('FacLong'))
        permits_data.append({
            'npdes_id': fac.get('CWPSourceID', ''),
            'name': fac.get('FacName', ''),
            'receiving_water': fac.get('CWPReceivingWaters', ''),
            'permit_status': fac.get('CWPPermitStatusDesc', ''),
            'compliance': fac.get('CWPSNCStatus', ''),
            'is_power_plant': _is_power_sic(fac.get('CWPSICCodes', '')),
            'distance_miles': round(_haversine(lat, lng, fac_lat, fac_lng), 1) if fac_lat and fac_lng else None,
        })

    # Calculate risk score
    water_using_plants = [p for p in plants_data if p.get('fuel') in ('NUC', 'COL', 'NG', 'BIT', 'SUB', 'LIG', 'PC')]
    total_water_mw = sum(p.get('capacity_mw', 0) for p in water_using_plants)
    discharge_count = len(permits_data)
    noncompliant = sum(1 for p in permits_data if 'SNC' in str(p.get('compliance', '')).upper())

    risk_score = min(100, int(
        (min(total_water_mw / 500, 30)) +  # Up to 30 pts for water-cooled capacity
        (min(discharge_count * 3, 30)) +     # Up to 30 pts for discharge permits
        (noncompliant * 10) +                 # 10 pts per noncompliant facility
        (min(len(water_using_plants) * 2, 20))  # Up to 20 pts for number of plants
    ))

    risk_level = 'low' if risk_score < 25 else 'moderate' if risk_score < 50 else 'high' if risk_score < 75 else 'critical'

    result = {
        'success': True,
        'source': 'EIA Form 860 + EPA ECHO Combined',
        'location': {'lat': lat, 'lng': lng, 'state': state, 'radius_miles': radius},
        'water_risk': {
            'score': risk_score,
            'level': risk_level,
            'factors': {
                'water_cooled_plants': len(water_using_plants),
                'water_cooled_capacity_mw': round(total_water_mw, 1),
                'discharge_permits_nearby': discharge_count,
                'noncompliant_facilities': noncompliant,
                'total_power_plants': len(plants_data),
            }
        },
        'power_plants': plants_data[:20],
        'discharge_permits': permits_data[:20],
        'queried_at': datetime.utcnow().isoformat()
    }

    _set_cached(cache_key, result)
    return jsonify(result)


# =============================================================================
# Summary / Info
# =============================================================================

@power_plant_bp.route('/api/v1/power-plants/summary')
def power_plant_summary():
    """Power Plant Intelligence API summary and available endpoints."""
    return jsonify({
        'success': True,
        'module': 'Power Plant Intelligence',
        'version': '1.0',
        'sources': {
            'eia_form_860': {
                'name': 'EIA Form 860 — Annual Electric Generator Report',
                'description': 'Plant-level data including coordinates, capacity, fuel type, prime mover, cooling systems, water source',
                'api': 'https://api.eia.gov/v2/electricity/operating-generator-capacity/',
                'update_frequency': 'Monthly (EIA-860M supplement)',
                'requires_key': True,
                'key_configured': bool(EIA_API_KEY),
            },
            'eia_form_923': {
                'name': 'EIA Form 923 — Power Plant Operations Report',
                'description': 'Generation, fuel consumption, and operational data by plant and fuel type',
                'api': 'https://api.eia.gov/v2/electricity/facility-fuel/',
                'update_frequency': 'Monthly/Annual',
                'requires_key': True,
                'key_configured': bool(EIA_API_KEY),
            },
            'epa_echo': {
                'name': 'EPA ECHO — Enforcement & Compliance History Online',
                'description': 'NPDES discharge permits, compliance status, inspection history, receiving waters',
                'api': 'https://echo.epa.gov/tools/web-services',
                'update_frequency': 'Weekly',
                'requires_key': False,
            },
        },
        'endpoints': {
            '/api/v1/power-plants/nearby': 'Power plants near coordinates (EIA 860)',
            '/api/v1/power-plants/detail/<plant_id>': 'Single plant detail by EIA plant ID',
            '/api/v1/power-plants/generation': 'Generation & fuel data by state (EIA 923)',
            '/api/v1/power-plants/permits': 'EPA ECHO discharge permits nearby',
            '/api/v1/power-plants/water-risk': 'Combined water risk assessment',
            '/api/v1/power-plants/summary': 'This endpoint — API info',
        },
        'data_center_relevance': {
            'water_cooling': 'Identifies water-cooled plants that compete for water resources near DC sites',
            'discharge_permits': 'Flags environmental compliance risks and water-adjacent infrastructure',
            'capacity_analysis': 'Shows available generation capacity and fuel diversity near sites',
            'risk_scoring': 'Combined water risk score for site evaluation and due diligence',
        }
    })


# =============================================================================
# Utility Functions
# =============================================================================

def _safe_float(val):
    """Safely convert value to float."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _haversine(lat1, lng1, lat2, lng2):
    """Calculate distance in miles between two lat/lng points."""
    import math
    R = 3959  # Earth radius in miles
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _is_power_sic(sic_codes):
    """Check if SIC codes indicate a power plant."""
    if not sic_codes:
        return False
    power_sics = {'4911', '4931', '4932', '4939', '4941', '4953'}
    codes = str(sic_codes).replace(',', ' ').split()
    return bool(set(codes) & power_sics)


# State bounding boxes for coordinate-to-state mapping
_STATE_BOUNDS = {
    'AL': (30.2, -88.5, 35.0, -84.9), 'AK': (51.2, -179.1, 71.4, -129.9),
    'AZ': (31.3, -114.8, 37.0, -109.0), 'AR': (33.0, -94.6, 36.5, -89.6),
    'CA': (32.5, -124.4, 42.0, -114.1), 'CO': (37.0, -109.1, 41.0, -102.0),
    'CT': (41.0, -73.7, 42.1, -71.8), 'DE': (38.5, -75.8, 39.8, -75.0),
    'FL': (24.5, -87.6, 31.0, -80.0), 'GA': (30.4, -85.6, 35.0, -80.8),
    'HI': (18.9, -160.2, 22.2, -154.8), 'ID': (42.0, -117.2, 49.0, -111.0),
    'IL': (37.0, -91.5, 42.5, -87.0), 'IN': (37.8, -88.1, 41.8, -84.8),
    'IA': (40.4, -96.6, 43.5, -90.1), 'KS': (37.0, -102.1, 40.0, -94.6),
    'KY': (36.5, -89.6, 39.1, -82.0), 'LA': (29.0, -94.0, 33.0, -89.0),
    'ME': (43.1, -71.1, 47.5, -66.9), 'MD': (37.9, -79.5, 39.7, -75.0),
    'MA': (41.2, -73.5, 42.9, -69.9), 'MI': (41.7, -90.4, 48.3, -82.1),
    'MN': (43.5, -97.2, 49.4, -89.5), 'MS': (30.2, -91.7, 35.0, -88.1),
    'MO': (36.0, -95.8, 40.6, -89.1), 'MT': (44.4, -116.0, 49.0, -104.0),
    'NE': (40.0, -104.1, 43.0, -95.3), 'NV': (35.0, -120.0, 42.0, -114.0),
    'NH': (42.7, -72.6, 45.3, -71.0), 'NJ': (38.9, -75.6, 41.4, -74.0),
    'NM': (31.3, -109.1, 37.0, -103.0), 'NY': (40.5, -79.8, 45.0, -71.9),
    'NC': (33.8, -84.3, 36.6, -75.5), 'ND': (45.9, -104.0, 49.0, -96.6),
    'OH': (38.4, -84.8, 42.0, -80.5), 'OK': (33.6, -103.0, 37.0, -94.4),
    'OR': (42.0, -124.6, 46.3, -116.5), 'PA': (39.7, -80.5, 42.3, -74.7),
    'RI': (41.1, -71.9, 42.0, -71.1), 'SC': (32.0, -83.4, 35.2, -78.5),
    'SD': (42.5, -104.1, 46.0, -96.4), 'TN': (35.0, -90.3, 36.7, -81.6),
    'TX': (25.8, -106.6, 36.5, -93.5), 'UT': (37.0, -114.1, 42.0, -109.0),
    'VT': (42.7, -73.4, 45.0, -71.5), 'VA': (36.5, -83.7, 39.5, -75.2),
    'WA': (45.5, -124.8, 49.0, -116.9), 'WV': (37.2, -82.6, 40.6, -77.7),
    'WI': (42.5, -92.9, 47.1, -86.3), 'WY': (41.0, -111.1, 45.0, -104.1),
    'DC': (38.8, -77.1, 39.0, -76.9),
}


def _coords_to_state(lat, lng):
    """Rough coordinate-to-state mapping using bounding boxes."""
    best = None
    best_dist = float('inf')
    for state, (min_lat, min_lng, max_lat, max_lng) in _STATE_BOUNDS.items():
        if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
            # Center distance for tie-breaking
            center_lat = (min_lat + max_lat) / 2
            center_lng = (min_lng + max_lng) / 2
            dist = abs(lat - center_lat) + abs(lng - center_lng)
            if dist < best_dist:
                best_dist = dist
                best = state
    return best


# =============================================================================
# Blueprint Registration Helper
# =============================================================================

def register_power_plant_intel(app):
    """Register the power plant intelligence blueprint."""
    app.register_blueprint(power_plant_bp)
    logger.info("⚡ Power Plant Intelligence: ✅ Registered")
    logger.info("   📍 /api/v1/power-plants/nearby, /detail, /generation, /permits, /water-risk")
    logger.info("   📊 Sources: EIA Form 860/923 + EPA ECHO (NPDES)")
    if not EIA_API_KEY:
        logger.warning("   ⚠️ EIA_API_KEY not set — EIA endpoints will return errors")
