"""
Energy Discovery Routes
=======================
Serves /api/energy-discovery/* endpoints for the Land & Power map
Energy Discovery Integration panel (power plants, transmission lines,
wind projects, pipelines) + /api/v1/capacity/heatmap for the
Capacity Headroom Heatmap layer.

Add to main.py:
    from routes.energy_discovery_routes import energy_discovery_bp
    app.register_blueprint(energy_discovery_bp)
"""

from flask import Blueprint, jsonify, request
import logging
import json
import math
import time

from util.transmission_tables import (
    GEOCODED_SNAPSHOT_KEY as _TX_SNAPSHOT_KEY,
    GEOCODED_SNAPSHOT_TABLE as _TX_SNAPSHOT_TABLE,
)

logger = logging.getLogger(__name__)

energy_discovery_bp = Blueprint('energy_discovery', __name__)

# /status runs ~8 COUNT(*) on large tables (substations 126K, transmission 52K,
# power_plants_eia 13K) — ~1.7s/call. On the 1-replica backend the land-power
# map's boot burst made that a flapping trigger (transient 503s observed in a
# 2026-06-06 Chrome QA sweep). The counts change slowly, so cache the payload.
_STATUS_CACHE = {'data': None, 'ts': 0.0}
_STATUS_TTL_S = 120

# ============================================================================
# MARKET DEFINITIONS (matches frontend MARKETS object)
# ============================================================================

MONITORED_MARKETS = {
    'phoenix': {'name': 'Phoenix, AZ', 'lat': 33.4484, 'lng': -112.0740, 'state': 'AZ', 'iso': 'WECC', 'tier': 1},
    'dallas': {'name': 'Dallas, TX', 'lat': 32.7767, 'lng': -96.7970, 'state': 'TX', 'iso': 'ERCOT', 'tier': 1},
    'northern_virginia': {'name': 'Northern Virginia', 'lat': 39.0438, 'lng': -77.4874, 'state': 'VA', 'iso': 'PJM', 'tier': 1},
    'atlanta': {'name': 'Atlanta, GA', 'lat': 33.7490, 'lng': -84.3880, 'state': 'GA', 'iso': 'MISO', 'tier': 1},
    'las_vegas': {'name': 'Las Vegas, NV', 'lat': 36.1699, 'lng': -115.1398, 'state': 'NV', 'iso': 'WECC', 'tier': 1},
    'salt_lake': {'name': 'Salt Lake City, UT', 'lat': 40.7608, 'lng': -111.8910, 'state': 'UT', 'iso': 'WECC', 'tier': 1},
    'columbus': {'name': 'Columbus, OH', 'lat': 39.9612, 'lng': -82.9988, 'state': 'OH', 'iso': 'PJM', 'tier': 1},
    'des_moines': {'name': 'Des Moines, IA', 'lat': 41.5868, 'lng': -93.6250, 'state': 'IA', 'iso': 'MISO', 'tier': 1},
    'chicago': {'name': 'Chicago, IL', 'lat': 41.8781, 'lng': -87.6298, 'state': 'IL', 'iso': 'PJM', 'tier': 1},
    'silicon_valley': {'name': 'Silicon Valley, CA', 'lat': 37.3861, 'lng': -122.0839, 'state': 'CA', 'iso': 'CAISO', 'tier': 1},
    'new_york_nj': {'name': 'New York / NJ', 'lat': 40.7128, 'lng': -74.0060, 'state': 'NJ', 'iso': 'PJM', 'tier': 1},
    'seattle_quincy': {'name': 'Seattle / Quincy, WA', 'lat': 47.2329, 'lng': -119.8526, 'state': 'WA', 'iso': 'WECC', 'tier': 1},
    'portland_hillsboro': {'name': 'Portland / Hillsboro, OR', 'lat': 45.5231, 'lng': -122.9898, 'state': 'OR', 'iso': 'WECC', 'tier': 1},
    'denver': {'name': 'Denver, CO', 'lat': 39.7392, 'lng': -104.9903, 'state': 'CO', 'iso': 'WECC', 'tier': 2},
    'san_antonio': {'name': 'San Antonio, TX', 'lat': 29.4241, 'lng': -98.4936, 'state': 'TX', 'iso': 'ERCOT', 'tier': 2},
    'houston': {'name': 'Houston, TX', 'lat': 29.7604, 'lng': -95.3698, 'state': 'TX', 'iso': 'ERCOT', 'tier': 2},
    'miami': {'name': 'Miami, FL', 'lat': 25.7617, 'lng': -80.1918, 'state': 'FL', 'iso': 'FRCC', 'tier': 2},
    'reno': {'name': 'Reno, NV', 'lat': 39.5296, 'lng': -119.8138, 'state': 'NV', 'iso': 'WECC', 'tier': 2},
    'sacramento': {'name': 'Sacramento, CA', 'lat': 38.5816, 'lng': -121.4944, 'state': 'CA', 'iso': 'CAISO', 'tier': 2},
    'minneapolis': {'name': 'Minneapolis, MN', 'lat': 44.9778, 'lng': -93.2650, 'state': 'MN', 'iso': 'MISO', 'tier': 3},
    'kansas_city': {'name': 'Kansas City, MO', 'lat': 39.0997, 'lng': -94.5786, 'state': 'MO', 'iso': 'SPP', 'tier': 3},
    'richmond': {'name': 'Richmond, VA', 'lat': 37.5407, 'lng': -77.4360, 'state': 'VA', 'iso': 'PJM', 'tier': 3},
    'nashville': {'name': 'Nashville, TN', 'lat': 36.1627, 'lng': -86.7816, 'state': 'TN', 'iso': 'MISO', 'tier': 3},
}

# ============================================================================
# SEED DATA — EIA Form 860 power plants (top facilities per market)
# ============================================================================

_POWER_PLANTS = [
    # Phoenix / AZ
    {'name': 'Palo Verde Nuclear', 'lat': 33.3881, 'lng': -112.8614, 'capacity_mw': 3937, 'fuel_type': 'Nuclear', 'operator': 'Arizona Public Service', 'state': 'AZ', 'source': 'EIA-860', 'market': 'phoenix'},
    {'name': 'Redhawk Power Station', 'lat': 33.3233, 'lng': -112.8439, 'capacity_mw': 1060, 'fuel_type': 'Natural Gas', 'operator': 'Arizona Public Service', 'state': 'AZ', 'source': 'EIA-860', 'market': 'phoenix'},
    {'name': 'West Phoenix Power Plant', 'lat': 33.3959, 'lng': -112.1651, 'capacity_mw': 655, 'fuel_type': 'Natural Gas', 'operator': 'Arizona Public Service', 'state': 'AZ', 'source': 'EIA-860', 'market': 'phoenix'},
    {'name': 'Agua Fria Generating Station', 'lat': 33.5600, 'lng': -112.1985, 'capacity_mw': 487, 'fuel_type': 'Natural Gas', 'operator': 'Arizona Public Service', 'state': 'AZ', 'source': 'EIA-860', 'market': 'phoenix'},
    {'name': 'Mesquite Generating Station', 'lat': 33.0635, 'lng': -112.7970, 'capacity_mw': 1250, 'fuel_type': 'Natural Gas', 'operator': 'Salt River Project', 'state': 'AZ', 'source': 'EIA-860', 'market': 'phoenix'},
    {'name': 'Solana Solar Station', 'lat': 32.9222, 'lng': -112.9778, 'capacity_mw': 280, 'fuel_type': 'Solar', 'operator': 'Abengoa Solar', 'state': 'AZ', 'source': 'EIA-860', 'market': 'phoenix'},
    # Dallas / TX
    {'name': 'Comanche Peak Nuclear', 'lat': 32.2979, 'lng': -97.7857, 'capacity_mw': 2430, 'fuel_type': 'Nuclear', 'operator': 'Luminant', 'state': 'TX', 'source': 'EIA-860', 'market': 'dallas'},
    {'name': 'Midlothian Power Plant', 'lat': 32.4562, 'lng': -96.9939, 'capacity_mw': 1560, 'fuel_type': 'Natural Gas', 'operator': 'Luminant', 'state': 'TX', 'source': 'EIA-860', 'market': 'dallas'},
    {'name': 'Forney Energy Center', 'lat': 32.7260, 'lng': -96.4270, 'capacity_mw': 1800, 'fuel_type': 'Natural Gas', 'operator': 'Forney Holdings', 'state': 'TX', 'source': 'EIA-860', 'market': 'dallas'},
    {'name': 'Wolf Hollow Gas Plant', 'lat': 32.3981, 'lng': -97.5580, 'capacity_mw': 720, 'fuel_type': 'Natural Gas', 'operator': 'Wolf Hollow', 'state': 'TX', 'source': 'EIA-860', 'market': 'dallas'},
    # Northern Virginia
    {'name': 'North Anna Nuclear', 'lat': 38.0608, 'lng': -77.7906, 'capacity_mw': 1892, 'fuel_type': 'Nuclear', 'operator': 'Dominion Energy', 'state': 'VA', 'source': 'EIA-860', 'market': 'northern_virginia'},
    {'name': 'Loudoun Peaker', 'lat': 39.0620, 'lng': -77.4680, 'capacity_mw': 660, 'fuel_type': 'Natural Gas', 'operator': 'Dominion Energy', 'state': 'VA', 'source': 'EIA-860', 'market': 'northern_virginia'},
    {'name': 'Possum Point Power Station', 'lat': 38.5509, 'lng': -77.2830, 'capacity_mw': 1173, 'fuel_type': 'Natural Gas', 'operator': 'Dominion Energy', 'state': 'VA', 'source': 'EIA-860', 'market': 'northern_virginia'},
    # Atlanta
    {'name': 'Plant Vogtle 1-4', 'lat': 33.1417, 'lng': -81.7600, 'capacity_mw': 4540, 'fuel_type': 'Nuclear', 'operator': 'Southern Nuclear', 'state': 'GA', 'source': 'EIA-860', 'market': 'atlanta'},
    {'name': 'Plant McDonough-Atkinson', 'lat': 33.6814, 'lng': -84.4922, 'capacity_mw': 2520, 'fuel_type': 'Natural Gas', 'operator': 'Georgia Power', 'state': 'GA', 'source': 'EIA-860', 'market': 'atlanta'},
    {'name': 'Plant Scherer', 'lat': 33.0600, 'lng': -83.8000, 'capacity_mw': 3520, 'fuel_type': 'Coal', 'operator': 'Georgia Power', 'state': 'GA', 'source': 'EIA-860', 'market': 'atlanta'},
    # Chicago
    {'name': 'Braidwood Nuclear', 'lat': 41.2447, 'lng': -88.2267, 'capacity_mw': 2386, 'fuel_type': 'Nuclear', 'operator': 'Constellation', 'state': 'IL', 'source': 'EIA-860', 'market': 'chicago'},
    {'name': 'LaSalle Nuclear', 'lat': 41.2439, 'lng': -88.6708, 'capacity_mw': 2320, 'fuel_type': 'Nuclear', 'operator': 'Constellation', 'state': 'IL', 'source': 'EIA-860', 'market': 'chicago'},
    {'name': 'Byron Nuclear', 'lat': 42.0753, 'lng': -89.2817, 'capacity_mw': 2347, 'fuel_type': 'Nuclear', 'operator': 'Constellation', 'state': 'IL', 'source': 'EIA-860', 'market': 'chicago'},
    # Columbus / Ohio
    {'name': 'Davis-Besse Nuclear', 'lat': 41.5967, 'lng': -83.0864, 'capacity_mw': 894, 'fuel_type': 'Nuclear', 'operator': 'Energy Harbor', 'state': 'OH', 'source': 'EIA-860', 'market': 'columbus'},
    {'name': 'Perry Nuclear', 'lat': 41.8000, 'lng': -81.1440, 'capacity_mw': 1256, 'fuel_type': 'Nuclear', 'operator': 'Energy Harbor', 'state': 'OH', 'source': 'EIA-860', 'market': 'columbus'},
    # Des Moines
    {'name': 'Marshalltown Generating Station', 'lat': 42.0289, 'lng': -92.9120, 'capacity_mw': 725, 'fuel_type': 'Natural Gas', 'operator': 'MidAmerican Energy', 'state': 'IA', 'source': 'EIA-860', 'market': 'des_moines'},
    {'name': 'Lundquist Wind Farm', 'lat': 42.4333, 'lng': -94.0667, 'capacity_mw': 300, 'fuel_type': 'Wind', 'operator': 'MidAmerican Energy', 'state': 'IA', 'source': 'EIA-860', 'market': 'des_moines'},
    # Las Vegas
    {'name': 'Chuck Lenzie Generating Station', 'lat': 36.3064, 'lng': -114.9861, 'capacity_mw': 1102, 'fuel_type': 'Natural Gas', 'operator': 'NV Energy', 'state': 'NV', 'source': 'EIA-860', 'market': 'las_vegas'},
    {'name': 'Silverhawk Generating Station', 'lat': 36.2414, 'lng': -115.2383, 'capacity_mw': 570, 'fuel_type': 'Natural Gas', 'operator': 'NV Energy', 'state': 'NV', 'source': 'EIA-860', 'market': 'las_vegas'},
    # Salt Lake
    {'name': 'Lake Side Power Plant', 'lat': 40.7500, 'lng': -111.9300, 'capacity_mw': 713, 'fuel_type': 'Natural Gas', 'operator': 'PacifiCorp', 'state': 'UT', 'source': 'EIA-860', 'market': 'salt_lake'},
    # Silicon Valley
    {'name': 'Metcalf Energy Center', 'lat': 37.2297, 'lng': -121.7614, 'capacity_mw': 600, 'fuel_type': 'Natural Gas', 'operator': 'Calpine', 'state': 'CA', 'source': 'EIA-860', 'market': 'silicon_valley'},
    {'name': 'Diablo Canyon Nuclear', 'lat': 35.2112, 'lng': -120.8561, 'capacity_mw': 2256, 'fuel_type': 'Nuclear', 'operator': 'PG&E', 'state': 'CA', 'source': 'EIA-860', 'market': 'silicon_valley'},
    # Seattle / Quincy
    {'name': 'Grand Coulee Dam', 'lat': 47.9560, 'lng': -118.9817, 'capacity_mw': 6809, 'fuel_type': 'Hydro', 'operator': 'Bureau of Reclamation', 'state': 'WA', 'source': 'EIA-860', 'market': 'seattle_quincy'},
    {'name': 'Columbia Nuclear', 'lat': 46.4711, 'lng': -119.3333, 'capacity_mw': 1190, 'fuel_type': 'Nuclear', 'operator': 'Energy NW', 'state': 'WA', 'source': 'EIA-860', 'market': 'seattle_quincy'},
    # Portland
    {'name': 'Boardman Coal Plant', 'lat': 45.6889, 'lng': -119.8328, 'capacity_mw': 585, 'fuel_type': 'Coal', 'operator': 'Portland General', 'state': 'OR', 'source': 'EIA-860', 'market': 'portland_hillsboro'},
    {'name': 'Coyote Springs Combined Cycle', 'lat': 45.6667, 'lng': -119.8000, 'capacity_mw': 242, 'fuel_type': 'Natural Gas', 'operator': 'Portland General', 'state': 'OR', 'source': 'EIA-860', 'market': 'portland_hillsboro'},
]

_WIND_PROJECTS = [
    {'project_name': 'Horse Heaven Wind Farm', 'lat': 45.9833, 'lng': -119.5167, 'project_capacity_mw': 1150, 'turbine_capacity_kw': 5000, 'manufacturer': 'GE', 'model': 'Haliade-X', 'state': 'WA', 'county': 'Benton', 'market': 'seattle_quincy'},
    {'project_name': 'Rolling Hills Wind Farm', 'lat': 42.1500, 'lng': -93.8333, 'project_capacity_mw': 443, 'turbine_capacity_kw': 2300, 'manufacturer': 'Siemens Gamesa', 'model': 'SG-2.3', 'state': 'IA', 'county': 'Adair', 'market': 'des_moines'},
    {'project_name': 'Highland Wind Farm', 'lat': 42.3333, 'lng': -94.2500, 'project_capacity_mw': 300, 'turbine_capacity_kw': 2000, 'manufacturer': 'Vestas', 'model': 'V110', 'state': 'IA', 'county': 'Calhoun', 'market': 'des_moines'},
    {'project_name': 'Flat Ridge 2 Wind Farm', 'lat': 37.2500, 'lng': -98.3333, 'project_capacity_mw': 419, 'turbine_capacity_kw': 1600, 'manufacturer': 'GE', 'model': 'GE-1.6', 'state': 'KS', 'county': 'Barber', 'market': 'kansas_city'},
    {'project_name': 'Panhandle Wind Ranch', 'lat': 35.5000, 'lng': -101.2500, 'project_capacity_mw': 458, 'turbine_capacity_kw': 2300, 'manufacturer': 'Siemens Gamesa', 'model': 'SG-2.3', 'state': 'TX', 'county': 'Carson', 'market': 'dallas'},
    {'project_name': 'Sweetwater Wind Farm', 'lat': 32.4667, 'lng': -100.4167, 'project_capacity_mw': 585, 'turbine_capacity_kw': 1500, 'manufacturer': 'GE', 'model': 'GE-1.5', 'state': 'TX', 'county': 'Nolan', 'market': 'dallas'},
    {'project_name': 'Meadow Lake Wind Farm', 'lat': 40.7500, 'lng': -87.1667, 'project_capacity_mw': 801, 'turbine_capacity_kw': 1500, 'manufacturer': 'Vestas', 'model': 'V82', 'state': 'IN', 'county': 'White', 'market': 'chicago'},
    {'project_name': 'Spring Valley Wind Farm', 'lat': 40.2000, 'lng': -114.7000, 'project_capacity_mw': 152, 'turbine_capacity_kw': 3000, 'manufacturer': 'Vestas', 'model': 'V112', 'state': 'NV', 'county': 'Spring Valley', 'market': 'las_vegas'},
    {'project_name': 'Shepherds Flat Wind Farm', 'lat': 45.5833, 'lng': -120.0000, 'project_capacity_mw': 845, 'turbine_capacity_kw': 2500, 'manufacturer': 'GE', 'model': 'GE-2.5', 'state': 'OR', 'county': 'Gilliam', 'market': 'portland_hillsboro'},
    {'project_name': 'Alta Wind Energy Center', 'lat': 35.0833, 'lng': -118.3500, 'project_capacity_mw': 1547, 'turbine_capacity_kw': 3000, 'manufacturer': 'GE', 'model': 'GE-1.5/2.85', 'state': 'CA', 'county': 'Kern', 'market': 'silicon_valley'},
]

_PIPELINES = [
    {'name': 'Transwestern Pipeline', 'lat': 33.4484, 'lng': -112.0740, 'capacity_mdth': 2184, 'diameter_inches': 36, 'operator': 'Energy Transfer', 'commodity': 'Natural Gas', 'state': 'AZ', 'states_served': 'TX, NM, AZ', 'market': 'phoenix'},
    {'name': 'El Paso Natural Gas', 'lat': 33.4000, 'lng': -112.5000, 'capacity_mdth': 5500, 'diameter_inches': 42, 'operator': 'Kinder Morgan', 'commodity': 'Natural Gas', 'state': 'AZ', 'states_served': 'TX, NM, AZ, CA', 'market': 'phoenix'},
    {'name': 'Atmos Pipeline Texas', 'lat': 32.7767, 'lng': -96.7970, 'capacity_mdth': 4200, 'diameter_inches': 30, 'operator': 'Atmos Energy', 'commodity': 'Natural Gas', 'state': 'TX', 'states_served': 'TX', 'market': 'dallas'},
    {'name': 'Enterprise TexOk', 'lat': 32.9000, 'lng': -96.5000, 'capacity_mdth': 3800, 'diameter_inches': 24, 'operator': 'Enterprise Products', 'commodity': 'NGL', 'state': 'TX', 'states_served': 'TX, OK', 'market': 'dallas'},
    {'name': 'Texas Eastern (TETCO)', 'lat': 39.0438, 'lng': -77.4874, 'capacity_mdth': 9400, 'diameter_inches': 36, 'operator': 'Energy Transfer', 'commodity': 'Natural Gas', 'state': 'VA', 'states_served': 'TX, LA, MS, AL, GA, TN, KY, OH, PA, NJ, NY', 'market': 'northern_virginia'},
    {'name': 'Transcontinental (Transco)', 'lat': 39.1000, 'lng': -77.3000, 'capacity_mdth': 17800, 'diameter_inches': 42, 'operator': 'Williams', 'commodity': 'Natural Gas', 'state': 'VA', 'states_served': 'TX, LA, MS, AL, GA, SC, NC, VA, MD, PA, NJ, NY', 'market': 'northern_virginia'},
    {'name': 'Southern Natural Gas', 'lat': 33.7490, 'lng': -84.3880, 'capacity_mdth': 3200, 'diameter_inches': 36, 'operator': 'Williams', 'commodity': 'Natural Gas', 'state': 'GA', 'states_served': 'LA, MS, AL, GA, SC', 'market': 'atlanta'},
    {'name': 'Natural Gas Pipeline (NGPL)', 'lat': 41.8781, 'lng': -87.6298, 'capacity_mdth': 5850, 'diameter_inches': 36, 'operator': 'Kinder Morgan', 'commodity': 'Natural Gas', 'state': 'IL', 'states_served': 'TX, OK, KS, NE, IA, IL', 'market': 'chicago'},
    {'name': 'Kern River Gas Transmission', 'lat': 36.1699, 'lng': -115.1398, 'capacity_mdth': 1800, 'diameter_inches': 36, 'operator': 'Berkshire Hathaway', 'commodity': 'Natural Gas', 'state': 'NV', 'states_served': 'WY, UT, NV, CA', 'market': 'las_vegas'},
    {'name': 'Questar Pipeline', 'lat': 40.7608, 'lng': -111.8910, 'capacity_mdth': 1200, 'diameter_inches': 24, 'operator': 'Dominion Energy', 'commodity': 'Natural Gas', 'state': 'UT', 'states_served': 'WY, CO, UT', 'market': 'salt_lake'},
    {'name': 'Columbia Gas Transmission', 'lat': 39.9612, 'lng': -82.9988, 'capacity_mdth': 3900, 'diameter_inches': 36, 'operator': 'TC Energy', 'commodity': 'Natural Gas', 'state': 'OH', 'states_served': 'KY, OH, PA, VA, WV, NY', 'market': 'columbus'},
    {'name': 'Northern Border Pipeline', 'lat': 41.5868, 'lng': -93.6250, 'capacity_mdth': 2400, 'diameter_inches': 42, 'operator': 'ONEOK', 'commodity': 'Natural Gas', 'state': 'IA', 'states_served': 'MT, ND, SD, MN, IA, IL', 'market': 'des_moines'},
    {'name': 'Northwest Pipeline', 'lat': 47.2329, 'lng': -119.8526, 'capacity_mdth': 3800, 'diameter_inches': 36, 'operator': 'Williams', 'commodity': 'Natural Gas', 'state': 'WA', 'states_served': 'NM, CO, WY, UT, ID, WA, OR', 'market': 'seattle_quincy'},
    {'name': 'Ruby Pipeline', 'lat': 45.5231, 'lng': -122.9898, 'capacity_mdth': 1500, 'diameter_inches': 42, 'operator': 'Tallgrass Energy', 'commodity': 'Natural Gas', 'state': 'OR', 'states_served': 'WY, UT, NV, OR', 'market': 'portland_hillsboro'},
]

_TRANSMISSION_LINES = [
    {'owner': 'APS', 'voltage_kv': 500, 'volt_class': 'EHV', 'sub_1': 'Palo Verde', 'sub_2': 'Kyrene', 'state': 'AZ', 'market': 'phoenix'},
    {'owner': 'SRP', 'voltage_kv': 230, 'volt_class': 'HV', 'sub_1': 'Santan', 'sub_2': 'Browning', 'state': 'AZ', 'market': 'phoenix'},
    {'owner': 'Oncor', 'voltage_kv': 345, 'volt_class': 'EHV', 'sub_1': 'Venus', 'sub_2': 'Midlothian', 'state': 'TX', 'market': 'dallas'},
    {'owner': 'Dominion', 'voltage_kv': 500, 'volt_class': 'EHV', 'sub_1': 'Loudoun', 'sub_2': 'Brambleton', 'state': 'VA', 'market': 'northern_virginia'},
    {'owner': 'Dominion', 'voltage_kv': 230, 'volt_class': 'HV', 'sub_1': 'Gainesville', 'sub_2': 'Vint Hill', 'state': 'VA', 'market': 'northern_virginia'},
    {'owner': 'Georgia Power', 'voltage_kv': 500, 'volt_class': 'EHV', 'sub_1': 'Plant Hatch', 'sub_2': 'McDonough', 'state': 'GA', 'market': 'atlanta'},
    {'owner': 'ComEd', 'voltage_kv': 345, 'volt_class': 'EHV', 'sub_1': 'Braidwood', 'sub_2': 'Crestwood', 'state': 'IL', 'market': 'chicago'},
    {'owner': 'AEP Ohio', 'voltage_kv': 765, 'volt_class': 'UHV', 'sub_1': 'Kammer', 'sub_2': 'Marysville', 'state': 'OH', 'market': 'columbus'},
    {'owner': 'MidAmerican', 'voltage_kv': 345, 'volt_class': 'EHV', 'sub_1': 'Grimes', 'sub_2': 'Hawthorn', 'state': 'IA', 'market': 'des_moines'},
    {'owner': 'NV Energy', 'voltage_kv': 500, 'volt_class': 'EHV', 'sub_1': 'Mead', 'sub_2': 'Northwest', 'state': 'NV', 'market': 'las_vegas'},
    {'owner': 'PG&E', 'voltage_kv': 500, 'volt_class': 'EHV', 'sub_1': 'Tesla', 'sub_2': 'Metcalf', 'state': 'CA', 'market': 'silicon_valley'},
    {'owner': 'BPA', 'voltage_kv': 500, 'volt_class': 'EHV', 'sub_1': 'Grand Coulee', 'sub_2': 'Vantage', 'state': 'WA', 'market': 'seattle_quincy'},
    {'owner': 'BPA', 'voltage_kv': 500, 'volt_class': 'EHV', 'sub_1': 'John Day', 'sub_2': 'Malin', 'state': 'OR', 'market': 'portland_hillsboro'},
]


# ============================================================================
# HELPER — filter by market
# ============================================================================

def _filter_market(data, market_key):
    """Filter data list by market. Empty string = all."""
    if not market_key:
        return data
    return [item for item in data if item.get('market', '') == market_key]


# ============================================================================
# MARKET RESOLUTION + GEOGRAPHIC FILTER (SH52-051 follow-on, 2026-08-09)
#
# The 2026-06-06 "live table" rewrite below moved these endpoints off the
# curated seed arrays and onto power_plants_eia / gas_pipelines / the
# transmission snapshot. `?market=` survived the rewrite only in the SEED
# fallback (`_filter_market`) — the live SQL had no market predicate and
# every row was stamped `'market': ''`. Result: all 23 monitored markets
# returned a byte-identical national top-N-by-capacity list.
#
# Measured 2026-08-09 against production: /power-plants?market=phoenix and
# ?market=atlanta returned the same sha1, 500 rows across 47 states, top row
# "Grand Coulee" (WA) for Phoenix. The data-sync "Energy discovery per
# market" step read that as "23 markets OK, 11,500 plants" — 23 copies of
# one list. Same vacuous-green class as shell #51 (FRESH != GROWTH).
#
# power_plants_eia has no `market` column (35 cols; lat/lng/state/county/city
# only), so the filter is geographic. There is already a partial index —
# idx_power_plants_lat_lng ON power_plants_eia(lat, lng) WHERE lat IS NOT
# NULL (scripts/load_power_plants.py) — so the bbox is index-supported.
# ============================================================================

# Aliases for market keys that callers send but MONITORED_MARKETS does not
# define. .github/workflows/data-sync.yml has shipped these four names since
# the step was written; without the alias they resolve to nothing.
_MARKET_ALIASES = {
    'salt_lake_city': 'salt_lake',
    'new_york': 'new_york_nj',
    'nyc': 'new_york_nj',
    'seattle': 'seattle_quincy',
    'quincy': 'seattle_quincy',
    'portland': 'portland_hillsboro',
    'hillsboro': 'portland_hillsboro',
    'nova': 'northern_virginia',
}

# Capture radius for the ?market= filter, km. Sized to the metro's GENERATION
# SHED, not its city limits — the plants that serve a market sit well outside
# it (Palo Verde is 75 km from downtown Phoenix; Grand Coulee is 105 km from
# Quincy). These match the markets the curated seed already assigns, so the
# live filter and the seed fallback agree on what "in the market" means.
_DEFAULT_MARKET_RADIUS_KM = 120.0
_MARKET_RADIUS_KM = {
    'seattle_quincy': 160.0,      # market literally spans Seattle -> Quincy
    'portland_hillsboro': 150.0,  # Boardman / Coyote Springs sit ~150 km east
    'silicon_valley': 200.0,      # Diablo Canyon is the CAISO anchor unit
    'atlanta': 180.0,             # Vogtle and Scherer are both well out
    'chicago': 140.0,
    'dallas': 140.0,
}

_KM_PER_DEG_LAT = 110.574


def _resolve_market(raw):
    """('', None) for no filter, (key, None) for a known market, (None, err)
    for a key we do not recognise.

    An unknown key is an ERROR, not a silent pass-through. Returning the
    national list for `?market=typo` is exactly how this defect stayed
    invisible for months: the caller asked for one market, got everything,
    and had no way to tell.
    """
    key = (raw or '').strip().lower()
    if not key:
        return '', None
    key = _MARKET_ALIASES.get(key, key)
    if key not in MONITORED_MARKETS:
        return None, {
            'success': False,
            'error': 'unknown_market',
            'market_requested': raw,
            'valid_markets': sorted(MONITORED_MARKETS.keys()),
            'aliases': _MARKET_ALIASES,
        }
    return key, None


def _market_bbox(market_key):
    """(min_lat, max_lat, min_lng, max_lng) for a RESOLVED market key, or None.

    Bounding box rather than great-circle distance: it is a sargable range
    scan against the existing (lat, lng) index, and at metro scale the corner
    over-capture is immaterial next to serving 47 states for every query.
    """
    if not market_key:
        return None
    m = MONITORED_MARKETS.get(market_key)
    if not m:
        return None
    lat, lng = float(m['lat']), float(m['lng'])
    radius_km = _MARKET_RADIUS_KM.get(market_key, _DEFAULT_MARKET_RADIUS_KM)
    dlat = radius_km / _KM_PER_DEG_LAT
    # Longitude degrees shrink with latitude; clamp so a high-latitude market
    # cannot blow the box open via a near-zero cosine.
    dlng = radius_km / max(10.0, _KM_PER_DEG_LAT * math.cos(math.radians(lat)))
    return (lat - dlat, lat + dlat, lng - dlng, lng + dlng)


def _bbox_sql(market_key):
    """('' , []) or (' AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s',
    [4 bounds]) — spliced into the live queries below."""
    box = _market_bbox(market_key)
    if not box:
        return '', []
    return (' AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s',
            [box[0], box[1], box[2], box[3]])


# ============================================================================
# LIVE-TABLE READ HELPERS (2026-06-06)
# The four data endpoints below used to return tiny hardcoded seed arrays
# (32 plants / 13 lines / 14 pipelines) — users correctly flagged the map as
# "light". They now query the populated EIA/HIFLD tables and FALL BACK to the
# curated seed on ANY error or empty result, so the endpoint can never return
# *fewer* items than before.
# ============================================================================

def _rows_from_db(sql, params, mapper):
    """Run a read-only query and map each row. Returns [] on any failure so
    the caller can fall back to seed data (never regress, never 500)."""
    conn = None
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if not conn:
            return []
        cur = conn.cursor()
        cur.execute(sql, params)
        return [mapper(r) for r in cur.fetchall()]
    except Exception as e:
        logger.warning(f"energy-discovery live query failed, using seed: {e}")
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _volt_class(kv):
    try:
        kv = float(kv or 0)
    except (TypeError, ValueError):
        return 'HV'
    if kv >= 765:
        return 'UHV'
    if kv >= 345:
        return 'EHV'
    if kv >= 100:
        return 'HV'
    return 'MV'


# ============================================================================
# ROUTES — /api/energy-discovery/*
# ============================================================================

@energy_discovery_bp.route('/api/energy-discovery/power-plants', methods=['GET'])
def energy_discovery_power_plants():
    """Power plants for the Energy Discovery panel — live from power_plants_eia
    (~13K rows); curated seed fallback on any error/empty."""
    try:
        market, err = _resolve_market(request.args.get('market', ''))
        if err:
            return jsonify(err), 400
        limit = min(int(request.args.get('limit', 2000)), 5000)
        bbox_sql, bbox_params = _bbox_sql(market)
        plants = _rows_from_db(
            "SELECT name, lat, lng, nameplate_capacity_mw, primary_fuel, "
            "utility_name, state FROM power_plants_eia "
            "WHERE lat IS NOT NULL AND lng IS NOT NULL" + bbox_sql +
            " ORDER BY nameplate_capacity_mw DESC NULLS LAST LIMIT %s",
            bbox_params + [limit],
            lambda r: {'name': r[0] or 'Power Plant',
                       'lat': float(r[1]), 'lng': float(r[2]),
                       'capacity_mw': float(r[3]) if r[3] is not None else 0,
                       'fuel_type': r[4] or 'Unknown', 'operator': r[5] or '',
                       'state': r[6] or '', 'source': 'EIA-860',
                       'market': market})
        if not plants:
            plants = _filter_market(_POWER_PLANTS, market)[:limit]
        return jsonify({'success': True, 'data': plants, 'count': len(plants),
                        'market': market,
                        'market_filtered': bool(market)})
    except Exception as e:
        logger.error(f"Energy discovery power-plants error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@energy_discovery_bp.route('/api/energy-discovery/transmission-lines', methods=['GET'])
def energy_discovery_transmission_lines():
    """Transmission lines for the Energy Discovery panel — live from
    transmission_lines_eia (56,108 rows); curated seed fallback on error/empty.

    ★ 2026-07-29 shell(#41): transmission_lines_eia is a frozen GEOCODED
    SNAPSHOT with no writer and no timestamp column. It is NOT the maintained
    set — transmission_lines holds 94,626 maintained rows but stores no
    geometry (returnGeometry=false in routes/transmission_ingest.py), so this
    coordinate-bearing query cannot be repointed at it. `count` is a FLOOR:
    38,518 maintained lines (40.7%) have no coordinates and cannot appear here.
    See util/transmission_tables.py.
    """
    try:
        market, err = _resolve_market(request.args.get('market', ''))
        if err:
            return jsonify(err), 400
        limit = min(int(request.args.get('limit', 2000)), 5000)
        bbox_sql, bbox_params = _bbox_sql(market)
        lines = _rows_from_db(
            "SELECT owner, voltage_kv, sub_1, sub_2, lat, lng, state "
            f"FROM {_TX_SNAPSHOT_TABLE} "
            "WHERE lat IS NOT NULL AND lng IS NOT NULL" + bbox_sql +
            " ORDER BY voltage_kv DESC NULLS LAST LIMIT %s",
            bbox_params + [limit],
            lambda r: {'owner': r[0] or '',
                       'voltage_kv': float(r[1]) if r[1] is not None else 0,
                       'volt_class': _volt_class(r[1]),
                       'sub_1': r[2] or '', 'sub_2': r[3] or '',
                       'lat': float(r[4]), 'lng': float(r[5]),
                       'state': r[6] or '', 'market': market})
        if not lines:
            lines = _filter_market(_TRANSMISSION_LINES, market)[:limit]
        return jsonify({'success': True, 'data': lines, 'count': len(lines),
                        'count_is_floor': True,
                        'market': market,
                        'market_filtered': bool(market),
                        'served_from_key': _TX_SNAPSHOT_KEY})
    except Exception as e:
        logger.error(f"Energy discovery transmission-lines error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@energy_discovery_bp.route('/api/energy-discovery/wind-projects', methods=['GET'])
def energy_discovery_wind_projects():
    """Wind projects for the Energy Discovery panel. The dedicated wind_projects
    table is empty, so serve real wind generation from power_plants_eia
    (primary_fuel='wind', ~hundreds of sites); curated seed fallback on error."""
    try:
        market, err = _resolve_market(request.args.get('market', ''))
        if err:
            return jsonify(err), 400
        limit = min(int(request.args.get('limit', 2000)), 5000)
        bbox_sql, bbox_params = _bbox_sql(market)
        projects = _rows_from_db(
            "SELECT name, lat, lng, nameplate_capacity_mw, utility_name, state, county "
            "FROM power_plants_eia "
            "WHERE lat IS NOT NULL AND lng IS NOT NULL AND primary_fuel ILIKE '%%wind%%'"
            + bbox_sql +
            " ORDER BY nameplate_capacity_mw DESC NULLS LAST LIMIT %s",
            bbox_params + [limit],
            lambda r: {'project_name': r[0] or 'Wind Project',
                       'lat': float(r[1]), 'lng': float(r[2]),
                       'project_capacity_mw': float(r[3]) if r[3] is not None else 0,
                       'turbine_capacity_kw': 0, 'manufacturer': '', 'model': '',
                       'operator': r[4] or '', 'state': r[5] or '',
                       'county': r[6] or '', 'market': market})
        if not projects:
            projects = _filter_market(_WIND_PROJECTS, market)[:limit]
        return jsonify({'success': True, 'data': projects, 'count': len(projects),
                        'market': market,
                        'market_filtered': bool(market)})
    except Exception as e:
        logger.error(f"Energy discovery wind-projects error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@energy_discovery_bp.route('/api/energy-discovery/pipelines', methods=['GET'])
def energy_discovery_pipelines():
    """Gas pipelines for the Energy Discovery panel — live from gas_pipelines
    (918 geocoded segments); curated seed fallback on error/empty.

    Location-aware: when lat/lng are supplied, return the pipeline points
    NEAREST that point (bbox pre-filter + distance sort) rather than an
    arbitrary unordered LIMIT slice. Without this the plain `LIMIT 200`
    returned a Texas-heavy national sample, so regional operators near a
    viewed site (e.g. National Fuel Gas around Bear Lake, PA) never surfaced
    even though their segments are in the table. No lat/lng → prior behavior.
    """
    try:
        market, err = _resolve_market(request.args.get('market', ''))
        if err:
            return jsonify(err), 400
        limit = min(int(request.args.get('limit', 500)), 1000)
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        radius = request.args.get('radius', default=150.0, type=float)  # miles
        mapper = lambda r: {'name': (r[0] or 'Gas Pipeline'),
                            'operator': r[0] or '',
                            'lat': float(r[1]), 'lng': float(r[2]),
                            'commodity': 'Natural Gas',
                            'pipeline_type': r[3] or '',
                            'state': '', 'market': market}
        if lat is not None and lng is not None:
            radius = max(1.0, min(radius, 1000.0))
            dlat = radius / 69.0
            dlng = radius / max(0.1, 69.0 * math.cos(math.radians(lat)))
            pipes = _rows_from_db(
                "SELECT operator, lat, lng, pipeline_type FROM gas_pipelines "
                "WHERE lat IS NOT NULL AND lng IS NOT NULL "
                "AND lat BETWEEN %s AND %s AND lng BETWEEN %s AND %s "
                "ORDER BY power(lat - %s, 2) + power(lng - %s, 2) ASC LIMIT %s",
                [lat - dlat, lat + dlat, lng - dlng, lng + dlng, lat, lng, limit],
                mapper)
        else:
            # Explicit lat/lng wins; otherwise a market is itself a location.
            bbox_sql, bbox_params = _bbox_sql(market)
            pipes = _rows_from_db(
                "SELECT operator, lat, lng, pipeline_type FROM gas_pipelines "
                "WHERE lat IS NOT NULL AND lng IS NOT NULL" + bbox_sql +
                " LIMIT %s",
                bbox_params + [limit], mapper)
        if not pipes:
            pipes = _filter_market(_PIPELINES, market)[:limit]
        return jsonify({'success': True, 'data': pipes, 'count': len(pipes),
                        'market': market,
                        'market_filtered': bool(market)})
    except Exception as e:
        logger.error(f"Energy discovery pipelines error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# AUTO-REPAIR: duplicate route '/api/energy-discovery/status' also in energy_auto_discovery.py:583 — review and remove one
@energy_discovery_bp.route('/api/energy-discovery/status', methods=['GET'])
def energy_discovery_status():
    """phase20_status_truth: query real DB tables instead of seed/cached state.

    Replaces the in-memory state with live row counts + last-updated
    timestamps. Used by the dashboard, watchdog, and Land-Power map UI
    as the freshness/health signal. Cached _STATUS_TTL_S seconds — the COUNT(*)
    sweep is slow and the boot burst otherwise flaps the 1-replica backend.
    """
    _now = time.time()
    if _STATUS_CACHE['data'] is not None and (_now - _STATUS_CACHE['ts']) < _STATUS_TTL_S:
        return jsonify({**_STATUS_CACHE['data'], '_cache': 'hit'})
    out = {
        'success': True,
        'data': {
            'markets_monitored': 23,
            'hifld_sources': 5,
            'running': True,
            'recent_syncs': [],
        },
    }
    conn = None
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if conn:
            cur = conn.cursor()

            def _count_max(table, ts_col='updated_at'):
                # Phase FF+7 (2026-05-18): try ts_col, fall back to
                # created_at, then plain COUNT(*). Some tables
                # (transmission_lines, gas_pipelines, fiber_routes) lack
                # updated_at — Railway logs flagged transmission_lines
                # explicitly on every refresh cycle.
                for col in (ts_col, 'created_at', 'inserted_at', None):
                    try:
                        if col:
                            cur.execute(f"SELECT COUNT(*), MAX({col}) FROM {table}")
                            r = cur.fetchone() or (0, None)
                            return int(r[0] or 0), (str(r[1]) if r[1] else None)
                        cur.execute(f"SELECT COUNT(*) FROM {table}")
                        r = cur.fetchone() or (0,)
                        return int(r[0] or 0), None
                    except Exception:
                        try: conn.rollback()
                        except Exception: pass
                        continue
                return 0, None

            # Phase FF+6 (2026-05-18): to_regclass guard + correct table names
            # to silence Railway log noise. power_plants uses created_at.
            # Phase FF+14-schemafix (2026-05-19): transmission_lines lacks
            # updated_at — only has created_at. Set right column upfront so
            # the db_utils wrapper doesn't log the failed first attempt
            # before _count_max falls back.
            for label, table, ts in [
                ('total_substations',      'substations',        'updated_at'),
                ('total_pipelines',        'gas_pipelines',      'updated_at'),
                ('total_power_plants',     'power_plants_eia',   None),  # no ts column on this table -> plain COUNT (silences created_at/inserted_at log spam)
                ('total_transmissions',    'transmission_lines', 'created_at'),
                ('total_wind_projects',    'wind_projects',      'updated_at'),
                # 2026-07-03: table names were WRONG (gas_compressors/gas_processings
                # don't exist) so to_regclass short-circuited both to 0, which
                # tripped the ingestion watchdog even though the data is present.
                # Real tables: gas_compressor_stations (1.7k) / gas_processing_plants
                # (478), timestamped by loaded_at (no updated_at/created_at).
                ('total_gas_compressors',  'gas_compressor_stations', 'loaded_at'),
                ('total_gas_processings',  'gas_processing_plants',   'loaded_at'),
                ('total_fiber_routes',     'fiber_routes',       'updated_at'),
            ]:
                try:
                    cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
                    if not (cur.fetchone() or [None])[0]:
                        out['data'][label] = 0
                        continue
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    continue
                n, latest = _count_max(table, ts)
                out['data'][label] = n
                if latest:
                    out['data'][label.replace('total_', 'latest_')] = latest

            # total capacity (substations carry voltage_kv, sum power plant capacity)
            try:
                cur.execute("SELECT COALESCE(SUM(nameplate_capacity_mw),0) FROM power_plants_eia")
                cap_row = cur.fetchone() or (0,)
                out['data']['total_capacity_mw'] = int(cap_row[0] or 0)
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # Wind: the dedicated wind_projects table is empty, so the loop above
            # reports 0. Count wind generation from power_plants_eia instead so
            # the badge reflects reality.
            try:
                cur.execute("SELECT COUNT(*) FROM power_plants_eia WHERE primary_fuel ILIKE '%%wind%%'")
                _wr = cur.fetchone()
                if _wr and _wr[0]:
                    out['data']['total_wind_projects'] = int(_wr[0])
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # recent_syncs from any source we can find
            # Phase FF+6: power_plants uses created_at, not updated_at
            #
            # Phase plant-count-truth (2026-07-29): the `power_plants` member is
            # GONE from this list. Measured live on this endpoint before the fix:
            #
            #     total_power_plants = 13446          <- power_plants_eia (:423)
            #     recent_syncs[power_plants] = 2026-03-30 07:30:25
            #
            # Those two are about DIFFERENT TABLES. The count is the real US EIA
            # fleet; the timestamp was MAX(created_at) over the bare
            # `power_plants` table, which holds 66 rows for the entire United
            # States — the same EIA-860 population loaded to ~0.5% because the
            # crawler's dedup step keys on rec['plantid'], a spelling the EIA v2
            # facility-fuel response does not return, and silently skipped
            # 54,934 of 55,000 records while reporting errors=0
            # (land_power_crawler.py crawl_power_plants, fixed in #1923).
            #
            # So a reader saw a fresh, correct 13,446 welded to a four-month-old
            # date and concluded the EIA fleet was four months stale. A stale
            # date beside a fresh number is worse than no date: it is a wrong
            # answer to "how current is this?", not a missing one.
            #
            # `power_plants_eia` carries NO timestamp column (it is a
            # full-replace load — see scripts/load_power_plants.py DDL, and
            # :423 already passes ts=None for exactly this reason), so it cannot
            # honestly report a sync time. It reports `at: None` WITH a reason
            # rather than borrowing the stub's date.
            #
            # This deliberately goes one step further than the sibling
            # /api/v1/energy/discovery/status (main.py, #1923), which drops a
            # table with no timestamp column silently: an omission leaves the
            # caller unable to tell "not tracked" from "never synced". House
            # rule is null + a reason, never a bare gap.
            #
            # The UNION ALL is also gone. It bound three tables into one
            # statement, so a single missing table or renamed column failed the
            # whole query and left recent_syncs as the seed `[]` — an empty list
            # published as if it meant "no syncs have happened". Per-table probe
            # + per-table reason instead.
            _SYNC_SOURCES = (
                # (table, preferred ts column)
                ('substations',      'updated_at'),
                ('fiber_routes',     'updated_at'),
                ('power_plants_eia', None),
            )
            _syncs = []
            for _tbl, _pref in _SYNC_SOURCES:
                try:
                    cur.execute(
                        "SELECT column_name FROM information_schema.columns "
                        " WHERE table_schema = 'public' AND table_name = %s",
                        (_tbl,))
                    _cols = {r[0] for r in cur.fetchall()}
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    _syncs.append({
                        'source': _tbl, 'at': None,
                        'unmeasured': 'could not read column list for this table',
                    })
                    continue
                if not _cols:
                    _syncs.append({
                        'source': _tbl, 'at': None,
                        'unmeasured': 'table absent on this deploy',
                    })
                    continue
                _ts = next((c for c in (_pref, 'updated_at', 'created_at',
                                        'loaded_at', 'retrieved_at')
                            if c and c in _cols), None)
                if not _ts:
                    _syncs.append({
                        'source': _tbl, 'at': None,
                        'unmeasured': (
                            'this table carries no timestamp column, so it has '
                            'no per-row sync time to report. Not stale — '
                            'unmeasured.'),
                    })
                    continue
                try:
                    cur.execute(f"SELECT MAX({_ts}) FROM {_tbl}")
                    _v = (cur.fetchone() or [None])[0]
                    _syncs.append({
                        'source': _tbl,
                        'at': str(_v) if _v else None,
                        'basis': f'MAX({_ts}) over {_tbl}',
                    })
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    _syncs.append({
                        'source': _tbl, 'at': None,
                        'unmeasured': f'MAX({_ts}) over {_tbl} failed',
                    })
            out['data']['recent_syncs'] = _syncs
            # Name the table and scope behind each published plant figure, so
            # `power_plants` can never again be read as the source of a count
            # that comes from power_plants_eia.
            out['data']['basis'] = {
                'total_power_plants': (
                    'COUNT(*) over power_plants_eia — US EIA-860 plant records. '
                    'NOT the bare `power_plants` table, which is a 66-row stub '
                    'of the same population.'),
                'total_capacity_mw': (
                    'SUM(nameplate_capacity_mw) over power_plants_eia, US only.'),
                'total_wind_projects': (
                    "COUNT(*) over power_plants_eia WHERE primary_fuel ILIKE "
                    "'%wind%' — wind PLANTS by primary fuel, not "
                    'interconnection-queue projects.'),
                'recent_syncs': (
                    'per-table MAX(timestamp); a table with no timestamp column '
                    'reports at=None plus a reason rather than borrowing '
                    "another table's date."),
            }

            # seed_data flag: false if substations > 1000 (real data ingested)
            out['data']['seed_data'] = (
                int(out['data'].get('total_substations', 0)) < 1000
            )
    except Exception as _e:
        out['data']['_error'] = type(_e).__name__ + ': ' + str(_e)[:200]
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # Cache only a clean result — never pin a transient DB error for 2 min.
    if not out.get('data', {}).get('_error'):
        _STATUS_CACHE['data'] = out
        _STATUS_CACHE['ts'] = _now
    return jsonify({**out, '_cache': 'miss'})
# AUTO-REPAIR: duplicate route '/api/v1/capacity/heatmap' also in capacity_headroom_api.py:594 — review and remove one

@energy_discovery_bp.route('/api/v1/capacity/heatmap', methods=['GET'])
def capacity_heatmap():
    """Return capacity headroom data per market for the heatmap layer"""
    try:
        markets = []
        for key, m in MONITORED_MARKETS.items():
            # Calculate scores from seed data
            local_plants = [p for p in _POWER_PLANTS if p.get('market') == key]
            local_pipes = [p for p in _PIPELINES if p.get('market') == key]
            local_mw = sum(p.get('capacity_mw', 0) for p in local_plants)
            pipe_capacity = sum(p.get('capacity_mdth', 0) for p in local_pipes)

            # Readiness score based on available infrastructure
            power_score = min(30, local_mw / 200)
            gas_score = min(25, pipe_capacity / 400)
            base = 35 + power_score + gas_score
            grade = 'A' if base >= 80 else 'B' if base >= 60 else 'C' if base >= 40 else 'D'
            label = {'A': 'Excellent Capacity', 'B': 'Good Capacity', 'C': 'Moderate Capacity', 'D': 'Limited Capacity'}[grade]

            markets.append({
                'market': key,
                'name': m['name'],
                'readiness': {'score': round(base, 1), 'grade': grade, 'label': label},
                'grid': {
                    'spare_capacity_pct': round(40 + power_score * 1.2, 1),
                    'spare_capacity_mw': local_mw,
                    'signal': 'green' if local_mw > 2000 else 'yellow' if local_mw > 500 else 'red'
                },
                'gas': {
                    'pipeline_count': len(local_pipes),
                    'headroom_mdth': pipe_capacity,
                    'signal': 'green' if pipe_capacity > 1000 else 'yellow' if pipe_capacity > 200 else 'red'
                },
                'power': {'local_plants': len(local_plants), 'local_capacity_mw': local_mw},
                'fiber': {'route_count': max(1, m['tier'])},
                'cost': {'electricity_rate_cents_kwh': round(6 + m['tier'] * 1.5, 2)}
            })

        # r-gate-everywhere (2026-06-27): this is the ungated dup that shadows the
        # @require_plan('pro') twin (first-rule-wins). The per-market grid MW
        # headroom + readiness score is the PAID product. Gate in place (safer
        # than reshuffling route registration): for non-paid, null the numeric
        # MW/score and keep grade/label/signal (the free traffic-light teaser).
        # Fail-closed — an unresolved caller is treated as gated.
        _paid = False
        try:
            from routes.dcpi import _dcpi_is_paid
            _paid = _dcpi_is_paid()
        except Exception:
            _paid = False
        if not _paid:
            for _m in markets:
                _m.get('readiness', {}).update({'score': None})
                _m.get('grid', {}).update({'spare_capacity_pct': None, 'spare_capacity_mw': None})
                _m.get('gas', {}).update({'headroom_mdth': None})
                _m.get('power', {}).update({'local_capacity_mw': None})
                _m.get('cost', {}).update({'electricity_rate_cents_kwh': None})
                _m['locked'] = True
            resp = jsonify({'success': True, 'markets': markets, 'count': len(markets),
                            '_gated': True, '_required_tier': 'pro',
                            '_upgrade_cta': 'Grid capacity headroom (MW) + readiness scores are DC Hub Pro — https://dchub.cloud/pricing'})
            resp.headers['Cache-Control'] = 'private, no-store'
            return resp
        return jsonify({'success': True, 'markets': markets, 'count': len(markets)})
    except Exception as e:
        logger.error(f"Capacity heatmap error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


logger.info("⚡ Energy Discovery Routes loaded — %d plants, %d wind, %d pipelines, %d lines, %d markets",
            len(_POWER_PLANTS), len(_WIND_PROJECTS), len(_PIPELINES), len(_TRANSMISSION_LINES), len(MONITORED_MARKETS))
