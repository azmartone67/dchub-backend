#!/usr/bin/env python3
"""
DC Hub Discovery Patch v2.1
============================
Fixes 5 bugs from v2.0 first run and re-runs all failed steps.

Fixes:
  1. EIA Generators: int() cast on total_available
  2. HIFLD: Use correct open ArcGIS endpoints  
  3. NASA POWER: ALTER TABLE to add new columns before INSERT
  4. PeeringDB: Proper transaction rollback on errors
  5. EIA RTO: Same transaction fix

Run: python3 dchub_discovery_patch_v2_1.py
"""

import os
import sys
import json
import time
import traceback
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

EIA_API_KEY = os.environ.get("EIA_API_KEY", "SuphqqIra7G46LHVDwb9CL5n4WYRwLu7ujeFXJMG")
DATABASE_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")

# Correct HIFLD Open Data ArcGIS URLs
HIFLD_URLS = {
    "gas_compressors": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0/query",
    "gas_processing": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Processing_Plants/FeatureServer/0/query",
}

# All 44 markets
DC_HUB_MARKETS = [
    {"market": "Northern Virginia", "lat": 39.04, "lon": -77.49, "state": "VA"},
    {"market": "Dallas-Fort Worth", "lat": 32.78, "lon": -96.80, "state": "TX"},
    {"market": "Phoenix", "lat": 33.45, "lon": -112.07, "state": "AZ"},
    {"market": "Chicago", "lat": 41.88, "lon": -87.63, "state": "IL"},
    {"market": "Silicon Valley", "lat": 37.39, "lon": -122.08, "state": "CA"},
    {"market": "New York/New Jersey", "lat": 40.74, "lon": -74.00, "state": "NJ"},
    {"market": "Atlanta", "lat": 33.75, "lon": -84.39, "state": "GA"},
    {"market": "Portland", "lat": 45.52, "lon": -122.68, "state": "OR"},
    {"market": "Seattle", "lat": 47.61, "lon": -122.33, "state": "WA"},
    {"market": "Denver", "lat": 39.74, "lon": -104.99, "state": "CO"},
    {"market": "Los Angeles", "lat": 34.05, "lon": -118.24, "state": "CA"},
    {"market": "Houston", "lat": 29.76, "lon": -95.37, "state": "TX"},
    {"market": "Miami", "lat": 25.76, "lon": -80.19, "state": "FL"},
    {"market": "Minneapolis", "lat": 44.98, "lon": -93.27, "state": "MN"},
    {"market": "Salt Lake City", "lat": 40.76, "lon": -111.89, "state": "UT"},
    {"market": "Columbus", "lat": 39.96, "lon": -82.99, "state": "OH"},
    {"market": "San Antonio", "lat": 29.42, "lon": -98.49, "state": "TX"},
    {"market": "Kansas City", "lat": 39.10, "lon": -94.58, "state": "MO"},
    {"market": "Indianapolis", "lat": 39.77, "lon": -86.16, "state": "IN"},
    {"market": "Nashville", "lat": 36.16, "lon": -86.78, "state": "TN"},
    {"market": "Reno", "lat": 39.53, "lon": -119.81, "state": "NV"},
    {"market": "Las Vegas", "lat": 36.17, "lon": -115.14, "state": "NV"},
    {"market": "Sacramento", "lat": 38.58, "lon": -121.49, "state": "CA"},
    {"market": "Richmond", "lat": 37.54, "lon": -77.44, "state": "VA"},
    {"market": "Charlotte", "lat": 35.23, "lon": -80.84, "state": "NC"},
    {"market": "Hillsboro", "lat": 45.52, "lon": -122.99, "state": "OR"},
    {"market": "Quincy", "lat": 47.23, "lon": -119.85, "state": "WA"},
    {"market": "Des Moines", "lat": 41.59, "lon": -93.62, "state": "IA"},
    {"market": "Omaha", "lat": 41.26, "lon": -95.94, "state": "NE"},
    {"market": "Memphis", "lat": 35.15, "lon": -90.05, "state": "TN"},
    {"market": "Abilene", "lat": 32.45, "lon": -99.73, "state": "TX"},
    {"market": "West Memphis", "lat": 35.15, "lon": -90.18, "state": "AR"},
    {"market": "Mount Pleasant", "lat": 42.71, "lon": -87.88, "state": "WI"},
    {"market": "Cheyenne", "lat": 41.14, "lon": -104.82, "state": "WY"},
    {"market": "Louisa County", "lat": 38.02, "lon": -78.00, "state": "VA"},
    {"market": "Stafford", "lat": 38.42, "lon": -77.41, "state": "VA"},
    {"market": "New Albany", "lat": 40.08, "lon": -82.81, "state": "OH"},
    {"market": "Papillion", "lat": 41.15, "lon": -96.04, "state": "NE"},
    {"market": "Singapore", "lat": 1.35, "lon": 103.82, "state": ""},
    {"market": "Frankfurt", "lat": 50.11, "lon": 8.68, "state": ""},
    {"market": "London", "lat": 51.51, "lon": -0.13, "state": ""},
    {"market": "Amsterdam", "lat": 52.37, "lon": 4.90, "state": ""},
    {"market": "Tokyo", "lat": 35.68, "lon": 139.69, "state": ""},
    {"market": "Sydney", "lat": -33.87, "lon": 151.21, "state": ""},
]


def get_db():
    import psycopg2
    db_url = DATABASE_URL
    if "sslmode" not in db_url:
        db_url += ("&" if "?" in db_url else "?") + "sslmode=require"
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    return conn


def api_get(url, headers=None, timeout=30):
    req = Request(url, headers=headers or {"User-Agent": "DCHub-Discovery/2.1"})
    try:
        resp = urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        print(f"  [HTTP {e.code}] {url[:100]}")
        try:
            print(f"  Body: {e.read().decode()[:300]}")
        except:
            pass
        return None
    except Exception as e:
        print(f"  [ERROR] {e}")
        return None


# ──────────────────────────────────────────────
# FIX 1: EIA Generators (remaining pages after 5000)
# ──────────────────────────────────────────────

def fix_eia_generators(conn):
    print("\n" + "="*60)
    print("FIX 1: EIA-860 Generators (continue from offset 5000)")
    print("="*60)
    
    cur = conn.cursor()
    
    # Check current count
    try:
        cur.execute("SELECT COUNT(*) FROM eia_generators")
        existing = cur.fetchone()[0]
        print(f"  Existing records: {existing}")
    except:
        conn.rollback()
        # Table doesn't exist, create it
        cur.execute("""
            CREATE TABLE IF NOT EXISTS eia_generators (
                id SERIAL PRIMARY KEY,
                plant_id TEXT,
                plant_name TEXT,
                state TEXT,
                county TEXT,
                latitude FLOAT,
                longitude FLOAT,
                sector TEXT,
                nameplate_capacity_mw FLOAT,
                net_summer_capacity_mw FLOAT,
                energy_source TEXT,
                energy_source_desc TEXT,
                prime_mover TEXT,
                operating_status TEXT,
                operating_year INT,
                balancing_authority TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        existing = 0
        print("  Created eia_generators table")
    
    base_url = "https://api.eia.gov/v2/electricity/operating-generator-capacity/data/"
    total_loaded = existing
    offset = existing  # Resume from where we left off
    batch_size = 5000
    
    while True:
        params = {
            "api_key": EIA_API_KEY,
            "frequency": "monthly",
            "data[0]": "nameplate-capacity-mw",
            "data[1]": "net-summer-capacity-mw",
            "sort[0][column]": "plantid",
            "sort[0][direction]": "asc",
            "offset": offset,
            "length": batch_size
        }
        
        url = base_url + "?" + urlencode(params, doseq=True)
        print(f"  Fetching offset={offset}...")
        
        data = api_get(url, timeout=60)
        if not data or "response" not in data:
            print(f"  [WARN] No response at offset {offset}")
            break
        
        records = data["response"].get("data", [])
        if not records:
            print(f"  No more records.")
            break
        
        insert_count = 0
        for r in records:
            try:
                lat = r.get("latitude")
                lon = r.get("longitude")
                if lat is not None:
                    try: lat = float(lat)
                    except: lat = None
                if lon is not None:
                    try: lon = float(lon)
                    except: lon = None
                
                cap = r.get("nameplate-capacity-mw")
                if cap is not None:
                    try: cap = float(cap)
                    except: cap = None
                net_cap = r.get("net-summer-capacity-mw")
                if net_cap is not None:
                    try: net_cap = float(net_cap)
                    except: net_cap = None
                
                op_year = r.get("operating_year") or r.get("operatingyear")
                if op_year:
                    try: op_year = int(op_year)
                    except: op_year = None
                
                cur.execute("""
                    INSERT INTO eia_generators 
                    (plant_id, plant_name, state, county, latitude, longitude,
                     sector, nameplate_capacity_mw, net_summer_capacity_mw,
                     energy_source, energy_source_desc, prime_mover,
                     operating_status, operating_year, balancing_authority)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    str(r.get("plantid", "")),
                    r.get("plantName") or r.get("plant_name", ""),
                    r.get("stateid") or r.get("state", ""),
                    r.get("county", ""),
                    lat, lon,
                    r.get("sector") or r.get("sector_name", ""),
                    cap, net_cap,
                    r.get("energy_source_code") or r.get("energysourcecode", ""),
                    r.get("energy_source_desc") or r.get("energysourcedesc", ""),
                    r.get("prime_mover_code") or r.get("primemovercode", ""),
                    r.get("status") or r.get("statuscode", ""),
                    op_year,
                    r.get("balancing_authority_code") or r.get("balancingauthoritycode", "")
                ))
                insert_count += 1
            except Exception as e:
                conn.rollback()
                continue
        
        conn.commit()
        total_loaded += insert_count
        print(f"  Loaded {insert_count} (total: {total_loaded})")
        
        # FIX: Cast total to int
        total_available = int(data["response"].get("total", 0))
        offset += batch_size
        
        if offset >= total_available or len(records) < batch_size:
            break
        
        time.sleep(1)
    
    # Add indexes
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_eia_gen_state ON eia_generators(state)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_eia_gen_coords ON eia_generators(latitude, longitude)")
        conn.commit()
    except:
        conn.rollback()
    
    print(f"  [DONE] EIA Generators total: {total_loaded}\n")
    return total_loaded - existing


# ──────────────────────────────────────────────
# FIX 2: HIFLD - try alternative endpoints and diagnose
# ──────────────────────────────────────────────

def fix_hifld(conn):
    print("\n" + "="*60)
    print("FIX 2: HIFLD Gas Infrastructure (alternative endpoints)")
    print("="*60)
    
    cur = conn.cursor()
    
    # Try multiple URL patterns for HIFLD open data
    endpoints = [
        {
            "name": "Gas Compressor Stations",
            "table": "hifld_gas_compressors",
            "urls": [
                "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0/query",
                "https://hifld-geoplatform.opendata.arcgis.com/api/v3/datasets/natural-gas-compressor-stations/downloads/data?format=geojson",
                "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/ArcGIS/rest/services/Natural_Gas_Compressor_Stations_1/FeatureServer/0/query",
            ]
        },
        {
            "name": "Gas Processing Plants",
            "table": "hifld_gas_processing_plants", 
            "urls": [
                "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Processing_Plants/FeatureServer/0/query",
                "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/ArcGIS/rest/services/Natural_Gas_Processing_Plants_1/FeatureServer/0/query",
            ]
        },
    ]
    
    total = 0
    
    for ep in endpoints:
        print(f"\n  --- {ep['name']} ---")
        loaded = 0
        
        for url_base in ep["urls"]:
            print(f"  Trying: {url_base[:80]}...")
            
            # Check if it's a query endpoint or direct download
            if "/query" in url_base:
                params = {
                    "where": "1=1",
                    "outFields": "*",
                    "outSR": "4326",
                    "f": "json",
                    "resultRecordCount": 5,
                    "returnGeometry": "true",
                    "returnCountOnly": "true"
                }
                count_url = url_base + "?" + urlencode(params)
                data = api_get(count_url, timeout=30)
                
                if data and "count" in data:
                    feature_count = data["count"]
                    print(f"  Found {feature_count} features!")
                    
                    if feature_count > 0:
                        loaded = _load_arcgis_features(conn, cur, ep["table"], url_base, feature_count)
                        break
                elif data and "error" in data:
                    print(f"  ArcGIS error: {data['error'].get('message', 'unknown')}")
                else:
                    print(f"  No count returned, trying next URL...")
            else:
                # GeoJSON direct download
                data = api_get(url_base, timeout=60)
                if data and "features" in data:
                    print(f"  GeoJSON: {len(data['features'])} features")
                    loaded = _load_geojson_features(conn, cur, ep["table"], data["features"])
                    break
        
        if loaded == 0:
            print(f"  [WARN] All URLs failed for {ep['name']}. These may be restricted HIFLD datasets.")
            print(f"  Manual download may be needed from: https://hifld-geoplatform.opendata.arcgis.com/")
        
        total += loaded
    
    return total


def _load_arcgis_features(conn, cur, table_name, url_base, total_count):
    """Load features from ArcGIS with pagination."""
    
    # First get a sample to discover fields
    params = {
        "where": "1=1",
        "outFields": "*",
        "outSR": "4326",
        "f": "json",
        "resultRecordCount": 1,
        "returnGeometry": "true"
    }
    sample = api_get(url_base + "?" + urlencode(params), timeout=30)
    if not sample or not sample.get("features"):
        return 0
    
    # Get field names from first feature
    first_attrs = sample["features"][0].get("attributes", {})
    fields = list(first_attrs.keys())
    
    # Create table
    col_defs = ["id SERIAL PRIMARY KEY"]
    safe_map = []
    for f in fields:
        safe = f.lower().replace(" ", "_").replace(".", "_")
        if safe == "objectid" or safe == "fid":
            continue
        val = first_attrs[f]
        if isinstance(val, float):
            col_defs.append(f'"{safe}" FLOAT')
        elif isinstance(val, int):
            col_defs.append(f'"{safe}" BIGINT')
        else:
            col_defs.append(f'"{safe}" TEXT')
        safe_map.append((f, safe))
    
    col_defs.extend(["latitude FLOAT", "longitude FLOAT", "created_at TIMESTAMP DEFAULT NOW()"])
    
    cur.execute(f"DROP TABLE IF EXISTS {table_name}")
    cur.execute(f"CREATE TABLE {table_name} ({', '.join(col_defs)})")
    conn.commit()
    
    print(f"  Created table {table_name} with {len(safe_map)} columns")
    
    # Paginate
    loaded = 0
    offset = 0
    page_size = 2000
    
    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "outSR": "4326",
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "returnGeometry": "true"
        }
        
        data = api_get(url_base + "?" + urlencode(params), timeout=60)
        if not data or not data.get("features"):
            break
        
        features = data["features"]
        for feat in features:
            attrs = feat.get("attributes", {})
            geom = feat.get("geometry", {})
            lat = geom.get("y")
            lon = geom.get("x")
            
            cols = []
            phs = []
            vals = []
            for orig, safe in safe_map:
                cols.append(f'"{safe}"')
                phs.append("%s")
                vals.append(attrs.get(orig))
            
            cols.extend(["latitude", "longitude"])
            phs.extend(["%s", "%s"])
            vals.extend([lat, lon])
            
            try:
                cur.execute(f"INSERT INTO {table_name} ({','.join(cols)}) VALUES ({','.join(phs)})", vals)
                loaded += 1
            except:
                conn.rollback()
        
        conn.commit()
        print(f"  Loaded {len(features)} (total: {loaded})")
        
        if not data.get("exceededTransferLimit") and len(features) < page_size:
            break
        offset += page_size
        time.sleep(0.5)
    
    print(f"  [DONE] {table_name}: {loaded} records")
    return loaded


def _load_geojson_features(conn, cur, table_name, features):
    """Load GeoJSON features into table."""
    if not features:
        return 0
    
    # Discover fields from first feature
    first_props = features[0].get("properties", {})
    col_defs = ["id SERIAL PRIMARY KEY"]
    safe_map = []
    for k, v in first_props.items():
        safe = k.lower().replace(" ", "_").replace(".", "_")
        if isinstance(v, float):
            col_defs.append(f'"{safe}" FLOAT')
        elif isinstance(v, int):
            col_defs.append(f'"{safe}" BIGINT')
        else:
            col_defs.append(f'"{safe}" TEXT')
        safe_map.append((k, safe))
    
    col_defs.extend(["latitude FLOAT", "longitude FLOAT", "created_at TIMESTAMP DEFAULT NOW()"])
    
    cur.execute(f"DROP TABLE IF EXISTS {table_name}")
    cur.execute(f"CREATE TABLE {table_name} ({', '.join(col_defs)})")
    conn.commit()
    
    loaded = 0
    for feat in features:
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [None, None])
        lon = coords[0] if len(coords) > 0 else None
        lat = coords[1] if len(coords) > 1 else None
        
        cols = []
        phs = []
        vals = []
        for orig, safe in safe_map:
            cols.append(f'"{safe}"')
            phs.append("%s")
            vals.append(props.get(orig))
        
        cols.extend(["latitude", "longitude"])
        phs.extend(["%s", "%s"])
        vals.extend([lat, lon])
        
        try:
            cur.execute(f"INSERT INTO {table_name} ({','.join(cols)}) VALUES ({','.join(phs)})", vals)
            loaded += 1
        except:
            conn.rollback()
    
    conn.commit()
    print(f"  [DONE] {table_name}: {loaded} records")
    return loaded


# ──────────────────────────────────────────────
# FIX 3: NASA POWER - ALTER TABLE then expand
# ──────────────────────────────────────────────

def fix_nasa_power(conn):
    print("\n" + "="*60)
    print("FIX 3: NASA POWER Climate (ALTER TABLE + expand to 44 markets)")
    print("="*60)
    
    cur = conn.cursor()
    
    # Add missing columns to existing table
    new_cols = [
        ("avg_wet_bulb_temp_c", "FLOAT"),
        ("cooling_degree_days", "FLOAT"),
        ("heating_degree_days", "FLOAT"),
        ("avg_solar_irradiance_kwh_m2", "FLOAT"),
        ("avg_wind_speed_m_s", "FLOAT"),
        ("avg_precipitation_mm", "FLOAT"),
        ("avg_surface_pressure_kpa", "FLOAT"),
        ("max_temp_c", "FLOAT"),
        ("min_temp_c", "FLOAT"),
    ]
    
    for col_name, col_type in new_cols:
        try:
            cur.execute(f"ALTER TABLE nasa_power_climate ADD COLUMN IF NOT EXISTS {col_name} {col_type}")
            conn.commit()
        except:
            conn.rollback()
    
    print("  Added new columns to nasa_power_climate")
    
    # Ensure UNIQUE constraint exists
    try:
        cur.execute("ALTER TABLE nasa_power_climate ADD CONSTRAINT nasa_power_market_unique UNIQUE (market)")
        conn.commit()
    except:
        conn.rollback()  # Already exists
    
    params_list = "T2M,T2M_MAX,T2M_MIN,RH2M,WS10M,ALLSKY_SFC_SW_DWN,T2MDEW,PRECTOTCORR,PS"
    
    loaded = 0
    skipped = 0
    
    for m in DC_HUB_MARKETS:
        market = m["market"]
        lat = m["lat"]
        lon = m["lon"]
        state = m["state"]
        
        # Check if already has expanded data
        try:
            cur.execute("SELECT cooling_degree_days FROM nasa_power_climate WHERE market = %s", (market,))
            row = cur.fetchone()
            if row and row[0] is not None:
                skipped += 1
                continue
        except:
            conn.rollback()
        
        url = (
            f"https://power.larc.nasa.gov/api/temporal/climatology/point"
            f"?parameters={params_list}"
            f"&community=RE"
            f"&longitude={lon}&latitude={lat}"
            f"&format=JSON"
        )
        
        print(f"  {market} ({lat}, {lon})...")
        data = api_get(url, timeout=45)
        
        if not data or "properties" not in data:
            print(f"    [WARN] No data")
            continue
        
        props = data["properties"]["parameter"]
        
        avg_temp = props.get("T2M", {}).get("ANN")
        max_temp = props.get("T2M_MAX", {}).get("ANN")
        min_temp = props.get("T2M_MIN", {}).get("ANN")
        humidity = props.get("RH2M", {}).get("ANN")
        wind = props.get("WS10M", {}).get("ANN")
        solar = props.get("ALLSKY_SFC_SW_DWN", {}).get("ANN")
        dew_point = props.get("T2MDEW", {}).get("ANN")
        precip = props.get("PRECTOTCORR", {}).get("ANN")
        pressure = props.get("PS", {}).get("ANN")
        
        # Calculate CDD/HDD
        monthly_temps = props.get("T2M", {})
        cdd = 0
        hdd = 0
        for month_key in ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]:
            mt = monthly_temps.get(month_key)
            if mt is not None:
                if mt > 18.3:
                    cdd += (mt - 18.3) * 30
                else:
                    hdd += (18.3 - mt) * 30
        
        try:
            # Try UPSERT
            cur.execute("""
                INSERT INTO nasa_power_climate 
                (market, state, latitude, longitude, avg_temp_c, max_temp_c, min_temp_c,
                 avg_humidity_pct, avg_wind_speed_m_s, avg_solar_irradiance_kwh_m2,
                 cooling_degree_days, heating_degree_days, avg_wet_bulb_temp_c,
                 avg_precipitation_mm, avg_surface_pressure_kpa)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (market) DO UPDATE SET
                    max_temp_c = EXCLUDED.max_temp_c,
                    min_temp_c = EXCLUDED.min_temp_c,
                    avg_wind_speed_m_s = EXCLUDED.avg_wind_speed_m_s,
                    avg_solar_irradiance_kwh_m2 = EXCLUDED.avg_solar_irradiance_kwh_m2,
                    cooling_degree_days = EXCLUDED.cooling_degree_days,
                    heating_degree_days = EXCLUDED.heating_degree_days,
                    avg_wet_bulb_temp_c = EXCLUDED.avg_wet_bulb_temp_c,
                    avg_precipitation_mm = EXCLUDED.avg_precipitation_mm,
                    avg_surface_pressure_kpa = EXCLUDED.avg_surface_pressure_kpa
            """, (
                market, state, lat, lon,
                avg_temp, max_temp, min_temp,
                humidity, wind, solar,
                round(cdd, 1) if cdd else None,
                round(hdd, 1) if hdd else None,
                dew_point, precip, pressure
            ))
            conn.commit()
            loaded += 1
            print(f"    ✓ {avg_temp}°C, CDD={round(cdd)}, solar={solar} kWh/m²/day")
        except Exception as e:
            conn.rollback()
            print(f"    [ERR] {e}")
        
        time.sleep(1)  # NASA rate limit
    
    print(f"  [DONE] NASA POWER: {loaded} loaded, {skipped} skipped\n")
    return loaded


# ──────────────────────────────────────────────
# FIX 4: PeeringDB IX (with proper transaction handling)
# ──────────────────────────────────────────────

def fix_peeringdb(conn):
    print("\n" + "="*60)
    print("FIX 4: PeeringDB Internet Exchanges")
    print("="*60)
    
    cur = conn.cursor()
    
    # Check if already loaded
    try:
        cur.execute("SELECT COUNT(*) FROM peeringdb_ix")
        existing = cur.fetchone()[0]
        if existing > 0:
            print(f"  Already have {existing} IX records. Skipping.")
            return 0
    except:
        conn.rollback()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS peeringdb_ix (
            id SERIAL PRIMARY KEY,
            ix_id INT,
            name TEXT,
            name_long TEXT,
            city TEXT,
            country TEXT,
            region TEXT,
            net_count INT,
            fac_count INT,
            website TEXT,
            latitude FLOAT,
            longitude FLOAT,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(ix_id)
        )
    """)
    conn.commit()
    
    url = "https://www.peeringdb.com/api/ix?depth=0&limit=0"
    headers = {"User-Agent": "DCHub-Discovery/2.1 (dchub.cloud)", "Accept": "application/json"}
    
    print("  Fetching PeeringDB IX...")
    data = api_get(url, headers=headers, timeout=60)
    
    if not data or "data" not in data:
        print("  [WARN] Rate limited or unavailable")
        return 0
    
    records = data["data"]
    loaded = 0
    
    for r in records:
        try:
            cur.execute("""
                INSERT INTO peeringdb_ix (ix_id, name, name_long, city, country, region, net_count, fac_count, website, latitude, longitude)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (ix_id) DO NOTHING
            """, (
                r.get("id"), r.get("name",""), r.get("name_long",""),
                r.get("city",""), r.get("country",""), r.get("region_continent",""),
                r.get("net_count",0), r.get("fac_count",0), r.get("website",""),
                r.get("latitude"), r.get("longitude")
            ))
            loaded += 1
        except Exception as e:
            conn.rollback()
            if loaded == 0:
                print(f"  [ERR] {e}")
    
    conn.commit()
    print(f"  [DONE] PeeringDB: {loaded} exchanges\n")
    return loaded


# ──────────────────────────────────────────────
# FIX 5: EIA RTO Hourly
# ──────────────────────────────────────────────

def fix_eia_rto(conn):
    print("\n" + "="*60)
    print("FIX 5: EIA RTO Hourly Grid Operations")
    print("="*60)
    
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS eia_rto_hourly (
            id SERIAL PRIMARY KEY,
            period TEXT,
            respondent TEXT,
            respondent_name TEXT,
            fueltype TEXT,
            type_name TEXT,
            value FLOAT,
            units TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    conn.commit()
    
    url = (
        f"https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/"
        f"?api_key={EIA_API_KEY}"
        f"&frequency=hourly"
        f"&data[0]=value"
        f"&sort[0][column]=period&sort[0][direction]=desc"
        f"&length=2000"
    )
    
    data = api_get(url, timeout=60)
    if not data or "response" not in data:
        print("  [WARN] No data")
        return 0
    
    records = data["response"].get("data", [])
    
    cur.execute("DELETE FROM eia_rto_hourly")
    loaded = 0
    
    for r in records:
        try:
            val = r.get("value")
            if val is not None:
                try: val = float(val)
                except: val = None
            cur.execute("""
                INSERT INTO eia_rto_hourly (period, respondent, respondent_name, fueltype, type_name, value, units)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (
                r.get("period",""),
                r.get("respondent",""),
                r.get("respondent-name") or r.get("respondentName",""),
                r.get("fueltype") or r.get("fueltypeid",""),
                r.get("type-name") or r.get("fueltypeDescription",""),
                val,
                r.get("value-units") or "MWh"
            ))
            loaded += 1
        except Exception as e:
            conn.rollback()
            if loaded == 0:
                print(f"  [ERR] {e}")
    
    conn.commit()
    
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rto_respondent ON eia_rto_hourly(respondent)")
        conn.commit()
    except:
        conn.rollback()
    
    print(f"  [DONE] EIA RTO: {loaded} records\n")
    return loaded


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    start = time.time()
    
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  DC Hub Discovery Patch v2.1 — Fixing 5 bugs           ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print(f"\nStarted: {datetime.now().isoformat()}")
    
    conn = get_db()
    results = {}
    
    # Fix 1: EIA Generators
    try:
        results["eia_generators"] = fix_eia_generators(conn)
    except Exception as e:
        print(f"  [ERR] {e}")
        traceback.print_exc()
        results["eia_generators"] = f"FAILED: {e}"
    
    # Fix 2: HIFLD
    try:
        results["hifld_gas"] = fix_hifld(conn)
    except Exception as e:
        print(f"  [ERR] {e}")
        results["hifld_gas"] = f"FAILED: {e}"
    
    # Fix 3: NASA POWER
    try:
        results["nasa_power"] = fix_nasa_power(conn)
    except Exception as e:
        print(f"  [ERR] {e}")
        results["nasa_power"] = f"FAILED: {e}"
    
    # Fix 4: PeeringDB
    try:
        results["peeringdb_ix"] = fix_peeringdb(conn)
    except Exception as e:
        print(f"  [ERR] {e}")
        results["peeringdb_ix"] = f"FAILED: {e}"
    
    # Fix 5: EIA RTO
    try:
        results["eia_rto"] = fix_eia_rto(conn)
    except Exception as e:
        print(f"  [ERR] {e}")
        results["eia_rto"] = f"FAILED: {e}"
    
    elapsed = time.time() - start
    
    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  PATCH v2.1 RESULTS                                     ║")
    print("╠══════════════════════════════════════════════════════════╣")
    total = 0
    for k, v in results.items():
        if isinstance(v, int):
            total += v
            print(f"║  {k:<30} {v:>8,} new records  ║")
        else:
            print(f"║  {k:<30} {str(v)[:25]:>25}  ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print(f"║  TOTAL NEW: {total:>10,}  |  Elapsed: {elapsed:.0f}s              ║")
    print("╚══════════════════════════════════════════════════════════╝")
    
    # Verify
    print("\n── TABLE COUNTS ──")
    cur = conn.cursor()
    for t in ["eia_generators","eia_gas_storage","eia_gas_consumption",
              "hifld_gas_compressors","hifld_gas_processing_plants",
              "nasa_power_climate","peeringdb_ix","eia_rto_hourly"]:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            print(f"  {t}: {cur.fetchone()[0]:,}")
        except:
            conn.rollback()
            print(f"  {t}: NOT FOUND")
    
    conn.close()
    print(f"\n🚀 Patch v2.1 complete!")


if __name__ == "__main__":
    main()
