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

logger = logging.getLogger(__name__)

energy_discovery_bp = Blueprint('energy_discovery', __name__)

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
# ROUTES — /api/energy-discovery/*
# ============================================================================

@energy_discovery_bp.route('/api/energy-discovery/power-plants', methods=['GET'])
def energy_discovery_power_plants():
    """Return power plant data for Energy Discovery panel"""
    try:
        market = request.args.get('market', '')
        limit = min(int(request.args.get('limit', 2000)), 5000)
        plants = _filter_market(_POWER_PLANTS, market)[:limit]
        return jsonify({'success': True, 'data': plants, 'count': len(plants)})
    except Exception as e:
        logger.error(f"Energy discovery power-plants error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@energy_discovery_bp.route('/api/energy-discovery/transmission-lines', methods=['GET'])
def energy_discovery_transmission_lines():
    """Return transmission line data for Energy Discovery panel"""
    try:
        market = request.args.get('market', '')
        limit = min(int(request.args.get('limit', 2000)), 5000)
        lines = _filter_market(_TRANSMISSION_LINES, market)[:limit]
        return jsonify({'success': True, 'data': lines, 'count': len(lines)})
    except Exception as e:
        logger.error(f"Energy discovery transmission-lines error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@energy_discovery_bp.route('/api/energy-discovery/wind-projects', methods=['GET'])
def energy_discovery_wind_projects():
    """Return wind project data for Energy Discovery panel"""
    try:
        market = request.args.get('market', '')
        limit = min(int(request.args.get('limit', 2000)), 5000)
        projects = _filter_market(_WIND_PROJECTS, market)[:limit]
        return jsonify({'success': True, 'data': projects, 'count': len(projects)})
    except Exception as e:
        logger.error(f"Energy discovery wind-projects error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@energy_discovery_bp.route('/api/energy-discovery/pipelines', methods=['GET'])
def energy_discovery_pipelines():
    """Return pipeline data for Energy Discovery panel"""
    try:
        market = request.args.get('market', '')
        limit = min(int(request.args.get('limit', 500)), 1000)
        pipes = _filter_market(_PIPELINES, market)[:limit]
        return jsonify({'success': True, 'data': pipes, 'count': len(pipes)})
    except Exception as e:
        logger.error(f"Energy discovery pipelines error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@energy_discovery_bp.route('/api/energy-discovery/status', methods=['GET'])
def energy_discovery_status():
    """phase20_status_truth: query real DB tables instead of seed/cached state.

    Replaces the in-memory state with live row counts + last-updated
    timestamps. Used by the dashboard, watchdog, and Land-Power map UI
    as the freshness/health signal.
    """
    out = {
        'success': True,
        'data': {
            'markets_monitored': 23,
            'hifld_sources': 5,
            'running': True,
            'recent_syncs': [],
        },
    }
    try:
        from db_utils import try_get_db
        conn = try_get_db()
        if conn:
            cur = conn.cursor()

            def _count_max(table, ts_col='updated_at'):
                try:
                    cur.execute(f"SELECT COUNT(*), MAX({ts_col}) FROM {table}")
                    r = cur.fetchone() or (0, None)
                    return int(r[0] or 0), str(r[1]) if r[1] else None
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
                    return 0, None

            for label, table, ts in [
                ('total_substations',      'substations',     'updated_at'),
                ('total_pipelines',        'pipelines',       'updated_at'),
                ('total_power_plants',     'power_plants',    'updated_at'),
                ('total_transmissions',    'transmission',    'updated_at'),
                ('total_wind_projects',    'wind_projects',   'updated_at'),
                ('total_gas_compressors',  'gas_compressors', 'updated_at'),
                ('total_gas_processings',  'gas_processings', 'updated_at'),
                ('total_fiber_routes',     'fiber_routes',    'updated_at'),
            ]:
                n, latest = _count_max(table, ts)
                out['data'][label] = n
                if latest:
                    out['data'][label.replace('total_', 'latest_')] = latest

            # total capacity (substations carry voltage_kv, sum power plant capacity)
            try:
                cur.execute("SELECT COALESCE(SUM(capacity_mw),0) FROM power_plants")
                cap_row = cur.fetchone() or (0,)
                out['data']['total_capacity_mw'] = int(cap_row[0] or 0)
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # recent_syncs from any source we can find
            try:
                cur.execute(
                    "SELECT 'substations' AS source, MAX(updated_at) AS at FROM substations "
                    "UNION ALL SELECT 'fiber_routes', MAX(updated_at) FROM fiber_routes "
                    "UNION ALL SELECT 'power_plants', MAX(updated_at) FROM power_plants"
                )
                out['data']['recent_syncs'] = [
                    {'source': r[0], 'at': str(r[1]) if r[1] else None}
                    for r in cur.fetchall()
                ]
            except Exception:
                try: conn.rollback()
                except Exception: pass

            # seed_data flag: false if substations > 1000 (real data ingested)
            out['data']['seed_data'] = (
                int(out['data'].get('total_substations', 0)) < 1000
            )

            try: conn.close()
            except Exception: pass
    except Exception as _e:
        out['data']['_error'] = type(_e).__name__ + ': ' + str(_e)[:200]

    return jsonify(out)

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

        return jsonify({'success': True, 'markets': markets, 'count': len(markets)})
    except Exception as e:
        logger.error(f"Capacity heatmap error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


logger.info("⚡ Energy Discovery Routes loaded — %d plants, %d wind, %d pipelines, %d lines, %d markets",
            len(_POWER_PLANTS), len(_WIND_PROJECTS), len(_PIPELINES), len(_TRANSMISSION_LINES), len(MONITORED_MARKETS))
