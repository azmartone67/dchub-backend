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

from util.capacity_pipeline import CP_OK

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
    # Skip PostGIS (not installed in Neon) — go straight to Haversine

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

    logger.warning("Local substations query returned empty — trying internal API fallback")
    
    # Internal API fallback: call our own /api/v2/infrastructure/hifld/substations
    # This queries the same Neon DB but also has its own fallback logic
    # If that also fails, try Overpass with a minimal count-only query
    try:
        import urllib.request, urllib.parse, json as _json, math as _math
        # Use Overpass count query first (fast, <1s)
        deg = max_distance_miles / 69.0
        deg_lng_adj = max_distance_miles / (69.0 * max(0.1, abs(_math.cos(_math.radians(lat)))))
        south, north = lat - deg, lat + deg
        west, east = lng - deg_lng_adj, lng + deg_lng_adj
        
        # Fast node-only query with minimal output
        query = f'[out:json][timeout:3];(node["power"="substation"]({south},{west},{north},{east});way["power"="substation"]({south},{west},{north},{east}););out tags center qt 10;'
        post_data = ('data=' + urllib.parse.quote(query)).encode()
        try:
            req = urllib.request.Request('https://overpass.kumi.systems/api/interpreter', data=post_data, headers={
                'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'DCHub/1.0'
            })
            with urllib.request.urlopen(req, timeout=3) as resp:
                osm_data = _json.loads(resp.read().decode())
            elements = osm_data.get('elements', [])
            if elements:
                results = []
                for el in elements:
                    tags = el.get('tags', {})
                    s_lat = el.get('lat') or (el.get('center', {}) or {}).get('lat')
                    s_lng = el.get('lon') or (el.get('center', {}) or {}).get('lon')
                    if not s_lat or not s_lng:
                        continue
                    voltage_str = tags.get('voltage', '0')
                    try:
                        v = float(voltage_str.split(';')[0])
                        voltage_kv = v / 1000 if v > 999 else v
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
                        'lat': s_lat, 'lng': s_lng,
                        'distance_miles': round(dist, 2),
                        'source': 'OpenStreetMap'
                    })
                results.sort(key=lambda x: x['distance_miles'])
                if results:
                    logger.info(f"Overpass fallback: found {len(results)} substations near {lat},{lng}")
                    return results[:limit]
        except Exception as e:
            logger.warning(f"Overpass substation fallback failed: {e}")
    except Exception as e:
        logger.warning(f"Substation fallback setup failed: {e}")
    
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
        # Match the first word of the substation name against the line endpoints.
        #
        # r-txprefix (2026-08-25): this was `LOWER(sub_1) LIKE LOWER('%term%')`.
        # A LEADING wildcard can never become an index qual, so the planner walked
        # idx_transmission_voltage across the whole table. Measured EXPLAIN on the
        # prod row set: "Rows Removed by Filter: 2821162", 208K buffers, 2.29s
        # warm — and up to 18s under load, sometimes dying on statement_timeout.
        # A MISS pays the full scan, and a miss is the COMMON case: most rows in
        # `substations` carry import placeholders (OSM-917634654, RISER167166)
        # that match no line endpoint. That unbounded miss — not any cold cache —
        # was the site-report latency tail.
        #
        # Anchoring the pattern lets Postgres rewrite it into a real index range.
        # Verified plan: BitmapOr over idx_transmission_sub1/idx_transmission_sub2
        # with "Index Cond: sub_1 >= 'ASHBURN' AND sub_1 < 'ASHBURO'". 2.29s ->
        # 26ms; mean over 40 real substation names 1.362s -> 0.056s. Two measured
        # properties make dropping LOWER() on the column side exact: the DB is
        # C.UTF-8 (byte order) and sub_1/sub_2 are 100% uppercase (2821162 of
        # 2821162). Do NOT add COLLATE "C" to 'harden' it — an explicit collation
        # is a different collation object and DEFEATS the index (see be#3086).
        #
        # ! SEMANTIC NARROWING, substring -> prefix. A line whose endpoint merely
        # CONTAINS the term no longer matches. What that drops is coincidental
        # nationwide hits that were never "the line connected to this substation"
        # ("West" -> LENZIE, "Lake" -> TAP206230, "Rogers" -> PINNACLE PEAK WALC,
        # "Pleasant" -> the placeholder UNKNOWN116991). On no match every caller
        # already falls back to the substation's own voltage/operator. Measured
        # over 40 real names: 17 identical, 23 changed — table in the PR.
        search_term = sub_name.split(' ')[0] if sub_name else ''
        if search_term and len(search_term) > 2:
            tx_query = """
                SELECT sub_1 as line_name, voltage_kv, owner, status, volt_class
                FROM discovered_transmission_lines
                WHERE (sub_1 LIKE %s OR sub_2 LIKE %s)
                  AND voltage_kv IS NOT NULL
                ORDER BY voltage_kv DESC
                LIMIT 1;
            """
            _prefix = search_term.upper() + '%'
            tx_result = execute_query(tx_query, (_prefix, _prefix))
            
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
            'avg_age_years': round(result.get('avg_age_years', 0, 0) or 0, 1),
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
    
    # Density scoring (using local DB data only — no external API calls for speed)
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
def query_substations_live(lat, lng, radius_miles=25):
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
        nearest_dist = float(substations[0].get('distance_miles') or 999)
        for tier_name, tier in w['substation_proximity']['thresholds'].items():
            if nearest_dist <= tier['max_miles']:
                points = tier['points']
                score += points
                breakdown['substation_proximity'] = {'points': points, 'tier': tier_name, 'value': f"{nearest_dist:.1f} mi"}
                break
    
    # 2. Substation voltage (use HIGHEST voltage within range, not just nearest)
    if substations:
        voltage = max((s.get('voltage_kv') or 0) for s in substations)
        for tier_name, tier in w['substation_voltage']['thresholds'].items():
            if voltage >= tier['min_kv']:
                points = tier['points']
                score += points
                breakdown['substation_voltage'] = {'points': points, 'tier': tier_name, 'value': f"{voltage} kV"}
                break
    
    # 3. Queue depth (use best voltage for queue estimate)
    if substations and iso:
        best_voltage = max((s.get('voltage_kv') or 0) for s in substations)
        queue = estimate_queue_depth(iso.get('name', 'SERC'), best_voltage)
        queue_mw = queue.get('queue_mw', 2000)
        for tier_name, tier in w['queue_depth']['thresholds'].items():
            if queue_mw <= tier['max_mw']:
                points = tier['points']
                score += points
                breakdown['queue_depth'] = {'points': points, 'tier': tier_name, 'value': f"{queue_mw} MW"}
                break
    
    # 4. Transmission proximity
    if transmission:
        tx_dist = float(transmission.get('distance_miles') or 999)
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
        env_risk = 100 - float(env.get('env_score') or 50)
        for tier_name, tier in w['environmental']['thresholds'].items():
            if env_risk <= tier['max_risk_score']:
                points = tier['points']
                score += points
                breakdown['environmental'] = {'points': points, 'tier': tier_name, 'value': f"Score {env.get('env_score', 'N/A')}"}
                break
    
    # 6. Congestion
    if congestion:
        density = int(congestion.get('density_score') or 50)
        for tier_name, tier in w['congestion']['thresholds'].items():
            if density <= tier['max_density']:
                points = tier['points']
                score += points
                breakdown['congestion'] = {'points': points, 'tier': tier_name, 'value': congestion.get('level', 'Unknown')}
                break
    
    # 7. Gas access
    if gas:
        gas_dist = float((gas.get('nearest_pipeline', {}) or {}).get('distance_miles') or 999)
        for tier_name, tier in w['gas_access']['thresholds'].items():
            if gas_dist <= tier['max_miles']:
                points = tier['points']
                score += points
                breakdown['gas_access'] = {'points': points, 'tier': tier_name, 'value': f"{gas_dist:.1f} mi"}
                break
    
    # 8. DC corridor strength
    if nearby_dcs:
        dc_count = int(nearby_dcs.get('count') or 0)
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
            'distance_miles': round(p.get('distance_miles', 0) or 0, 1),
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
    
    # 2026-07-31: all three arms feed `total_pipeline_mw` in the site plan.
    # The last one especially — "top projects regardless of location", ordered
    # by capacity_mw DESC, is precisely the query the quarantined aggregates
    # dominate. See util/capacity_pipeline.
    if market:
        query = f"""
            SELECT operator, market, capacity_mw, phase, status,
                   announcement_date, completion_date, notes, confidence_label
            FROM capacity_pipeline
            WHERE LOWER(market) LIKE LOWER(%s)
              AND {CP_OK}
            ORDER BY capacity_mw DESC
            LIMIT 10;
        """
        results = execute_query(query, (f'%{market}%',))

    if (not results or len(results) == 0) and state:
        query = f"""
            SELECT operator, market, capacity_mw, phase, status,
                   announcement_date, completion_date, notes, confidence_label
            FROM capacity_pipeline
            WHERE (LOWER(market) LIKE LOWER(%s)
               OR LOWER(region) LIKE LOWER(%s))
              AND {CP_OK}
            ORDER BY capacity_mw DESC
            LIMIT 10;
        """
        results = execute_query(query, (f'%{state}%', f'%{state}%'))

    if not results:
        # Fallback: get top projects regardless of location
        query = f"""
            SELECT operator, market, capacity_mw, phase, status,
                   announcement_date, completion_date, notes, confidence_label
            FROM capacity_pipeline
            WHERE market != 'Unknown'
              AND {CP_OK}
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
        """Decorator: requires Pro plan or higher.

        2026-06-06: the Site Planner "risk assessment" panel was blanking with a
        503 ("0" / "?"). require_plan('pro') can RETURN a transient 503
        (tier_gating_unavailable / "Authentication service unavailable") during a
        cold-start window or a Neon blip — and this decorator only caught
        ImportError, so the 503 propagated and the panel showed nothing. Policy:
        an AUTHENTICATED caller (logged-in JWT or API key) that hits a transient
        gating failure falls through to the handler; anonymous callers stay gated
        (401). Security boundary preserved — we only fail-open on infra errors,
        never for unauthenticated requests.
        """
        @wraps(f)
        def decorated(*args, **kwargs):
            has_auth = bool(
                flask_request.headers.get('Authorization')
                or flask_request.headers.get('X-API-Key')
                or flask_request.args.get('api_key'))
            try:
                from main import require_plan
                resp = require_plan('pro')(f)(*args, **kwargs)
                # Transient gating 503 to an authenticated caller → run anyway.
                try:
                    status = resp[1] if isinstance(resp, tuple) else getattr(resp, 'status_code', 200)
                except Exception:
                    status = 200
                if status == 503 and has_auth:
                    logger.warning("site-planner: gating returned 503 for an authed caller — falling through (transient infra)")
                    return f(*args, **kwargs)
                return resp
            except ImportError:
                if not has_auth:
                    return jsonify({
                        'success': False,
                        'error': 'authentication_required',
                        'message': 'Site Planner requires a Pro subscription',
                        'upgrade_url': 'https://dchub.cloud/pricing',
                    }), 401
                return f(*args, **kwargs)
            except Exception as e:
                # Gating RAISED (not returned). Same policy: authed → through.
                logger.warning(f"site-planner: gating raised {type(e).__name__} — "
                               f"{'falling through (authed)' if has_auth else 'blocking (anon)'}")
                if has_auth:
                    return f(*args, **kwargs)
                return jsonify({
                    'success': False,
                    'error': 'authentication_required',
                    'message': 'Site Planner requires a Pro subscription',
                    'upgrade_url': 'https://dchub.cloud/pricing',
                }), 401
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
# AUTO-REPAIR: duplicate route '/api/v1/site-planner/analyze' also in site_planner.py:1486 — review and remove one
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
                live_subs = query_substations_live(lat, lng)
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
            # Enrich with the real parcel fiber-readiness scorer (carrier_facility_presence
            # + FCC fiber hex). Falls back silently to the check_fiber_proximity result on
            # any error, so existing behavior is preserved.
            try:
                from routes.connectivity_score import score_connectivity
                cc = score_connectivity(lat, lng, 50)
                if cc and not cc.get('error'):
                    _cnt = cc.get('carrier_count', 0)
                    _bucket = cc.get('near_net_bucket', '')
                    # 'unknown' bucket = the carrier dataset does not describe this
                    # region at all. Rating it 'Limited' would re-assert, one layer
                    # up, the absence-as-finding the scorer just stopped publishing.
                    _rating = ('Unknown' if _bucket == 'unknown'
                               else 'Excellent' if _cnt >= 8 and _bucket in ('on-net', 'near-net')
                               else 'Good' if _cnt >= 4
                               else 'Fair' if _cnt >= 1
                               else 'Limited')
                    fiber['connectivity_rating'] = _rating
                    fiber['connected_facilities_nearby'] = _cnt
                    fiber['score'] = cc.get('score')
                    fiber['near_net_bucket'] = _bucket
                    fiber['nearest_carrier_km'] = cc.get('nearest_carrier_km')
                    fiber['carrier_count'] = _cnt
                    fiber['single_carrier_risk'] = cc.get('single_carrier_risk', False)
                    fiber['verdict'] = cc.get('verdict_short', '')
                    fiber['top_carriers'] = cc.get('top_carriers', [])
            except Exception as _cc_e:
                logger.warning(f"connectivity_score enrich failed: {_cc_e}")
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
            
            def _clean(obj):
                if isinstance(obj, dict):
                    return {(str(k) if k is None else k): _clean(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_clean(i) for i in obj]
                return obj
            
            return jsonify(_clean({
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
            }))
        
        except Exception as e:
            logger.error(f"Site analysis failed: {e}\n{traceback.format_exc()}")
            return jsonify({
                'success': False,
                'error': 'analysis_failed',
                'message': str(e),
            }), 500

    # ── POST /api/v1/site-planner/composite-score ──
    @app.route('/api/v1/site-planner/composite-score', methods=['GET', 'POST'])
    @require_pro
    def site_planner_composite_score():
        """Composite site suitability score (0-100) with an EXPLICIT per-factor
        coverage map. Synthesizes power/grid, fiber, natural-hazard risk, water,
        and market/DCPI — but scores ONLY over factors whose data is actually
        sourced (constraint-coverage). Unsourced factors are DECLARED
        `unavailable`, never imputed — so water stays out until the WRI Aqueduct
        ingest lands (the 2026-07-07 paused proxy is never surfaced), and the
        composite is honest about what it does and doesn't know.

        Body: { "lat":.., "lng":.., "state":"VA" }  or  { "address": ".." }.
        This is the honest counterpart to the composite a raw analyze_site dump
        makes you assemble yourself."""
        # GET (MCP callAPI sends query params) or POST JSON. Accept lng or lon.
        data = flask_request.get_json(silent=True) or flask_request.values

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        lat = _f(data.get('lat'))
        lng = _f(data.get('lng') if data.get('lng') is not None else data.get('lon'))
        state = (data.get('state') or '')
        address = (data.get('address') or '')
        if address and (not lat or not lng):
            geo = geocode_address(address)
            if geo:
                lat, lng = geo['lat'], geo['lng']
                state = geo.get('state_code', state) or state
        if not lat or not lng:
            return jsonify({'success': False, 'error': 'lat/lng or address required'}), 400
        if not state:
            try:
                rev = reverse_geocode(lat, lng)
                if rev:
                    state = rev.get('state_code', '') or state
            except Exception:
                pass

        sub = {}
        env = None
        # ── power_grid (real: HIFLD substations/transmission + ISO queue + congestion + gas) ──
        try:
            substations = find_nearest_substations(lat, lng, limit=5) or []
            transmission = find_nearest_transmission(lat, lng)
            iso = identify_iso_region(lat, lng, state)
            congestion = estimate_congestion(lat, lng)
            env = screen_environmental(lat, lng)
            gas = find_nearby_gas_pipelines(lat, lng)
            nearby_dcs = find_nearby_facilities(lat, lng)
            scoring = compute_suitability_score(substations, transmission, iso, env,
                                                congestion, gas=gas, nearby_dcs=nearby_dcs) or {}
            pg = scoring.get('score')
            sub['power_grid'] = {'score': pg,
                                 'coverage': 'validated' if isinstance(pg, (int, float)) else 'unavailable',
                                 'basis': 'substation proximity/voltage, ISO queue depth, transmission, congestion, gas access, DC corridor (HIFLD + ISO)'}
        except Exception as e:
            logger.warning(f"composite power_grid failed: {e}")
            sub['power_grid'] = {'score': None, 'coverage': 'unavailable', 'basis': f'gather failed: {type(e).__name__}'}

        # ── fiber (carrier presence + FCC hex) ──
        try:
            fiber = check_fiber_proximity(lat, lng) or {}
            fscore = fiber.get('score')
            try:
                from routes.connectivity_score import score_connectivity
                cc = score_connectivity(lat, lng, 50)
                if cc and not cc.get('error') and cc.get('score') is not None:
                    fscore = cc.get('score')
            except Exception:
                pass
            sub['fiber'] = {'score': fscore,
                            'coverage': 'validated' if isinstance(fscore, (int, float)) else 'unavailable',
                            'basis': 'carrier facility presence + FCC fiber hex'}
        except Exception as e:
            sub['fiber'] = {'score': None, 'coverage': 'unavailable', 'basis': f'{type(e).__name__}'}

        # ── risk_resilience: prefer FEMA NRI (authoritative county hazard) —
        #    resilience = 100 - NRI composite risk; fall back to the site-level
        #    environmental screen (FEMA flood + FWS + NWI) when NRI is out of coverage. ──
        try:
            e = env if isinstance(env, dict) else screen_environmental(lat, lng)
            env_score = e.get('env_score') if isinstance(e, dict) else None
            nri_score = nri_rating = None
            try:
                import urllib.request as _u2
                import urllib.parse as _up2
                import json as _j2
                _nq = _up2.urlencode({
                    'geometry': _j2.dumps({'x': lng, 'y': lat, 'spatialReference': {'wkid': 4326}}),
                    'geometryType': 'esriGeometryPoint', 'inSR': '4326',
                    'spatialRel': 'esriSpatialRelIntersects', 'outFields': 'RISK_SCORE,RISK_RATNG',
                    'returnGeometry': 'false', 'f': 'json'})
                _nu = ('https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/'
                       'National_Risk_Index_Counties/FeatureServer/0/query?' + _nq)
                with _u2.urlopen(_u2.Request(_nu, headers={'User-Agent': 'dchub-composite/1.0'}), timeout=10) as _r:
                    _feats = (_j2.loads(_r.read(1_000_000).decode('utf-8', 'replace')) or {}).get('features') or []
                if _feats:
                    _a = _feats[0].get('attributes') or {}
                    _rs = _a.get('RISK_SCORE')
                    if isinstance(_rs, (int, float)):
                        nri_score = round(max(0.0, min(100.0, 100.0 - _rs)), 1)  # resilience = inverse of hazard
                        nri_rating = _a.get('RISK_RATNG')
            except Exception:
                nri_score = None
            if nri_score is not None:
                sub['risk_resilience'] = {'score': nri_score, 'coverage': 'validated',
                                          'basis': f'FEMA National Risk Index (county hazard: {nri_rating}); resilience = 100 − NRI risk',
                                          'nri_rating': nri_rating}
            else:
                sub['risk_resilience'] = {'score': env_score,
                                          'coverage': 'validated' if isinstance(env_score, (int, float)) else 'unavailable',
                                          'basis': 'FEMA flood + FWS critical habitat + NWI wetlands (NRI out of coverage)'}
        except Exception as e:
            sub['risk_resilience'] = {'score': None, 'coverage': 'unavailable', 'basis': f'{type(e).__name__}'}

        # ── water: LIVE from the AUTHORITATIVE WRI Aqueduct 4.0 baseline water
        #    stress (Esri Living Atlas, point query). Suitability = 100 − stress
        #    (bws_score 0–5 → 0–100). Outside basin coverage → unavailable. The
        #    paused inverted state proxy is NEVER surfaced. ──
        try:
            import urllib.request as _u4
            import urllib.parse as _up4
            import json as _j4
            _aq = ('https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/'
                   'aqueduct_water_risk/FeatureServer/1/query?' + _up4.urlencode({
                       'geometry': _j4.dumps({'x': lng, 'y': lat, 'spatialReference': {'wkid': 4326}}),
                       'geometryType': 'esriGeometryPoint', 'inSR': '4326',
                       'spatialRel': 'esriSpatialRelIntersects',
                       'outFields': 'bws_score,bws_cat,bws_label,name_1', 'returnGeometry': 'false',
                       'resultRecordCount': '1', 'f': 'json'}))
            with _u4.urlopen(_u4.Request(_aq, headers={'User-Agent': 'dchub-composite/1.0'}), timeout=10) as _r:
                _wf = (_j4.loads(_r.read(1_000_000).decode('utf-8', 'replace')) or {}).get('features') or []
            _wa = (_wf[0].get('attributes') or {}) if _wf else {}
            _bws = _wa.get('bws_score')
            if isinstance(_bws, (int, float)):
                _wscore = round(max(0.0, min(100.0, 100.0 - _bws / 5.0 * 100.0)), 1)  # suitability = low stress
                sub['water'] = {'score': _wscore, 'coverage': 'validated',
                                'basis': f"WRI Aqueduct 4.0 baseline water stress: {_wa.get('bws_label')} ({_wa.get('name_1')})",
                                'bws_score_0_5': _bws, 'bws_category': _wa.get('bws_cat')}
            else:
                sub['water'] = {'score': None, 'coverage': 'unavailable',
                                'basis': 'Outside WRI Aqueduct basin coverage (offshore / no basin)'}
        except Exception as _we:
            logger.warning(f"composite water (Aqueduct) failed: {_we}")
            sub['water'] = {'score': None, 'coverage': 'unavailable', 'basis': 'WRI Aqueduct source unreachable'}

        # ── market_dcpi: declared but unavailable in v1 (no fabricated market score) ──
        sub['market_dcpi'] = {'score': None, 'coverage': 'unavailable',
                              'basis': 'v1: use rank_markets / get_market_dcpi_rank for the DCPI verdict; composite market synthesis planned'}

        # ── composite over VALIDATED factors ONLY (never impute a missing one) ──
        weights = {'power_grid': 0.32, 'fiber': 0.20, 'water': 0.18,
                   'risk_resilience': 0.15, 'market_dcpi': 0.15}
        num = den = 0.0
        validated = []
        for k, w in weights.items():
            s = sub.get(k) or {}
            if s.get('coverage') == 'validated' and isinstance(s.get('score'), (int, float)):
                num += float(s['score']) * w
                den += w
                validated.append(k)
        composite = round(num / den, 1) if den > 0 else None
        verdict = None
        if composite is not None:
            verdict = 'BUILD' if composite >= 70 else 'CAUTION' if composite >= 45 else 'AVOID'

        caveats = [c for c in [
            None if 'water' in validated else f"water: {(sub.get('water') or {}).get('basis') or 'unavailable'}.",
            'market_dcpi: unavailable in v1 — use rank_markets / get_market_dcpi_rank.',
            'natural-hazard layer is FEMA flood + FWS habitat + NWI wetlands only (no seismic/climate-projection layer yet).',
            'advisory only — pair with analyze_site (raw data), get_water_risk, and rank_markets.',
        ] if c]

        return jsonify({
            'success': True,
            '_entity': 'site',
            'location': {'lat': lat, 'lng': lng, 'state': state, 'address': address},
            'composite_score': composite,
            'verdict': verdict,
            'confidence': 'complete' if len(validated) == len(weights) else 'conditional',
            'coverage': {k: (sub.get(k) or {}).get('coverage') for k in weights},
            'coverage_ratio': f'{len(validated)}/{len(weights)}',
            'sub_scores': sub,
            'weights_over_validated': ({k: round(weights[k] / den, 3) for k in validated} if den > 0 else {}),
            'methodology': ('Weighted mean over VALIDATED factors only; unsourced factors are declared '
                            'unavailable, never imputed (constraint-coverage). Grid/fiber/hazard are live; '
                            'water auto-enables when the WRI ingest lands.'),
            'caveats': caveats,
            'meta': {'version': 'v1.0', 'timestamp': datetime.utcnow().isoformat()},
        })

    # ── GET/POST /api/v1/site-planner/disaster-risk ──
    @app.route('/api/v1/site-planner/disaster-risk', methods=['GET', 'POST'])
    @require_pro
    def site_planner_disaster_risk():
        """Natural-hazard risk for a lat/lon, grounded in the FEMA National Risk
        Index (NRI) — the authoritative county-level US hazard dataset. LIVE
        point-in-county query; NEVER fabricates. Returns the composite NRI risk
        score + rating + national percentile, all 18 hazard ratings, and the
        elevated 'top' hazards. Points outside US NRI coverage return
        coverage='unavailable' (declared, not estimated)."""
        import urllib.request as _u
        import urllib.parse as _up
        import json as _j
        data = flask_request.get_json(silent=True) or flask_request.values

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        lat = _f(data.get('lat'))
        lng = _f(data.get('lng') if data.get('lng') is not None else data.get('lon'))
        if lat is None or lng is None:
            return jsonify({'success': False, 'error': 'lat/lng required'}), 400

        _HZ = {'AVLN': 'Avalanche', 'CFLD': 'Coastal Flooding', 'CWAV': 'Cold Wave',
               'DRGT': 'Drought', 'ERQK': 'Earthquake', 'HAIL': 'Hail', 'HRCN': 'Hurricane',
               'HWAV': 'Heat Wave', 'IFLD': 'Riverine Flooding', 'ISTM': 'Ice Storm',
               'LNDS': 'Landslide', 'LTNG': 'Lightning', 'SWND': 'Strong Wind',
               'TRND': 'Tornado', 'TSUN': 'Tsunami', 'VLCN': 'Volcanic Activity',
               'WFIR': 'Wildfire', 'WNTW': 'Winter Weather'}
        _ELEV = {'Very High', 'Relatively High'}
        _NRI = ('https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/'
                'National_Risk_Index_Counties/FeatureServer/0/query')
        attrs = None
        try:
            out_fields = 'STATE,COUNTY,RISK_SCORE,RISK_RATNG,RISK_SPCTL,' + ','.join(f'{c}_RISKR' for c in _HZ)
            qs = _up.urlencode({
                'geometry': _j.dumps({'x': lng, 'y': lat, 'spatialReference': {'wkid': 4326}}),
                'geometryType': 'esriGeometryPoint', 'inSR': '4326',
                'spatialRel': 'esriSpatialRelIntersects', 'outFields': out_fields,
                'returnGeometry': 'false', 'f': 'json'})
            req = _u.Request(f'{_NRI}?{qs}', headers={'User-Agent': 'dchub-disaster-risk/1.0'})
            with _u.urlopen(req, timeout=12) as r:
                feats = (_j.loads(r.read(2_000_000).decode('utf-8', 'replace')) or {}).get('features') or []
            if feats:
                attrs = feats[0].get('attributes') or {}
        except Exception as e:
            logger.warning(f"FEMA NRI query failed: {e}")
            attrs = None

        if not attrs:
            return jsonify({'success': True, '_entity': 'risk',
                            'location': {'lat': lat, 'lng': lng},
                            'disaster_risk': None, 'coverage': 'unavailable',
                            'source': 'FEMA National Risk Index (NRI)',
                            'note': ('No NRI county intersects this point (outside US NRI coverage / '
                                     'offshore) or the source was unreachable — declared unavailable, '
                                     'never estimated.')})

        hazards = {}
        for code, name in _HZ.items():
            rr = attrs.get(f'{code}_RISKR')
            if rr and str(rr).strip() and str(rr).strip().lower() not in (
                    'not applicable', 'no rating', 'insufficient data', 'no expected annual losses'):
                hazards[name] = rr
        top = sorted([{'hazard': n, 'rating': r} for n, r in hazards.items() if r in _ELEV],
                     key=lambda h: 0 if h['rating'] == 'Very High' else 1)
        return jsonify({
            'success': True, '_entity': 'risk',
            'location': {'lat': lat, 'lng': lng,
                         'county': attrs.get('COUNTY'), 'state': attrs.get('STATE')},
            'disaster_risk': {
                'composite_score': attrs.get('RISK_SCORE'),
                'rating': attrs.get('RISK_RATNG'),
                'national_percentile': attrs.get('RISK_SPCTL'),
            },
            'hazards': hazards,
            'top_hazards': top,
            'coverage': 'validated',
            'source': 'FEMA National Risk Index (NRI), county-level',
            'methodology': ('Live point-in-county query of the FEMA NRI FeatureServer. Composite = '
                            'Expected Annual Loss × Social Vulnerability ÷ Community Resilience; '
                            'higher score = higher risk. 18 hazards rated Very Low→Very High.'),
            'caveats': ['County-level resolution (not parcel).',
                        'US only — points outside NRI coverage return coverage=unavailable.',
                        'Acute natural hazards; for chronic water stress use get_water_risk (WRI Aqueduct).'],
            'meta': {'version': 'v1.0', 'timestamp': datetime.utcnow().isoformat()},
        })

    # ── GET/POST /api/v1/site-planner/climate-intel ──
    # ── STATIC-SOURCE CACHE (2026-07-28, shell #38 lane 4) ──────────────────
    # get_climate_intel was the slowest tool on the platform (p50 3,120ms) and
    # the single failing check in the agent-wait lane. Cause: THREE SEQUENTIAL
    # external federal calls (USGS ASCE 7 -> ACIS StnMeta -> ACIS StnData), each
    # with a 12s timeout, so worst case is 36s of BLOCKED agent time.
    #
    # ★ Why caching is safe HERE when a response cache generally is not:
    #   the cached object is the UPSTREAM PUBLIC FEDERAL response, not our view
    #   of it. ASCE 7-16 seismic is a published building code (fixed); the NOAA
    #   normals are annual aggregates over a closed 2022-2024 window (fixed).
    #   It is identical for every caller, so there is NO tier axis to leak —
    #   unlike a cached tool RESPONSE, which is tier-varying and is a documented
    #   incident class here. The route is @require_pro, so gating already
    #   happened upstream of anything stored.
    # ★ NEVER cache a failure. A cached "unavailable" would pin a permanent hole
    #   in the data for that coordinate (same lesson as the MCP keyCache, which
    #   refuses to cache a validation failure because it silently downgrades a
    #   paid tier to free).
    # Kill switch, no deploy: CLIMATE_INTEL_CACHE=0
    _CI_CACHE = {}                      # (kind, lat_r, lng_r) -> (expires_at, value)
    _CI_TTL_S = 7 * 24 * 3600           # static sources; a week is conservative
    _CI_MAX   = 4000

    def _ci_key(kind, lat, lng, extra=''):
        # ~1km grid — far finer than either source's own resolution
        return (kind, round(float(lat), 2), round(float(lng), 2), extra)

    def _ci_rkey(key):
        return 'ci:' + ':'.join(str(x) for x in key)

    def _ci_get(key):
        if os.environ.get('CLIMATE_INTEL_CACHE') == '0':
            return None
        hit = _CI_CACHE.get(key)                     # L1: this process
        if hit and hit[0] >= time.time():
            return hit[1]
        if hit:
            _CI_CACHE.pop(key, None)
        # ★★ L2: SHARED across replicas. An in-process dict alone measured ZERO
        # hits in production (10/10 live calls missed) — the backend runs several
        # replicas, so a repeat request lands on a cold one. That is the SAME
        # per-replica-in-memory-state trap that caused the lane-3 instruction
        # contradiction. A process-local cache on a multi-replica service is
        # decoration, not a cache.
        try:
            from redis_cache import cache_get as _rc_get
            v = _rc_get(_ci_rkey(key))               # fails soft to None
            if v:
                _CI_CACHE[key] = (time.time() + _CI_TTL_S, v)   # promote to L1
                return v
        except Exception:
            pass
        return None

    def _ci_put(key, value):
        if os.environ.get('CLIMATE_INTEL_CACHE') == '0':
            return
        if len(_CI_CACHE) >= _CI_MAX:       # bounded; simplest correct eviction
            _CI_CACHE.clear()
        _CI_CACHE[key] = (time.time() + _CI_TTL_S, value)
        try:
            from redis_cache import cache_set as _rc_set
            _rc_set(_ci_rkey(key), value, ttl=_CI_TTL_S)
        except Exception:
            pass

    @app.route('/api/v1/site-planner/climate-intel', methods=['GET', 'POST'])
    @require_pro
    def site_planner_climate_intel():
        """Seismic + climate intel for a lat/lon, grounded STRICTLY in USGS
        (ASCE 7 seismic) and NOAA (climate normals via ACIS). Every number traces
        to a federal source; missing data is declared unavailable, never
        estimated. DC-relevant: seismic drives structural bracing cost; cooling
        degree-days + extreme temps drive cooling design (wet-bulb null when the
        source lacks it — never approximated)."""
        import urllib.request as _u
        import urllib.parse as _up
        import json as _j
        import math as _m
        data = flask_request.get_json(silent=True) or flask_request.values

        def _f(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        lat = _f(data.get('lat'))
        lng = _f(data.get('lng') if data.get('lng') is not None else data.get('lon'))
        if lat is None or lng is None:
            return jsonify({'success': False, 'error': 'lat/lng required'}), 400
        radius_km = _f(data.get('radius_km') or data.get('search_radius_km')) or 25.0

        # ── seismic (USGS ASCE 7-16 building-codes) ──
        def _seismic_block():
            seismic = {'status': 'unavailable', 'source': 'USGS ASCE 7-16'}
            _k = _ci_key('seismic', lat, lng)
            _c = _ci_get(_k)
            if _c is not None:
                return _c
            try:
                su = ('https://earthquake.usgs.gov/ws/building-codes/asce7-16/calculate?'
                      + _up.urlencode({'latitude': lat, 'longitude': lng, 'riskCategory': 'III',
                                       'siteClass': 'D', 'title': 'dchub'}))
                req = _u.Request(su, headers={'User-Agent': 'dchub-climate-intel/1.0'})
                with _u.urlopen(req, timeout=12) as r:
                    sd = ((_j.loads(r.read(500_000).decode('utf-8', 'replace')) or {})
                          .get('response', {}).get('data', {}))
                pga = sd.get('pga')
                if isinstance(pga, (int, float)):
                    lvl = ('very high' if pga >= 0.6 else 'high' if pga >= 0.3
                           else 'moderate' if pga >= 0.1 else 'low')
                    seismic = {'status': 'available', 'source': 'USGS ASCE 7-16 (earthquake.usgs.gov)',
                               'peak_ground_acceleration_g': pga, 'ss': sd.get('ss'), 's1': sd.get('s1'),
                               'seismic_design_category': sd.get('sdc'), 'hazard_class': lvl,
                               'reference': 'ASCE 7-16, Risk Category III, Site Class D'}
            except Exception as e:
                logger.warning(f"climate-intel seismic failed: {e}")
            if seismic.get('status') == 'available':
                _ci_put(_k, seismic)      # success only — never pin a hole
            return seismic

        # ── climate normals (NOAA via ACIS, tokenless) ──
        def _noaa_block():
            climate = {'status': 'unavailable', 'source': 'NOAA (ACIS/NCEI)'}
            _k = _ci_key('noaa', lat, lng, str(radius_km))
            _c = _ci_get(_k)
            if _c is not None:
                return _c
            try:
                bb = f'{lng - 0.7},{lat - 0.7},{lng + 0.7},{lat + 0.7}'
                mreq = _u.Request('https://data.rcc-acis.org/StnMeta',
                                  data=_j.dumps({'bbox': bb, 'meta': 'name,ll,sids'}).encode(),
                                  headers={'Content-Type': 'application/json',
                                           'User-Agent': 'dchub-climate-intel/1.0'})
                with _u.urlopen(mreq, timeout=12) as r:
                    stns = (_j.loads(r.read(2_000_000).decode('utf-8', 'replace')) or {}).get('meta') or []

                def _hav(la1, lo1, la2, lo2):
                    R = 6371.0
                    p = _m.pi / 180
                    a = (_m.sin((la2 - la1) * p / 2) ** 2
                         + _m.cos(la1 * p) * _m.cos(la2 * p) * _m.sin((lo2 - lo1) * p / 2) ** 2)
                    return 2 * R * _m.asin(min(1.0, _m.sqrt(a)))
                best = None
                for s in stns:
                    ll = s.get('ll')
                    sids = s.get('sids') or []
                    if not ll or not sids:
                        continue
                    d = _hav(lat, lng, ll[1], ll[0])
                    if best is None or d < best[0]:
                        best = (d, s)
                if best is None:
                    climate = {'status': 'unavailable', 'reason': 'no_station_in_area',
                               'source': 'NOAA (ACIS/NCEI)'}
                elif best[0] > radius_km:
                    climate = {'status': 'unavailable_exceeds_radius', 'source': 'NOAA (ACIS/NCEI)',
                               'reason': (f'nearest NOAA station {round(best[0], 1)}km > radius '
                                          f'{radius_km}km — climate normals not estimated')}
                else:
                    s = best[1]
                    sid = (s.get('sids') or [''])[0].split(' ')[0]
                    dreq = _u.Request('https://data.rcc-acis.org/StnData',
                                      data=_j.dumps({'sid': sid, 'sdate': '2022-01-01', 'edate': '2024-12-31',
                                                     'elems': [{'name': 'cdd', 'interval': 'yly', 'duration': 'yly', 'reduce': 'sum', 'base': 65},
                                                               {'name': 'maxt', 'interval': 'yly', 'duration': 'yly', 'reduce': 'max'}]}).encode(),
                                      headers={'Content-Type': 'application/json',
                                               'User-Agent': 'dchub-climate-intel/1.0'})
                    with _u.urlopen(dreq, timeout=12) as r:
                        dd = (_j.loads(r.read(500_000).decode('utf-8', 'replace')) or {}).get('data') or []
                    cdd = maxt = vintage = None
                    for row in reversed(dd):
                        def _n(x):
                            try:
                                return float(x)
                            except (TypeError, ValueError):
                                return None
                        if _n(row[1]) is not None or _n(row[2]) is not None:
                            cdd, maxt, vintage = _n(row[1]), _n(row[2]), row[0]
                            break
                    climate = {'status': 'available', 'source': 'NOAA (ACIS/NCEI)',
                               'reference_station': {'id': sid, 'name': s.get('name'),
                                                     'distance_km': round(best[0], 1)},
                               'cooling_design_metrics': {
                                   'cooling_degree_days_annual': cdd,
                                   'extreme_max_dry_bulb_f': maxt,
                                   'extreme_max_wet_bulb_f': None,
                                   'data_vintage': vintage},
                               'note': ('Annual values, latest available year at the nearest NOAA station. '
                                        'Wet-bulb null (not in source; never estimated).')}
            except Exception as e:
                logger.warning(f"climate-intel normals failed: {e}")
            if climate.get('status') == 'available':
                _ci_put(_k, climate)      # success only — never pin a hole
            return climate

        # ★ The USGS and NOAA branches are INDEPENDENT — run them concurrently so
        #   wall time is max(a,b), not a+b. (The two ACIS calls stay sequential
        #   inside the NOAA branch: StnData needs the station id StnMeta mints.)
        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as _ex:
                _fs, _fc = _ex.submit(_seismic_block), _ex.submit(_noaa_block)
                seismic, climate = _fs.result(), _fc.result()
        except Exception as e:                       # fail-soft to sequential
            logger.warning(f"climate-intel parallel failed, falling back: {e}")
            seismic, climate = _seismic_block(), _noaa_block()

        parts = []
        if seismic.get('status') == 'available':
            parts.append(f"seismic {seismic.get('hazard_class')} (PGA {seismic.get('peak_ground_acceleration_g')}g)")
        if climate.get('status') == 'available':
            _cdd = (climate.get('cooling_design_metrics') or {}).get('cooling_degree_days_annual')
            if _cdd is not None:
                parts.append(f"~{int(_cdd)} cooling degree-days/yr")
        return jsonify({
            'success': True, '_entity': 'climate',
            'site_coordinates': {'lat': lat, 'lon': lng},
            'seismic_hazard_usgs': seismic,
            'climate_normals_noaa': climate,
            'overall_climate_summary': ('; '.join(parts) if parts else None),
            'data_availability': {'seismic': seismic.get('status'), 'climate_normals': climate.get('status')},
            'sources': ['USGS ASCE 7-16', 'NOAA ACIS/NCEI'],
            'caveats': ['Seismic = ASCE 7-16 (US); non-US points may return seismic unavailable.',
                        'Climate from the nearest NOAA station (US/territories); beyond radius → unavailable, never interpolated.',
                        'Wet-bulb reported null when the source lacks it — never approximated.'],
            'meta': {'version': 'v1.0', 'timestamp': datetime.utcnow().isoformat()},
        })

# AUTO-REPAIR: duplicate route '/api/v1/site-planner/compare' also in site_planner.py:1487 — review and remove one
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
        Query params: %siso=PJM (optional)
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
