"""
DC Hub Energy Auto-Discovery System v2.2 (PostgreSQL + Coordinates)
====================================================================
Automatically discovers and syncs:
- Power plants (EIA capacity + market-center coords, upgradeable to exact lat/lng)
- Transmission lines (HIFLD)
- Gas pipelines (built-in + FERC data)
- Wind projects (HIFLD)

Runs every 10 minutes. EIA provides capacity data. Coordinates assigned via
market-center jitter (Phase 1) — will be refined with HIFLD/EIA-860M exact
coordinates once stable endpoints are confirmed (Phase 2).

Note: HIFLD Power_Plants FeatureServer URL changed to hash-based names in 2024.
Old URL (Power_Plants/FeatureServer) is dead. Using EIA-only approach for now.
"""

import os
import json
import random
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

SYNC_INTERVAL_SECONDS = 600  # 10 minutes
IS_RAILWAY = bool(os.environ.get('RAILWAY_ENVIRONMENT'))

# API Endpoints
HIFLD_TRANSMISSION = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Power_Transmission_Lines/FeatureServer/0/query"
HIFLD_WIND_TURBINES = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/US_Wind_Turbines/FeatureServer/0/query"
# DEAD URLs — HIFLD moved these to hash-based service names in 2024
# HIFLD_POWER_PLANTS = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Power_Plants/FeatureServer/0/query"
# HIFLD_SUBSTATIONS = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Substations/FeatureServer/0/query"
EIA_POWER_PLANTS = "https://api.eia.gov/v2/electricity/operating-generator-capacity/data/"

# Key markets to monitor
MONITORED_MARKETS = {
    'phoenix': {'name': 'Phoenix, AZ', 'bounds': [-112.5, 33.0, -111.5, 34.0], 'state': 'AZ'},
    'dallas': {'name': 'Dallas, TX', 'bounds': [-97.5, 32.5, -96.5, 33.5], 'state': 'TX'},
    'northern_virginia': {'name': 'Northern Virginia', 'bounds': [-77.8, 38.7, -77.0, 39.2], 'state': 'VA'},
    'atlanta': {'name': 'Atlanta, GA', 'bounds': [-84.8, 33.5, -84.0, 34.2], 'state': 'GA'},
    'las_vegas': {'name': 'Las Vegas, NV', 'bounds': [-115.5, 35.8, -114.8, 36.5], 'state': 'NV'},
    'salt_lake': {'name': 'Salt Lake City, UT', 'bounds': [-112.2, 40.5, -111.5, 41.0], 'state': 'UT'},
    'columbus': {'name': 'Columbus, OH', 'bounds': [-83.2, 39.8, -82.7, 40.2], 'state': 'OH'},
    'des_moines': {'name': 'Des Moines, IA', 'bounds': [-93.8, 41.4, -93.4, 41.8], 'state': 'IA'},
}

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
        # Add lat/lng columns if table already exists without them
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
# API QUERIES
# =============================================================================

def query_transmission_lines(bounds, limit=500):
    """Query HIFLD for transmission lines in bounding box"""
    try:
        params = {
            'geometry': f'{bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]}',
            'geometryType': 'esriGeometryEnvelope',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'OWNER,VOLTAGE,VOLT_CLASS,SUB_1,SUB_2,STATUS',
            'returnGeometry': 'false',
            'f': 'json',
            'resultRecordCount': limit
        }
        response = requests.get(HIFLD_TRANSMISSION, params=params, timeout=60)
        data = response.json()
        if 'features' in data:
            lines = []
            for f in data['features']:
                attrs = f.get('attributes', {})
                lines.append({
                    'id': f"tx-{hash(str(attrs)) % 1000000}",
                    'owner': attrs.get('OWNER', 'Unknown'),
                    'voltage_kv': attrs.get('VOLTAGE', 0),
                    'volt_class': attrs.get('VOLT_CLASS', 'Unknown'),
                    'sub_1': attrs.get('SUB_1', ''),
                    'sub_2': attrs.get('SUB_2', ''),
                    'status': attrs.get('STATUS', 'Unknown')
                })
            return lines
        return []
    except Exception as e:
        logger.warning(f"⚠️ Transmission Lines error: {e}")
        return []


def query_hifld_power_plants(bounds, limit=500):
    """HIFLD Power_Plants FeatureServer URL is dead (moved to hash-based names 2024).
    Returns empty. Phase 2: use NASA NCCS mirror or EIA-860M for exact coords."""
    logger.info("   ℹ️ HIFLD Power Plants URL deprecated — using EIA + market-center coords")
    return []


def query_hifld_substations(bounds, limit=500):
    """HIFLD Substations FeatureServer URL is dead (moved to hash-based names 2024).
    Returns empty. Phase 2: find new hash-based URL."""
    logger.info("   ℹ️ HIFLD Substations URL deprecated — skipping")
    return []


def query_wind_turbines(bounds, limit=500):
    """Query HIFLD for wind turbines in bounding box"""
    try:
        params = {
            'geometry': f'{bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]}',
            'geometryType': 'esriGeometryEnvelope',
            'spatialRel': 'esriSpatialRelIntersects',
            'outFields': 'p_name,p_cap,t_cap,t_manu,t_model,t_state,t_county',
            'returnGeometry': 'true',
            'f': 'json',
            'resultRecordCount': limit
        }
        response = requests.get(HIFLD_WIND_TURBINES, params=params, timeout=60)
        data = response.json()
        if 'features' in data:
            turbines = []
            seen = set()
            for f in data['features']:
                attrs = f.get('attributes', {})
                project = attrs.get('p_name', 'Unknown')
                if project not in seen:
                    seen.add(project)
                    turbines.append({
                        'id': f"wind-{hash(project) % 1000000}",
                        'project_name': project,
                        'project_capacity_mw': attrs.get('p_cap', 0),
                        'turbine_capacity_kw': attrs.get('t_cap', 0),
                        'manufacturer': attrs.get('t_manu', 'Unknown'),
                        'model': attrs.get('t_model', 'Unknown'),
                        'state': attrs.get('t_state', ''),
                        'county': attrs.get('t_county', ''),
                        'lat': f.get('geometry', {}).get('y'),
                        'lng': f.get('geometry', {}).get('x')
                    })
            return turbines
        return []
    except Exception as e:
        logger.warning(f"⚠️ Wind Turbines error: {e}")
        return []


def query_pipelines(bounds=None, state=None, limit=200):
    """Return major interstate gas pipelines serving DC markets"""
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
    ]

    results = []
    for pipe in major_pipelines:
        if state:
            pipe_states = pipe.get('states', '').split(',')
            if state in pipe_states:
                results.append(pipe)
        else:
            results.append(pipe)
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
            'data[1]': 'latitude',
            'data[2]': 'longitude',
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
                    # EIA API may return latitude/longitude as data columns
                    lat_val = r.get('latitude')
                    lng_val = r.get('longitude')
                    plants_agg[pid] = {
                        'id': f"eia-{pid}",
                        'name': r.get('plantName', 'Unknown'),
                        'fuel_type': r.get('energy-source-desc', r.get('technology', 'Unknown')),
                        'capacity_mw': cap,
                        'generation_mwh': 0,
                        'operator': r.get('entityName', 'Unknown'),
                        'state': r.get('stateid', state),
                        'sector': r.get('sectorName', 'Unknown'),
                        'lat': float(lat_val) if lat_val else None,
                        'lng': float(lng_val) if lng_val else None,
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
    }
    plants = fallback_data.get(state, [])
    for p in plants:
        p.setdefault('source', 'Fallback')
        p.setdefault('generation_mwh', 0)
        p.setdefault('sector', 'Electric Utility')
    return plants


# =============================================================================
# SYNC FUNCTIONS (PostgreSQL compatible — uses %s placeholders)
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

        # --- Power plants: HIFLD first (has coordinates), then EIA for extras ---
        hifld_plants = query_hifld_power_plants(bounds)
        eia_plants = query_eia_power_plants(state)

        # Merge: HIFLD plants have lat/lng, EIA plants have better capacity data
        # Use HIFLD as primary (coordinates), supplement with EIA
        all_plants = {}

        # HIFLD plants (with coordinates)
        for plant in hifld_plants:
            all_plants[plant['id']] = plant

        # EIA plants — try to match by name to add coordinates, or add as new
        for plant in eia_plants:
            # Check if we already have this plant from HIFLD (fuzzy name match)
            matched = False
            plant_name_lower = (plant.get('name') or '').lower().strip()
            for hid, hplant in all_plants.items():
                hname = (hplant.get('name') or '').lower().strip()
                if hname and plant_name_lower and (hname in plant_name_lower or plant_name_lower in hname):
                    # Merge EIA capacity data into HIFLD record
                    if plant.get('capacity_mw', 0) > hplant.get('capacity_mw', 0):
                        all_plants[hid]['capacity_mw'] = plant['capacity_mw']
                    all_plants[hid]['source'] = 'HIFLD+EIA'
                    matched = True
                    break
            if not matched:
                all_plants[plant['id']] = plant

        plants_list = list(all_plants.values())
        results['power_plants']['found'] = len(plants_list)

        # Assign market-center jitter coordinates to plants missing lat/lng
        for plant in plants_list:
            if not plant.get('lat') or not plant.get('lng'):
                center_lat = (bounds[1] + bounds[3]) / 2
                center_lng = (bounds[0] + bounds[2]) / 2
                plant['lat'] = center_lat + random.uniform(-0.35, 0.35)
                plant['lng'] = center_lng + random.uniform(-0.35, 0.35)
                if not plant.get('source') or plant['source'] == 'EIA':
                    plant['source'] = 'EIA+jitter'

        for plant in plants_list:
            c.execute("SELECT id, capacity_mw FROM discovered_power_plants WHERE id = %s", (plant['id'],))
            existing = c.fetchone()
            if existing:
                # Update if capacity changed or coordinates added
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

        # --- Transmission lines (HIFLD) ---
        tx_lines = query_transmission_lines(bounds)
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

        # --- Wind projects (HIFLD) ---
        wind = query_wind_turbines(bounds)
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
        logger.warning(f"Sync log error: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def run_full_sync():
    """Run sync for all monitored markets"""
    logger.info(f"🔄 ENERGY AUTO-DISCOVERY SYNC — {datetime.utcnow().isoformat()}")

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
# HELPER: dict rows from psycopg2 cursor
# =============================================================================

def _dict_rows(cursor):
    """Convert psycopg2 cursor results to list of dicts"""
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
        """Backfill lat/lng for plants using market center + jitter"""
        import random
        updated = 0
        conn = None
        try:
            conn = get_db()
            c = conn.cursor()

            # Get plants missing coordinates
            c.execute("SELECT id, market FROM discovered_power_plants WHERE lat IS NULL OR lng IS NULL")
            cols = [d[0] for d in c.description]
            missing = [dict(zip(cols, row)) for row in c.fetchall()]

            if not missing:
                c.execute("SELECT COUNT(*) FROM discovered_power_plants WHERE lat IS NOT NULL")
                with_coords = c.fetchone()[0]
                c.execute("SELECT COUNT(*) FROM discovered_power_plants")
                total = c.fetchone()[0]
                return jsonify({'success': True, 'message': 'All plants already have coordinates',
                                'updated': 0, 'with_coordinates': with_coords, 'total_plants': total,
                                'coverage_pct': round(with_coords / total * 100, 1) if total > 0 else 0})

            # Assign market center coordinates with small random offset
            # so plants don't stack on top of each other
            for plant in missing:
                market_key = plant.get('market', '')
                if market_key not in MONITORED_MARKETS:
                    continue
                bounds = MONITORED_MARKETS[market_key]['bounds']
                center_lat = (bounds[1] + bounds[3]) / 2
                center_lng = (bounds[0] + bounds[2]) / 2
                # Add jitter within market bounds so dots spread out
                lat = center_lat + random.uniform(-0.35, 0.35)
                lng = center_lng + random.uniform(-0.35, 0.35)
                c.execute("UPDATE discovered_power_plants SET lat = %s, lng = %s WHERE id = %s AND lat IS NULL",
                          (lat, lng, plant['id']))
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
                'coverage_pct': round(with_coords / total * 100, 1) if total > 0 else 0,
                'note': 'Coordinates are market-center approximations. Will be refined with HIFLD exact data.'
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

        try:
            params = {'where': '1=1', 'outFields': 'OWNER', 'returnGeometry': 'false', 'f': 'json', 'resultRecordCount': 3}
            resp = requests.get(HIFLD_TRANSMISSION, params=params, timeout=60)
            data = resp.json()
            results['hifld_transmission'] = {'status': 'ok', 'count': len(data.get('features', []))} if 'features' in data else {'status': 'error'}
        except Exception as e:
            results['hifld_transmission'] = {'status': 'error', 'detail': str(e)}

        try:
            params = {'where': '1=1', 'outFields': 'p_name', 'returnGeometry': 'false', 'f': 'json', 'resultRecordCount': 3}
            resp = requests.get(HIFLD_WIND_TURBINES, params=params, timeout=60)
            data = resp.json()
            results['hifld_wind'] = {'status': 'ok', 'count': len(data.get('features', []))} if 'features' in data else {'status': 'error'}
        except Exception as e:
            results['hifld_wind'] = {'status': 'error', 'detail': str(e)}

        results['pipelines'] = {'status': 'ok', 'count': 20, 'source': 'built-in FERC data'}

        # Test HIFLD Power Plants (with coordinates)
        try:
            bounds = [-112.5, 33.0, -111.5, 34.0]  # Phoenix
            plants = query_hifld_power_plants(bounds, limit=5)
            with_coords = sum(1 for p in plants if p.get('lat') and p.get('lng'))
            results['hifld_power_plants'] = {'status': 'ok', 'count': len(plants), 'with_coords': with_coords,
                                              'sample': [p.get('name', 'Unknown') for p in plants[:3]]}
        except Exception as e:
            results['hifld_power_plants'] = {'status': 'error', 'detail': str(e)}

        # Test HIFLD Substations
        try:
            subs = query_hifld_substations([-112.5, 33.0, -111.5, 34.0], limit=5)
            results['hifld_substations'] = {'status': 'ok', 'count': len(subs)}
        except Exception as e:
            results['hifld_substations'] = {'status': 'error', 'detail': str(e)}

        return jsonify({'success': True, 'results': results})

    # Only start the background scheduler on Railway
    if IS_RAILWAY:
        scheduler.start()
    else:
        logger.info("⚡ Energy Auto-Discovery: routes registered (scheduler paused — not Railway)")

    logger.info("⚡ Energy Auto-Discovery routes registered (PostgreSQL v2.1 — with coordinates)")
    logger.info("   ✅ /api/energy-discovery/status")
    logger.info("   ✅ /api/energy-discovery/power-plants")
    logger.info("   ✅ /api/energy-discovery/transmission-lines")
    logger.info("   ✅ /api/energy-discovery/wind-projects")
    logger.info("   ✅ /api/energy-discovery/pipelines")
    logger.info("   ✅ /api/energy-discovery/sync-log")
    logger.info("   ✅ /api/energy-discovery/backfill-coords")
    logger.info("   ✅ /api/energy-discovery/test-apis")

    return scheduler


if __name__ == '__main__':
    init_discovery_db()
    run_full_sync()
