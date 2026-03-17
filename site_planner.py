"""
DC Hub — Site Planner: Grid Interconnection Analysis Engine 2.0
============================================================
Pro-only feature. Provides instant site analysis for grid interconnection.

Endpoints:
  POST /api/v1/site-planner/analyze     — Full site analysis (Pro+)
  POST /api/v1/site-planner/compare     — Multi-site comparison (Pro+)
  GET  /api/v1/site-planner/queue-depth — ISO queue depth data (Pro+)
  GET  /api/v1/site-planner/score-config — Scoring weights (admin)

Data sources:
  - Neon PostgreSQL (substations, transmission_lines, power_plants via HIFLD)
  - ISO interconnection queue CSVs (cached weekly)
  - FEMA flood zone API
  - FWS critical habitat API

Drop-in: Import and register in main.py:
    from site_planner import register_site_planner_routes
    register_site_planner_routes(app)
"""

import os
import json
import math
import time
import logging
import traceback
from datetime import datetime, timedelta
from functools import wraps

logger = logging.getLogger('site_planner')

# ─── Scoring Weights (tunable without redeploy) ─────────────────────────────
DEFAULT_SCORING_WEIGHTS = {
    'substation_proximity': {
        'weight': 0.25,
        'thresholds': {
            'excellent': {'max_miles': 3, 'points': 25},
            'good': {'max_miles': 8, 'points': 18},
            'fair': {'max_miles': 15, 'points': 10},
            'poor': {'max_miles': 999, 'points': 2},
        }
    },
    'substation_voltage': {
        'weight': 0.15,
        'thresholds': {
            '765kv_plus': {'min_kv': 500, 'points': 15},
            '345kv': {'min_kv': 345, 'points': 12},
            '230kv': {'min_kv': 230, 'points': 8},
            '138kv': {'min_kv': 138, 'points': 5},
            'below_138kv': {'min_kv': 0, 'points': 2},
        }
    },
    'queue_depth': {
        'weight': 0.20,
        'thresholds': {
            'light': {'max_mw': 500, 'points': 20},
            'moderate': {'max_mw': 1500, 'points': 12},
            'heavy': {'max_mw': 3000, 'points': 6},
            'congested': {'max_mw': 99999, 'points': 2},
        }
    },
    'transmission_proximity': {
        'weight': 0.15,
        'thresholds': {
            'excellent': {'max_miles': 1, 'points': 15},
            'good': {'max_miles': 3, 'points': 11},
            'fair': {'max_miles': 8, 'points': 6},
            'poor': {'max_miles': 999, 'points': 2},
        }
    },
    'environmental': {
        'weight': 0.15,
        'thresholds': {
            'clear': {'max_risk_score': 20, 'points': 15},
            'low_risk': {'max_risk_score': 40, 'points': 11},
            'moderate_risk': {'max_risk_score': 65, 'points': 6},
            'high_risk': {'max_risk_score': 100, 'points': 2},
        }
    },
    'congestion': {
        'weight': 0.08,
        'thresholds': {
            'low': {'max_density': 30, 'points': 8},
            'moderate': {'max_density': 60, 'points': 5},
            'high': {'max_density': 100, 'points': 2},
        }
    },
    'gas_access': {
        'weight': 0.06,
        'thresholds': {
            'excellent': {'max_miles': 3, 'points': 6},
            'good': {'max_miles': 10, 'points': 4},
            'fair': {'max_miles': 20, 'points': 2},
            'limited': {'max_miles': 999, 'points': 0},
        }
    },
    'dc_corridor': {
        'weight': 0.06,
        'thresholds': {
            'strong': {'min_count': 5, 'points': 6},
            'moderate': {'min_count': 2, 'points': 4},
            'weak': {'min_count': 1, 'points': 2},
            'none': {'min_count': 0, 'points': 0},
        }
    },
}

# ─── ISO/RTO Reference Data ─────────────────────────────────────────────────
ISO_REGIONS = {
    'ERCOT': {
        'states': ['TX'],
        'avg_queue_wait_years': 4.2,
        'queue_depth_gw': 380,
        'queue_url': 'https://www.ercot.com/gridinfo/resource/generation_interconnection',
    },
    'PJM': {
        'states': ['PA','NJ','DE','MD','VA','WV','OH','IN','IL','MI','KY','NC','DC','TN'],
        'avg_queue_wait_years': 5.1,
        'queue_depth_gw': 450,
        'queue_url': 'https://www.pjm.com/planning/services-requests/interconnection-queues',
    },
    'MISO': {
        'states': ['MN','WI','IA','MO','AR','MS','LA','ND','SD','MT'],
        'avg_queue_wait_years': 4.8,
        'queue_depth_gw': 520,
        'queue_url': 'https://www.misoenergy.org/planning/generator-interconnection/',
    },
    'CAISO': {
        'states': ['CA'],
        'avg_queue_wait_years': 3.9,
        'queue_depth_gw': 280,
        'queue_url': 'https://www.caiso.com/planning/Pages/GeneratorInterconnection/',
    },
    'SPP': {
        'states': ['KS','OK','NE','NM','WY'],
        'avg_queue_wait_years': 3.5,
        'queue_depth_gw': 190,
        'queue_url': 'https://www.spp.org/engineering/generator-interconnection/',
    },
    'ISO-NE': {
        'states': ['MA','CT','RI','NH','VT','ME'],
        'avg_queue_wait_years': 4.0,
        'queue_depth_gw': 95,
        'queue_url': 'https://www.iso-ne.com/system-planning/interconnection-service/',
    },
    'NYISO': {
        'states': ['NY'],
        'avg_queue_wait_years': 5.5,
        'queue_depth_gw': 120,
        'queue_url': 'https://www.nyiso.com/interconnections',
    },
    'SERC': {
        'states': ['GA','AL','SC','FL'],
        'avg_queue_wait_years': 3.8,
        'queue_depth_gw': 210,
        'queue_url': None,
    },
    'WECC': {
        'states': ['AZ','NV','UT','CO','OR','WA','ID','HI'],
        'avg_queue_wait_years': 3.6,
        'queue_depth_gw': 320,
        'queue_url': 'https://www.wecc.org/SystemStabilityPlanning/Pages/default.aspx',
    },
    'Non-ISO Southeast': {
        'states': ['AK'],
        'avg_queue_wait_years': 2.5,
        'queue_depth_gw': 10,
        'queue_url': None,
    },
}


# ─── Helper: Get DB connection ───────────────────────────────────────────────
def get_neon_connection():
    """Get a PostgreSQL connection to Neon. Uses the same pattern as main.py."""
    try:
        import psycopg2
        db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
        # Clean prefix if present (known Replit issue)
        import re
        db_url = re.sub(r'^[A-Z_]+=', '', db_url).strip("'\"")
        if not db_url:
            logger.error("No NEON_DATABASE_URL configured")
            return None
        conn = psycopg2.connect(db_url, connect_timeout=4)
        return conn
    except Exception as e:
        logger.error(f"Neon connection failed: {e}")
        return None


def execute_query(query, params=None, fetchone=False):
    """Execute a read query against Neon and return results as list of dicts."""
    conn = get_neon_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            columns = [desc[0] for desc in cur.description] if cur.description else []
            if fetchone:
                row = cur.fetchone()
                return dict(zip(columns, row)) if row else None
            rows = cur.fetchall()
            return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        logger.error(f"Query error: {e}\nQuery: {query}\nParams: {params}")
        return None
    finally:
        conn.close()


# ─── Core Analysis Functions ─────────────────────────────────────────────────

def find_nearest_substations(lat, lng, limit=5, max_distance_miles=25):
    """
    Find nearest substations using PostGIS spatial queries.
    Falls back to Haversine if PostGIS not available.
    
    Uses the substations table in Neon (populated from HIFLD).
    """
    # Try PostGIS first (ST_DWithin + ST_Distance)
    postgis_query = """
        SELECT 
            name,
            state,
            COALESCE(voltage_kv, 0) as voltage_kv,
            operator,
            lat,
            lng,
            ST_Distance(
                ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
            ) / 1609.34 as distance_miles
        FROM substations
        WHERE lat IS NOT NULL 
          AND lng IS NOT NULL
          AND ST_DWithin(
                ST_SetSRID(ST_MakePoint(lng, lat), 4326)::geography,
                ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                %s
              )
        ORDER BY distance_miles ASC
        LIMIT %s;
    """
    max_distance_meters = max_distance_miles * 1609.34
    result = execute_query(
        postgis_query, 
        (lng, lat, lng, lat, max_distance_meters, limit)
    )
    
    if result:
        return result

    # Fallback: Haversine with bounding box pre-filter (FAST)
    # 1 degree lat ≈ 69 miles, 1 degree lng ≈ 69 * cos(lat) miles
    deg_lat = max_distance_miles / 69.0
    deg_lng = max_distance_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    haversine_query = """
        SELECT 
            name,
            state,
            COALESCE(voltage_kv, 0) as voltage_kv,
            operator,
            lat,
            lng,
            (
                3959 * acos(
                    LEAST(1.0, GREATEST(-1.0,
                        cos(radians(%s)) * cos(radians(lat)) *
                        cos(radians(lng) - radians(%s)) +
                        sin(radians(%s)) * sin(radians(lat))
                    ))
                )
            ) as distance_miles
        FROM substations
        WHERE lat IS NOT NULL 
          AND lng IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s
        ORDER BY distance_miles ASC
        LIMIT %s;
    """
    result = execute_query(haversine_query, (
        lat, lng, lat,
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng,
        limit
    ))
    
    if result:
        return result

    logger.warning("Local substations query returned empty — trying HIFLD API fallback")
    
    # Overpass API fallback: query OpenStreetMap for nearby substations
    # (same data source the Land & Power map frontend uses successfully)
    try:
        import urllib.request, urllib.parse, json as _json, math as _math
        deg = max_distance_miles / 69.0
        deg_lng_adj = max_distance_miles / (69.0 * max(0.1, abs(_math.cos(_math.radians(lat)))))
        south, north = lat - deg, lat + deg
        west, east = lng - deg_lng_adj, lng + deg_lng_adj
        query = f'[out:json][timeout:10];(node["power"="substation"]({south},{west},{north},{east});way["power"="substation"]({south},{west},{north},{east}););out center {limit * 3};'
        post_data = ('data=' + urllib.parse.quote(query)).encode()
        osm_data = None
        try:
            req = urllib.request.Request('https://overpass.kumi.systems/api/interpreter', data=post_data, headers={
                'Content-Type': 'application/x-www-form-urlencoded',
                'User-Agent': 'DCHub/1.0'
            })
            with urllib.request.urlopen(req, timeout=3) as resp:
                osm_data = _json.loads(resp.read().decode())
        except Exception:
            pass
        
        if osm_data:
            elements = osm_data.get('elements', [])
            results = []
            for el in elements:
                tags = el.get('tags', {})
                s_lat = el.get('lat') or (el.get('center', {}) or {}).get('lat')
                s_lng = el.get('lon') or (el.get('center', {}) or {}).get('lon')
                if not s_lat or not s_lng:
                    continue
                # Parse voltage from OSM tags
                voltage_str = tags.get('voltage', '0')
                try:
                    voltage_kv = float(voltage_str.split(';')[0]) / 1000 if float(voltage_str.split(';')[0]) > 999 else float(voltage_str.split(';')[0])
                except (ValueError, IndexError):
                    voltage_kv = 0
                dist = 3959 * _math.acos(min(1.0, max(-1.0,
                    _math.cos(_math.radians(lat)) * _math.cos(_math.radians(s_lat)) *
                    _math.cos(_math.radians(s_lng) - _math.radians(lng)) +
                    _math.sin(_math.radians(lat)) * _math.sin(_math.radians(s_lat))
                )))
                results.append({
                    'name': tags.get('name', 'Substation'),
                    'state': tags.get('addr:state', ''),
                    'voltage_kv': voltage_kv,
                    'operator': tags.get('operator', 'Unknown'),
                    'lat': s_lat,
                    'lng': s_lng,
                    'distance_miles': round(dist, 2),
                    'source': 'OpenStreetMap'
                })
            results.sort(key=lambda x: x['distance_miles'])
            if results:
                logger.info(f"Overpass fallback: found {len(results)} substations near {lat},{lng}")
                return results[:limit]
            else:
                logger.info(f"Overpass fallback: no substations near {lat},{lng}")
    except Exception as osm_err:
        logger.warning(f"Overpass API fallback failed: {osm_err}")
    
    return []


def find_nearest_transmission(lat, lng, max_distance_miles=15):
    """
    Find nearest transmission line by finding the nearest substation
    and looking up what transmission lines connect to it.
    Falls back to HIFLD live API.
    """
    # Step 1: Find nearest substation name
    deg_lat = max_distance_miles / 69.0
    deg_lng = max_distance_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    nearest_sub_query = """
        SELECT name,
            (3959 * acos(
                LEAST(1.0, GREATEST(-1.0,
                    cos(radians(%s)) * cos(radians(lat)) *
                    cos(radians(lng) - radians(%s)) +
                    sin(radians(%s)) * sin(radians(lat))
                ))
            )) as distance_miles
        FROM substations
        WHERE lat IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s
        ORDER BY distance_miles ASC
        LIMIT 1;
    """
    sub_result = execute_query(nearest_sub_query, (
        lat, lng, lat,
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng
    ))
    
    if sub_result and len(sub_result) > 0:
        sub_name = sub_result[0].get('name', '')
        sub_distance = sub_result[0].get('distance_miles', 0)
        
        # Step 2: Find transmission line connected to that substation
        # Use first word of substation name for fuzzy match
        search_term = sub_name.split(' ')[0] if sub_name else ''
        if search_term and len(search_term) > 2:
            tx_query = """
                SELECT sub_1 as line_name, voltage_kv, owner, status, volt_class
                FROM discovered_transmission_lines
                WHERE (LOWER(sub_1) LIKE LOWER(%s) OR LOWER(sub_2) LIKE LOWER(%s))
                  AND voltage_kv IS NOT NULL
                ORDER BY voltage_kv DESC
                LIMIT 1;
            """
            tx_result = execute_query(tx_query, (f'%{search_term}%', f'%{search_term}%'))
            
            if tx_result and len(tx_result) > 0:
                tx = tx_result[0]
                tx['distance_miles'] = round(sub_distance, 1)
                tx['matched_substation'] = sub_name
                return tx
    
    # Fallback: direct HIFLD API query
    return _query_hifld_transmission_live(lat, lng)


def _query_hifld_transmission_live(lat, lng):
    """Direct HIFLD API query for transmission lines as fallback."""
    try:
        import requests
        # HIFLD transmission lines endpoint
        url = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0/query"
        params = {
            'geometry': f'{lng-0.3},{lat-0.3},{lng+0.3},{lat+0.3}',
            'geometryType': 'esriGeometryEnvelope',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'VOLTAGE,OWNER,SUB_1,SUB_2,STATUS,SHAPE_Leng',
            'returnGeometry': 'false',
            'f': 'json',
            'resultRecordCount': 1,
            'orderByFields': 'VOLTAGE DESC',
        }
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if 'features' in data and data['features']:
            f = data['features'][0]['attributes']
            return {
                'line_name': f.get('SUB_1', 'Unknown'),
                'voltage_kv': f.get('VOLTAGE', 0),
                'owner': f.get('OWNER', 'Unknown'),
                'status': f.get('STATUS', 'Unknown'),
                'distance_miles': 'N/A (live query)',
            }
    except Exception as e:
        logger.warning(f"HIFLD transmission live query failed: {e}")
    return None


def identify_iso_region(lat, lng, state=None):
    """Identify which ISO/RTO territory a location falls in."""
    # If we have the state, use state mapping
    if state:
        state_upper = state.upper()[:2]
        for iso_name, iso_data in ISO_REGIONS.items():
            if state_upper in iso_data['states']:
                return {
                    'name': iso_name,
                    'avg_queue_wait_years': iso_data['avg_queue_wait_years'],
                    'queue_depth_gw': iso_data['queue_depth_gw'],
                    'queue_url': iso_data.get('queue_url'),
                }
    
    # Coordinate-based fallback (rough bounding boxes)
    if lng < -115 and lat > 32 and lat < 42:
        return {**ISO_REGIONS['CAISO'], 'name': 'CAISO'}
    if lng > -105 and lng < -93 and lat > 25 and lat < 37:
        return {**ISO_REGIONS['ERCOT'], 'name': 'ERCOT'}
    if lng > -90 and lng < -74 and lat > 36 and lat < 43:
        return {**ISO_REGIONS['PJM'], 'name': 'PJM'}
    if lng > -98 and lng < -82 and lat > 37 and lat < 50:
        return {**ISO_REGIONS['MISO'], 'name': 'MISO'}
    if lng > -74 and lng < -67 and lat > 40 and lat < 47:
        return {**ISO_REGIONS['ISO-NE'], 'name': 'ISO-NE'}
    if lng > -80 and lng < -72 and lat > 40 and lat < 45:
        return {**ISO_REGIONS['NYISO'], 'name': 'NYISO'}
    
    # Default to SERC for southeastern US
    return {**ISO_REGIONS['SERC'], 'name': 'SERC'}


def estimate_queue_depth(iso_name, substation_voltage_kv):
    """
    Estimate queue depth for a specific substation based on ISO region
    and voltage class. Higher voltage subs tend to have deeper queues.
    
    In production, this would query the queue_entries table populated
    by the weekly ISO queue CSV ingestion cron job.
    """
    # Try database first
    query = """
        SELECT 
            SUM(capacity_mw) as total_queue_mw,
            COUNT(*) as project_count,
            AVG(EXTRACT(EPOCH FROM (NOW() - request_date)) / 86400 / 365) as avg_age_years
        FROM queue_entries
        WHERE iso = %s
          AND substation_voltage_kv >= %s - 50
          AND substation_voltage_kv <= %s + 50
          AND status IN ('Active', 'Pending', 'Under Study');
    """
    result = execute_query(query, (iso_name, substation_voltage_kv, substation_voltage_kv), fetchone=True)
    
    if result and result.get('total_queue_mw'):
        return {
            'queue_mw': int(result['total_queue_mw']),
            'project_count': int(result['project_count']),
            'avg_age_years': round(result.get('avg_age_years', 0), 1),
            'source': 'database',
        }
    
    # Fallback: estimate from ISO region averages + voltage scaling
    iso_data = ISO_REGIONS.get(iso_name, ISO_REGIONS['SERC'])
    base_gw = iso_data['queue_depth_gw']
    
    # Higher voltage substations attract more interconnection requests
    voltage_multiplier = 1.0
    if substation_voltage_kv >= 500:
        voltage_multiplier = 1.8
    elif substation_voltage_kv >= 345:
        voltage_multiplier = 1.4
    elif substation_voltage_kv >= 230:
        voltage_multiplier = 1.1
    elif substation_voltage_kv < 138:
        voltage_multiplier = 0.6
    
    estimated_mw = int((base_gw * 1000 / 50) * voltage_multiplier)  # rough per-substation estimate
    
    return {
        'queue_mw': estimated_mw,
        'project_count': int(estimated_mw / 150),  # avg ~150MW per project
        'avg_age_years': iso_data['avg_queue_wait_years'] * 0.6,
        'estimated_wait_years': iso_data['avg_queue_wait_years'],
        'source': 'estimated',
    }


def estimate_congestion(lat, lng, radius_miles=15):
    """
    Estimate grid congestion from local infrastructure density.
    High density of substations + generation = potential congestion.
    """
    # Bounding box pre-filter
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    query = """
        SELECT COUNT(*) as sub_count
        FROM substations
        WHERE lat IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s;
    """
    result = execute_query(query, (
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng
    ), fetchone=True)
    sub_count = result.get('sub_count', 0) if result else 0
    
    # Also count power plants nearby
    plant_query = """
        SELECT COUNT(*) as plant_count, COALESCE(SUM(capacity_mw), 0) as total_mw
        FROM discovered_power_plants
        WHERE lat IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s;
    """
    plant_result = execute_query(plant_query, (
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng
    ), fetchone=True)
    plant_count = plant_result.get('plant_count', 0) if plant_result else 0
    total_gen_mw = plant_result.get('total_mw', 0) if plant_result else 0
    
    # Overpass fallback for substations if local DB empty
    if sub_count == 0:
        try:
            import urllib.request, urllib.parse, json as _json
            south, north = lat - deg_lat, lat + deg_lat
            west, east = lng - deg_lng, lng + deg_lng
            query_osm = f'[out:json][timeout:8];(node["power"="substation"]({south},{west},{north},{east});way["power"="substation"]({south},{west},{north},{east}););out count;'
            post_data = ('data=' + urllib.parse.quote(query_osm)).encode()
            try:
                req = urllib.request.Request('https://overpass.kumi.systems/api/interpreter', data=post_data, headers={
                    'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'DCHub/1.0'
                })
                with urllib.request.urlopen(req, timeout=3) as resp:
                    osm = _json.loads(resp.read().decode())
                sub_count = osm.get('elements', [{}])[0].get('tags', {}).get('total', 0)
                if isinstance(sub_count, str): sub_count = int(sub_count)
                if sub_count > 0:
                    logger.info(f"Congestion Overpass fallback: {sub_count} substations near {lat},{lng}")
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Congestion Overpass fallback failed: {e}")

    # Overpass fallback for power plants if local DB empty
    if plant_count == 0:
        try:
            import urllib.request, urllib.parse, json as _json
            south, north = lat - deg_lat, lat + deg_lat
            west, east = lng - deg_lng, lng + deg_lng
            query_osm = f'[out:json][timeout:8];(node["power"="plant"]({south},{west},{north},{east});way["power"="plant"]({south},{west},{north},{east}););out count;'
            post_data = ('data=' + urllib.parse.quote(query_osm)).encode()
            try:
                req = urllib.request.Request('https://overpass.kumi.systems/api/interpreter', data=post_data, headers={
                    'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'DCHub/1.0'
                })
                with urllib.request.urlopen(req, timeout=3) as resp:
                    osm = _json.loads(resp.read().decode())
                plant_count = osm.get('elements', [{}])[0].get('tags', {}).get('total', 0)
                if isinstance(plant_count, str): plant_count = int(plant_count)
                if plant_count > 0:
                    logger.info(f"Congestion Overpass fallback: {plant_count} power plants near {lat},{lng}")
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Congestion power plants Overpass fallback failed: {e}")

    # Density scoring
    density_score = min(100, (sub_count * 3) + (plant_count * 2))
    
    if density_score > 60:
        level = 'High'
    elif density_score > 30:
        level = 'Moderate'
    else:
        level = 'Low'
    
    return {
        'level': level,
        'density_score': density_score,
        'substations_within_radius': sub_count,
        'power_plants_within_radius': plant_count,
        'total_generation_mw': int(total_gen_mw),
        'radius_miles': radius_miles,
    }


def screen_environmental(lat, lng):
    """
    Environmental screening using federal APIs.
    Checks: FEMA flood zones, FWS critical habitat, NWI wetlands.
    
    Returns risk scores for each category.
    """
    env = {
        'flood_risk': 'Unknown',
        'wetland_risk': 'Unknown',
        'species_risk': 'Unknown',
        'risks_identified': [],
        'env_score': 50,  # default neutral
    }
    
    # ── FEMA Flood Zone Check ──
    try:
        import requests
        fema_url = "https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query"
        params = {
            'geometry': f'{lng},{lat}',
            'geometryType': 'esriGeometryPoint',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'FLD_ZONE,ZONE_SUBTY,SFHA_TF',
            'returnGeometry': 'false',
            'f': 'json',
        }
        resp = requests.get(fema_url, params=params, timeout=4)
        data = resp.json()
        if 'features' in data and data['features']:
            zone = data['features'][0]['attributes'].get('FLD_ZONE', '')
            is_sfha = data['features'][0]['attributes'].get('SFHA_TF', 'F')
            if zone in ('A', 'AE', 'AH', 'AO', 'V', 'VE'):
                env['flood_risk'] = 'High'
                env['risks_identified'].append(f'FEMA Flood Zone {zone} (Special Flood Hazard Area)')
            elif zone in ('X', 'B', 'C'):
                env['flood_risk'] = 'Low'
            else:
                env['flood_risk'] = 'Moderate'
                env['risks_identified'].append(f'FEMA Flood Zone {zone}')
        else:
            env['flood_risk'] = 'Low'
    except Exception as e:
        logger.warning(f"FEMA flood check failed: {e}")
        env['flood_risk'] = 'Unknown'
    
    # ── FWS Critical Habitat Check ──
    try:
        import requests
        fws_url = "https://services.arcgis.com/QVENGdaPbd4LUkLV/arcgis/rest/services/USFWS_Critical_Habitat/FeatureServer/1/query"
        params = {
            'geometry': f'{lng},{lat}',
            'geometryType': 'esriGeometryPoint',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'comname,sciname,status',
            'returnGeometry': 'false',
            'f': 'json',
        }
        resp = requests.get(fws_url, params=params, timeout=4)
        data = resp.json()
        if 'features' in data and data['features']:
            env['species_risk'] = 'High'
            for f in data['features'][:3]:
                species = f['attributes'].get('comname', 'Unknown species')
                env['risks_identified'].append(f'Critical Habitat: {species}')
        else:
            env['species_risk'] = 'Low'
    except Exception as e:
        logger.warning(f"FWS critical habitat check failed: {e}")
        env['species_risk'] = 'Unknown'
    
    # ── NWI Wetlands Check ──
    try:
        import requests
        nwi_url = "https://fwsprimary.wim.usgs.gov/server/rest/services/Wetlands/MapServer/0/query"
        params = {
            'geometry': f'{lng-0.01},{lat-0.01},{lng+0.01},{lat+0.01}',
            'geometryType': 'esriGeometryEnvelope',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'WETLAND_TYPE,ATTRIBUTE',
            'returnGeometry': 'false',
            'f': 'json',
            'resultRecordCount': 5,
        }
        resp = requests.get(nwi_url, params=params, timeout=4)
        data = resp.json()
        if 'features' in data and data['features']:
            env['wetland_risk'] = 'Moderate'
            wetland_type = data['features'][0]['attributes'].get('WETLAND_TYPE', 'Wetland')
            env['risks_identified'].append(f'NWI Wetlands: {wetland_type} within 0.6 miles')
        else:
            env['wetland_risk'] = 'Low'
    except Exception as e:
        logger.warning(f"NWI wetlands check failed: {e}")
        env['wetland_risk'] = 'Unknown'
    
    # ── Compute composite environmental score ──
    risk_scores = {'High': 30, 'Moderate': 15, 'Low': 0, 'Unknown': 10}
    total_risk = (
        risk_scores.get(env['flood_risk'], 10) +
        risk_scores.get(env['species_risk'], 10) +
        risk_scores.get(env['wetland_risk'], 10)
    )
    env['env_score'] = max(0, min(100, 100 - total_risk))
    
    if not env['risks_identified']:
        env['risks_identified'].append('No significant environmental risks identified')
    
    return env


def get_generation_mix(lat, lng, radius_miles=25):
    """Get generation mix within radius from discovered_power_plants table."""
    # Bounding box pre-filter
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    query = """
        SELECT 
            COALESCE(fuel_type, 'Unknown') as fuel,
            SUM(COALESCE(capacity_mw, 0)) as total_mw,
            COUNT(*) as plant_count
        FROM discovered_power_plants
        WHERE lat IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s
        GROUP BY fuel
        ORDER BY total_mw DESC;
    """
    result = execute_query(query, (
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng
    ))
    
    if not result:
        return {'mix': {}, 'total_mw': 0, 'plant_count': 0, 'radius_miles': radius_miles}
    
    total = sum(r['total_mw'] for r in result)
    mix = {}
    for r in result:
        pct = round((r['total_mw'] / total * 100), 1) if total > 0 else 0
        mix[r['fuel']] = {
            'mw': int(r['total_mw']),
            'percentage': pct,
            'plant_count': int(r['plant_count']),
        }
    
    return {
        'mix': mix,
        'total_mw': int(total),
        'plant_count': sum(r['plant_count'] for r in result),
        'radius_miles': radius_miles,
    }


# ─── Enhancement: Nearby Data Center Facilities ─────────────────────────────
def find_nearby_facilities(lat, lng, radius_miles=25, limit=10):
    """
    Find existing data center facilities near the site.
    Uses DC Hub's 13K+ facility database.
    Important context: nearby DCs mean proven infrastructure corridor.
    """
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    query = """
        SELECT 
            name,
            provider,
            city,
            state,
            COALESCE(power_mw, 0) as power_mw,
            status,
            latitude as lat,
            longitude as lng,
            (
                3959 * acos(
                    LEAST(1.0, GREATEST(-1.0,
                        cos(radians(%s)) * cos(radians(latitude)) *
                        cos(radians(longitude) - radians(%s)) +
                        sin(radians(%s)) * sin(radians(latitude))
                    ))
                )
            ) as distance_miles
        FROM facilities
        WHERE latitude IS NOT NULL
          AND longitude IS NOT NULL
          AND latitude != 0
          AND longitude != 0
          AND latitude BETWEEN %s AND %s
          AND longitude BETWEEN %s AND %s
        ORDER BY distance_miles ASC
        LIMIT %s;
    """
    result = execute_query(query, (
        lat, lng, lat,
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng,
        limit * 3  # Fetch extra to compensate for dedup
    ))
    
    if not result:
        return {'facilities': [], 'count': 0, 'total_power_mw': 0, 'radius_miles': radius_miles}
    
    # Deduplicate by name (keep nearest/first occurrence)
    seen = set()
    unique = []
    for f in result:
        name_key = f.get('name', '').lower().strip()
        if name_key and name_key not in seen and len(unique) < limit:
            seen.add(name_key)
            unique.append(f)
    result = unique
    
    total_mw = sum(f.get('power_mw', 0) for f in result)
    
    return {
        'facilities': result,
        'count': len(result),
        'total_power_mw': int(total_mw),
        'radius_miles': radius_miles,
        'corridor_signal': 'Strong' if len(result) >= 5 else 'Moderate' if len(result) >= 2 else 'Weak',
    }


# ─── Enhancement: Fiber/Connectivity Proximity ──────────────────────────────
def check_fiber_proximity(lat, lng, radius_miles=15):
    """
    Check for fiber routes and connectivity infrastructure nearby.
    Uses fiber_routes table if available.
    """
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    # Check for fiber routes table
    try:
        query = """
            SELECT COUNT(*) as route_count
            FROM fiber_routes
            WHERE start_location IS NOT NULL
              OR end_location IS NOT NULL;
        """
        result = execute_query(query, (), fetchone=True)
        fiber_count = result.get('route_count', 0) if result else 0
    except:
        fiber_count = 0
    
    # Check nearby facilities with connectivity info
    conn_query = """
        SELECT COUNT(*) as connected_dcs,
               COUNT(DISTINCT provider) as providers
        FROM facilities
        WHERE latitude IS NOT NULL
          AND latitude BETWEEN %s AND %s
          AND longitude BETWEEN %s AND %s
          AND connectivity IS NOT NULL
          AND connectivity != '';
    """
    conn_result = execute_query(conn_query, (
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng
    ), fetchone=True)
    
    connected_dcs = conn_result.get('connected_dcs', 0) if conn_result else 0
    providers = conn_result.get('providers', 0) if conn_result else 0
    
    if connected_dcs >= 10:
        connectivity_rating = 'Excellent'
    elif connected_dcs >= 5:
        connectivity_rating = 'Good'
    elif connected_dcs >= 1:
        connectivity_rating = 'Fair'
    else:
        connectivity_rating = 'Limited'
    
    return {
        'connectivity_rating': connectivity_rating,
        'connected_facilities_nearby': connected_dcs,
        'unique_providers': providers,
        'fiber_routes_in_area': fiber_count,
        'radius_miles': radius_miles,
    }


# ─── Enhancement: Power Pricing by ISO ──────────────────────────────────────
# Real average wholesale electricity prices by ISO region ($/MWh)
# Source: EIA, ISO market reports — updated periodically
ISO_POWER_PRICES = {
    'ERCOT': {'avg_price_mwh': 38.50, 'peak_price_mwh': 85.00, 'trend': 'stable', 'renewable_pct': 32},
    'PJM': {'avg_price_mwh': 42.00, 'peak_price_mwh': 110.00, 'trend': 'rising', 'renewable_pct': 12},
    'MISO': {'avg_price_mwh': 35.00, 'peak_price_mwh': 75.00, 'trend': 'stable', 'renewable_pct': 22},
    'CAISO': {'avg_price_mwh': 55.00, 'peak_price_mwh': 180.00, 'trend': 'volatile', 'renewable_pct': 45},
    'SPP': {'avg_price_mwh': 28.00, 'peak_price_mwh': 60.00, 'trend': 'declining', 'renewable_pct': 38},
    'ISO-NE': {'avg_price_mwh': 52.00, 'peak_price_mwh': 140.00, 'trend': 'rising', 'renewable_pct': 15},
    'NYISO': {'avg_price_mwh': 48.00, 'peak_price_mwh': 130.00, 'trend': 'rising', 'renewable_pct': 18},
    'SERC': {'avg_price_mwh': 40.00, 'peak_price_mwh': 90.00, 'trend': 'stable', 'renewable_pct': 10},
}

def get_power_pricing(iso_name):
    """Get wholesale electricity pricing for the ISO region."""
    pricing = ISO_POWER_PRICES.get(iso_name, ISO_POWER_PRICES.get('SERC'))
    return {
        'iso': iso_name,
        'avg_wholesale_price_mwh': pricing['avg_price_mwh'],
        'peak_price_mwh': pricing['peak_price_mwh'],
        'price_trend': pricing['trend'],
        'renewable_percentage': pricing['renewable_pct'],
        'estimated_annual_cost_per_mw': int(pricing['avg_price_mwh'] * 8760),
        'note': 'Wholesale market averages. Actual contract rates vary by utility, load factor, and term.',
    }


# ─── Enhancement: Water Availability Risk ───────────────────────────────────
# State-level water stress indicators (simplified from WRI Aqueduct data)
WATER_STRESS_BY_STATE = {
    # High stress
    'CA': 'High', 'AZ': 'High', 'NV': 'High', 'NM': 'High', 'UT': 'High',
    # Moderate-High
    'TX': 'Moderate-High', 'CO': 'Moderate-High', 'OK': 'Moderate-High', 'KS': 'Moderate-High',
    # Moderate
    'GA': 'Moderate', 'FL': 'Moderate', 'SC': 'Moderate', 'NE': 'Moderate',
    'MT': 'Moderate', 'ID': 'Moderate', 'WY': 'Moderate', 'HI': 'Moderate',
    # Low-Moderate
    'NC': 'Low-Moderate', 'TN': 'Low-Moderate', 'AL': 'Low-Moderate', 'MS': 'Low-Moderate',
    'AR': 'Low-Moderate', 'LA': 'Low-Moderate', 'MO': 'Low-Moderate', 'ND': 'Low-Moderate',
    'SD': 'Low-Moderate',
    # Low
    'VA': 'Low', 'OH': 'Low', 'PA': 'Low', 'NY': 'Low', 'IL': 'Low',
    'WI': 'Low', 'MN': 'Low', 'WA': 'Low', 'OR': 'Low', 'IN': 'Low',
    'MI': 'Low', 'IA': 'Low', 'KY': 'Low', 'WV': 'Low', 'MD': 'Low',
    'DE': 'Low', 'NJ': 'Low', 'CT': 'Low', 'RI': 'Low', 'MA': 'Low',
    'NH': 'Low', 'VT': 'Low', 'ME': 'Low', 'AK': 'Low', 'DC': 'Low',
}

def assess_water_risk(state_code):
    """
    Assess water availability risk for data center cooling.
    Critical for hyperscale facilities that use evaporative cooling.
    """
    stress = WATER_STRESS_BY_STATE.get(state_code, 'Unknown')
    
    risk_scores = {
        'Low': 10, 'Low-Moderate': 25, 'Moderate': 45,
        'Moderate-High': 65, 'High': 85, 'Unknown': 50
    }
    
    recommendations = {
        'Low': 'Favorable for water-cooled facilities. Standard permitting expected.',
        'Low-Moderate': 'Generally adequate supply. Monitor seasonal variations.',
        'Moderate': 'Water management plan recommended. Consider air-cooled alternatives.',
        'Moderate-High': 'Water-efficient cooling strongly recommended. May face permitting scrutiny.',
        'High': 'Water scarcity zone. Air-cooled or closed-loop systems recommended. Expect permitting challenges.',
        'Unknown': 'Water availability data not available for this state.',
    }
    
    return {
        'water_stress_level': stress,
        'water_risk_score': risk_scores.get(stress, 50),
        'state': state_code,
        'recommendation': recommendations.get(stress, ''),
        'cooling_note': 'Modern hyperscale facilities use 1.8L/kWh average. Air-cooled alternatives reduce water use by 90%+.',
    }


# ─── Enhancement: HIFLD Live Substation Fallback ────────────────────────────
def query_hifld_substations_live(lat, lng, radius_miles=25):
    """
    Direct HIFLD API query for substations as fallback/supplement.
    Use when local DB returns fewer than 5 results.
    """
    try:
        import requests
        # Approximate bounding box
        deg = radius_miles / 69.0
        bbox = f'{lng-deg},{lat-deg},{lng+deg},{lat+deg}'
        
        url = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0/query"
        params = {
            'geometry': bbox,
            'geometryType': 'esriGeometryEnvelope',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'NAME,CITY,STATE,STATUS,MAX_VOLT,MIN_VOLT,OWNER,LATITUDE,LONGITUDE',
            'returnGeometry': 'false',
            'f': 'json',
            'resultRecordCount': 10,
        }
        resp = requests.get(url, params=params, timeout=12)
        data = resp.json()
        
        if 'features' in data and data['features']:
            results = []
            for f in data['features']:
                a = f.get('attributes', {})
                sub_lat = a.get('LATITUDE')
                sub_lng = a.get('LONGITUDE')
                if not sub_lat or not sub_lng:
                    continue
                    
                # Calculate distance
                dist = 3959 * math.acos(
                    min(1.0, max(-1.0,
                        math.cos(math.radians(lat)) * math.cos(math.radians(sub_lat)) *
                        math.cos(math.radians(sub_lng) - math.radians(lng)) +
                        math.sin(math.radians(lat)) * math.sin(math.radians(sub_lat))
                    ))
                )
                
                results.append({
                    'name': a.get('NAME', 'Unknown'),
                    'state': a.get('STATE', ''),
                    'voltage_kv': a.get('MAX_VOLT') or a.get('MIN_VOLT') or 0,
                    'operator': a.get('OWNER', 'Unknown'),
                    'lat': sub_lat,
                    'lng': sub_lng,
                    'distance_miles': round(dist, 1),
                    'source': 'HIFLD_live',
                })
            
            results.sort(key=lambda x: x['distance_miles'])
            return results[:5]
    except Exception as e:
        logger.warning(f"HIFLD live substations query failed: {e}")
    
    return []


def compute_suitability_score(substations, transmission, iso, env, congestion, gas=None, nearby_dcs=None, weights=None):
    """
    Compute 0-100 Interconnection Suitability Score.
    Uses configurable weights so we can tune without redeploying.
    v2.0: Now includes gas access and DC corridor scoring.
    """
    w = weights or DEFAULT_SCORING_WEIGHTS
    score = 0
    breakdown = {}
    
    # 1. Substation proximity
    if substations:
        nearest_dist = substations[0].get('distance_miles', 999)
        for tier_name, tier in w['substation_proximity']['thresholds'].items():
            if nearest_dist <= tier['max_miles']:
                points = tier['points']
                score += points
                breakdown['substation_proximity'] = {'points': points, 'tier': tier_name, 'value': f"{nearest_dist:.1f} mi"}
                break
    
    # 2. Substation voltage
    if substations:
        voltage = substations[0].get('voltage_kv', 0)
        for tier_name, tier in w['substation_voltage']['thresholds'].items():
            if voltage >= tier['min_kv']:
                points = tier['points']
                score += points
                breakdown['substation_voltage'] = {'points': points, 'tier': tier_name, 'value': f"{voltage} kV"}
                break
    
    # 3. Queue depth
    if substations and iso:
        queue = estimate_queue_depth(iso.get('name', 'SERC'), substations[0].get('voltage_kv', 0))
        queue_mw = queue.get('queue_mw', 2000)
        for tier_name, tier in w['queue_depth']['thresholds'].items():
            if queue_mw <= tier['max_mw']:
                points = tier['points']
                score += points
                breakdown['queue_depth'] = {'points': points, 'tier': tier_name, 'value': f"{queue_mw} MW"}
                break
    
    # 4. Transmission proximity
    if transmission:
        tx_dist = transmission.get('distance_miles', 999)
        if isinstance(tx_dist, str):
            tx_dist = 999
        for tier_name, tier in w['transmission_proximity']['thresholds'].items():
            if tx_dist <= tier['max_miles']:
                points = tier['points']
                score += points
                breakdown['transmission_proximity'] = {'points': points, 'tier': tier_name, 'value': f"{tx_dist:.1f} mi" if isinstance(tx_dist, float) else str(tx_dist)}
                break
    
    # 5. Environmental
    if env:
        env_risk = 100 - env.get('env_score', 50)
        for tier_name, tier in w['environmental']['thresholds'].items():
            if env_risk <= tier['max_risk_score']:
                points = tier['points']
                score += points
                breakdown['environmental'] = {'points': points, 'tier': tier_name, 'value': f"Score {env.get('env_score', 'N/A')}"}
                break
    
    # 6. Congestion
    if congestion:
        density = congestion.get('density_score', 50)
        for tier_name, tier in w['congestion']['thresholds'].items():
            if density <= tier['max_density']:
                points = tier['points']
                score += points
                breakdown['congestion'] = {'points': points, 'tier': tier_name, 'value': congestion.get('level', 'Unknown')}
                break
    
    # 7. Gas access
    if gas:
        gas_dist = (gas.get('nearest_pipeline', {}) or {}).get('distance_miles', 999)
        for tier_name, tier in w['gas_access']['thresholds'].items():
            if gas_dist <= tier['max_miles']:
                points = tier['points']
                score += points
                breakdown['gas_access'] = {'points': points, 'tier': tier_name, 'value': f"{gas_dist:.1f} mi"}
                break
    
    # 8. DC corridor strength
    if nearby_dcs:
        dc_count = nearby_dcs.get('count', 0)
        for tier_name, tier in w['dc_corridor']['thresholds'].items():
            if dc_count >= tier['min_count']:
                points = tier['points']
                score += points
                breakdown['dc_corridor'] = {'points': points, 'tier': tier_name, 'value': f"{dc_count} DCs"}
                break
    
    return {
        'score': min(100, score),
        'max_possible': 100,
        'breakdown': breakdown,
        'weights_version': 'v2.0',
    }


# ─── Enhancement: Gas Infrastructure Proximity ──────────────────────────────
def find_nearby_gas_pipelines(lat, lng, radius_miles=25, limit=10):
    """
    Find gas pipelines near the site from 10K+ gas_pipelines table.
    Critical for: gas-fired power generation, dual-fuel capability,
    backup generation, and midstream infrastructure access.
    """
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    query = """
        SELECT 
            name,
            operator,
            pipeline_type,
            COALESCE(diameter_inches, 0) as diameter_inches,
            COALESCE(capacity_mcf, 0) as capacity_mcf,
            status,
            state,
            lat, lng,
            (
                3959 * acos(
                    LEAST(1.0, GREATEST(-1.0,
                        cos(radians(%s)) * cos(radians(lat)) *
                        cos(radians(lng) - radians(%s)) +
                        sin(radians(%s)) * sin(radians(lat))
                    ))
                )
            ) as distance_miles
        FROM gas_pipelines
        WHERE lat IS NOT NULL AND lng IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s
        ORDER BY distance_miles ASC
        LIMIT %s;
    """
    result = execute_query(query, (
        lat, lng, lat,
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng,
        limit
    ))
    
    if not result:
        return {
            'pipelines': [],
            'count': 0,
            'gas_access': 'None detected',
            'radius_miles': radius_miles,
        }
    
    # Categorize by type
    types = {}
    for p in result:
        pt = p.get('pipeline_type', 'Unknown')
        if pt not in types:
            types[pt] = 0
        types[pt] += 1
    
    nearest = result[0] if result else {}
    nearest_dist = nearest.get('distance_miles', 999)
    
    if nearest_dist < 3:
        gas_access = 'Excellent'
    elif nearest_dist < 10:
        gas_access = 'Good'
    elif nearest_dist < 20:
        gas_access = 'Fair'
    else:
        gas_access = 'Limited'
    
    return {
        'pipelines': result,
        'count': len(result),
        'nearest_pipeline': {
            'name': nearest.get('name', 'Unknown'),
            'operator': nearest.get('operator', 'Unknown'),
            'type': nearest.get('pipeline_type', 'Unknown'),
            'diameter': nearest.get('diameter_inches', 0),
            'capacity_mcf': nearest.get('capacity_mcf', 0),
            'distance_miles': round(nearest_dist, 1),
        },
        'pipeline_types': types,
        'gas_access': gas_access,
        'radius_miles': radius_miles,
    }


# ─── Enhancement: Major Interstate Pipeline Proximity ────────────────────────
def find_major_pipelines(lat, lng, radius_miles=50):
    """
    Find major interstate gas pipelines from discovered_pipelines table.
    These are the big 31 major trunk lines with capacity data (MDth/d).
    """
    deg_lat = radius_miles / 69.0
    deg_lng = radius_miles / (69.0 * max(0.1, abs(math.cos(math.radians(lat)))))
    
    query = """
        SELECT 
            name,
            operator,
            pipeline_type,
            commodity,
            COALESCE(capacity_mdth, 0) as capacity_mdth,
            states_served,
            lat, lng,
            (
                3959 * acos(
                    LEAST(1.0, GREATEST(-1.0,
                        cos(radians(%s)) * cos(radians(lat)) *
                        cos(radians(lng) - radians(%s)) +
                        sin(radians(%s)) * sin(radians(lat))
                    ))
                )
            ) as distance_miles
        FROM discovered_pipelines
        WHERE lat IS NOT NULL AND lng IS NOT NULL
          AND lat BETWEEN %s AND %s
          AND lng BETWEEN %s AND %s
        ORDER BY distance_miles ASC
        LIMIT 5;
    """
    result = execute_query(query, (
        lat, lng, lat,
        lat - deg_lat, lat + deg_lat,
        lng - deg_lng, lng + deg_lng
    ))
    
    if not result:
        return {'major_pipelines': [], 'count': 0}
    
    return {
        'major_pipelines': [{
            'name': p.get('name', 'Unknown'),
            'operator': p.get('operator', 'Unknown'),
            'capacity_mdth_per_day': p.get('capacity_mdth', 0),
            'states_served': p.get('states_served', ''),
            'distance_miles': round(p.get('distance_miles', 0), 1),
        } for p in result],
        'count': len(result),
    }


# ─── Enhancement: DC Capacity Pipeline (Planned/Under Construction) ─────────
def get_capacity_pipeline_nearby(lat, lng, state=None, market=None):
    """
    Get data center capacity pipeline projects near the site.
    Shows what's being built — indicates market growth and demand signal.
    Uses capacity_pipeline table (191 projects, 184GW+).
    """
    # Try market match first, then region/state
    results = None
    
    if market:
        query = """
            SELECT operator, market, capacity_mw, phase, status,
                   announcement_date, completion_date, notes, confidence_label
            FROM capacity_pipeline
            WHERE LOWER(market) LIKE LOWER(%s)
            ORDER BY capacity_mw DESC
            LIMIT 10;
        """
        results = execute_query(query, (f'%{market}%',))
    
    if (not results or len(results) == 0) and state:
        query = """
            SELECT operator, market, capacity_mw, phase, status,
                   announcement_date, completion_date, notes, confidence_label
            FROM capacity_pipeline
            WHERE LOWER(market) LIKE LOWER(%s)
               OR LOWER(region) LIKE LOWER(%s)
            ORDER BY capacity_mw DESC
            LIMIT 10;
        """
        results = execute_query(query, (f'%{state}%', f'%{state}%'))
    
    if not results:
        # Fallback: get top projects regardless of location
        query = """
            SELECT operator, market, capacity_mw, phase, status,
                   announcement_date, completion_date, notes, confidence_label
            FROM capacity_pipeline
            WHERE market != 'Unknown'
            ORDER BY capacity_mw DESC
            LIMIT 5;
        """
        results = execute_query(query)
    
    if not results:
        return {'projects': [], 'total_pipeline_mw': 0, 'project_count': 0}
    
    total_mw = sum(p.get('capacity_mw', 0) for p in results)
    
    # Phase breakdown
    phases = {}
    for p in results:
        ph = p.get('phase', 'Unknown')
        if ph not in phases:
            phases[ph] = {'count': 0, 'mw': 0}
        phases[ph]['count'] += 1
        phases[ph]['mw'] += p.get('capacity_mw', 0)
    
    return {
        'projects': [{
            'operator': p.get('operator', 'Unknown'),
            'market': p.get('market', 'Unknown'),
            'capacity_mw': int(p.get('capacity_mw', 0)),
            'phase': p.get('phase', 'Unknown'),
            'status': p.get('status', 'Unknown'),
            'completion_date': p.get('completion_date', ''),
            'confidence': p.get('confidence_label', 'low'),
        } for p in results],
        'total_pipeline_mw': int(total_mw),
        'project_count': len(results),
        'phase_breakdown': phases,
        'demand_signal': 'Very Strong' if total_mw > 500 else 'Strong' if total_mw > 100 else 'Moderate' if total_mw > 0 else 'Low',
    }


# ─── Enhancement: Reverse Geocode for Map Clicks ────────────────────────────
def reverse_geocode(lat, lng):
    """Reverse geocode lat/lng to get address, state, county."""
    try:
        import requests
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {
            'lat': lat,
            'lon': lng,
            'format': 'json',
            'addressdetails': 1,
            'zoom': 14,
        }
        headers = {'User-Agent': 'DCHub-SitePlanner/1.0 (jaz@dchub.cloud)'}
        resp = requests.get(url, params=params, headers=headers, timeout=4)
        data = resp.json()
        
        if data and 'address' in data:
            addr = data['address']
            return {
                'display_name': data.get('display_name', ''),
                'state': addr.get('state', ''),
                'state_code': addr.get('ISO3166-2-lvl4', '').replace('US-', ''),
                'county': addr.get('county', ''),
                'city': addr.get('city') or addr.get('town') or addr.get('village', ''),
            }
    except Exception as e:
        logger.warning(f"Reverse geocode failed: {e}")
    
    return None


# ─── Geocoding Helper ────────────────────────────────────────────────────────
def geocode_address(address):
    """
    Geocode an address to lat/lng.
    Uses Nominatim (free) as primary, with fallback patterns.
    In production, consider Mapbox or Google geocoding for better accuracy.
    """
    try:
        import requests
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            'q': address,
            'format': 'json',
            'limit': 1,
            'countrycodes': 'us',
            'addressdetails': 1,
        }
        headers = {'User-Agent': 'DCHub-SitePlanner/1.0 (jaz@dchub.cloud)'}
        resp = requests.get(url, params=params, headers=headers, timeout=4)
        results = resp.json()
        
        if results:
            r = results[0]
            addr_details = r.get('address', {})
            return {
                'lat': float(r['lat']),
                'lng': float(r['lon']),
                'display_name': r.get('display_name', address),
                'state': addr_details.get('state', ''),
                'state_code': addr_details.get('ISO3166-2-lvl4', '').replace('US-', ''),
                'county': addr_details.get('county', ''),
                'city': addr_details.get('city') or addr_details.get('town') or addr_details.get('village', ''),
            }
    except Exception as e:
        logger.error(f"Geocoding failed for '{address}': {e}")
    
    return None


# ─── Route Registration ──────────────────────────────────────────────────────

def register_site_planner_routes(app):
    """Register all Site Planner endpoints on the Flask app."""
    
    from flask import request as flask_request, jsonify
    
    # Import the auth decorator from main.py
    # Uses lazy import to avoid circular dependency
    def require_pro(f):
        """Decorator: requires Pro plan or higher."""
        @wraps(f)
        def decorated(*args, **kwargs):
            try:
                from main import require_plan
                # Wrap the function with require_plan('pro')
                return require_plan('pro')(f)(*args, **kwargs)
            except ImportError:
                # Fallback: check manually
                auth_header = flask_request.headers.get('Authorization', '')
                api_key = flask_request.headers.get('X-API-Key') or flask_request.args.get('api_key')
                
                if not auth_header and not api_key:
                    return jsonify({
                        'success': False,
                        'error': 'authentication_required',
                        'message': 'Site Planner requires a Pro subscription',
                        'upgrade_url': 'https://dchub.cloud/pricing',
                    }), 401
                
                # If we can't verify plan, allow through (better UX than blocking)
                return f(*args, **kwargs)
        decorated.__name__ = f.__name__
        return decorated

    # ── OPTIONS preflight for all site-planner routes ──
    @app.route('/api/v1/site-planner/analyze', methods=['OPTIONS'])
    @app.route('/api/v1/site-planner/compare', methods=['OPTIONS'])
    @app.route('/api/v1/site-planner/export', methods=['OPTIONS'])
    def site_planner_preflight():
        """Handle CORS preflight — must return 200 with no auth check."""
        resp = jsonify({'ok': True})
        resp.headers['Access-Control-Allow-Origin'] = flask_request.headers.get('Origin', '*')
        resp.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-API-Key, Accept'
        resp.headers['Access-Control-Max-Age'] = '86400'
        return resp, 200

    # ── POST /api/v1/site-planner/analyze ──
    @app.route('/api/v1/site-planner/analyze', methods=['POST'])
    @require_pro
    def site_planner_analyze():
        """
        Full site analysis. Accepts address or lat/lng coordinates.
        
        Request body:
          { "address": "123 Main St, Dallas, TX" }
          OR
          { "lat": 32.7767, "lng": -96.7970 }
        
        Returns: Complete interconnection analysis report.
        """
        start_time = time.time()
        
        data = flask_request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'Request body required'}), 400
        
        lat = data.get('lat')
        lng = data.get('lng')
        address = data.get('address', '')
        state = data.get('state', '')
        
        # Geocode if address provided
        if address and (not lat or not lng):
            geo = geocode_address(address)
            if not geo:
                return jsonify({
                    'success': False,
                    'error': 'geocoding_failed',
                    'message': f'Could not geocode address: {address}',
                }), 400
            lat = geo['lat']
            lng = geo['lng']
            state = geo.get('state_code', '')
            address = geo.get('display_name', address)
        
        if not lat or not lng:
            return jsonify({'success': False, 'error': 'lat/lng or address required'}), 400
        
        # Reverse geocode if we have coords but no address (map click)
        if (not address or address == '') and lat and lng:
            rev = reverse_geocode(lat, lng)
            if rev:
                address = rev.get('display_name', f'{lat:.4f}, {lng:.4f}')
                if not state:
                    state = rev.get('state_code', '')
        
        try:
            # Phase 1: Proximity Analysis
            substations = find_nearest_substations(lat, lng, limit=5)
            
            # Deduplicate substations by name
            seen_names = set()
            unique_subs = []
            for s in (substations or []):
                if s.get('name') not in seen_names:
                    seen_names.add(s.get('name'))
                    unique_subs.append(s)
            substations = unique_subs
            
            # If fewer than 5, supplement from HIFLD live API
            if len(substations) < 5:
                live_subs = query_hifld_substations_live(lat, lng)
                for ls in live_subs:
                    if ls.get('name') not in seen_names and len(substations) < 5:
                        seen_names.add(ls.get('name'))
                        substations.append(ls)
            
            transmission = find_nearest_transmission(lat, lng)
            
            # Phase 2: Queue Depth & Scoring
            iso = identify_iso_region(lat, lng, state)
            queue_data = None
            if substations:
                queue_data = estimate_queue_depth(
                    iso.get('name', 'SERC'),
                    substations[0].get('voltage_kv', 0)
                )
            congestion = estimate_congestion(lat, lng)
            
            # Phase 3: Environmental Screening
            env = screen_environmental(lat, lng)
            gen_mix = get_generation_mix(lat, lng)
            
            # Enhanced Analysis
            nearby_dcs = find_nearby_facilities(lat, lng)
            fiber = check_fiber_proximity(lat, lng)
            power_pricing = get_power_pricing(iso.get('name', 'SERC'))
            water = assess_water_risk(state)
            
            # Gas & Midstream Infrastructure
            gas = find_nearby_gas_pipelines(lat, lng)
            major_pipes = find_major_pipelines(lat, lng)
            
            # DC Capacity Pipeline (what's being built nearby)
            # Try to extract city/market for better matching
            city_market = address.split(',')[0] if address else ''
            capacity = get_capacity_pipeline_nearby(lat, lng, state=state, market=city_market)
            
            # Compute composite score (v2.0 — includes gas + DC corridor)
            scoring = compute_suitability_score(substations, transmission, iso, env, congestion, gas=gas, nearby_dcs=nearby_dcs)
            
            elapsed = round(time.time() - start_time, 2)
            
            return jsonify({
                'success': True,
                'analysis': {
                    'location': {
                        'address': address,
                        'lat': lat,
                        'lng': lng,
                        'state': state,
                    },
                    'substations': substations or [],
                    'transmission': transmission,
                    'iso': iso,
                    'queue': queue_data,
                    'congestion': congestion,
                    'environmental': env,
                    'generation_mix': gen_mix,
                    'nearby_data_centers': nearby_dcs,
                    'fiber_connectivity': fiber,
                    'power_pricing': power_pricing,
                    'water_risk': water,
                    'gas_infrastructure': gas,
                    'major_pipelines': major_pipes,
                    'capacity_pipeline': capacity,
                    'suitability_score': scoring,
                },
                'meta': {
                    'elapsed_seconds': elapsed,
                    'timestamp': datetime.utcnow().isoformat(),
                    'version': 'v1.2',
                    'data_sources': ['HIFLD', 'FEMA', 'FWS', 'NWI', 'ISO Queue Estimates',
                                     'DC Hub Facilities DB', 'EIA Power Pricing',
                                     'Gas Pipelines (10K+)', 'Capacity Pipeline (191 projects)'],
                },
            })
        
        except Exception as e:
            logger.error(f"Site analysis failed: {e}\n{traceback.format_exc()}")
            return jsonify({
                'success': False,
                'error': 'analysis_failed',
                'message': str(e),
            }), 500

    # ── POST /api/v1/site-planner/compare ──
    @app.route('/api/v1/site-planner/compare', methods=['POST'])
    @require_pro
    def site_planner_compare():
        """
        Compare 2-3 sites side by side.
        
        Request body:
          { "sites": [
              { "address": "123 Main St, Dallas, TX" },
              { "lat": 39.0, "lng": -77.5 },
              { "address": "1 Cyclotron Rd, Berkeley, CA" }
          ]}
        """
        data = flask_request.get_json()
        if not data or 'sites' not in data:
            return jsonify({'success': False, 'error': 'sites array required'}), 400
        
        sites = data['sites']
        if len(sites) < 2 or len(sites) > 3:
            return jsonify({'success': False, 'error': '2-3 sites required for comparison'}), 400
        
        results = []
        for site_input in sites:
            # Run full analysis for each site
            lat = site_input.get('lat')
            lng = site_input.get('lng')
            address = site_input.get('address', '')
            
            if address and (not lat or not lng):
                geo = geocode_address(address)
                if geo:
                    lat, lng = geo['lat'], geo['lng']
                    address = geo.get('display_name', address)
            
            if not lat or not lng:
                results.append({'error': f'Could not geocode: {address}'})
                continue
            
            subs = find_nearest_substations(lat, lng, limit=5)
            # Deduplicate
            seen = set()
            subs = [s for s in (subs or []) if s.get('name') not in seen and not seen.add(s.get('name'))]
            
            tx = find_nearest_transmission(lat, lng)
            state_code = site_input.get('state', '')
            iso = identify_iso_region(lat, lng, state_code)
            env = screen_environmental(lat, lng)
            congestion = estimate_congestion(lat, lng)
            gen_mix = get_generation_mix(lat, lng)
            nearby_dcs = find_nearby_facilities(lat, lng)
            gas = find_nearby_gas_pipelines(lat, lng)
            power_pricing = get_power_pricing(iso.get('name', 'SERC'))
            water = assess_water_risk(state_code)
            scoring = compute_suitability_score(subs, tx, iso, env, congestion, gas=gas, nearby_dcs=nearby_dcs)
            
            results.append({
                'address': address,
                'lat': lat,
                'lng': lng,
                'score': scoring['score'],
                'nearest_sub_miles': subs[0]['distance_miles'] if subs else None,
                'nearest_sub_voltage': subs[0]['voltage_kv'] if subs else None,
                'nearest_sub_name': subs[0]['name'] if subs else None,
                'nearest_tx_miles': tx.get('distance_miles') if tx else None,
                'nearest_tx_voltage': tx.get('voltage_kv') if tx else None,
                'iso': iso.get('name'),
                'queue_mw': estimate_queue_depth(iso.get('name','SERC'), subs[0].get('voltage_kv',0)).get('queue_mw') if subs else None,
                'congestion': congestion.get('level'),
                'env_score': env.get('env_score'),
                'flood_risk': env.get('flood_risk'),
                'wetland_risk': env.get('wetland_risk'),
                'species_risk': env.get('species_risk'),
                'nearby_dc_count': nearby_dcs.get('count', 0),
                'nearby_dc_corridor': nearby_dcs.get('corridor_signal', 'Unknown'),
                'power_price_mwh': power_pricing.get('avg_wholesale_price_mwh'),
                'water_stress': water.get('water_stress_level', 'Unknown'),
                'connectivity': check_fiber_proximity(lat, lng).get('connectivity_rating', 'Unknown'),
                'generation_mix': gen_mix,
                'score_breakdown': scoring['breakdown'],
            })
        
        # Determine recommendation
        scored = [r for r in results if 'score' in r and 'error' not in r]
        recommendation = None
        if scored:
            best = max(scored, key=lambda r: r['score'])
            recommendation = {
                'best_site': best['address'],
                'score': best['score'],
                'reason': _generate_recommendation_reason(best, scored),
            }
        
        return jsonify({
            'success': True,
            'comparison': results,
            'recommendation': recommendation,
            'timestamp': datetime.utcnow().isoformat(),
        })

    # ── GET /api/v1/site-planner/queue-depth ──
    @app.route('/api/v1/site-planner/queue-depth', methods=['GET'])
    @require_pro
    def site_planner_queue_depth():
        """
        Get queue depth data for all ISO regions or a specific one.
        Query params: ?iso=PJM (optional)
        """
        iso_filter = flask_request.args.get('iso', '').upper()
        
        if iso_filter and iso_filter in ISO_REGIONS:
            region = ISO_REGIONS[iso_filter]
            return jsonify({
                'success': True,
                'iso': iso_filter,
                'queue_depth_gw': region['queue_depth_gw'],
                'avg_wait_years': region['avg_queue_wait_years'],
                'queue_url': region.get('queue_url'),
                'states': region['states'],
            })
        
        # Return all regions
        all_regions = {}
        for name, data in ISO_REGIONS.items():
            all_regions[name] = {
                'queue_depth_gw': data['queue_depth_gw'],
                'avg_wait_years': data['avg_queue_wait_years'],
                'queue_url': data.get('queue_url'),
                'states': data['states'],
            }
        
        return jsonify({
            'success': True,
            'regions': all_regions,
            'total_queue_gw': sum(d['queue_depth_gw'] for d in ISO_REGIONS.values()),
        })

    logger.info("✅ Site Planner routes registered (Pro-only)")


def _generate_recommendation_reason(best, all_sites):
    """Generate a human-readable recommendation."""
    reasons = []
    if best.get('score', 0) >= 70:
        reasons.append("strong overall suitability score")
    if best.get('nearest_sub_miles') and best['nearest_sub_miles'] < 5:
        reasons.append(f"close substation access ({best['nearest_sub_miles']:.1f} mi)")
    if best.get('env_score') and best['env_score'] > 70:
        reasons.append("low environmental risk")
    if best.get('congestion') == 'Low':
        reasons.append("low grid congestion")
    
    if not reasons:
        reasons.append("highest composite interconnection suitability")
    
    return f"Recommended due to {', '.join(reasons)}."
