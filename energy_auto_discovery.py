"""
DC Hub Energy Auto-Discovery System v2.3 (PostgreSQL + 23 Markets)
====================================================================
v2.3 Fixes:
  - Added inSR/outSR=4326 to ALL HIFLD spatial queries
  - State-based WHERE fallback when bounding box returns 0
  - Wind queried by STATE (rural, not in metro bounding boxes)
  - Better error logging (logs actual HIFLD response on failure)
  - Increased timeouts for large spatial queries

Automatically discovers and syncs:
- Power plants (HIFLD with lat/lng + EIA capacity data)
- Substations (HIFLD with coordinates)
- Transmission lines (HIFLD)
- Gas pipelines (built-in + FERC data)
- Wind projects (HIFLD — queried by state)

Runs every 15 minutes. 23 monitored markets.
"""

import os
import json
import requests
import threading
import time
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from flask import jsonify, request
from db_utils import get_db

logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

SYNC_INTERVAL_SECONDS = 900
IS_RAILWAY = bool(os.environ.get('RAILWAY_ENVIRONMENT'))

# API Endpoints
HIFLD_TRANSMISSION = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0/query"
HIFLD_WIND_TURBINES = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/US_Wind_Turbines/FeatureServer/0/query"
HIFLD_POWER_PLANTS = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Power_Plants/FeatureServer/0/query"
HIFLD_SUBSTATIONS = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0/query"
EIA_POWER_PLANTS = "https://api.eia.gov/v2/electricity/operating-generator-capacity/data/"

# Key markets to monitor — bounds = [west_lng, south_lat, east_lng, north_lat]
MONITORED_MARKETS = {
    # === Original 8 Primary Markets ===
    'phoenix': {'name': 'Phoenix, AZ', 'bounds': [-112.5, 33.0, -111.5, 34.0], 'state': 'AZ'},
    'dallas': {'name': 'Dallas, TX', 'bounds': [-97.5, 32.5, -96.5, 33.5], 'state': 'TX'},
    'northern_virginia': {'name': 'Northern Virginia', 'bounds': [-77.8, 38.7, -77.0, 39.2], 'state': 'VA'},
    'atlanta': {'name': 'Atlanta, GA', 'bounds': [-84.8, 33.5, -84.0, 34.2], 'state': 'GA'},
    'las_vegas': {'name': 'Las Vegas, NV', 'bounds': [-115.5, 35.8, -114.8, 36.5], 'state': 'NV'},
    'salt_lake': {'name': 'Salt Lake City, UT', 'bounds': [-112.2, 40.5, -111.5, 41.0], 'state': 'UT'},
    'columbus': {'name': 'Columbus, OH', 'bounds': [-83.2, 39.8, -82.7, 40.2], 'state': 'OH'},
    'des_moines': {'name': 'Des Moines, IA', 'bounds': [-93.8, 41.4, -93.4, 41.8], 'state': 'IA'},
    # === Tier 1: Major DC Hubs ===
    'chicago': {'name': 'Chicago, IL', 'bounds': [-88.3, 41.6, -87.3, 42.2], 'state': 'IL'},
    'silicon_valley': {'name': 'Silicon Valley, CA', 'bounds': [-122.5, 37.1, -121.5, 37.7], 'state': 'CA'},
    'new_york_nj': {'name': 'New York / New Jersey', 'bounds': [-74.5, 40.4, -73.7, 41.0], 'state': 'NJ'},
    'seattle_quincy': {'name': 'Seattle / Quincy, WA', 'bounds': [-122.6, 47.0, -119.5, 47.8], 'state': 'WA'},
    'portland_hillsboro': {'name': 'Portland / Hillsboro, OR', 'bounds': [-123.2, 45.3, -122.4, 45.7], 'state': 'OR'},
    # === Tier 2: Fast-Growing ===
    'denver': {'name': 'Denver, CO', 'bounds': [-105.3, 39.5, -104.6, 40.0], 'state': 'CO'},
    'san_antonio': {'name': 'San Antonio, TX', 'bounds': [-98.8, 29.2, -98.1, 29.7], 'state': 'TX'},
    'houston': {'name': 'Houston, TX', 'bounds': [-95.8, 29.5, -95.0, 30.0], 'state': 'TX'},
    'miami': {'name': 'Miami, FL', 'bounds': [-80.5, 25.5, -80.0, 26.0], 'state': 'FL'},
    'reno': {'name': 'Reno, NV', 'bounds': [-120.1, 39.3, -119.5, 39.8], 'state': 'NV'},
    'sacramento': {'name': 'Sacramento, CA', 'bounds': [-121.8, 38.3, -121.1, 38.8], 'state': 'CA'},
    # === Tier 3: Emerging ===
    'minneapolis': {'name': 'Minneapolis, MN', 'bounds': [-93.5, 44.8, -93.0, 45.2], 'state': 'MN'},
    'kansas_city': {'name': 'Kansas City, MO', 'bounds': [-94.9, 38.9, -94.3, 39.3], 'state': 'MO'},
    'richmond': {'name': 'Richmond, VA', 'bounds': [-77.7, 37.3, -77.1, 37.8], 'state': 'VA'},
    'nashville': {'name': 'Nashville, TN', 'bounds': [-87.1, 35.9, -86.4, 36.4], 'state': 'TN'},
}

# Track which states we've already synced wind for (avoid duplicate per-state queries)
_wind_synced_states = set()

# =============================================================================
# DATABASE SETUP (PostgreSQL compatible)
# =============================================================================

def init_discovery_db():
    """Initialize auto-discovery tables (PostgreSQL syntax)"""
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS discovered_power_plants (
                id TEXT PRIMARY KEY,
                name TEXT,
                fuel_type TEXT,
                capacity_mw REAL,
                generation_mwh REAL,
                operator TEXT,
                state TEXT,
                sector TEXT,
                market TEXT,
                lat REAL,
                lng REAL,
                discovered_at TEXT,
                last_updated TEXT,
                source TEXT DEFAULT 'EIA',
                is_new INTEGER DEFAULT 1
            )
        """)
        for col in ['lat REAL', 'lng REAL']:
            try:
                c.execute(f"ALTER TABLE discovered_power_plants ADD COLUMN {col}")
            except:
                pass

        c.execute("""
            CREATE TABLE IF NOT EXISTS discovered_transmission_lines (
                id TEXT PRIMARY KEY,
                owner TEXT,
                voltage_kv REAL,
                volt_class TEXT,
                sub_1 TEXT,
                sub_2 TEXT,
                status TEXT,
                market TEXT,
                discovered_at TEXT,
                last_updated TEXT,
                source TEXT DEFAULT 'HIFLD',
                is_new INTEGER DEFAULT 1
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS discovered_wind_projects (
                id TEXT PRIMARY KEY,
                project_name TEXT,
                project_capacity_mw REAL,
                turbine_capacity_kw REAL,
                manufacturer TEXT,
                model TEXT,
                state TEXT,
                county TEXT,
                lat REAL,
                lng REAL,
                market TEXT,
                discovered_at TEXT,
                last_updated TEXT,
                source TEXT DEFAULT 'HIFLD',
                is_new INTEGER DEFAULT 1
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS discovered_pipelines (
                id TEXT PRIMARY KEY,
                operator TEXT,
                pipeline_type TEXT,
                status TEXT,
                diameter_inches REAL,
                commodity TEXT,
                name TEXT,
                capacity_mdth REAL,
                lat REAL,
                lng REAL,
                states_served TEXT,
                state TEXT,
                market TEXT,
                discovered_at TEXT,
                last_updated TEXT,
                source TEXT,
                is_new INTEGER DEFAULT 1
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS energy_sync_log (
                id SERIAL PRIMARY KEY,
                sync_type TEXT,
                market TEXT,
                items_found INTEGER,
                new_items INTEGER,
                updated_items INTEGER,
                errors TEXT,
                duration_seconds REAL,
                synced_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS energy_discovery_stats (
                id SERIAL PRIMARY KEY,
                stat_date TEXT,
                total_power_plants INTEGER,
                total_transmission_lines INTEGER,
                total_wind_projects INTEGER,
                total_pipelines INTEGER,
                new_transmission_lines INTEGER,
                new_wind_projects INTEGER,
                new_pipelines INTEGER,
                markets_synced INTEGER,
                created_at TEXT
            )
        """)

        conn.commit()
        logger.info("✅ Energy Auto-Discovery tables initialized (PostgreSQL)")
    except Exception as e:
        logger.error(f"⚠️ Energy discovery DB init error: {e}")
        if conn:
            try:
                conn.rollback()
            except:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


# =============================================================================
# API QUERIES — v2.3 FIXED (inSR/outSR + state fallback + logging)
# =============================================================================

def _hifld_spatial_query(url, bounds, out_fields, return_geometry=False, limit=500, label="HIFLD"):
    """
    Generic HIFLD spatial query with proper coordinate system params.
    Falls back to no geometry filter if spatial query returns 0.
    """
    # Build envelope string: xmin,ymin,xmax,ymax
    envelope = f'{bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]}'
    params = {
        'geometry': envelope,
        'geometryType': 'esriGeometryEnvelope',
        'spatialRel': 'esriSpatialRelIntersects',
        'inSR': '4326',
        'outSR': '4326',
        'outFields': out_fields,
        'returnGeometry': 'true' if return_geometry else 'false',
        'f': 'json',
        'resultRecordCount': limit,
    }
    try:
        response = requests.get(url, params=params, timeout=90)
        data = response.json()

        if 'error' in data:
            logger.warning(f"⚠️ {label} API error: {data['error']}")
            return []

        features = data.get('features', [])
        return features

    except requests.Timeout:
        logger.warning(f"⚠️ {label} timeout (90s) for bounds {envelope}")
        return []
    except Exception as e:
        logger.warning(f"⚠️ {label} query error: {e}")
        return []


def _hifld_state_query(url, state, out_fields, state_field='STATE', return_geometry=False, limit=500, label="HIFLD"):
    """Query HIFLD by state WHERE clause (fallback when spatial returns 0)."""
    params = {
        'where': f"{state_field} = '{state}'",
        'outFields': out_fields,
        'outSR': '4326',
        'returnGeometry': 'true' if return_geometry else 'false',
        'f': 'json',
        'resultRecordCount': limit,
    }
    try:
        response = requests.get(url, params=params, timeout=90)
        data = response.json()
        if 'error' in data:
            logger.warning(f"⚠️ {label} state query error: {data['error']}")
            return []
        return data.get('features', [])
    except Exception as e:
        logger.warning(f"⚠️ {label} state query failed: {e}")
        return []


def query_transmission_lines(bounds, state=None, limit=500):
    """Query HIFLD for transmission lines — spatial first, state fallback"""
    out_fields = 'OWNER,VOLTAGE,VOLT_CLASS,SUB_1,SUB_2,STATUS'

    features = _hifld_spatial_query(
        HIFLD_TRANSMISSION, bounds, out_fields,
        return_geometry=False, limit=limit, label="TX Lines (spatial)"
    )

    # Fallback: query by state if spatial returned nothing
    if not features and state:
        logger.info(f"      ↳ TX Lines spatial=0, trying state={state}...")
        features = _hifld_state_query(
            HIFLD_TRANSMISSION, state, out_fields,
            state_field='STATE', limit=limit, label="TX Lines (state)"
        )

    lines = []
    for f in features:
        attrs = f.get('attributes', {})
        voltage = attrs.get('VOLTAGE', 0) or 0
        owner = attrs.get('OWNER', 'Unknown') or 'Unknown'
        # Create a more stable ID using owner + voltage + substations
        sub1 = attrs.get('SUB_1', '') or ''
        sub2 = attrs.get('SUB_2', '') or ''
        id_str = f"{owner}-{voltage}-{sub1}-{sub2}"
        lines.append({
            'id': f"tx-{abs(hash(id_str)) % 100000000}",
            'owner': owner,
            'voltage_kv': voltage,
            'volt_class': attrs.get('VOLT_CLASS', 'Unknown') or 'Unknown',
            'sub_1': sub1,
            'sub_2': sub2,
            'status': attrs.get('STATUS', 'Unknown') or 'Unknown',
        })

    if lines:
        logger.info(f"   ⚡ {len(lines)} transmission lines found")
    return lines


def query_hifld_power_plants(bounds, state=None, limit=500):
    """Query HIFLD for power plants WITH lat/lng coordinates"""
    out_fields = 'NAME,PRIM_FUEL,TOTAL_MW,OPER_CAP,OPERATOR,STATE,STATUS,COUNTY,SECTOR_NAM'

    features = _hifld_spatial_query(
        HIFLD_POWER_PLANTS, bounds, out_fields,
        return_geometry=True, limit=limit, label="Power Plants (spatial)"
    )

    # Fallback: query by state
    if not features and state:
        logger.info(f"      ↳ Power Plants spatial=0, trying state={state}...")
        features = _hifld_state_query(
            HIFLD_POWER_PLANTS, state, out_fields,
            state_field='STATE', return_geometry=True, limit=limit,
            label="Power Plants (state)"
        )

    plants = []
    for f in features:
        attrs = f.get('attributes', {})
        geom = f.get('geometry', {})
        name = attrs.get('NAME', 'Unknown') or 'Unknown'
        cap = attrs.get('TOTAL_MW') or attrs.get('OPER_CAP') or 0
        try:
            cap = float(cap)
        except:
            cap = 0

        if cap < 1:
            continue

        lat = geom.get('y')
        lng = geom.get('x')

        plants.append({
            'id': f"hifld-{abs(hash(name + str(lng))) % 100000000}",
            'name': name,
            'fuel_type': attrs.get('PRIM_FUEL', 'Unknown') or 'Unknown',
            'capacity_mw': cap,
            'generation_mwh': 0,
            'operator': attrs.get('OPERATOR', 'Unknown') or 'Unknown',
            'state': attrs.get('STATE', '') or '',
            'sector': attrs.get('SECTOR_NAM', 'Unknown') or 'Unknown',
            'lat': lat,
            'lng': lng,
            'source': 'HIFLD',
        })

    if plants:
        logger.info(f"   📡 HIFLD: {len(plants)} power plants with coordinates")
    return plants


def query_hifld_substations(bounds, state=None, limit=500):
    """Query HIFLD for substations with coordinates"""
    out_fields = 'NAME,STATE,STATUS,OWNER,MAX_VOLT,MIN_VOLT,LINES,TYPE'

    features = _hifld_spatial_query(
        HIFLD_SUBSTATIONS, bounds, out_fields,
        return_geometry=True, limit=limit, label="Substations (spatial)"
    )

    if not features and state:
        logger.info(f"      ↳ Substations spatial=0, trying state={state}...")
        features = _hifld_state_query(
            HIFLD_SUBSTATIONS, state, out_fields,
            state_field='STATE', return_geometry=True, limit=limit,
            label="Substations (state)"
        )

    subs = []
    for f in features:
        attrs = f.get('attributes', {})
        geom = f.get('geometry', {})
        name = attrs.get('NAME', 'Unknown') or 'Unknown'
        subs.append({
            'id': f"sub-{abs(hash(name + str(geom.get('x', '')))) % 100000000}",
            'name': name,
            'owner': attrs.get('OWNER', 'Unknown') or 'Unknown',
            'voltage_kv': attrs.get('MAX_VOLT', 0) or 0,
            'min_voltage_kv': attrs.get('MIN_VOLT', 0) or 0,
            'status': attrs.get('STATUS', 'Unknown') or 'Unknown',
            'lines': attrs.get('LINES', 0) or 0,
            'type': attrs.get('TYPE', 'Unknown') or 'Unknown',
            'state': attrs.get('STATE', '') or '',
            'lat': geom.get('y'),
            'lng': geom.get('x'),
        })

    if subs:
        logger.info(f"   📡 HIFLD: {len(subs)} substations with coordinates")
    return subs


def query_wind_turbines(bounds=None, state=None, limit=500):
    """
    Query HIFLD for wind turbines.
    v2.3: Queries by STATE (wind farms are rural, not in metro bounding boxes).
    Deduplicates by project name.
    """
    features = []

    # Primary: query by state (wind farms are rarely inside metro bounding boxes)
    if state:
        features = _hifld_state_query(
            HIFLD_WIND_TURBINES, state, 'p_name,p_cap,t_cap,t_manu,t_model,t_state,t_county',
            state_field='t_state', return_geometry=True, limit=limit,
            label=f"Wind ({state})"
        )

    # Fallback: spatial query if state didn't work
    if not features and bounds:
        features = _hifld_spatial_query(
            HIFLD_WIND_TURBINES, bounds,
            'p_name,p_cap,t_cap,t_manu,t_model,t_state,t_county',
            return_geometry=True, limit=limit, label="Wind (spatial)"
        )

    turbines = []
    seen = set()
    for f in features:
        attrs = f.get('attributes', {})
        project = attrs.get('p_name', 'Unknown') or 'Unknown'
        if project in seen or project == 'Unknown':
            continue
        seen.add(project)
        geom = f.get('geometry', {})
        turbines.append({
            'id': f"wind-{abs(hash(project)) % 100000000}",
            'project_name': project,
            'project_capacity_mw': attrs.get('p_cap', 0) or 0,
            'turbine_capacity_kw': attrs.get('t_cap', 0) or 0,
            'manufacturer': attrs.get('t_manu', 'Unknown') or 'Unknown',
            'model': attrs.get('t_model', 'Unknown') or 'Unknown',
            'state': attrs.get('t_state', '') or '',
            'county': attrs.get('t_county', '') or '',
            'lat': geom.get('y'),
            'lng': geom.get('x'),
        })

    if turbines:
        logger.info(f"   🌬️ {len(turbines)} wind projects in {state or 'area'}")
    return turbines


def query_pipelines(bounds=None, state=None, limit=200):
    """Return major interstate gas pipelines serving DC markets + any DB-discovered ones"""
    major_pipelines = [
        {'id': 'pipe-transco-001', 'operator': 'Transcontinental Gas (Williams)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 42, 'commodity': 'Natural Gas', 'name': 'Transco Pipeline', 'capacity_mdth': 17400, 'lat': 39.0, 'lng': -77.5, 'states': 'TX,LA,MS,AL,GA,SC,NC,VA,MD,PA,NJ,NY'},
        {'id': 'pipe-tet-001', 'operator': 'Texas Eastern (Enbridge)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'Texas Eastern Pipeline', 'capacity_mdth': 10200, 'lat': 33.7, 'lng': -84.3, 'states': 'TX,LA,MS,AL,GA,TN,KY,OH,PA,NJ,NY'},
        {'id': 'pipe-elp-001', 'operator': 'El Paso Natural Gas (Kinder Morgan)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'El Paso Natural Gas', 'capacity_mdth': 5600, 'lat': 33.4, 'lng': -112.0, 'states': 'TX,NM,AZ,CA,NV'},
        {'id': 'pipe-kern-001', 'operator': 'Kern River Gas (Berkshire Hathaway)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'Kern River Pipeline', 'capacity_mdth': 2300, 'lat': 36.2, 'lng': -115.1, 'states': 'WY,UT,NV,CA'},
        {'id': 'pipe-sonat-001', 'operator': 'Southern Natural Gas (Williams)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'Southern Natural Gas', 'capacity_mdth': 6500, 'lat': 33.7, 'lng': -84.3, 'states': 'TX,LA,MS,AL,GA,SC'},
        {'id': 'pipe-cgt-001', 'operator': 'Columbia Gas Transmission (TC Energy)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'Columbia Gas Transmission', 'capacity_mdth': 6200, 'lat': 39.9, 'lng': -82.9, 'states': 'WV,VA,OH,PA,NY,KY,MD'},
        {'id': 'pipe-tgp-001', 'operator': 'Tennessee Gas Pipeline (TC Energy)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'Tennessee Gas Pipeline', 'capacity_mdth': 7800, 'lat': 40.7, 'lng': -74.0, 'states': 'TX,LA,MS,AL,TN,KY,WV,PA,NJ,NY,CT,MA'},
        {'id': 'pipe-cpc-001', 'operator': 'Colorado Interstate Gas (TC Energy)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 26, 'commodity': 'Natural Gas', 'name': 'Colorado Interstate Gas', 'capacity_mdth': 2800, 'lat': 40.7, 'lng': -105.0, 'states': 'WY,CO'},
        {'id': 'pipe-kmp-001', 'operator': 'Kinder Morgan Texas Pipeline', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 42, 'commodity': 'Natural Gas', 'name': 'Kinder Morgan Texas', 'capacity_mdth': 8400, 'lat': 32.7, 'lng': -96.7, 'states': 'TX'},
        {'id': 'pipe-etp-001', 'operator': 'Energy Transfer Partners', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 42, 'commodity': 'Natural Gas', 'name': 'Panhandle Eastern', 'capacity_mdth': 6300, 'lat': 39.7, 'lng': -86.1, 'states': 'TX,OK,KS,MO,IL,IN,OH'},
        {'id': 'pipe-dom-001', 'operator': 'Dominion Energy Transmission', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 30, 'commodity': 'Natural Gas', 'name': 'Dominion Transmission', 'capacity_mdth': 4800, 'lat': 39.0, 'lng': -77.5, 'states': 'WV,VA,PA,OH,NY'},
        {'id': 'pipe-wbi-001', 'operator': 'WBI Energy (MDU Resources)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 24, 'commodity': 'Natural Gas', 'name': 'WBI Energy Pipeline', 'capacity_mdth': 1500, 'lat': 41.5, 'lng': -93.6, 'states': 'MT,ND,SD,WY,IA,MN'},
        {'id': 'pipe-nng-001', 'operator': 'Northern Natural Gas (Berkshire Hathaway)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 30, 'commodity': 'Natural Gas', 'name': 'Northern Natural Gas', 'capacity_mdth': 5400, 'lat': 41.5, 'lng': -93.6, 'states': 'TX,OK,KS,NE,SD,MN,IA,WI,MI,IL'},
        {'id': 'pipe-rub-001', 'operator': 'Ruby Pipeline (Kinder Morgan)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 42, 'commodity': 'Natural Gas', 'name': 'Ruby Pipeline', 'capacity_mdth': 1500, 'lat': 40.8, 'lng': -117.0, 'states': 'WY,UT,NV,OR'},
        {'id': 'pipe-stx-001', 'operator': 'Southern Star Central (Black Hills)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 24, 'commodity': 'Natural Gas', 'name': 'Southern Star Central', 'capacity_mdth': 3500, 'lat': 37.6, 'lng': -97.3, 'states': 'TX,OK,KS,MO,CO'},
        {'id': 'pipe-slng-001', 'operator': 'Southern LNG (Kinder Morgan)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'Elba Express Pipeline', 'capacity_mdth': 2400, 'lat': 32.0, 'lng': -80.8, 'states': 'GA,SC'},
        {'id': 'pipe-twp-001', 'operator': 'Tallgrass Interstate (Tallgrass Energy)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 26, 'commodity': 'Natural Gas', 'name': 'Tallgrass Interstate', 'capacity_mdth': 2800, 'lat': 39.0, 'lng': -95.6, 'states': 'WY,CO,KS,NE,MO'},
        {'id': 'pipe-questar-001', 'operator': 'Questar Pipeline (Dominion)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 24, 'commodity': 'Natural Gas', 'name': 'Questar Pipeline', 'capacity_mdth': 2100, 'lat': 40.7, 'lng': -111.8, 'states': 'UT,WY,CO'},
        {'id': 'pipe-mvp-001', 'operator': 'Mountain Valley Pipeline LLC', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 42, 'commodity': 'Natural Gas', 'name': 'Mountain Valley Pipeline', 'capacity_mdth': 2000, 'lat': 37.2, 'lng': -80.4, 'states': 'WV,VA'},
        {'id': 'pipe-nexus-001', 'operator': 'NEXUS Gas Transmission (Enbridge/DTE)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'NEXUS Gas Transmission', 'capacity_mdth': 1500, 'lat': 41.0, 'lng': -83.0, 'states': 'OH,MI'},
        {'id': 'pipe-nwpl-001', 'operator': 'Northwest Pipeline (Williams)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 30, 'commodity': 'Natural Gas', 'name': 'Northwest Pipeline', 'capacity_mdth': 3800, 'lat': 47.6, 'lng': -122.3, 'states': 'WA,OR,WY,UT,CO,NM'},
        {'id': 'pipe-socal-001', 'operator': 'SoCal Gas (Sempra)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'SoCal Gas System', 'capacity_mdth': 4000, 'lat': 34.0, 'lng': -118.2, 'states': 'CA,AZ'},
        {'id': 'pipe-pgande-001', 'operator': 'Pacific Gas & Electric', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'PG&E Gas Transmission', 'capacity_mdth': 3600, 'lat': 37.8, 'lng': -122.4, 'states': 'CA'},
        {'id': 'pipe-gulfstream-001', 'operator': 'Gulfstream Natural Gas (Williams)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'Gulfstream Pipeline', 'capacity_mdth': 1400, 'lat': 27.9, 'lng': -82.5, 'states': 'AL,MS,FL'},
        {'id': 'pipe-fgt-001', 'operator': 'Florida Gas Transmission (Kinder Morgan)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'Florida Gas Transmission', 'capacity_mdth': 2900, 'lat': 30.3, 'lng': -81.6, 'states': 'TX,LA,MS,AL,FL'},
        {'id': 'pipe-ngpl-001', 'operator': 'Natural Gas Pipeline of America (Kinder Morgan)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'NGPL Pipeline', 'capacity_mdth': 6300, 'lat': 41.9, 'lng': -87.6, 'states': 'TX,LA,AR,MS,TN,KY,IL,IN,IA'},
        {'id': 'pipe-midcontinent-001', 'operator': 'Midcontinent Express (Kinder Morgan)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 42, 'commodity': 'Natural Gas', 'name': 'Midcontinent Express', 'capacity_mdth': 1800, 'lat': 32.3, 'lng': -90.2, 'states': 'TX,LA,MS,AL'},
        {'id': 'pipe-gtl-001', 'operator': 'Gulf South Pipeline (Boardwalk)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 30, 'commodity': 'Natural Gas', 'name': 'Gulf South Pipeline', 'capacity_mdth': 2700, 'lat': 30.0, 'lng': -90.0, 'states': 'TX,LA,MS,AL,FL,TN'},
        {'id': 'pipe-viking-001', 'operator': 'Viking Gas Transmission (ONEOK)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 24, 'commodity': 'Natural Gas', 'name': 'Viking Gas Transmission', 'capacity_mdth': 900, 'lat': 45.0, 'lng': -93.3, 'states': 'ND,MN,WI'},
        {'id': 'pipe-ans-001', 'operator': 'ANR Pipeline (TC Energy)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 36, 'commodity': 'Natural Gas', 'name': 'ANR Pipeline', 'capacity_mdth': 5200, 'lat': 41.0, 'lng': -87.0, 'states': 'TX,LA,MS,TN,KY,OH,MI,WI,IL'},
        {'id': 'pipe-east-tn-001', 'operator': 'East Tennessee Natural Gas (Enbridge)', 'pipeline_type': 'Interstate Transmission', 'status': 'Active', 'diameter': 24, 'commodity': 'Natural Gas', 'name': 'East Tennessee Natural Gas', 'capacity_mdth': 1400, 'lat': 36.2, 'lng': -86.8, 'states': 'TN,VA,WV'},
    ]

    # Filter hardcoded by state if requested
    results = []
    for pipe in major_pipelines:
        if state:
            pipe_states = pipe.get('states', '').split(',')
            if state in pipe_states:
                results.append(pipe)
        else:
            results.append(pipe)
    
    # Merge in DB-discovered pipelines from HIFLD bulk pulls
    try:
        conn = get_db()
        c = conn.cursor()
        query = "SELECT * FROM discovered_pipelines WHERE source != 'EIA/FERC'"
        params = []
        if state:
            query += " AND (state = %s OR states_served LIKE %s)"
            params.extend([state, f"%{state}%"])
        query += " ORDER BY capacity_mdth DESC NULLS LAST LIMIT %s"
        params.append(max(0, limit - len(results)))
        c.execute(query, params)
        db_pipes = _dict_rows(c)
        
        # Dedup by name similarity — don't add DB pipe if hardcoded already covers it
        hardcoded_names = set(p['name'].lower() for p in results)
        for db_pipe in db_pipes:
            db_name = (db_pipe.get('name') or '').lower()
            if not any(h in db_name or db_name in h for h in hardcoded_names if len(h) > 5):
                results.append(db_pipe)
        
        conn.close()
    except Exception as e:
        logger.debug(f"DB pipeline merge error: {e}")
    
    return results[:limit]


def query_eia_power_plants(state, api_key=None):
    """Query EIA API for power plants by state"""
    eia_key = api_key or os.environ.get('EIA_API_KEY')

    if not eia_key:
        logger.info(f"   ℹ️ EIA API key not set — using fallback data for {state}")
        return get_fallback_power_plants(state)

    try:
        params = {
            'api_key': eia_key,
            'frequency': 'monthly',
            'data[0]': 'nameplate-capacity-mw',
            'facets[stateid][]': state,
            'facets[status][]': 'OP',
            'sort[0][column]': 'period',
            'sort[0][direction]': 'desc',
            'offset': 0,
            'length': 5000
        }
        response = requests.get(EIA_POWER_PLANTS, params=params, timeout=60)
        data = response.json()

        if 'response' in data and 'data' in data['response']:
            rows = data['response']['data']
            if not rows:
                return get_fallback_power_plants(state)

            latest_period = rows[0].get('period', '')
            latest_rows = [r for r in rows if r.get('period') == latest_period]

            plants_agg = {}
            for r in latest_rows:
                pid = r.get('plantid')
                if not pid:
                    continue
                cap = float(r.get('nameplate-capacity-mw', 0) or 0)
                if pid not in plants_agg:
                    plants_agg[pid] = {
                        'id': f"eia-{pid}",
                        'name': r.get('plantName', 'Unknown'),
                        'fuel_type': r.get('energy-source-desc', r.get('technology', 'Unknown')),
                        'capacity_mw': cap,
                        'generation_mwh': 0,
                        'operator': r.get('entityName', 'Unknown'),
                        'state': r.get('stateid', state),
                        'sector': r.get('sectorName', 'Unknown'),
                        'source': 'EIA',
                    }
                else:
                    plants_agg[pid]['capacity_mw'] += cap

            return sorted(plants_agg.values(), key=lambda x: x['capacity_mw'], reverse=True)
        return get_fallback_power_plants(state)
    except Exception as e:
        logger.warning(f"⚠️ EIA Power Plants error for {state}: {e}")
        return get_fallback_power_plants(state)


def get_fallback_power_plants(state):
    """Fallback power plant data when EIA API unavailable"""
    fallback_data = {
        'AZ': [
            {'id': 'pp-palo-verde', 'name': 'Palo Verde Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 3937, 'state': 'AZ', 'operator': 'Arizona Public Service'},
            {'id': 'pp-gila-river', 'name': 'Gila River Power Station', 'fuel_type': 'Natural Gas', 'capacity_mw': 2200, 'state': 'AZ', 'operator': 'Gila River Power'},
            {'id': 'pp-arlington', 'name': 'Arlington Valley Energy', 'fuel_type': 'Natural Gas', 'capacity_mw': 570, 'state': 'AZ', 'operator': 'LS Power'},
            {'id': 'pp-sundance', 'name': 'Sundance Energy', 'fuel_type': 'Natural Gas', 'capacity_mw': 450, 'state': 'AZ', 'operator': 'Tucson Electric Power'},
        ],
        'TX': [
            {'id': 'pp-comanche-peak', 'name': 'Comanche Peak Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 2430, 'state': 'TX', 'operator': 'Vistra'},
            {'id': 'pp-south-texas', 'name': 'South Texas Project', 'fuel_type': 'Nuclear', 'capacity_mw': 2700, 'state': 'TX', 'operator': 'NRG Energy'},
            {'id': 'pp-ercot-wind', 'name': 'ERCOT Wind Fleet', 'fuel_type': 'Wind', 'capacity_mw': 35000, 'state': 'TX', 'operator': 'Various'},
            {'id': 'pp-permian-solar', 'name': 'Permian Basin Solar', 'fuel_type': 'Solar', 'capacity_mw': 5000, 'state': 'TX', 'operator': 'Various'},
        ],
        'VA': [
            {'id': 'pp-north-anna', 'name': 'North Anna Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 1892, 'state': 'VA', 'operator': 'Dominion Energy'},
            {'id': 'pp-surry', 'name': 'Surry Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 1676, 'state': 'VA', 'operator': 'Dominion Energy'},
            {'id': 'pp-chesterfield', 'name': 'Chesterfield Power Station', 'fuel_type': 'Natural Gas', 'capacity_mw': 1640, 'state': 'VA', 'operator': 'Dominion Energy'},
            {'id': 'pp-warren', 'name': 'Warren County Power Station', 'fuel_type': 'Natural Gas', 'capacity_mw': 1329, 'state': 'VA', 'operator': 'Dominion Energy'},
        ],
        'GA': [
            {'id': 'pp-vogtle', 'name': 'Vogtle Electric', 'fuel_type': 'Nuclear', 'capacity_mw': 4540, 'state': 'GA', 'operator': 'Georgia Power'},
            {'id': 'pp-hatch', 'name': 'Edwin I. Hatch Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 1759, 'state': 'GA', 'operator': 'Georgia Power'},
            {'id': 'pp-scherer', 'name': 'Robert W. Scherer', 'fuel_type': 'Coal', 'capacity_mw': 3520, 'state': 'GA', 'operator': 'Georgia Power'},
        ],
        'NV': [
            {'id': 'pp-copper-mountain', 'name': 'Copper Mountain Solar', 'fuel_type': 'Solar', 'capacity_mw': 802, 'state': 'NV', 'operator': 'Sempra'},
            {'id': 'pp-higgins', 'name': 'Higgins Generating Station', 'fuel_type': 'Natural Gas', 'capacity_mw': 600, 'state': 'NV', 'operator': 'NV Energy'},
        ],
        'UT': [
            {'id': 'pp-intermountain', 'name': 'Intermountain Power Plant', 'fuel_type': 'Coal', 'capacity_mw': 1800, 'state': 'UT', 'operator': 'Intermountain Power Agency'},
            {'id': 'pp-hunter', 'name': 'Hunter Power Plant', 'fuel_type': 'Coal', 'capacity_mw': 1320, 'state': 'UT', 'operator': 'PacifiCorp'},
        ],
        'OH': [
            {'id': 'pp-davis-besse', 'name': 'Davis-Besse Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 894, 'state': 'OH', 'operator': 'Energy Harbor'},
            {'id': 'pp-perry', 'name': 'Perry Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 1240, 'state': 'OH', 'operator': 'Energy Harbor'},
            {'id': 'pp-gavin', 'name': 'General James M. Gavin', 'fuel_type': 'Coal', 'capacity_mw': 2600, 'state': 'OH', 'operator': 'Gavin Power'},
        ],
        'IA': [
            {'id': 'pp-iowa-wind', 'name': 'Iowa Wind Fleet', 'fuel_type': 'Wind', 'capacity_mw': 12000, 'state': 'IA', 'operator': 'Various'},
            {'id': 'pp-walter-scott', 'name': 'Walter Scott Jr. Energy Center', 'fuel_type': 'Coal', 'capacity_mw': 1630, 'state': 'IA', 'operator': 'MidAmerican Energy'},
        ],
        'IL': [
            {'id': 'pp-braidwood', 'name': 'Braidwood Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 2386, 'state': 'IL', 'operator': 'Constellation Energy'},
            {'id': 'pp-byron', 'name': 'Byron Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 2347, 'state': 'IL', 'operator': 'Constellation Energy'},
            {'id': 'pp-lasalle', 'name': 'LaSalle County Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 2320, 'state': 'IL', 'operator': 'Constellation Energy'},
            {'id': 'pp-dresden', 'name': 'Dresden Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 1805, 'state': 'IL', 'operator': 'Constellation Energy'},
        ],
        'CA': [
            {'id': 'pp-diablo-canyon', 'name': 'Diablo Canyon Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 2256, 'state': 'CA', 'operator': 'Pacific Gas & Electric'},
            {'id': 'pp-helms', 'name': 'Helms Pumped Storage', 'fuel_type': 'Pumped Storage', 'capacity_mw': 1212, 'state': 'CA', 'operator': 'Pacific Gas & Electric'},
            {'id': 'pp-ca-solar-fleet', 'name': 'California Solar Fleet', 'fuel_type': 'Solar', 'capacity_mw': 18000, 'state': 'CA', 'operator': 'Various'},
            {'id': 'pp-moss-landing', 'name': 'Moss Landing Power Plant', 'fuel_type': 'Natural Gas', 'capacity_mw': 2560, 'state': 'CA', 'operator': 'Vistra'},
        ],
        'NJ': [
            {'id': 'pp-salem', 'name': 'Salem Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 2275, 'state': 'NJ', 'operator': 'PSEG Nuclear'},
            {'id': 'pp-hope-creek', 'name': 'Hope Creek Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 1170, 'state': 'NJ', 'operator': 'PSEG Nuclear'},
            {'id': 'pp-linden', 'name': 'Linden Cogeneration', 'fuel_type': 'Natural Gas', 'capacity_mw': 1100, 'state': 'NJ', 'operator': 'NAES Corporation'},
        ],
        'WA': [
            {'id': 'pp-columbia-gen', 'name': 'Columbia Generating Station', 'fuel_type': 'Nuclear', 'capacity_mw': 1174, 'state': 'WA', 'operator': 'Energy Northwest'},
            {'id': 'pp-grand-coulee', 'name': 'Grand Coulee Dam', 'fuel_type': 'Hydro', 'capacity_mw': 6809, 'state': 'WA', 'operator': 'Bureau of Reclamation'},
            {'id': 'pp-chief-joseph', 'name': 'Chief Joseph Dam', 'fuel_type': 'Hydro', 'capacity_mw': 2620, 'state': 'WA', 'operator': 'US Army Corps'},
            {'id': 'pp-wa-wind-fleet', 'name': 'Washington Wind Fleet', 'fuel_type': 'Wind', 'capacity_mw': 3200, 'state': 'WA', 'operator': 'Various'},
        ],
        'OR': [
            {'id': 'pp-the-dalles', 'name': 'The Dalles Dam', 'fuel_type': 'Hydro', 'capacity_mw': 1823, 'state': 'OR', 'operator': 'US Army Corps'},
            {'id': 'pp-john-day', 'name': 'John Day Dam', 'fuel_type': 'Hydro', 'capacity_mw': 2160, 'state': 'OR', 'operator': 'US Army Corps'},
            {'id': 'pp-or-wind-fleet', 'name': 'Oregon Wind Fleet', 'fuel_type': 'Wind', 'capacity_mw': 4000, 'state': 'OR', 'operator': 'Various'},
        ],
        'CO': [
            {'id': 'pp-comanche-co', 'name': 'Comanche Generating Station', 'fuel_type': 'Coal', 'capacity_mw': 1410, 'state': 'CO', 'operator': 'Xcel Energy'},
            {'id': 'pp-pawnee', 'name': 'Pawnee Station', 'fuel_type': 'Coal', 'capacity_mw': 505, 'state': 'CO', 'operator': 'Xcel Energy'},
            {'id': 'pp-co-wind-fleet', 'name': 'Colorado Wind Fleet', 'fuel_type': 'Wind', 'capacity_mw': 5500, 'state': 'CO', 'operator': 'Various'},
            {'id': 'pp-co-solar-fleet', 'name': 'Colorado Solar Fleet', 'fuel_type': 'Solar', 'capacity_mw': 2000, 'state': 'CO', 'operator': 'Various'},
        ],
        'FL': [
            {'id': 'pp-st-lucie', 'name': 'St. Lucie Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 1990, 'state': 'FL', 'operator': 'Florida Power & Light'},
            {'id': 'pp-turkey-point', 'name': 'Turkey Point Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 1760, 'state': 'FL', 'operator': 'Florida Power & Light'},
            {'id': 'pp-fl-solar-fleet', 'name': 'Florida Solar Fleet', 'fuel_type': 'Solar', 'capacity_mw': 8000, 'state': 'FL', 'operator': 'Various'},
            {'id': 'pp-manatee', 'name': 'Manatee Energy Center', 'fuel_type': 'Natural Gas', 'capacity_mw': 1800, 'state': 'FL', 'operator': 'Florida Power & Light'},
        ],
        'MN': [
            {'id': 'pp-monticello', 'name': 'Monticello Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 671, 'state': 'MN', 'operator': 'Xcel Energy'},
            {'id': 'pp-prairie-island', 'name': 'Prairie Island Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 1100, 'state': 'MN', 'operator': 'Xcel Energy'},
            {'id': 'pp-mn-wind-fleet', 'name': 'Minnesota Wind Fleet', 'fuel_type': 'Wind', 'capacity_mw': 4700, 'state': 'MN', 'operator': 'Various'},
        ],
        'MO': [
            {'id': 'pp-callaway', 'name': 'Callaway Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 1190, 'state': 'MO', 'operator': 'Ameren Missouri'},
            {'id': 'pp-labadie', 'name': 'Labadie Energy Center', 'fuel_type': 'Coal', 'capacity_mw': 2372, 'state': 'MO', 'operator': 'Ameren Missouri'},
            {'id': 'pp-iatan', 'name': 'Iatan Generating Station', 'fuel_type': 'Coal', 'capacity_mw': 1534, 'state': 'MO', 'operator': 'Evergy'},
        ],
        'TN': [
            {'id': 'pp-watts-bar', 'name': 'Watts Bar Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 2330, 'state': 'TN', 'operator': 'Tennessee Valley Authority'},
            {'id': 'pp-sequoyah', 'name': 'Sequoyah Nuclear', 'fuel_type': 'Nuclear', 'capacity_mw': 2290, 'state': 'TN', 'operator': 'Tennessee Valley Authority'},
            {'id': 'pp-cumberland', 'name': 'Cumberland Fossil Plant', 'fuel_type': 'Coal', 'capacity_mw': 2470, 'state': 'TN', 'operator': 'Tennessee Valley Authority'},
        ],
    }
    plants = fallback_data.get(state, [])
    for p in plants:
        p.setdefault('source', 'Fallback')
        p.setdefault('generation_mwh', 0)
        p.setdefault('sector', 'Electric Utility')
    return plants


# =============================================================================
# SYNC FUNCTIONS (PostgreSQL compatible)
# =============================================================================

def sync_market(market_key, market_info):
    """Sync all energy data for a single market"""
    start_time = time.time()
    bounds = market_info['bounds']
    state = market_info['state']
    now = datetime.utcnow().isoformat()

    results = {
        'power_plants': {'found': 0, 'new': 0, 'updated': 0},
        'transmission_lines': {'found': 0, 'new': 0, 'updated': 0},
        'wind_projects': {'found': 0, 'new': 0, 'updated': 0},
        'pipelines': {'found': 0, 'new': 0, 'updated': 0},
        'errors': []
    }

    conn = None
    try:
        conn = get_db()
        c = conn.cursor()

        # --- Power plants: HIFLD (coordinates) + EIA (capacity) ---
        hifld_plants = query_hifld_power_plants(bounds, state=state)
        eia_plants = query_eia_power_plants(state)

        all_plants = {}
        for plant in hifld_plants:
            all_plants[plant['id']] = plant

        for plant in eia_plants:
            matched = False
            plant_name_lower = (plant.get('name') or '').lower().strip()
            for hid, hplant in all_plants.items():
                hname = (hplant.get('name') or '').lower().strip()
                if hname and plant_name_lower and (hname in plant_name_lower or plant_name_lower in hname):
                    if plant.get('capacity_mw', 0) > hplant.get('capacity_mw', 0):
                        all_plants[hid]['capacity_mw'] = plant['capacity_mw']
                    all_plants[hid]['source'] = 'HIFLD+EIA'
                    matched = True
                    break
            if not matched:
                all_plants[plant['id']] = plant

        plants_list = list(all_plants.values())
        results['power_plants']['found'] = len(plants_list)

        for plant in plants_list:
            c.execute("SELECT id, capacity_mw FROM discovered_power_plants WHERE id = %s", (plant['id'],))
            existing = c.fetchone()
            if existing:
                c.execute("""UPDATE discovered_power_plants
                    SET capacity_mw = GREATEST(capacity_mw, %s), last_updated = %s, is_new = 0,
                        lat = COALESCE(lat, %s), lng = COALESCE(lng, %s),
                        source = COALESCE(NULLIF(%s, ''), source)
                    WHERE id = %s""",
                    (plant.get('capacity_mw', 0), now,
                     plant.get('lat'), plant.get('lng'),
                     plant.get('source', ''), plant['id']))
                results['power_plants']['updated'] += 1
            else:
                c.execute("""INSERT INTO discovered_power_plants
                    (id, name, fuel_type, capacity_mw, generation_mwh, operator, state, sector, market, lat, lng, discovered_at, last_updated, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO UPDATE SET
                        lat = COALESCE(EXCLUDED.lat, discovered_power_plants.lat),
                        lng = COALESCE(EXCLUDED.lng, discovered_power_plants.lng),
                        capacity_mw = GREATEST(EXCLUDED.capacity_mw, discovered_power_plants.capacity_mw),
                        last_updated = EXCLUDED.last_updated""",
                    (plant['id'], plant['name'], plant['fuel_type'], plant.get('capacity_mw', 0),
                     plant.get('generation_mwh', 0), plant.get('operator', 'Unknown'), plant['state'],
                     plant.get('sector', 'Unknown'), market_key,
                     plant.get('lat'), plant.get('lng'),
                     now, now, plant.get('source', 'EIA'))
                )
                results['power_plants']['new'] += 1

        # --- Transmission lines (HIFLD — spatial + state fallback) ---
        tx_lines = query_transmission_lines(bounds, state=state)
        results['transmission_lines']['found'] = len(tx_lines)
        for line in tx_lines:
            c.execute("SELECT id FROM discovered_transmission_lines WHERE id = %s", (line['id'],))
            if not c.fetchone():
                c.execute("""INSERT INTO discovered_transmission_lines
                    (id, owner, voltage_kv, volt_class, sub_1, sub_2, status, market, discovered_at, last_updated)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING""",
                    (line['id'], line['owner'], line['voltage_kv'], line['volt_class'],
                     line['sub_1'], line['sub_2'], line['status'], market_key, now, now))
                results['transmission_lines']['new'] += 1

        # --- Wind projects (HIFLD — by STATE, deduplicated across markets) ---
        global _wind_synced_states
        if state not in _wind_synced_states:
            wind = query_wind_turbines(bounds=bounds, state=state)
            _wind_synced_states.add(state)
            results['wind_projects']['found'] = len(wind)
            for project in wind:
                c.execute("SELECT id FROM discovered_wind_projects WHERE id = %s", (project['id'],))
                if not c.fetchone():
                    c.execute("""INSERT INTO discovered_wind_projects
                        (id, project_name, project_capacity_mw, turbine_capacity_kw, manufacturer, model, state, county, lat, lng, market, discovered_at, last_updated)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (id) DO NOTHING""",
                        (project['id'], project['project_name'], project['project_capacity_mw'],
                         project['turbine_capacity_kw'], project['manufacturer'], project['model'],
                         project['state'], project['county'], project['lat'], project['lng'],
                         market_key, now, now))
                    results['wind_projects']['new'] += 1
        else:
            # Already synced this state's wind in a previous market
            pass

        # --- Pipelines (built-in data) ---
        pipelines = query_pipelines(bounds, state=state)
        results['pipelines']['found'] = len(pipelines)
        for pipe in pipelines:
            c.execute("SELECT id FROM discovered_pipelines WHERE id = %s", (pipe['id'],))
            if not c.fetchone():
                c.execute("""INSERT INTO discovered_pipelines
                    (id, operator, pipeline_type, status, diameter_inches, commodity, name, capacity_mdth, lat, lng, states_served, state, market, discovered_at, last_updated, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (id) DO NOTHING""",
                    (pipe['id'], pipe['operator'], pipe.get('pipeline_type', 'Transmission'),
                     pipe.get('status', 'Active'), pipe.get('diameter', 0), pipe.get('commodity', 'Natural Gas'),
                     pipe.get('name', ''), pipe.get('capacity_mdth', 0), pipe.get('lat', 0), pipe.get('lng', 0),
                     pipe.get('states', ''), state, market_key, now, now, 'EIA/FERC'))
                results['pipelines']['new'] += 1

        conn.commit()

    except Exception as e:
        results['errors'].append(str(e))
        logger.error(f"⚠️ Sync error for {market_key}: {e}")
        if conn:
            try:
                conn.rollback()
            except:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

    duration = time.time() - start_time
    log_sync(market_key, results, duration)
    return results


def log_sync(market, results, duration):
    """Log sync results to database"""
    conn = None
    try:
        conn = get_db()
        c = conn.cursor()
        total_found = sum(results[k]['found'] for k in ['power_plants', 'transmission_lines', 'wind_projects', 'pipelines'])
        total_new = sum(results[k]['new'] for k in ['power_plants', 'transmission_lines', 'wind_projects', 'pipelines'])
        total_updated = sum(results[k].get('updated', 0) for k in ['power_plants', 'transmission_lines', 'wind_projects', 'pipelines'])

        c.execute("""INSERT INTO energy_sync_log (sync_type, market, items_found, new_items, updated_items, errors, duration_seconds, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            ('auto_discovery', market, total_found, total_new, total_updated,
             json.dumps(results['errors']), duration, datetime.utcnow().isoformat()))
        conn.commit()
    except Exception as e:
        logger.error(f"⚠️ Log sync error: {e}")
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def run_full_sync():
    """Run sync for all monitored markets"""
    logger.info(f"🔄 ENERGY AUTO-DISCOVERY SYNC v2.3 — {datetime.utcnow().isoformat()}")

    # Reset per-sync state tracking
    global _wind_synced_states
    _wind_synced_states = set()

    start_time = time.time()
    total_results = {
        'markets': 0,
        'power_plants': {'found': 0, 'new': 0},
        'transmission_lines': {'found': 0, 'new': 0},
        'wind_projects': {'found': 0, 'new': 0},
        'pipelines': {'found': 0, 'new': 0}
    }

    for market_key, market_info in MONITORED_MARKETS.items():
        logger.info(f"   📍 Syncing {market_info['name']}...")
        results = sync_market(market_key, market_info)
        total_results['markets'] += 1
        for cat in ['power_plants', 'transmission_lines', 'wind_projects', 'pipelines']:
            total_results[cat]['found'] += results[cat]['found']
            total_results[cat]['new'] += results[cat]['new']

    duration = time.time() - start_time
    logger.info(f"✅ ENERGY SYNC COMPLETE in {duration:.1f}s — "
                f"{total_results['markets']} markets, "
                f"{total_results['power_plants']['found']} plants, "
                f"{total_results['transmission_lines']['found']} tx lines, "
                f"{total_results['wind_projects']['found']} wind, "
                f"{total_results['pipelines']['found']} pipelines")

    return total_results


# =============================================================================
# SCHEDULER
# =============================================================================

class EnergyAutoDiscoveryScheduler:
    def __init__(self, interval_seconds=SYNC_INTERVAL_SECONDS):
        self.interval = interval_seconds
        self.running = False
        self.thread = None
        self.last_sync = None
        self.sync_count = 0

    def _run_loop(self):
        while self.running:
            try:
                self.last_sync = datetime.utcnow()
                self.sync_count += 1
                run_full_sync()
            except Exception as e:
                logger.error(f"⚠️ Scheduler error: {e}")
            for _ in range(self.interval):
                if not self.running:
                    break
                time.sleep(1)

    def start(self):
        if self.running:
            return
        init_discovery_db()
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info(f"🚀 Energy Auto-Discovery Scheduler started (every {self.interval}s)")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)

    def get_status(self):
        return {
            'running': self.running,
            'interval_seconds': self.interval,
            'last_sync': self.last_sync.isoformat() if self.last_sync else None,
            'sync_count': self.sync_count,
            'markets_monitored': len(MONITORED_MARKETS)
        }


# =============================================================================
# HELPER
# =============================================================================

def _dict_rows(cursor):
    if not cursor.description:
        return []
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


# =============================================================================
# FLASK ROUTES
# =============================================================================

def register_energy_discovery_routes(app):
    """Register API routes for energy auto-discovery"""

    scheduler = EnergyAutoDiscoveryScheduler()

    @app.route('/api/energy-discovery/status', methods=['GET'])
    def energy_discovery_status():
        status = scheduler.get_status()
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM discovered_power_plants")
            status['total_power_plants'] = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM discovered_transmission_lines")
            status['total_transmission_lines'] = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM discovered_wind_projects")
            status['total_wind_projects'] = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM discovered_pipelines")
            status['total_pipelines'] = c.fetchone()[0]
            c.execute("SELECT COALESCE(SUM(capacity_mw), 0) FROM discovered_power_plants")
            status['total_capacity_mw'] = float(c.fetchone()[0])
            c.execute("SELECT COUNT(*) FROM discovered_power_plants WHERE is_new = 1")
            status['new_power_plants'] = c.fetchone()[0]
        except Exception as e:
            status['db_error'] = str(e)
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass
        return jsonify({'success': True, 'data': status})

    @app.route('/api/energy-discovery/start', methods=['POST'])
    def energy_discovery_start():
        scheduler.start()
        return jsonify({'success': True, 'message': 'Scheduler started'})

    @app.route('/api/energy-discovery/stop', methods=['POST'])
    def energy_discovery_stop():
        scheduler.stop()
        return jsonify({'success': True, 'message': 'Scheduler stopped'})

    @app.route('/api/energy-discovery/sync-now', methods=['POST'])
    def energy_discovery_sync_now():
        results = run_full_sync()
        return jsonify({'success': True, 'data': results})

    @app.route('/api/energy-discovery/markets', methods=['GET'])
    def energy_discovery_markets():
        return jsonify({'success': True, 'data': MONITORED_MARKETS})

    @app.route('/api/energy-discovery/power-plants', methods=['GET'])
    def energy_discovery_power_plants():
        market = request.args.get('market')
        state = request.args.get('state')
        fuel_type = request.args.get('fuel_type')
        min_capacity = request.args.get('min_capacity', type=float)
        limit = request.args.get('limit', 100, type=int)
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()
            query = "SELECT * FROM discovered_power_plants WHERE 1=1"
            params = []
            if market:
                query += " AND market = %s"; params.append(market)
            if state:
                query += " AND state = %s"; params.append(state)
            if fuel_type:
                query += " AND fuel_type ILIKE %s"; params.append(f"%{fuel_type}%")
            if min_capacity:
                query += " AND capacity_mw >= %s"; params.append(min_capacity)
            query += " ORDER BY capacity_mw DESC LIMIT %s"; params.append(limit)
            c.execute(query, params)
            plants = _dict_rows(c)
            return jsonify({'success': True, 'data': plants, 'count': len(plants)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass

    @app.route('/api/energy-discovery/transmission-lines', methods=['GET'])
    def energy_discovery_transmission_lines():
        market = request.args.get('market')
        min_voltage = request.args.get('min_voltage', type=float)
        limit = request.args.get('limit', 100, type=int)
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()
            query = "SELECT * FROM discovered_transmission_lines WHERE 1=1"
            params = []
            if market:
                query += " AND market = %s"; params.append(market)
            if min_voltage:
                query += " AND voltage_kv >= %s"; params.append(min_voltage)
            query += " ORDER BY voltage_kv DESC LIMIT %s"; params.append(limit)
            c.execute(query, params)
            lines = _dict_rows(c)
            return jsonify({'success': True, 'data': lines, 'count': len(lines)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass

    @app.route('/api/energy-discovery/wind-projects', methods=['GET'])
    def energy_discovery_wind_projects():
        market = request.args.get('market')
        state = request.args.get('state')
        limit = request.args.get('limit', 100, type=int)
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()
            query = "SELECT * FROM discovered_wind_projects WHERE 1=1"
            params = []
            if market:
                query += " AND market = %s"; params.append(market)
            if state:
                query += " AND state = %s"; params.append(state)
            query += " ORDER BY project_capacity_mw DESC LIMIT %s"; params.append(limit)
            c.execute(query, params)
            projects = _dict_rows(c)
            return jsonify({'success': True, 'data': projects, 'count': len(projects)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass

    @app.route('/api/energy-discovery/pipelines', methods=['GET'])
    def energy_discovery_pipelines():
        market = request.args.get('market')
        state = request.args.get('state')
        operator = request.args.get('operator')
        limit = request.args.get('limit', 100, type=int)
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()
            query = "SELECT * FROM discovered_pipelines WHERE 1=1"
            params = []
            if market:
                query += " AND market = %s"; params.append(market)
            if state:
                query += " AND state = %s"; params.append(state)
            if operator:
                query += " AND operator ILIKE %s"; params.append(f"%{operator}%")
            query += " ORDER BY capacity_mdth DESC NULLS LAST LIMIT %s"; params.append(limit)
            c.execute(query, params)
            pipes = _dict_rows(c)
            return jsonify({'success': True, 'data': pipes, 'count': len(pipes)})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass

    @app.route('/api/energy-discovery/sync-log', methods=['GET'])
    def energy_discovery_sync_log():
        limit = request.args.get('limit', 20, type=int)
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT * FROM energy_sync_log ORDER BY synced_at DESC LIMIT %s", (limit,))
            logs = _dict_rows(c)
            return jsonify({'success': True, 'data': logs})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass

    @app.route('/api/energy-discovery/backfill-coords', methods=['POST'])
    def energy_discovery_backfill_coords():
        """Backfill lat/lng for existing plants using HIFLD"""
        updated = 0
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT id, name, state, market FROM discovered_power_plants WHERE lat IS NULL OR lng IS NULL LIMIT 500")
            cols = [d[0] for d in c.description]
            missing = [dict(zip(cols, row)) for row in c.fetchall()]

            if not missing:
                return jsonify({'success': True, 'message': 'All plants already have coordinates', 'updated': 0})

            markets_done = set()
            for plant in missing:
                market_key = plant.get('market', '')
                if market_key in markets_done or market_key not in MONITORED_MARKETS:
                    continue
                markets_done.add(market_key)

                mkt = MONITORED_MARKETS[market_key]
                hifld_plants = query_hifld_power_plants(mkt['bounds'], state=mkt['state'], limit=1000)

                for hp in hifld_plants:
                    if not hp.get('lat') or not hp.get('lng'):
                        continue
                    hp_name = (hp.get('name') or '').lower().strip()
                    for mp in missing:
                        if mp.get('market') != market_key:
                            continue
                        mp_name = (mp.get('name') or '').lower().strip()
                        if hp_name and mp_name and (hp_name in mp_name or mp_name in hp_name):
                            c.execute("UPDATE discovered_power_plants SET lat = %s, lng = %s, last_updated = %s WHERE id = %s AND lat IS NULL",
                                      (hp['lat'], hp['lng'], datetime.utcnow().isoformat(), mp['id']))
                            updated += 1

                for hp in hifld_plants:
                    if hp.get('lat') and hp.get('lng'):
                        c.execute("""INSERT INTO discovered_power_plants
                            (id, name, fuel_type, capacity_mw, generation_mwh, operator, state, sector, market, lat, lng, discovered_at, last_updated, source)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (id) DO UPDATE SET
                                lat = COALESCE(EXCLUDED.lat, discovered_power_plants.lat),
                                lng = COALESCE(EXCLUDED.lng, discovered_power_plants.lng)""",
                            (hp['id'], hp['name'], hp['fuel_type'], hp.get('capacity_mw', 0),
                             0, hp.get('operator', ''), hp['state'], hp.get('sector', ''),
                             market_key, hp['lat'], hp['lng'],
                             datetime.utcnow().isoformat(), datetime.utcnow().isoformat(), 'HIFLD'))
                        updated += 1

            conn.commit()

            c.execute("SELECT COUNT(*) FROM discovered_power_plants WHERE lat IS NOT NULL AND lng IS NOT NULL")
            with_coords = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM discovered_power_plants")
            total = c.fetchone()[0]

            return jsonify({
                'success': True,
                'updated': updated,
                'with_coordinates': with_coords,
                'total_plants': total,
                'coverage_pct': round(with_coords / total * 100, 1) if total > 0 else 0
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e), 'updated': updated})
        finally:
            if conn:
                try:
                    conn.close()
                except:
                    pass

    @app.route('/api/energy-discovery/test-apis', methods=['GET'])
    def energy_discovery_test_apis():
        results = {}
        eia_key = os.environ.get('EIA_API_KEY')
        if eia_key:
            try:
                params = {'api_key': eia_key, 'frequency': 'annual', 'data[0]': 'generation', 'facets[state][]': 'AZ', 'length': 5}
                resp = requests.get("https://api.eia.gov/v2/electricity/facility-fuel/data/", params=params, timeout=60)
                data = resp.json()
                if 'response' in data and 'data' in data['response']:
                    results['eia'] = {'status': 'ok', 'count': len(data['response']['data'])}
                else:
                    results['eia'] = {'status': 'error', 'detail': str(data)[:200]}
            except Exception as e:
                results['eia'] = {'status': 'error', 'detail': str(e)}
        else:
            results['eia'] = {'status': 'fallback', 'message': 'EIA_API_KEY not set'}

        # Test transmission (spatial with inSR)
        try:
            bounds = [-112.5, 33.0, -111.5, 34.0]
            tx = query_transmission_lines(bounds, state='AZ', limit=5)
            results['hifld_transmission'] = {'status': 'ok', 'count': len(tx),
                                              'method': 'spatial+state_fallback'}
        except Exception as e:
            results['hifld_transmission'] = {'status': 'error', 'detail': str(e)}

        # Test wind (by state)
        try:
            wind = query_wind_turbines(state='TX', limit=10)
            results['hifld_wind'] = {'status': 'ok', 'count': len(wind),
                                      'method': 'state_query',
                                      'sample': [t['project_name'] for t in wind[:3]]}
        except Exception as e:
            results['hifld_wind'] = {'status': 'error', 'detail': str(e)}

        results['pipelines'] = {'status': 'ok', 'count': 31, 'source': 'built-in FERC data'}

        # Test power plants (spatial with inSR + state fallback)
        try:
            bounds = [-112.5, 33.0, -111.5, 34.0]
            plants = query_hifld_power_plants(bounds, state='AZ', limit=5)
            with_coords = sum(1 for p in plants if p.get('lat') and p.get('lng'))
            results['hifld_power_plants'] = {'status': 'ok', 'count': len(plants),
                                              'with_coords': with_coords,
                                              'method': 'spatial+state_fallback',
                                              'sample': [p.get('name', '?') for p in plants[:3]]}
        except Exception as e:
            results['hifld_power_plants'] = {'status': 'error', 'detail': str(e)}

        # Test substations
        try:
            subs = query_hifld_substations([-112.5, 33.0, -111.5, 34.0], state='AZ', limit=5)
            results['hifld_substations'] = {'status': 'ok', 'count': len(subs),
                                             'method': 'spatial+state_fallback'}
        except Exception as e:
            results['hifld_substations'] = {'status': 'error', 'detail': str(e)}

        return jsonify({'success': True, 'results': results})

    if IS_RAILWAY:
        scheduler.start()
    else:
        logger.info("⚡ Energy Auto-Discovery: routes registered (scheduler paused — not Railway)")

    logger.info("⚡ Energy Auto-Discovery v2.3 routes registered (PostgreSQL — 23 markets)")
    logger.info("   ✅ Fixed: inSR/outSR=4326 on all HIFLD queries")
    logger.info("   ✅ Fixed: State fallback when spatial returns 0")
    logger.info("   ✅ Fixed: Wind queried by state (rural, not metro)")

    return scheduler


if __name__ == '__main__':
    init_discovery_db()
    run_full_sync()
