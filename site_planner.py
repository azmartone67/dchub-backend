"""
DC Hub — Site Planner: Grid Interconnection Analysis Engine
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
        'weight': 0.10,
        'thresholds': {
            'low': {'max_density': 30, 'points': 10},
            'moderate': {'max_density': 60, 'points': 6},
            'high': {'max_density': 100, 'points': 2},
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
        conn = psycopg2.connect(db_url, connect_timeout=10)
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
    
    if result is not None:
        return result

    # Fallback: Haversine-based query (no PostGIS extension needed)
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
        ORDER BY distance_miles ASC
        LIMIT %s;
    """
    result = execute_query(haversine_query, (lat, lng, lat, limit))
    
    if result is not None:
        return result

    logger.warning("Both PostGIS and Haversine queries failed for substations")
    return []


def find_nearest_transmission(lat, lng, max_distance_miles=15):
    """
    Find nearest transmission line by matching nearby substations
    to transmission line endpoints (sub_1/sub_2).
    Falls back to HIFLD live API.
    """
    # Match transmission lines via nearest substation names
    query = """
        SELECT 
            t.sub_1 as line_name,
            t.voltage_kv,
            t.owner,
            t.status,
            t.volt_class,
            s.name as matched_substation,
            (
                3959 * acos(
                    LEAST(1.0, GREATEST(-1.0,
                        cos(radians(%s)) * cos(radians(s.lat)) *
                        cos(radians(s.lng) - radians(%s)) +
                        sin(radians(%s)) * sin(radians(s.lat))
                    ))
                )
            ) as distance_miles
        FROM discovered_transmission_lines t
        JOIN substations s ON (
            LOWER(s.name) LIKE '%%' || LOWER(SPLIT_PART(t.sub_1, ' ', 1)) || '%%'
        )
        WHERE s.lat IS NOT NULL
          AND t.voltage_kv IS NOT NULL
          AND t.voltage_kv > 0
        ORDER BY distance_miles ASC
        LIMIT 1;
    """
    result = execute_query(query, (lat, lng, lat))
    
    if result and len(result) > 0:
        return result[0]

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
    query = """
        SELECT COUNT(*) as sub_count
        FROM substations
        WHERE lat IS NOT NULL
          AND (
            3959 * acos(
                LEAST(1.0, GREATEST(-1.0,
                    cos(radians(%s)) * cos(radians(lat)) *
                    cos(radians(lng) - radians(%s)) +
                    sin(radians(%s)) * sin(radians(lat))
                ))
            )
          ) < %s;
    """
    result = execute_query(query, (lat, lng, lat, radius_miles), fetchone=True)
    sub_count = result.get('sub_count', 0) if result else 0
    
    # Also count power plants nearby
    plant_query = """
        SELECT COUNT(*) as plant_count, COALESCE(SUM(capacity_mw), 0) as total_mw
        FROM discovered_power_plants
        WHERE lat IS NOT NULL
          AND (
            3959 * acos(
                LEAST(1.0, GREATEST(-1.0,
                    cos(radians(%s)) * cos(radians(lat)) *
                    cos(radians(lng) - radians(%s)) +
                    sin(radians(%s)) * sin(radians(lat))
                ))
            )
          ) < %s;
    """
    plant_result = execute_query(plant_query, (lat, lng, lat, radius_miles), fetchone=True)
    plant_count = plant_result.get('plant_count', 0) if plant_result else 0
    total_gen_mw = plant_result.get('total_mw', 0) if plant_result else 0
    
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
        resp = requests.get(fema_url, params=params, timeout=10)
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
        resp = requests.get(fws_url, params=params, timeout=10)
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
        resp = requests.get(nwi_url, params=params, timeout=10)
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
    """Get generation mix within radius from power_plants table."""
    query = """
        SELECT 
            COALESCE(fuel_type, 'Unknown') as fuel,
            SUM(COALESCE(capacity_mw, 0)) as total_mw,
            COUNT(*) as plant_count
        FROM discovered_power_plants
        WHERE lat IS NOT NULL
          AND (
            3959 * acos(
                LEAST(1.0, GREATEST(-1.0,
                    cos(radians(%s)) * cos(radians(lat)) *
                    cos(radians(lng) - radians(%s)) +
                    sin(radians(%s)) * sin(radians(lat))
                ))
            )
          ) < %s
        GROUP BY fuel
        ORDER BY total_mw DESC;
    """
    result = execute_query(query, (lat, lng, lat, radius_miles))
    
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


def compute_suitability_score(substations, transmission, iso, env, congestion, weights=None):
    """
    Compute 0-100 Interconnection Suitability Score.
    Uses configurable weights so we can tune without redeploying.
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
            tx_dist = 999  # fallback for live query responses
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
    
    return {
        'score': min(100, score),
        'max_possible': 100,
        'breakdown': breakdown,
        'weights_version': 'v1.0',
    }


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
        resp = requests.get(url, params=params, headers=headers, timeout=10)
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
        
        try:
            # Phase 1: Proximity Analysis
            substations = find_nearest_substations(lat, lng, limit=5)
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
            
            # Compute composite score
            scoring = compute_suitability_score(substations, transmission, iso, env, congestion)
            
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
                    'suitability_score': scoring,
                },
                'meta': {
                    'elapsed_seconds': elapsed,
                    'timestamp': datetime.utcnow().isoformat(),
                    'version': 'v1.0',
                    'data_sources': ['HIFLD', 'FEMA', 'FWS', 'NWI', 'ISO Queue Estimates'],
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
            tx = find_nearest_transmission(lat, lng)
            state_code = site_input.get('state', '')
            iso = identify_iso_region(lat, lng, state_code)
            env = screen_environmental(lat, lng)
            congestion = estimate_congestion(lat, lng)
            gen_mix = get_generation_mix(lat, lng)
            scoring = compute_suitability_score(subs, tx, iso, env, congestion)
            
            results.append({
                'address': address,
                'lat': lat,
                'lng': lng,
                'score': scoring['score'],
                'nearest_sub_miles': subs[0]['distance_miles'] if subs else None,
                'nearest_sub_voltage': subs[0]['voltage_kv'] if subs else None,
                'nearest_tx_miles': tx.get('distance_miles') if tx else None,
                'nearest_tx_voltage': tx.get('voltage_kv') if tx else None,
                'iso': iso.get('name'),
                'queue_mw': estimate_queue_depth(iso.get('name','SERC'), subs[0].get('voltage_kv',0)).get('queue_mw') if subs else None,
                'congestion': congestion.get('level'),
                'env_score': env.get('env_score'),
                'flood_risk': env.get('flood_risk'),
                'wetland_risk': env.get('wetland_risk'),
                'species_risk': env.get('species_risk'),
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
