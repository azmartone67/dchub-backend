"""
DC Hub Energy Infrastructure API Routes v3
==========================================
Enhanced with:
- Fixed pipeline scoring (case-insensitive matching)
- Point-to-line distance for accurate pipeline proximity
- More comprehensive recommendations
- Tallgrass/REX pipeline detection
- Power plant proximity scoring
- The routes that proxied live ArcGIS layers were retired 2026-09-13 (410)

INSTALLATION:
1. Replace existing energy_infrastructure_routes.py in Replit
2. Restart the server
"""

from flask import request, jsonify
import copy
import re
import time
from math import cos, radians, sin, sqrt, atan2, inf
from internal_auth import get_internal_key_for_client
from utils.pipeline_alias import expand_query, matches_any  # phase32_alias_normalize
# ============================================================================
# Phase 34 — alias-wired query
# ============================================================================
# Where SQL queries match the new "WHERE LOWER(col) = ANY(%s)" pattern,
# the parameter must now be passed as a tuple of lowercase candidates:
#
#     from utils.pipeline_alias import expand_query
#     candidates = tuple(c.lower() for c in expand_query(query))
#     cur.execute(sql, (candidates,))
#
# This makes 'amazon' match AWS rows, 'google' match GCP, etc.
# ============================================================================

# =============================================================================
# CONFIGURATION
# =============================================================================# =============================================================================
# CONFIGURATION
# =============================================================================

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

# The four layers the score reads. A layer whose read failed is UNMEASURED: it
# scores no points, it is not scored as zero, and every score that depends on it
# is null. Before 2026-09-22 a failed or empty read scored 0 and the site still got
# the 30-point base: Ashburn, VA, with no data at all, was published as 30
# "Challenging".
LAYERS = ('substations', 'transmissionLines', 'powerPlants', 'pipelines')
_POWER_LAYERS = ('substations', 'transmissionLines', 'powerPlants')


def _distance_m(lat, lng, item):
    """Metres from (lat, lng) to one layer item, or inf when it has no position.

    An item carries a measured `distance_km`, a point (geometry x/y, or
    coordinates), or a polyline (geometry paths). Pipelines and plants arrive as
    points: before 2026-09-22 the pipeline distance read only `paths`, so no
    pipeline was ever found and gasScore was 0 wherever pipelines were mapped."""
    try:
        if item.get('distance_km') is not None:
            return float(item['distance_km']) * 1000
        geom = item.get('geometry') or {}
        if geom.get('x') is not None and geom.get('y') is not None:
            return haversine_distance(lat, lng, float(geom['y']), float(geom['x']))
        if geom.get('coordinates'):
            x, y = geom['coordinates'][:2]
            return haversine_distance(lat, lng, float(y), float(x))
        best = inf
        for path in geom.get('paths') or []:
            best = min(best, point_to_line_distance(lat, lng, path))
        return best
    except (TypeError, ValueError, KeyError, IndexError, AttributeError):
        return inf


def _nearest(lat, lng, items):
    """(metres, item) for the nearest positioned item, or (inf, None)."""
    best, best_item = inf, None
    for item in items or []:
        dist = _distance_m(lat, lng, item)
        if dist < best:
            best, best_item = dist, item
    return best, best_item


def calculate_infrastructure_score(lat, lng, substations, pipelines, transmission_lines,
                                   power_plants, unavailable=None):
    """
    Infrastructure access score (0-100) from proximity to power and gas.

    `unavailable` maps a layer in LAYERS to the reason it could not be read.
    powerScore is null when any power layer is unavailable, gasScore when the
    pipelines are, and overallScore and rating when either is. A layer that was
    read and holds nothing within the radius is a measurement, and scores 0.
    """
    unavailable = {k: v for k, v in (unavailable or {}).items() if k in LAYERS}
    base_score = 30  # Base score
    recommendations = []
    details = {}

    # ===================
    # POWER SCORE (max 50)
    # ===================
    power_score = 0

    # Substations (max 25 pts)
    if 'substations' not in unavailable:
        dist, sub = _nearest(lat, lng, substations)
        if dist < inf:
            sub_dist_km = dist / 1000
            details['nearestSubstationKm'] = round(sub_dist_km, 1)
            details['nearestSubstationName'] = (sub.get('attributes') or {}).get('NAME', 'Unknown')

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
        else:
            recommendations.append("ℹ️ No mapped substation within the search radius")

    # Transmission lines (max 15 pts)
    if 'transmissionLines' not in unavailable:
        dist, line = _nearest(lat, lng, transmission_lines)
        if dist < inf:
            line_dist_km = dist / 1000
            details['nearestTransmissionKm'] = round(line_dist_km, 1)
            details['nearestTransmissionVoltage'] = (line.get('attributes') or {}).get('VOLTAGE')

            if line_dist_km < 2:
                power_score += 15
            elif line_dist_km < 5:
                power_score += 12
            elif line_dist_km < 10:
                power_score += 8
            elif line_dist_km < 20:
                power_score += 4

    # Power plants (max 10 pts)
    if 'powerPlants' not in unavailable:
        dist, plant = _nearest(lat, lng, power_plants)
        if dist < inf:
            plant_dist_km = dist / 1000
            details['nearestPowerPlantKm'] = round(plant_dist_km, 1)
            attrs = plant.get('attributes') or {}
            details['nearestPowerPlantName'] = attrs.get('NAME', 'Unknown')
            details['nearestPowerPlantMW'] = attrs.get('TOTAL_MW')
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

    if 'pipelines' in unavailable:
        pass
    elif pipelines:
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

        details['pipelineOperators'] = sorted(pipeline_operators)[:5]
        details['hasTallgrass'] = has_tallgrass
        details['hasInterstate'] = has_interstate

        dist, nearest_pipe = _nearest(lat, lng, gas_pipelines)
        if dist < inf:
            pipe_dist_km = dist / 1000
            details['nearestPipelineKm'] = round(pipe_dist_km, 1)
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
            recommendations.append("ℹ️ No gas pipelines found in search area")
    else:
        recommendations.append("ℹ️ No active gas pipeline mapped within the search radius")

    for layer, reason in unavailable.items():
        recommendations.append(f"ℹ️ {layer} could not be read, so it is not scored: {reason}")

    # ===================
    # OVERALL SCORE
    # ===================
    power_measured = not any(layer in unavailable for layer in _POWER_LAYERS)
    gas_measured = 'pipelines' not in unavailable
    overall_score = rating = None
    if power_measured and gas_measured:
        # Weight: 50% power, 50% gas
        overall_score = int(base_score + (power_score * 0.5) + (gas_score * 0.5))
        overall_score = min(100, max(0, overall_score))

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
        'powerScore': power_score if power_measured else None,
        'gasScore': gas_score if gas_measured else None,
        'recommendations': recommendations,
        'details': details,
        'unavailable': unavailable,
    }


# =============================================================================
# SITE ANALYSIS: WHAT IS READ, AND FROM WHERE
# =============================================================================

# `radius` arrives in two units. js/land-power-enhancements.js sends metres (50000,
# the documented unit) and so does the legacy Python MCP server (miles * 1609);
# js/site-scoring-integration.js, js/energy-patch.js and
# js/land-power-button-fixes.js send kilometres (50 or 25). Read as metres,
# radius=25 searched a 50 m box: Ashburn, VA came back with every count 0 on
# 2026-09-22, and 148 substations at radius=25000. No site search is under 1 km
# or over 1,000 km, so a radius of 1,000 or more is metres and a smaller one is
# kilometres. `radius_km` is always kilometres.
RADIUS_KM_DEFAULT = 25.0
RADIUS_KM_MIN = 1.0
RADIUS_KM_MAX = 100.0
_RADIUS_METRES_FROM = 1000.0

# Rows read per layer, nearest first. A layer that fills it publishes its count
# as a floor (coverage.<layer>.count_is_floor). The lists served are shorter.
_ROW_CAP = 5000
_KM_PER_MILE = 1.609344

_SUBSTATIONS_SQL = """
    SELECT name, city, state, zip, type, status, owner, max_volt, min_volt, lat, lng
      FROM substations
     WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
     ORDER BY POWER(lat - %s, 2) + POWER((lng - %s) * %s, 2)
     LIMIT %s
"""
_PIPELINES_SQL = """
    SELECT name, operator, pipeline_type, diameter_inches, status, lat, lng
      FROM gas_pipelines
     WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
       AND status = 'active'
     ORDER BY POWER(lat - %s, 2) + POWER((lng - %s) * %s, 2)
     LIMIT %s
"""
# The EIA plant fleet (routes/power_plants_ingest.py, the Land & Power plants
# layer). discovered_power_plants holds source RECORDS from three overlapping
# catalogs, not plants, and some sit at the wrong place: on 2026-09-22 it put
# Surry Nuclear 19 km from Ashburn (the plant is in Surry County, ~250 km away),
# and it filled the old LIMIT of 100 within 25 km, so the count was the cap.
_PLANTS_SQL = """
    SELECT name, utility_name, nameplate_capacity_mw, primary_fuel, lat, lng
      FROM power_plants_eia
     WHERE lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s
     ORDER BY POWER(lat - %s, 2) + POWER((lng - %s) * %s, 2)
     LIMIT %s
"""

_SOURCES = {
    'substations': 'substations',
    'pipelines': "gas_pipelines (status 'active'), one mapped point per segment",
    'powerPlants': 'power_plants_eia (the EIA plant fleet)',
    'transmissionLines': ('transmission_lines, through '
                          'site_planner.find_nearest_transmission_measured'),
}
_TRANSMISSION_COUNT_REASON = (
    'not counted: transmission_lines stores no line geometry, so this endpoint '
    'measures only the nearest line, placed at the substation its endpoint names')


def resolve_radius_km(args):
    """(radius_km, how the request's radius was read). See RADIUS_KM_DEFAULT."""
    explicit = args.get('radius_km', type=float)
    raw = args.get('radius', type=float)
    if explicit is not None:
        given, read_as, km = explicit, 'km', explicit
    elif raw is None:
        given, read_as, km = None, 'default', RADIUS_KM_DEFAULT
    elif raw >= _RADIUS_METRES_FROM:
        given, read_as, km = raw, 'm', raw / 1000.0
    else:
        given, read_as, km = raw, 'km', raw
    radius_km = min(RADIUS_KM_MAX, max(RADIUS_KM_MIN, km))
    return radius_km, {'given': given, 'read_as': read_as,
                       'clamped': not (radius_km == km)}


def _num(value):
    """A column value as a float, or None. A NUMERIC column arrives as Decimal,
    which jsonify would serve as a string."""
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _within(rows, lat, lng, radius_km, lat_i, lng_i):
    """[(row, metres)] for the rows inside the radius, nearest first."""
    out = []
    for row in rows:
        try:
            dist = haversine_distance(lat, lng, float(row[lat_i]), float(row[lng_i]))
        except (TypeError, ValueError):
            continue
        if dist <= radius_km * 1000:
            out.append((row, dist))
    out.sort(key=lambda pair: pair[1])
    return out


def _read_transmission(lat, lng, radius_km):
    """([nearest line as a layer item] or [], None) when measured, else (None, reason)."""
    try:
        from site_planner import find_nearest_transmission_measured
        line, measured = find_nearest_transmission_measured(
            lat, lng, max_distance_miles=radius_km / _KM_PER_MILE)
    except Exception as e:  # noqa: BLE001 — reported as the layer's reason
        return None, f'the nearest-line lookup raised {type(e).__name__}'
    if not measured:
        return None, 'the nearest-line lookup could not run (a query error or no connection)'
    if not line or line.get('distance_miles') is None:
        return [], None
    km = float(line['distance_miles']) * _KM_PER_MILE
    if km > radius_km:
        return [], None
    return [{
        'distance_km': round(km, 2),
        'attributes': {
            'NAME': line.get('line_name'), 'VOLTAGE': _num(line.get('voltage_kv')),
            'OWNER': line.get('owner'), 'STATUS': line.get('status'),
            'SUBSTATION': line.get('matched_substation'),
        },
    }], None


def read_site_layers(lat, lng, radius_km):
    """Every layer the score reads, within radius_km of (lat, lng).

    Returns {layer: {'status': 'measured', 'items': [...], 'count': n,
    'count_is_floor': bool}} or {layer: {'status': 'unavailable', 'reason': ...}}.
    Each statement runs on its own: one that fails is rolled back, so it cannot
    abort the transaction the next one runs in, and marks only its own layer."""
    lat_delta = radius_km / 111.32
    lng_delta = radius_km / (111.32 * max(0.01, abs(cos(radians(lat)))))
    box = (lat - lat_delta, lat + lat_delta, lng - lng_delta, lng + lng_delta)
    order = (lat, lng, max(0.01, abs(cos(radians(lat)))))
    layers = {}

    def _item_substation(row):
        return {'geometry': {'x': _num(row[10]), 'y': _num(row[9])},
                'attributes': {'NAME': row[0] or 'Unknown', 'CITY': row[1], 'STATE': row[2],
                               'ZIP': row[3], 'TYPE': row[4], 'STATUS': row[5],
                               'OWNER': row[6], 'MAX_VOLT': _num(row[7]),
                               'MIN_VOLT': _num(row[8])}}

    def _item_pipeline(row):
        return {'geometry': {'x': _num(row[6]), 'y': _num(row[5])},
                'attributes': {'name': row[0], 'typepipe': row[2] or 'gas', 'operator': row[1],
                               'status': row[4], 'COMMODITY': 'gas', 'diameter': _num(row[3])}}

    def _item_plant(row):
        return {'geometry': {'x': _num(row[5]), 'y': _num(row[4])},
                'attributes': {'NAME': row[0] or 'Unknown', 'UTILITY_NA': row[1],
                               'TOTAL_MW': _num(row[2]), 'PRIM_FUEL': row[3] or 'Unknown',
                               'SOURCE': 'EIA'}}

    reads = (('substations', _SUBSTATIONS_SQL, 9, 10, _item_substation),
             ('pipelines', _PIPELINES_SQL, 5, 6, _item_pipeline),
             ('powerPlants', _PLANTS_SQL, 4, 5, _item_plant))

    conn = None
    put_back = None
    try:
        from main import get_pg_connection, return_pg_connection
        conn, put_back = get_pg_connection(), return_pg_connection
    except Exception as e:  # noqa: BLE001 — reported as each layer's reason
        conn = None
        no_conn = f'no database connection ({type(e).__name__})'
    else:
        no_conn = 'no database connection'
    try:
        for layer, sql, lat_i, lng_i, to_item in reads:
            if conn is None:
                layers[layer] = {'status': 'unavailable', 'reason': no_conn}
                continue
            try:
                cur = conn.cursor()
                try:
                    cur.execute(sql, box + order + (_ROW_CAP,))
                    rows = cur.fetchall()
                finally:
                    cur.close()
            except Exception as e:  # noqa: BLE001 — reported as the layer's reason
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                print(f"site-analysis {layer} read failed: {type(e).__name__}: {e}")
                layers[layer] = {'status': 'unavailable',
                                 'reason': f'the {layer} read failed ({type(e).__name__})'}
                continue
            near = _within(rows, lat, lng, radius_km, lat_i, lng_i)
            layers[layer] = {'status': 'measured', 'items': [to_item(r) for r, _ in near],
                             'count': len(near), 'count_is_floor': len(rows) >= _ROW_CAP}
    finally:
        if conn is not None:
            try:
                put_back(conn)
            except Exception:  # noqa: BLE001
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    items, reason = _read_transmission(lat, lng, radius_km)
    if reason:
        layers['transmissionLines'] = {'status': 'unavailable', 'reason': reason}
    else:
        layers['transmissionLines'] = {'status': 'measured', 'items': items, 'count': None,
                                       'count_is_floor': False}
    return layers


def build_site_analysis(lat, lng, radius_km, radius_read):
    """The full /api/v1/energy/site-analysis answer (Pro and above)."""
    layers = read_site_layers(lat, lng, radius_km)

    def _items(layer):
        return layers[layer].get('items') or []

    unavailable = {k: v['reason'] for k, v in layers.items() if v['status'] != 'measured'}
    score_data = calculate_infrastructure_score(
        lat, lng, _items('substations'), _items('pipelines'), _items('transmissionLines'),
        _items('powerPlants'), unavailable)

    counts, coverage = {}, {}
    for layer in LAYERS:
        info = layers[layer]
        entry = {'status': info['status'], 'source': _SOURCES[layer]}
        if info['status'] == 'measured':
            counts[layer] = info['count']
            entry['count_is_floor'] = info['count_is_floor']
            if layer == 'transmissionLines':
                entry['count'] = _TRANSMISSION_COUNT_REASON
                entry['basis'] = ('distance to the substation the nearest line is '
                                  'attributed to, not to the line itself')
            elif layer == 'pipelines':
                entry['basis'] = 'distance to the nearest mapped pipeline point'
        else:
            counts[layer] = None
            entry['reason'] = info['reason']
        coverage[layer] = entry

    plants = _items('powerPlants')
    formatted_plants = []
    for p in plants[:20]:
        attr = p.get('attributes', {})
        formatted_plants.append({
            'name': attr.get('NAME', 'Unknown'),
            'fuel_type': attr.get('PRIM_FUEL', 'Unknown'),
            'capacity_mw': attr.get('TOTAL_MW'),
            'generation_mwh': None,
            'capacity_factor': None,
            'status': None,
            'operator': attr.get('UTILITY_NA'),
            'source': attr.get('SOURCE', 'EIA'),
        })
    plants_read = layers['powerPlants']['status'] == 'measured'
    capacities = [(p.get('attributes') or {}).get('TOTAL_MW') for p in plants]
    total_capacity_mw = (round(sum(c for c in capacities if c is not None), 1)
                         if plants_read else None)

    return {
        'location': {'lat': lat, 'lng': lng},
        'radius': int(round(radius_km * 1000)),
        'radius_km': radius_km,
        'radius_read': radius_read,
        'scores': score_data,
        'counts': counts,
        'coverage': coverage,
        'infrastructure': {
            'substations': _items('substations')[:20],  # Limit for response size
            'pipelines': _items('pipelines')[:20],
            'transmissionLines': _items('transmissionLines')[:10],
            'powerPlants': plants[:10],
        },
        'power_infrastructure': {
            'plants': formatted_plants,
            'total_count': counts['powerPlants'],
            'total_capacity_mw': total_capacity_mw,
            # power_plants_eia carries nameplate capacity, not generation.
            'total_generation_mwh': None,
        },
    }


# Land & Power details are Pro (owner, 2026-09-22; util/plan_tease.lp_gate). Below
# Pro this route serves the grade, the counts and the names; every score,
# distance, voltage and MW figure is null and no asset list or coordinate is
# served. Keyless callers get the wall. Until 2026-09-22 a free key got the full
# scores beside a note that exact detail needed "a free key or sign-in".
_PREVIEW_LOCKED = (
    'scores.overallScore', 'scores.powerScore', 'scores.gasScore',
    'scores.recommendations', 'scores.details.nearestSubstationKm',
    'scores.details.nearestTransmissionKm', 'scores.details.nearestTransmissionVoltage',
    'scores.details.nearestPowerPlantKm', 'scores.details.nearestPowerPlantMW',
    'scores.details.nearestPipelineKm', 'infrastructure', 'power_infrastructure.plants',
    'power_infrastructure.total_capacity_mw', 'location',
)
_PREVIEW_NAMES = ('nearestSubstationName', 'nearestPowerPlantName', 'nearestPowerPlantFuel',
                  'nearestPipelineOperator', 'nearestPipelineType', 'hasInterstate',
                  'hasTallgrass')
_PREVIEW_CTA = ("Land & Power preview: the grade, the counts and the names. Scores, "
                "distances, voltages, capacities and asset locations come with Pro.")


def site_analysis_preview(full):
    """The below-Pro view of build_site_analysis(): (body, locked fields, rows)."""
    from util.plan_tease import round2, TEASE_ROWS
    scores = full.get('scores') or {}
    details = {k: (v if k in _PREVIEW_NAMES else None)
               for k, v in (scores.get('details') or {}).items()}
    if 'pipelineOperators' in (scores.get('details') or {}):
        details['pipelineOperators'] = list(scores['details']['pipelineOperators'])[:TEASE_ROWS]
    loc = full.get('location') or {}
    power = full.get('power_infrastructure') or {}
    data = {
        'location': {'lat': round2(loc.get('lat')), 'lng': round2(loc.get('lng'))},
        'radius': full.get('radius'),
        'radius_km': full.get('radius_km'),
        'radius_read': full.get('radius_read'),
        'scores': {
            'overallScore': None,
            'rating': scores.get('rating'),
            'powerScore': None,
            'gasScore': None,
            'recommendations': [],
            'details': details,
            'unavailable': scores.get('unavailable'),
        },
        'counts': full.get('counts'),
        'coverage': full.get('coverage'),
        'infrastructure': {k: [] for k in (full.get('infrastructure') or {})},
        'power_infrastructure': {
            'plants': [],
            'total_count': power.get('total_count'),
            'total_capacity_mw': None,
            'total_generation_mwh': None,
        },
        '_gated': True,
        '_upgrade_cta': _PREVIEW_CTA,
    }
    return {'success': True, 'data': data}, list(_PREVIEW_LOCKED), 1


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
        - radius: Search radius; 1,000 or more is metres, less is kilometres
          (default 25 km; see RADIUS_KM_DEFAULT)
        - radius_km: Search radius in kilometres (wins over radius)
        """
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)

        if lat is None or lng is None or not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            return jsonify({'success': False, 'error': 'lat and lng required'}), 400

        from util.plan_tease import lp_gate
        radius_km, radius_read = resolve_radius_km(request.args)
        cache_key = f"site-analysis:{lat:.4f}:{lng:.4f}:{radius_km:.3f}"

        def _analysis():
            """(result, cached). A result with an unread layer is not cached."""
            cached = get_cached(cache_key)
            if cached:
                return copy.deepcopy(cached), True
            result = build_site_analysis(lat, lng, radius_km, radius_read)
            if not result['scores']['unavailable']:
                set_cache(cache_key, copy.deepcopy(result))
            return result, False

        def _full():
            result, cached = _analysis()
            body = {'success': True, 'data': result}
            if cached:
                body['cached'] = True
            return jsonify(body)

        def _tease():
            return site_analysis_preview(_analysis()[0])

        # A keyless caller gets the wall before _full or _tease runs, so nothing
        # is read for it.
        return lp_gate(_full, _tease)
    
    # ── RETIRED 2026-09-13 ──────────────────────────────────────────────────
    # This route passed its bounding box to the DOT natural-gas pipelines layer
    # (geo.dot.gov Natural_Gas_Pipelines_US_EIA), which answered HTTP 500 with an
    # "Application Error" page on every probe on 2026-09-13, so the route could only
    # answer 500.
    #
    # Retired rather than repointed: dchub-frontend and dchub-mcp-server do not fetch
    # this path, and Railway logged no request to it in the seven days to 2026-09-13.
    # Pipelines near a point are already served from the gas_pipelines table, which
    # routes/gas_pipeline_ingest.py writes, by the route named in `instead`.
    @app.route('/api/v1/energy/pipelines', methods=['GET'])
    def get_pipelines():
        """RETIRED 2026-09-13: answers 410 before any network work (see the note above)."""
        return jsonify({
            'success': False,
            'retired': True,
            'retired_at': '2026-09-13',
            'error': 'route_retired',
            'reason': ('This route queried a DOT natural-gas pipelines service that answers '
                       'HTTP 500, so it could not return a pipeline.'),
            'instead': '/api/v2/infrastructure/hifld/gas-pipelines?lat=<lat>&lng=<lng>&radius=<miles>',
        }), 410
    
    # ── RETIRED 2026-09-13 ──────────────────────────────────────────────────
    # This route passed its bounding box to the Electric_Substations service on
    # services1.arcgis.com/Hp6G80Pky0om7QvQ, which no longer exists: ArcGIS answers
    # HTTP 200 with {"error":{"code":400,"message":"Invalid URL"}}, and that org's
    # 527-service directory lists no substations service. query_arcgis returned the
    # error body as data, so the route answered success:true, count:0 on every call.
    #
    # Retired rather than repointed. dchub-frontend defines two fetches of this path
    # that nothing invokes, and both send arguments this route rejected with 400
    # (lat/lng/radius, state/limit); dchub-mcp-server does not call it. Railway
    # logged one request in the seven days to 2026-09-13, the probe behind this
    # change. The national layer that still answers (services5 HDRa0B57OVrv2E1q) is
    # the one routes/substation_ingest.py refuses to write: 38,479 of its 75,328
    # names are UNKNOWN<id> placeholders, 18,433 rows carry MAX_VOLT -999999, and its
    # data was last edited 2021-02-25. The substations table is already served by
    # the route named in `instead`, which the Land & Power map reads.
    @app.route('/api/v1/energy/substations', methods=['GET'])
    def get_substations():
        """RETIRED 2026-09-13: answers 410 before any network work (see the note above)."""
        return jsonify({
            'success': False,
            'retired': True,
            'retired_at': '2026-09-13',
            'error': 'route_retired',
            'reason': ('This route queried an ArcGIS substations service that no longer '
                       'exists, and answered every request with zero substations.'),
            'instead': '/api/v2/infrastructure/hifld/substations?lat=<lat>&lng=<lng>&radius=<miles>',
        }), 410

    # ── RETIRED 2026-09-13 ──────────────────────────────────────────────────
    # This route passed its bounding box to Electric_Power_Transmission_Lines on
    # services1.arcgis.com/Hp6G80Pky0om7QvQ. That layer still answers, but it is a
    # superseded copy: 52,244 lines on 2026-09-13, against 89,744 on the services5
    # layer util/hifld_layers.py names as canonical.
    #
    # Retired rather than repointed: dchub-frontend and dchub-mcp-server do not fetch
    # this path, and Railway logged no request to it in the seven days to 2026-09-13.
    # Lines near a point are already served from the maintained transmission_lines
    # table by the route named in `instead`.
    @app.route('/api/v1/energy/transmission', methods=['GET'])
    def get_transmission_lines():
        """RETIRED 2026-09-13: answers 410 before any network work (see the note above)."""
        return jsonify({
            'success': False,
            'retired': True,
            'retired_at': '2026-09-13',
            'error': 'route_retired',
            'reason': ('This route queried a superseded copy of the transmission lines layer, '
                       "missing about two in five of the current layer's lines."),
            'instead': '/api/v1/grid/transmission-proximity?lat=<lat>&lon=<lon>&radius_km=<km>&min_kv=<kv>',
        }), 410
    
    # ── RETIRED 2026-09-13 ──────────────────────────────────────────────────
    # Both branches of this route read the Power_Plants service on
    # services1.arcgis.com/Hp6G80Pky0om7QvQ, which no longer exists: ArcGIS answers
    # HTTP 200 with {"error":{"code":400,"message":"Invalid URL"}}. A bounding box
    # therefore answered 500. A lat/lng request called requests.get directly, found no
    # features, and fell back to a state-wide query of discovered_power_plants, the
    # state picked from 11 hardcoded bounding boxes: no plant coordinates in the
    # answer, and nothing at all for a point outside those boxes.
    #
    # Retired rather than repointed: dchub-frontend's one fetch of this path is in a
    # script no page loads, dchub-mcp-server calls /nearby, and Railway logged no
    # request to it in the seven days to 2026-09-13. The route named in `instead`
    # answers the question from EIA generator data. This route carries no plan gate
    # because it serves no data, so it left main.py's LOCKED_GATE_MANIFEST, whose boot
    # canary counts a 410 as ungated.
    @app.route('/api/v1/energy/power-plants', methods=['GET'])
    def get_power_plants():
        """RETIRED 2026-09-13: answers 410 before any network work (see the note above)."""
        return jsonify({
            'success': False,
            'retired': True,
            'retired_at': '2026-09-13',
            'error': 'route_retired',
            'reason': 'This route read an ArcGIS power plants service that no longer exists.',
            'instead': '/api/v1/energy/power-plants/nearby?lat=<lat>&lng=<lng>&radius=<miles>',
        }), 410
    
    # ── RETIRED 2026-09-13 ──────────────────────────────────────────────────
    # This route proxied a state oil and gas regulator's live GIS layer, chosen by
    # ?state= or else by four hardcoded bounding boxes tried in the order CA, NM, CO,
    # TX. Two of the four layers no longer answered on 2026-09-13: New Mexico's
    # OCD_Wells (HTTP 200, error 400 "Invalid URL") and Colorado's COGCC SurfaceHoles
    # (HTTP 404). The boxes overlap, so a point in far west Texas, El Paso or Pecos,
    # went to New Mexico's layer, and a point in Nevada to California's.
    #
    # Retired: dchub-frontend and dchub-mcp-server do not fetch this path, Railway
    # logged no request to it in the seven days to 2026-09-13, and DC Hub keeps no well
    # data to serve instead, so `instead` names the two regulator layers that still
    # answered.
    @app.route('/api/v1/energy/wells', methods=['GET'])
    def get_wells():
        """RETIRED 2026-09-13: answers 410 before any network work (see the note above)."""
        return jsonify({
            'success': False,
            'retired': True,
            'retired_at': '2026-09-13',
            'error': 'route_retired',
            'reason': ("This route proxied state regulators' well layers, two of which no longer "
                       "answer, and sent points in west Texas to New Mexico's."),
            'instead': ('No DC Hub route serves well data. Texas RRC Well Locations: '
                        'https://gis.rrc.texas.gov/server/rest/services/rrc_public/RRC_Public_Viewer_Srvs/MapServer/1'
                        ' ; California WellSTAR Wells: '
                        'https://gis.conservation.ca.gov/server/rest/services/WellSTAR/Wells/MapServer/0'),
        }), 410
    
    # ── RETIRED 2026-09-13 ──────────────────────────────────────────────────
    # This route never served pipelines. It queried layer 0 of the Texas RRC public
    # viewer MapServer, which is "Well Number": on 2026-09-13 it returned permitted
    # well locations, keyed by API number, and the route served them as pipelines.
    # That service's pipeline layers are 12 to 14.
    #
    # Retired rather than repointed: dchub-frontend and dchub-mcp-server do not fetch
    # this path, and Railway logged no request to it in the seven days to 2026-09-13.
    # Natural-gas pipelines in Texas are served from the gas_pipelines table by the
    # route named in `instead`.
    @app.route('/api/v1/energy/texas-pipelines', methods=['GET'])
    def get_texas_pipelines():
        """RETIRED 2026-09-13: answers 410 before any network work (see the note above)."""
        return jsonify({
            'success': False,
            'retired': True,
            'retired_at': '2026-09-13',
            'error': 'route_retired',
            'reason': 'This route served the Texas RRC well-number layer as pipelines.',
            'instead': '/api/v2/infrastructure/hifld/gas-pipelines?lat=<lat>&lng=<lng>&radius=<miles>',
        }), 410
    
    # ── RETIRED 2026-09-13 ──────────────────────────────────────────────────
    # This route scored each site from four live ArcGIS layers, and three returned
    # nothing when measured on 2026-09-13: the services1 Electric_Substations and
    # Power_Plants services no longer exist (HTTP 200, error 400 "Invalid URL"), and
    # the DOT natural-gas pipelines server answered HTTP 500. Each query sat in its
    # own `except: pass`, so a site was scored on transmission lines alone and served
    # as a success. Ashburn, VA came back with 0 substations, 0 pipelines, 0 power
    # plants and an overallScore of 37 ("Challenging").
    #
    # No caller: dchub-frontend's one fetch of this path is never invoked and sends a
    # POST, which this GET-only route refused with 405; dchub-mcp-server's
    # compare_sites tool reads /api/site-score. Railway logged one request in the
    # seven days to 2026-09-13, the probe behind this change. Sites are compared from
    # maintained tables by the route named in `instead`.
    @app.route('/api/v1/energy/compare-sites', methods=['GET'])
    def compare_sites():
        """RETIRED 2026-09-13: answers 410 before any network work (see the note above)."""
        return jsonify({
            'success': False,
            'retired': True,
            'retired_at': '2026-09-13',
            'error': 'route_retired',
            'reason': ('This route scored sites from live ArcGIS layers, three of the four '
                       'of which no longer answer, and served the result as a success.'),
            'instead': 'POST /api/v1/site-planner/compare {"sites": [{"lat": <lat>, "lng": <lng>}, ...]} (2 or 3 sites)',
        }), 410

    print("✅ Energy Infrastructure API v3 routes registered (with fallback endpoints):")
    print("   GET /api/v1/energy/site-analysis")
    print("   GET /api/v1/energy/pipelines - RETIRED 2026-09-13 (410)")
    print("   GET /api/v1/energy/substations - RETIRED 2026-09-13 (410)")
    print("   GET /api/v1/energy/transmission - RETIRED 2026-09-13 (410)")
    print("   GET /api/v1/energy/power-plants - RETIRED 2026-09-13 (410)")
    print("   GET /api/v1/energy/wells - RETIRED 2026-09-13 (410)")
    print("   GET /api/v1/energy/texas-pipelines - RETIRED 2026-09-13 (410)")
    print("   GET /api/v1/energy/compare-sites - RETIRED 2026-09-13 (410)")
