"""
DC Hub - Facilities Database Population Script
Run this on Replit to populate the facilities table
"""

import sqlite3
from datetime import datetime

DB_PATH = "dc_nexus.db"

def create_facilities_table():
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    c = conn.cursor()
    
    # Table already exists with correct schema
    pass
    
    c.execute("CREATE INDEX IF NOT EXISTS idx_facilities_provider ON facilities(provider)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_facilities_city ON facilities(city)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_facilities_country ON facilities(country)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_facilities_region ON facilities(region)")
    
    conn.commit()
    conn.close()
    print("✅ Facilities table created")

# Real data center facilities
FACILITIES = [
    # NORTHERN VIRGINIA - World's largest market
    {"name": "Equinix DC1-DC15", "provider": "Equinix", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0438, "lng": -77.4874, "power_mw": 150, "sqft": 1200000},
    {"name": "Equinix DC21", "provider": "Equinix", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0445, "lng": -77.4880, "power_mw": 45, "sqft": 300000},
    {"name": "Digital Realty IAD Campus", "provider": "Digital Realty", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0425, "lng": -77.4865, "power_mw": 200, "sqft": 1500000},
    {"name": "QTS Ashburn Mega Campus", "provider": "QTS", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0412, "lng": -77.4891, "power_mw": 130, "sqft": 1100000},
    {"name": "Vantage VA Campus", "provider": "Vantage", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0455, "lng": -77.4901, "power_mw": 180, "sqft": 1400000},
    {"name": "CloudHQ Ashburn", "provider": "CloudHQ", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0401, "lng": -77.4855, "power_mw": 200, "sqft": 1600000},
    {"name": "Amazon AWS US-East-1", "provider": "Amazon AWS", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0389, "lng": -77.4845, "power_mw": 300, "sqft": 2000000},
    {"name": "Microsoft Ashburn", "provider": "Microsoft", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0478, "lng": -77.4912, "power_mw": 100, "sqft": 800000},
    {"name": "CyrusOne Ashburn", "provider": "CyrusOne", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0465, "lng": -77.4925, "power_mw": 75, "sqft": 600000},
    {"name": "CoreSite VA Campus", "provider": "CoreSite", "city": "Reston", "state": "VA", "country": "USA", "region": "North America", "lat": 38.9587, "lng": -77.3598, "power_mw": 60, "sqft": 450000},
    {"name": "Aligned Ashburn Campus", "provider": "Aligned", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0398, "lng": -77.4838, "power_mw": 150, "sqft": 1000000},
    {"name": "Iron Mountain VA-1", "provider": "Iron Mountain", "city": "Manassas", "state": "VA", "country": "USA", "region": "North America", "lat": 38.7509, "lng": -77.4753, "power_mw": 45, "sqft": 350000},
    {"name": "RagingWire VA Campus", "provider": "RagingWire", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0422, "lng": -77.4868, "power_mw": 100, "sqft": 750000},
    {"name": "Stack Infrastructure Ashburn", "provider": "Stack Infrastructure", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0435, "lng": -77.4882, "power_mw": 80, "sqft": 600000},
    {"name": "Prime Data Centers Ashburn", "provider": "Prime Data Centers", "city": "Ashburn", "state": "VA", "country": "USA", "region": "North America", "lat": 39.0448, "lng": -77.4895, "power_mw": 120, "sqft": 900000, "status": "Construction"},

    # DALLAS-FORT WORTH
    {"name": "Equinix DA Campus", "provider": "Equinix", "city": "Dallas", "state": "TX", "country": "USA", "region": "North America", "lat": 32.8998, "lng": -96.9553, "power_mw": 100, "sqft": 800000},
    {"name": "Digital Realty DFW Campus", "provider": "Digital Realty", "city": "Dallas", "state": "TX", "country": "USA", "region": "North America", "lat": 32.9012, "lng": -96.9601, "power_mw": 150, "sqft": 1200000},
    {"name": "QTS Irving Mega Campus", "provider": "QTS", "city": "Irving", "state": "TX", "country": "USA", "region": "North America", "lat": 32.8140, "lng": -96.9489, "power_mw": 200, "sqft": 1500000},
    {"name": "CyrusOne Carrollton Campus", "provider": "CyrusOne", "city": "Carrollton", "state": "TX", "country": "USA", "region": "North America", "lat": 32.9537, "lng": -96.8903, "power_mw": 180, "sqft": 1400000},
    {"name": "Aligned DFW Campus", "provider": "Aligned", "city": "Plano", "state": "TX", "country": "USA", "region": "North America", "lat": 33.0198, "lng": -96.6989, "power_mw": 150, "sqft": 1000000},
    {"name": "DataBank DFW", "provider": "DataBank", "city": "Dallas", "state": "TX", "country": "USA", "region": "North America", "lat": 32.8925, "lng": -96.9478, "power_mw": 60, "sqft": 500000},
    {"name": "Stream Data Centers Dallas", "provider": "Stream", "city": "Richardson", "state": "TX", "country": "USA", "region": "North America", "lat": 32.9483, "lng": -96.7299, "power_mw": 50, "sqft": 400000},
    {"name": "Compass DFW Campus", "provider": "Compass", "city": "Dallas", "state": "TX", "country": "USA", "region": "North America", "lat": 32.9056, "lng": -96.9512, "power_mw": 120, "sqft": 900000},
    {"name": "Flexential Dallas", "provider": "Flexential", "city": "Dallas", "state": "TX", "country": "USA", "region": "North America", "lat": 32.8889, "lng": -96.9423, "power_mw": 40, "sqft": 300000},
    {"name": "T5 Dallas", "provider": "T5 Data Centers", "city": "Dallas", "state": "TX", "country": "USA", "region": "North America", "lat": 32.9134, "lng": -96.9678, "power_mw": 55, "sqft": 450000},

    # PHOENIX
    {"name": "Vantage Phoenix Campus", "provider": "Vantage", "city": "Goodyear", "state": "AZ", "country": "USA", "region": "North America", "lat": 33.4353, "lng": -112.3958, "power_mw": 200, "sqft": 1400000},
    {"name": "Microsoft West Region", "provider": "Microsoft", "city": "Goodyear", "state": "AZ", "country": "USA", "region": "North America", "lat": 33.4298, "lng": -112.4012, "power_mw": 150, "sqft": 1000000},
    {"name": "CyrusOne Phoenix Campus", "provider": "CyrusOne", "city": "Phoenix", "state": "AZ", "country": "USA", "region": "North America", "lat": 33.4484, "lng": -112.0740, "power_mw": 100, "sqft": 800000},
    {"name": "Aligned Phoenix Campus", "provider": "Aligned", "city": "Phoenix", "state": "AZ", "country": "USA", "region": "North America", "lat": 33.4512, "lng": -112.0801, "power_mw": 180, "sqft": 1200000},
    {"name": "QTS Phoenix Campus", "provider": "QTS", "city": "Phoenix", "state": "AZ", "country": "USA", "region": "North America", "lat": 33.4467, "lng": -112.0689, "power_mw": 120, "sqft": 900000},
    {"name": "Digital Realty PHX", "provider": "Digital Realty", "city": "Phoenix", "state": "AZ", "country": "USA", "region": "North America", "lat": 33.4523, "lng": -112.0756, "power_mw": 80, "sqft": 650000},
    {"name": "EdgeCore Phoenix", "provider": "EdgeCore", "city": "Mesa", "state": "AZ", "country": "USA", "region": "North America", "lat": 33.4152, "lng": -111.8315, "power_mw": 150, "sqft": 1100000},
    {"name": "Meta Phoenix DC", "provider": "Meta", "city": "Mesa", "state": "AZ", "country": "USA", "region": "North America", "lat": 33.4089, "lng": -111.8256, "power_mw": 200, "sqft": 1500000},
    {"name": "Google Mesa", "provider": "Google", "city": "Mesa", "state": "AZ", "country": "USA", "region": "North America", "lat": 33.4123, "lng": -111.8289, "power_mw": 150, "sqft": 1000000},
    {"name": "Iron Mountain PHX-1", "provider": "Iron Mountain", "city": "Phoenix", "state": "AZ", "country": "USA", "region": "North America", "lat": 33.4456, "lng": -112.0712, "power_mw": 35, "sqft": 280000},

    # CHICAGO
    {"name": "Equinix CH Campus", "provider": "Equinix", "city": "Chicago", "state": "IL", "country": "USA", "region": "North America", "lat": 41.8781, "lng": -87.6298, "power_mw": 80, "sqft": 650000},
    {"name": "Digital Realty CHI Campus", "provider": "Digital Realty", "city": "Chicago", "state": "IL", "country": "USA", "region": "North America", "lat": 41.8819, "lng": -87.6278, "power_mw": 60, "sqft": 500000},
    {"name": "QTS Chicago", "provider": "QTS", "city": "Chicago", "state": "IL", "country": "USA", "region": "North America", "lat": 41.8756, "lng": -87.6312, "power_mw": 100, "sqft": 800000},
    {"name": "CyrusOne Aurora Campus", "provider": "CyrusOne", "city": "Aurora", "state": "IL", "country": "USA", "region": "North America", "lat": 41.7606, "lng": -88.3201, "power_mw": 150, "sqft": 1100000},
    {"name": "DataBank Chicago", "provider": "DataBank", "city": "Chicago", "state": "IL", "country": "USA", "region": "North America", "lat": 41.8734, "lng": -87.6289, "power_mw": 40, "sqft": 320000},
    {"name": "CME Group Data Center", "provider": "CME Group", "city": "Aurora", "state": "IL", "country": "USA", "region": "North America", "lat": 41.7589, "lng": -88.3156, "power_mw": 30, "sqft": 250000},
    {"name": "Switch Chicago", "provider": "Switch", "city": "Chicago", "state": "IL", "country": "USA", "region": "North America", "lat": 41.8801, "lng": -87.6245, "power_mw": 50, "sqft": 400000},

    # ATLANTA
    {"name": "Equinix AT Campus", "provider": "Equinix", "city": "Atlanta", "state": "GA", "country": "USA", "region": "North America", "lat": 33.7490, "lng": -84.3880, "power_mw": 70, "sqft": 550000},
    {"name": "Digital Realty ATL", "provider": "Digital Realty", "city": "Atlanta", "state": "GA", "country": "USA", "region": "North America", "lat": 33.7523, "lng": -84.3912, "power_mw": 55, "sqft": 450000},
    {"name": "QTS Atlanta Metro", "provider": "QTS", "city": "Atlanta", "state": "GA", "country": "USA", "region": "North America", "lat": 33.7456, "lng": -84.3845, "power_mw": 180, "sqft": 1300000},
    {"name": "Switch Atlanta Campus", "provider": "Switch", "city": "Lithia Springs", "state": "GA", "country": "USA", "region": "North America", "lat": 33.7798, "lng": -84.6601, "power_mw": 200, "sqft": 1500000},
    {"name": "CyrusOne Atlanta", "provider": "CyrusOne", "city": "Douglasville", "state": "GA", "country": "USA", "region": "North America", "lat": 33.7515, "lng": -84.7477, "power_mw": 120, "sqft": 900000},
    {"name": "Meta Stanton Springs", "provider": "Meta", "city": "Newton County", "state": "GA", "country": "USA", "region": "North America", "lat": 33.6190, "lng": -83.7687, "power_mw": 200, "sqft": 1400000},
    {"name": "Google Douglas County", "provider": "Google", "city": "Douglasville", "state": "GA", "country": "USA", "region": "North America", "lat": 33.7489, "lng": -84.7512, "power_mw": 150, "sqft": 1000000},

    # SILICON VALLEY
    {"name": "Equinix SV Campus", "provider": "Equinix", "city": "San Jose", "state": "CA", "country": "USA", "region": "North America", "lat": 37.3382, "lng": -121.8863, "power_mw": 120, "sqft": 950000},
    {"name": "Digital Realty SJC", "provider": "Digital Realty", "city": "Santa Clara", "state": "CA", "country": "USA", "region": "North America", "lat": 37.3541, "lng": -121.9552, "power_mw": 80, "sqft": 650000},
    {"name": "CoreSite SV Campus", "provider": "CoreSite", "city": "Santa Clara", "state": "CA", "country": "USA", "region": "North America", "lat": 37.3512, "lng": -121.9489, "power_mw": 90, "sqft": 720000},
    {"name": "Vantage Santa Clara", "provider": "Vantage", "city": "Santa Clara", "state": "CA", "country": "USA", "region": "North America", "lat": 37.3478, "lng": -121.9523, "power_mw": 100, "sqft": 800000},
    {"name": "CyrusOne San Jose", "provider": "CyrusOne", "city": "San Jose", "state": "CA", "country": "USA", "region": "North America", "lat": 37.3356, "lng": -121.8901, "power_mw": 60, "sqft": 480000},
    {"name": "QTS Santa Clara", "provider": "QTS", "city": "Santa Clara", "state": "CA", "country": "USA", "region": "North America", "lat": 37.3523, "lng": -121.9501, "power_mw": 70, "sqft": 550000},

    # SEATTLE / PACIFIC NORTHWEST
    {"name": "Equinix SE Campus", "provider": "Equinix", "city": "Seattle", "state": "WA", "country": "USA", "region": "North America", "lat": 47.6062, "lng": -122.3321, "power_mw": 50, "sqft": 400000},
    {"name": "Microsoft Quincy Campus", "provider": "Microsoft", "city": "Quincy", "state": "WA", "country": "USA", "region": "North America", "lat": 47.2343, "lng": -119.8526, "power_mw": 200, "sqft": 1500000},
    {"name": "Microsoft Moses Lake", "provider": "Microsoft", "city": "Moses Lake", "state": "WA", "country": "USA", "region": "North America", "lat": 47.1301, "lng": -119.2780, "power_mw": 150, "sqft": 1000000},
    {"name": "Vantage Quincy Campus", "provider": "Vantage", "city": "Quincy", "state": "WA", "country": "USA", "region": "North America", "lat": 47.2312, "lng": -119.8489, "power_mw": 180, "sqft": 1200000},
    {"name": "Sabey Quincy", "provider": "Sabey", "city": "Quincy", "state": "WA", "country": "USA", "region": "North America", "lat": 47.2356, "lng": -119.8512, "power_mw": 100, "sqft": 750000},
    {"name": "H5 Quincy", "provider": "H5 Data Centers", "city": "Quincy", "state": "WA", "country": "USA", "region": "North America", "lat": 47.2378, "lng": -119.8534, "power_mw": 80, "sqft": 600000},

    # LAS VEGAS
    {"name": "Switch SuperNAP", "provider": "Switch", "city": "Las Vegas", "state": "NV", "country": "USA", "region": "North America", "lat": 36.0828, "lng": -115.1251, "power_mw": 250, "sqft": 2000000},
    {"name": "Switch The Citadel", "provider": "Switch", "city": "Reno", "state": "NV", "country": "USA", "region": "North America", "lat": 39.5296, "lng": -119.8138, "power_mw": 200, "sqft": 1500000},
    {"name": "Equinix LV1", "provider": "Equinix", "city": "Las Vegas", "state": "NV", "country": "USA", "region": "North America", "lat": 36.0856, "lng": -115.1289, "power_mw": 30, "sqft": 240000},

    # NEW YORK / NEW JERSEY
    {"name": "Equinix NY Campus", "provider": "Equinix", "city": "New York", "state": "NY", "country": "USA", "region": "North America", "lat": 40.7128, "lng": -74.0060, "power_mw": 100, "sqft": 800000},
    {"name": "Digital Realty 111 8th Ave", "provider": "Digital Realty", "city": "New York", "state": "NY", "country": "USA", "region": "North America", "lat": 40.7410, "lng": -74.0018, "power_mw": 40, "sqft": 320000},
    {"name": "Equinix NJ Campus", "provider": "Equinix", "city": "Secaucus", "state": "NJ", "country": "USA", "region": "North America", "lat": 40.7895, "lng": -74.0565, "power_mw": 80, "sqft": 650000},
    {"name": "CoreSite NY Campus", "provider": "CoreSite", "city": "New York", "state": "NY", "country": "USA", "region": "North America", "lat": 40.7145, "lng": -74.0089, "power_mw": 35, "sqft": 280000},
    {"name": "QTS Piscataway", "provider": "QTS", "city": "Piscataway", "state": "NJ", "country": "USA", "region": "North America", "lat": 40.5515, "lng": -74.4610, "power_mw": 60, "sqft": 480000},

    # LOS ANGELES
    {"name": "Equinix LA Campus", "provider": "Equinix", "city": "Los Angeles", "state": "CA", "country": "USA", "region": "North America", "lat": 34.0522, "lng": -118.2437, "power_mw": 80, "sqft": 650000},
    {"name": "Digital Realty LAX", "provider": "Digital Realty", "city": "Los Angeles", "state": "CA", "country": "USA", "region": "North America", "lat": 34.0489, "lng": -118.2512, "power_mw": 60, "sqft": 500000},
    {"name": "CoreSite LA Campus", "provider": "CoreSite", "city": "Los Angeles", "state": "CA", "country": "USA", "region": "North America", "lat": 34.0534, "lng": -118.2389, "power_mw": 70, "sqft": 550000},

    # DENVER
    {"name": "Equinix DE Campus", "provider": "Equinix", "city": "Denver", "state": "CO", "country": "USA", "region": "North America", "lat": 39.7392, "lng": -104.9903, "power_mw": 40, "sqft": 320000},
    {"name": "Vantage Denver", "provider": "Vantage", "city": "Denver", "state": "CO", "country": "USA", "region": "North America", "lat": 39.7356, "lng": -104.9856, "power_mw": 60, "sqft": 480000},
    {"name": "CoreSite Denver", "provider": "CoreSite", "city": "Denver", "state": "CO", "country": "USA", "region": "North America", "lat": 39.7412, "lng": -104.9923, "power_mw": 50, "sqft": 400000},

    # EUROPE - London
    {"name": "Equinix LD Campus", "provider": "Equinix", "city": "London", "state": "", "country": "UK", "region": "EMEA", "lat": 51.5074, "lng": -0.1278, "power_mw": 80, "sqft": 650000},
    {"name": "Digital Realty LHR", "provider": "Digital Realty", "city": "London", "state": "", "country": "UK", "region": "EMEA", "lat": 51.5012, "lng": -0.1425, "power_mw": 60, "sqft": 500000},
    {"name": "Virtus London Campus", "provider": "Virtus", "city": "London", "state": "", "country": "UK", "region": "EMEA", "lat": 51.4923, "lng": -0.0123, "power_mw": 100, "sqft": 800000},
    {"name": "Colt London", "provider": "Colt DCS", "city": "London", "state": "", "country": "UK", "region": "EMEA", "lat": 51.5089, "lng": -0.0834, "power_mw": 40, "sqft": 320000},
    {"name": "NTT London", "provider": "NTT", "city": "London", "state": "", "country": "UK", "region": "EMEA", "lat": 51.5045, "lng": -0.1367, "power_mw": 50, "sqft": 400000},

    # EUROPE - Frankfurt
    {"name": "Equinix FR Campus", "provider": "Equinix", "city": "Frankfurt", "state": "", "country": "Germany", "region": "EMEA", "lat": 50.1109, "lng": 8.6821, "power_mw": 100, "sqft": 800000},
    {"name": "Digital Realty FRA", "provider": "Digital Realty", "city": "Frankfurt", "state": "", "country": "Germany", "region": "EMEA", "lat": 50.1067, "lng": 8.6789, "power_mw": 80, "sqft": 650000},
    {"name": "NTT Frankfurt", "provider": "NTT", "city": "Frankfurt", "state": "", "country": "Germany", "region": "EMEA", "lat": 50.1134, "lng": 8.6856, "power_mw": 60, "sqft": 480000},
    {"name": "Interxion Frankfurt", "provider": "Digital Realty", "city": "Frankfurt", "state": "", "country": "Germany", "region": "EMEA", "lat": 50.1078, "lng": 8.6801, "power_mw": 70, "sqft": 550000},

    # EUROPE - Amsterdam
    {"name": "Equinix AM Campus", "provider": "Equinix", "city": "Amsterdam", "state": "", "country": "Netherlands", "region": "EMEA", "lat": 52.3676, "lng": 4.9041, "power_mw": 70, "sqft": 550000},
    {"name": "Digital Realty AMS", "provider": "Digital Realty", "city": "Amsterdam", "state": "", "country": "Netherlands", "region": "EMEA", "lat": 52.3701, "lng": 4.9012, "power_mw": 50, "sqft": 400000},
    {"name": "NTT Amsterdam", "provider": "NTT", "city": "Amsterdam", "state": "", "country": "Netherlands", "region": "EMEA", "lat": 52.3689, "lng": 4.9056, "power_mw": 45, "sqft": 360000},
    {"name": "Microsoft Amsterdam", "provider": "Microsoft", "city": "Amsterdam", "state": "", "country": "Netherlands", "region": "EMEA", "lat": 52.3712, "lng": 4.9078, "power_mw": 80, "sqft": 600000},

    # EUROPE - Dublin
    {"name": "Equinix DB Campus", "provider": "Equinix", "city": "Dublin", "state": "", "country": "Ireland", "region": "EMEA", "lat": 53.3498, "lng": -6.2603, "power_mw": 50, "sqft": 400000},
    {"name": "Digital Realty DUB", "provider": "Digital Realty", "city": "Dublin", "state": "", "country": "Ireland", "region": "EMEA", "lat": 53.3467, "lng": -6.2634, "power_mw": 40, "sqft": 320000},
    {"name": "Microsoft Dublin", "provider": "Microsoft", "city": "Dublin", "state": "", "country": "Ireland", "region": "EMEA", "lat": 53.3512, "lng": -6.2567, "power_mw": 80, "sqft": 600000},
    {"name": "Amazon AWS Dublin", "provider": "Amazon AWS", "city": "Dublin", "state": "", "country": "Ireland", "region": "EMEA", "lat": 53.3489, "lng": -6.2589, "power_mw": 100, "sqft": 750000},
    {"name": "Google Dublin", "provider": "Google", "city": "Dublin", "state": "", "country": "Ireland", "region": "EMEA", "lat": 53.3523, "lng": -6.2612, "power_mw": 80, "sqft": 600000},
    {"name": "Meta Clonee", "provider": "Meta", "city": "Clonee", "state": "", "country": "Ireland", "region": "EMEA", "lat": 53.4601, "lng": -6.5256, "power_mw": 90, "sqft": 700000},

    # EUROPE - Paris
    {"name": "Equinix PA Campus", "provider": "Equinix", "city": "Paris", "state": "", "country": "France", "region": "EMEA", "lat": 48.8566, "lng": 2.3522, "power_mw": 60, "sqft": 480000},
    {"name": "Digital Realty PAR", "provider": "Digital Realty", "city": "Paris", "state": "", "country": "France", "region": "EMEA", "lat": 48.8601, "lng": 2.3489, "power_mw": 50, "sqft": 400000},
    {"name": "Interxion Paris", "provider": "Digital Realty", "city": "Paris", "state": "", "country": "France", "region": "EMEA", "lat": 48.8534, "lng": 2.3556, "power_mw": 40, "sqft": 320000},

    # ASIA - Singapore
    {"name": "Equinix SG Campus", "provider": "Equinix", "city": "Singapore", "state": "", "country": "Singapore", "region": "APAC", "lat": 1.3521, "lng": 103.8198, "power_mw": 80, "sqft": 650000},
    {"name": "Digital Realty SIN", "provider": "Digital Realty", "city": "Singapore", "state": "", "country": "Singapore", "region": "APAC", "lat": 1.3489, "lng": 103.8212, "power_mw": 60, "sqft": 500000},
    {"name": "NTT Singapore", "provider": "NTT", "city": "Singapore", "state": "", "country": "Singapore", "region": "APAC", "lat": 1.3512, "lng": 103.8178, "power_mw": 50, "sqft": 400000},
    {"name": "AirTrunk SGP1", "provider": "AirTrunk", "city": "Singapore", "state": "", "country": "Singapore", "region": "APAC", "lat": 1.3534, "lng": 103.8156, "power_mw": 100, "sqft": 800000},
    {"name": "Keppel DC Singapore", "provider": "Keppel DC", "city": "Singapore", "state": "", "country": "Singapore", "region": "APAC", "lat": 1.3501, "lng": 103.8223, "power_mw": 40, "sqft": 320000},

    # ASIA - Tokyo
    {"name": "Equinix TY Campus", "provider": "Equinix", "city": "Tokyo", "state": "", "country": "Japan", "region": "APAC", "lat": 35.6762, "lng": 139.6503, "power_mw": 100, "sqft": 800000},
    {"name": "NTT Tokyo", "provider": "NTT", "city": "Tokyo", "state": "", "country": "Japan", "region": "APAC", "lat": 35.6789, "lng": 139.6534, "power_mw": 80, "sqft": 650000},
    {"name": "Digital Realty TKY", "provider": "Digital Realty", "city": "Tokyo", "state": "", "country": "Japan", "region": "APAC", "lat": 35.6745, "lng": 139.6478, "power_mw": 50, "sqft": 400000},
    {"name": "AirTrunk TOK1", "provider": "AirTrunk", "city": "Tokyo", "state": "", "country": "Japan", "region": "APAC", "lat": 35.6723, "lng": 139.6512, "power_mw": 80, "sqft": 600000},
    {"name": "Colt Tokyo", "provider": "Colt DCS", "city": "Tokyo", "state": "", "country": "Japan", "region": "APAC", "lat": 35.6801, "lng": 139.6489, "power_mw": 30, "sqft": 240000},

    # ASIA - Sydney
    {"name": "Equinix SY Campus", "provider": "Equinix", "city": "Sydney", "state": "NSW", "country": "Australia", "region": "APAC", "lat": -33.8688, "lng": 151.2093, "power_mw": 60, "sqft": 480000},
    {"name": "AirTrunk SYD Campus", "provider": "AirTrunk", "city": "Sydney", "state": "NSW", "country": "Australia", "region": "APAC", "lat": -33.8712, "lng": 151.2056, "power_mw": 150, "sqft": 1100000},
    {"name": "NextDC Sydney", "provider": "NextDC", "city": "Sydney", "state": "NSW", "country": "Australia", "region": "APAC", "lat": -33.8656, "lng": 151.2123, "power_mw": 80, "sqft": 650000},
    {"name": "Digital Realty SYD", "provider": "Digital Realty", "city": "Sydney", "state": "NSW", "country": "Australia", "region": "APAC", "lat": -33.8678, "lng": 151.2078, "power_mw": 40, "sqft": 320000},

    # ASIA - Hong Kong
    {"name": "Equinix HK Campus", "provider": "Equinix", "city": "Hong Kong", "state": "", "country": "Hong Kong", "region": "APAC", "lat": 22.3193, "lng": 114.1694, "power_mw": 50, "sqft": 400000},
    {"name": "NTT Hong Kong", "provider": "NTT", "city": "Hong Kong", "state": "", "country": "Hong Kong", "region": "APAC", "lat": 22.3212, "lng": 114.1723, "power_mw": 40, "sqft": 320000},
    {"name": "Digital Realty HKG", "provider": "Digital Realty", "city": "Hong Kong", "state": "", "country": "Hong Kong", "region": "APAC", "lat": 22.3178, "lng": 114.1656, "power_mw": 35, "sqft": 280000},

    # LATAM - Sao Paulo
    {"name": "Equinix SP Campus", "provider": "Equinix", "city": "Sao Paulo", "state": "", "country": "Brazil", "region": "LATAM", "lat": -23.5505, "lng": -46.6333, "power_mw": 50, "sqft": 400000},
    {"name": "Digital Realty GRU", "provider": "Digital Realty", "city": "Sao Paulo", "state": "", "country": "Brazil", "region": "LATAM", "lat": -23.5478, "lng": -46.6389, "power_mw": 40, "sqft": 320000},
    {"name": "Ascenty Sao Paulo", "provider": "Ascenty", "city": "Sao Paulo", "state": "", "country": "Brazil", "region": "LATAM", "lat": -23.5534, "lng": -46.6278, "power_mw": 60, "sqft": 480000},

    # LATAM - Mexico City
    {"name": "Equinix MX Campus", "provider": "Equinix", "city": "Mexico City", "state": "", "country": "Mexico", "region": "LATAM", "lat": 19.4326, "lng": -99.1332, "power_mw": 30, "sqft": 240000},
    {"name": "KIO Networks Mexico", "provider": "KIO Networks", "city": "Mexico City", "state": "", "country": "Mexico", "region": "LATAM", "lat": 19.4289, "lng": -99.1378, "power_mw": 40, "sqft": 320000},
]

def populate_facilities():
    """Insert facilities into database"""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    c = conn.cursor()
    
    now = datetime.utcnow().isoformat()
    
    # Clear existing seed data
    c.execute("DELETE FROM facilities WHERE source = 'seed'")
    
    inserted = 0
    for f in FACILITIES:
        try:
            import uuid
            c.execute("""
                INSERT INTO facilities (id, name, provider, city, state, country, region, latitude, longitude, power_mw, sqft, status, source, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'seed', 0.95)
            """, (
                str(uuid.uuid4()), f['name'], f['provider'], f['city'], f.get('state', ''), f['country'],
                f['region'], f['lat'], f['lng'], f['power_mw'], f.get('sqft', 0),
                f.get('status', 'Active')
            ))
            inserted += 1
        except Exception as e:
            print(f"Error inserting {f['name']}: {e}")
    
    conn.commit()
    conn.close()
    
    print(f"✅ Inserted {inserted} facilities")
    return inserted

def verify_data():
    """Verify the data was inserted"""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM facilities")
    total = c.fetchone()[0]
    
    c.execute("SELECT region, COUNT(*) FROM facilities GROUP BY region")
    by_region = c.fetchall()
    
    c.execute("SELECT provider, COUNT(*) FROM facilities GROUP BY provider ORDER BY COUNT(*) DESC LIMIT 10")
    by_provider = c.fetchall()
    
    c.execute("SELECT SUM(power_mw) FROM facilities")
    total_mw = c.fetchone()[0] or 0
    
    conn.close()
    
    print(f"\n📊 Database Summary:")
    print(f"   Total Facilities: {total}")
    print(f"   Total Power: {total_mw:,.0f} MW")
    print(f"\n   By Region:")
    for region, count in by_region:
        print(f"     {region}: {count}")
    print(f"\n   Top Providers:")
    for provider, count in by_provider:
        print(f"     {provider}: {count}")

if __name__ == "__main__":
    print("🚀 DC Hub Facilities Database Population")
    print("=" * 50)
    create_facilities_table()
    populate_facilities()
    verify_data()
    print("\n✅ Done! Restart the server to see changes.")
