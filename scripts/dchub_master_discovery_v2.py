#!/usr/bin/env python3
"""
DC Hub Master Discovery Enhancement v2.0
=========================================
One script to rule them all. Grabs every data gap identified in the
March 24-25 2026 audit and loads directly into Neon PostgreSQL.

Data Sources & Targets:
  1. EIA-860 Operating Generator Capacity → eia_generators (NEW table)
  2. EIA Natural Gas Storage → eia_gas_storage (NEW table)  
  3. EIA Natural Gas Consumption → eia_gas_consumption (NEW table)
  4. HIFLD Gas Compressor Stations → hifld_gas_compressors (NEW table)
  5. HIFLD Gas Processing Plants → hifld_gas_processing_plants (NEW table)
  6. HIFLD Electric Service Territories → hifld_service_territories (NEW table)
  7. NASA POWER Climate Expansion → nasa_power_climate (UPDATE existing, add markets)
  8. PeeringDB IX Completion → peeringdb_ix (NEW table)
  9. HIFLD substations table audit → diagnose the 0-row mystery
  10. EIA RTO Hourly Grid Ops → eia_rto_hourly (NEW table - latest snapshot)

Run: python3 dchub_master_discovery_v2.py
Env: DATABASE_URL or NEON_DATABASE_URL must be set

Author: DC Hub Auto-Discovery Engine
Date: 2026-03-25
"""

import os
import sys
import json
import time
import traceback
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────

EIA_API_KEY = os.environ.get("EIA_API_KEY", "SuphqqIra7G46LHVDwb9CL5n4WYRwLu7ujeFXJMG")
DATABASE_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL", "")

# All 44 DC Hub markets for NASA POWER expansion
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

# HIFLD ArcGIS Feature Service URLs
HIFLD_URLS = {
    "gas_compressors": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0/query",
    "gas_processing": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Natural_Gas_Processing_Plants/FeatureServer/0/query",
    "service_territories": "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/Electric_Retail_Service_Territories/FeatureServer/0/query",
}

# ──────────────────────────────────────────────
# DB CONNECTION
# ──────────────────────────────────────────────

def get_db_connection():
    """Get a psycopg2 connection to Neon."""
    try:
        import psycopg2
    except ImportError:
        print("[!] Installing psycopg2-binary...")
        os.system("pip install psycopg2-binary --break-system-packages -q")
        import psycopg2
    
    db_url = DATABASE_URL
    if not db_url:
        print("[FATAL] No DATABASE_URL or NEON_DATABASE_URL set!")
        sys.exit(1)
    
    # Force SSL
    if "sslmode" not in db_url:
        db_url += ("&" if "?" in db_url else "?") + "sslmode=require"
    
    conn = psycopg2.connect(db_url)
    conn.autocommit = False
    return conn


def api_get(url, headers=None, timeout=30):
    """Simple HTTP GET with error handling."""
    req = Request(url, headers=headers or {"User-Agent": "DCHub-Discovery/2.0"})
    try:
        resp = urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        print(f"  [HTTP {e.code}] {url[:120]}")
        try:
            body = e.read().decode("utf-8")[:500]
            print(f"  Response: {body}")
        except:
            pass
        return None
    except (URLError, Exception) as e:
        print(f"  [ERROR] {e} — {url[:120]}")
        return None


# ──────────────────────────────────────────────
# 0. DIAGNOSTIC: Find where HIFLD substations live
# ──────────────────────────────────────────────

def diagnose_substations(conn):
    """Find where the 79,755 HIFLD substation records actually are."""
    print("\n" + "="*60)
    print("STEP 0: Diagnosing HIFLD Substations (79,755 missing from hifld_substations)")
    print("="*60)
    
    cur = conn.cursor()
    
    # Check all tables that might have substation data
    candidate_tables = [
        "hifld_substations", "substations", "infrastructure_layers",
        "hifld_data", "energy_substations", "power_substations",
        "discovered_substations", "hifld_electric_substations"
    ]
    
    for table in candidate_tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            print(f"  ✓ {table}: {count:,} rows")
            if count > 0:
                cur.execute(f"SELECT * FROM {table} LIMIT 1")
                cols = [desc[0] for desc in cur.description]
                print(f"    Columns: {', '.join(cols[:10])}{'...' if len(cols) > 10 else ''}")
        except Exception:
            conn.rollback()
            # Table doesn't exist
            pass
    
    # Also check for any table with 'sub' or 'hifld' in the name
    cur.execute("""
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public' 
        AND (table_name LIKE '%sub%' OR table_name LIKE '%hifld%' OR table_name LIKE '%infra%')
        ORDER BY table_name
    """)
    tables = cur.fetchall()
    print(f"\n  Tables matching sub/hifld/infra patterns:")
    for (t,) in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM \"{t}\"")
            count = cur.fetchone()[0]
            print(f"    {t}: {count:,} rows")
        except:
            conn.rollback()
    
    conn.commit()
    print("  [DONE] Substation diagnosis complete\n")


# ──────────────────────────────────────────────
# 1. EIA-860 Operating Generator Capacity
# ──────────────────────────────────────────────

def load_eia_generators(conn):
    """
    Load EIA-860 operable generator inventory.
    Every US power plant with coordinates, fuel type, capacity, status.
    """
    print("\n" + "="*60)
    print("STEP 1: EIA-860 Operating Generator Capacity")
    print("="*60)
    
    cur = conn.cursor()
    
    # Create table
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
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_eia_gen_state ON eia_generators(state);
        CREATE INDEX IF NOT EXISTS idx_eia_gen_coords ON eia_generators(latitude, longitude);
        CREATE INDEX IF NOT EXISTS idx_eia_gen_source ON eia_generators(energy_source);
    """)
    conn.commit()
    
    # EIA APIv2 - operating generator capacity
    base_url = "https://api.eia.gov/v2/electricity/operating-generator-capacity/data/"
    
    total_loaded = 0
    offset = 0
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
            print(f"  [WARN] No response at offset {offset}, stopping.")
            break
        
        records = data["response"].get("data", [])
        if not records:
            print(f"  No more records at offset {offset}.")
            break
        
        # Batch insert
        insert_count = 0
        for r in records:
            try:
                lat = r.get("latitude")
                lon = r.get("longitude")
                if lat is not None:
                    lat = float(lat)
                if lon is not None:
                    lon = float(lon)
                
                cap = r.get("nameplate-capacity-mw")
                if cap is not None:
                    cap = float(cap)
                net_cap = r.get("net-summer-capacity-mw")
                if net_cap is not None:
                    net_cap = float(net_cap)
                
                op_year = r.get("operating_year") or r.get("operatingyear")
                if op_year:
                    try:
                        op_year = int(op_year)
                    except:
                        op_year = None
                
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
                    cap,
                    net_cap,
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
                if insert_count == 0:
                    print(f"  [ERR] First record failed: {e}")
                    print(f"  Sample record keys: {list(r.keys())[:15]}")
                continue
        
        conn.commit()
        total_loaded += insert_count
        print(f"  Loaded {insert_count} generators (total: {total_loaded})")
        
        # Check if we got a full page
        total_available = data["response"].get("total", 0)
        offset += batch_size
        
        if offset >= total_available or len(records) < batch_size:
            break
        
        time.sleep(1)  # Rate limit respect
    
    print(f"  [DONE] EIA Generators: {total_loaded} total records loaded\n")
    return total_loaded


# ──────────────────────────────────────────────
# 2. EIA Natural Gas Storage
# ──────────────────────────────────────────────

def load_eia_gas_storage(conn):
    """Load EIA weekly natural gas storage data."""
    print("\n" + "="*60)
    print("STEP 2: EIA Natural Gas Storage (Weekly)")
    print("="*60)
    
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS eia_gas_storage (
            id SERIAL PRIMARY KEY,
            period TEXT,
            region TEXT,
            process TEXT,
            process_name TEXT,
            series TEXT,
            series_desc TEXT,
            value FLOAT,
            units TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gas_storage_region ON eia_gas_storage(region)")
    conn.commit()
    
    url = (
        f"https://api.eia.gov/v2/natural-gas/stor/wkly/data/"
        f"?api_key={EIA_API_KEY}"
        f"&frequency=weekly"
        f"&data[0]=value"
        f"&sort[0][column]=period&sort[0][direction]=desc"
        f"&length=500"
    )
    
    data = api_get(url, timeout=60)
    if not data or "response" not in data:
        print("  [WARN] No data from EIA gas storage API")
        return 0
    
    records = data["response"].get("data", [])
    loaded = 0
    
    for r in records:
        try:
            val = r.get("value")
            if val is not None:
                val = float(val)
            cur.execute("""
                INSERT INTO eia_gas_storage 
                (period, region, process, process_name, series, series_desc, value, units)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                r.get("period", ""),
                r.get("area-name") or r.get("areaName", ""),
                r.get("process") or r.get("process-name", ""),
                r.get("process-name") or r.get("processName", ""),
                r.get("series") or "",
                r.get("series-description") or r.get("seriesDescription", ""),
                val,
                r.get("value-units") or r.get("units", "Bcf")
            ))
            loaded += 1
        except Exception as e:
            conn.rollback()
            if loaded == 0:
                print(f"  [ERR] {e}")
                print(f"  Keys: {list(r.keys())}")
            continue
    
    conn.commit()
    print(f"  [DONE] EIA Gas Storage: {loaded} records loaded\n")
    return loaded


# ──────────────────────────────────────────────
# 3. EIA Natural Gas Consumption
# ──────────────────────────────────────────────

def load_eia_gas_consumption(conn):
    """Load EIA natural gas consumption by state."""
    print("\n" + "="*60)
    print("STEP 3: EIA Natural Gas Consumption")
    print("="*60)
    
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS eia_gas_consumption (
            id SERIAL PRIMARY KEY,
            period TEXT,
            state TEXT,
            state_name TEXT,
            sector TEXT,
            sector_name TEXT,
            value FLOAT,
            units TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gas_cons_state ON eia_gas_consumption(state)")
    conn.commit()
    
    url = (
        f"https://api.eia.gov/v2/natural-gas/cons/sum/data/"
        f"?api_key={EIA_API_KEY}"
        f"&frequency=monthly"
        f"&data[0]=value"
        f"&sort[0][column]=period&sort[0][direction]=desc"
        f"&length=2000"
    )
    
    data = api_get(url, timeout=60)
    if not data or "response" not in data:
        print("  [WARN] No data from EIA gas consumption API")
        return 0
    
    records = data["response"].get("data", [])
    loaded = 0
    
    for r in records:
        try:
            val = r.get("value")
            if val is not None:
                val = float(val)
            cur.execute("""
                INSERT INTO eia_gas_consumption
                (period, state, state_name, sector, sector_name, value, units)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (
                r.get("period", ""),
                r.get("stateid") or r.get("stateId", ""),
                r.get("stateDescription") or r.get("state-name", ""),
                r.get("process") or r.get("sectorid", ""),
                r.get("process-name") or r.get("sectorDescription", ""),
                val,
                r.get("value-units") or r.get("units", "MMcf")
            ))
            loaded += 1
        except Exception as e:
            conn.rollback()
            if loaded == 0:
                print(f"  [ERR] {e}")
                print(f"  Keys: {list(r.keys())}")
            continue
    
    conn.commit()
    print(f"  [DONE] EIA Gas Consumption: {loaded} records loaded\n")
    return loaded


# ──────────────────────────────────────────────
# 4. HIFLD Gas Compressor Stations
# ──────────────────────────────────────────────

def load_hifld_arcgis(conn, table_name, url_key, label):
    """Generic HIFLD ArcGIS Feature Service loader with pagination."""
    print(f"\n{'='*60}")
    print(f"STEP: {label}")
    print("="*60)
    
    cur = conn.cursor()
    base_url = HIFLD_URLS.get(url_key)
    if not base_url:
        print(f"  [SKIP] No URL for {url_key}")
        return 0
    
    # First, get field info
    info_url = base_url.replace("/query", "") + "?f=json"
    info = api_get(info_url, timeout=30)
    
    if not info:
        print(f"  [WARN] Cannot reach HIFLD endpoint: {label}")
        return 0
    
    fields = info.get("fields", [])
    field_names = [f["name"] for f in fields if f["name"].upper() != "OBJECTID"]
    print(f"  Fields: {', '.join(field_names[:10])}{'...' if len(field_names) > 10 else ''}")
    
    # Create table dynamically
    col_defs = ["id SERIAL PRIMARY KEY"]
    safe_cols = []
    for f in fields:
        name = f["name"].lower().replace(" ", "_")
        if name == "objectid":
            continue
        ftype = f.get("type", "esriFieldTypeString")
        if "Double" in ftype or "Single" in ftype:
            col_defs.append(f'"{name}" FLOAT')
        elif "Integer" in ftype or "Small" in ftype:
            col_defs.append(f'"{name}" BIGINT')
        else:
            col_defs.append(f'"{name}" TEXT')
        safe_cols.append((f["name"], name))
    
    col_defs.append("latitude FLOAT")
    col_defs.append("longitude FLOAT")
    col_defs.append("created_at TIMESTAMP DEFAULT NOW()")
    
    cur.execute(f'DROP TABLE IF EXISTS {table_name}')
    cur.execute(f'CREATE TABLE {table_name} ({", ".join(col_defs)})')
    conn.commit()
    
    # Paginated query
    total_loaded = 0
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
        
        query_url = base_url + "?" + urlencode(params)
        print(f"  Fetching offset={offset}...")
        
        data = api_get(query_url, timeout=60)
        if not data:
            print(f"  [WARN] No response at offset {offset}")
            break
        
        features = data.get("features", [])
        if not features:
            break
        
        for feat in features:
            attrs = feat.get("attributes", {})
            geom = feat.get("geometry", {})
            lat = geom.get("y")
            lon = geom.get("x")
            
            values = []
            placeholders = []
            col_names_sql = []
            
            for orig_name, safe_name in safe_cols:
                val = attrs.get(orig_name)
                col_names_sql.append(f'"{safe_name}"')
                placeholders.append("%s")
                values.append(val)
            
            col_names_sql.extend(["latitude", "longitude"])
            placeholders.extend(["%s", "%s"])
            values.extend([lat, lon])
            
            try:
                cur.execute(
                    f'INSERT INTO {table_name} ({", ".join(col_names_sql)}) VALUES ({", ".join(placeholders)})',
                    values
                )
                total_loaded += 1
            except Exception as e:
                conn.rollback()
                if total_loaded == 0:
                    print(f"  [ERR] {e}")
                continue
        
        conn.commit()
        print(f"  Loaded {len(features)} features (total: {total_loaded})")
        
        exceeded = data.get("exceededTransferLimit", False)
        offset += page_size
        
        if not exceeded and len(features) < page_size:
            break
        
        time.sleep(0.5)
    
    # Add spatial index
    try:
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table_name}_coords ON {table_name}(latitude, longitude)")
        conn.commit()
    except:
        conn.rollback()
    
    print(f"  [DONE] {label}: {total_loaded} records loaded\n")
    return total_loaded


# ──────────────────────────────────────────────
# 7. NASA POWER Climate Expansion
# ──────────────────────────────────────────────

def load_nasa_power_expansion(conn):
    """
    Expand NASA POWER climate data to all 44 DC Hub markets.
    Adds CDD, wet bulb temp, solar irradiance, wind speed.
    """
    print("\n" + "="*60)
    print("STEP 7: NASA POWER Climate Expansion (44 markets)")
    print("="*60)
    
    cur = conn.cursor()
    
    # Ensure table exists with expanded columns
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nasa_power_climate (
            id SERIAL PRIMARY KEY,
            market TEXT,
            state TEXT,
            latitude FLOAT,
            longitude FLOAT,
            avg_temp_c FLOAT,
            max_temp_c FLOAT,
            min_temp_c FLOAT,
            avg_humidity_pct FLOAT,
            avg_wind_speed_m_s FLOAT,
            avg_solar_irradiance_kwh_m2 FLOAT,
            cooling_degree_days FLOAT,
            heating_degree_days FLOAT,
            avg_wet_bulb_temp_c FLOAT,
            avg_precipitation_mm FLOAT,
            avg_surface_pressure_kpa FLOAT,
            data_source TEXT DEFAULT 'NASA POWER Climatology',
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(market)
        )
    """)
    conn.commit()
    
    # NASA POWER parameters for data center siting
    # T2M=temp, T2M_MAX, T2M_MIN, RH2M=humidity, WS10M=wind,
    # ALLSKY_SFC_SW_DWN=solar, T2MDEW=dew point (proxy wet bulb),
    # PRECTOTCORR=precipitation, PS=surface pressure, CDD18_3=CDD, HDD18_3=HDD
    params_list = "T2M,T2M_MAX,T2M_MIN,RH2M,WS10M,ALLSKY_SFC_SW_DWN,T2MDEW,PRECTOTCORR,PS"
    
    loaded = 0
    skipped = 0
    
    for m in DC_HUB_MARKETS:
        market = m["market"]
        lat = m["lat"]
        lon = m["lon"]
        state = m["state"]
        
        # Check if already exists with expanded data
        cur.execute("SELECT avg_wet_bulb_temp_c FROM nasa_power_climate WHERE market = %s", (market,))
        existing = cur.fetchone()
        if existing and existing[0] is not None:
            skipped += 1
            continue
        
        url = (
            f"https://power.larc.nasa.gov/api/temporal/climatology/point"
            f"?parameters={params_list}"
            f"&community=RE"
            f"&longitude={lon}&latitude={lat}"
            f"&format=JSON"
        )
        
        print(f"  Fetching: {market} ({lat}, {lon})...")
        data = api_get(url, timeout=45)
        
        if not data or "properties" not in data:
            print(f"  [WARN] No NASA data for {market}")
            continue
        
        props = data["properties"]["parameter"]
        
        # Extract annual averages
        avg_temp = props.get("T2M", {}).get("ANN")
        max_temp = props.get("T2M_MAX", {}).get("ANN")
        min_temp = props.get("T2M_MIN", {}).get("ANN")
        humidity = props.get("RH2M", {}).get("ANN")
        wind = props.get("WS10M", {}).get("ANN")
        solar = props.get("ALLSKY_SFC_SW_DWN", {}).get("ANN")
        dew_point = props.get("T2MDEW", {}).get("ANN")
        precip = props.get("PRECTOTCORR", {}).get("ANN")
        pressure = props.get("PS", {}).get("ANN")
        
        # Approximate CDD (sum of monthly (T-18.3) where T > 18.3)
        monthly_temps = props.get("T2M", {})
        cdd = 0
        hdd = 0
        for month_key in ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]:
            mt = monthly_temps.get(month_key)
            if mt is not None:
                days_in_month = 30  # approx
                if mt > 18.3:
                    cdd += (mt - 18.3) * days_in_month
                else:
                    hdd += (18.3 - mt) * days_in_month
        
        try:
            cur.execute("""
                INSERT INTO nasa_power_climate 
                (market, state, latitude, longitude, avg_temp_c, max_temp_c, min_temp_c,
                 avg_humidity_pct, avg_wind_speed_m_s, avg_solar_irradiance_kwh_m2,
                 cooling_degree_days, heating_degree_days, avg_wet_bulb_temp_c,
                 avg_precipitation_mm, avg_surface_pressure_kpa, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (market) DO UPDATE SET
                    avg_temp_c = EXCLUDED.avg_temp_c,
                    max_temp_c = EXCLUDED.max_temp_c,
                    min_temp_c = EXCLUDED.min_temp_c,
                    avg_humidity_pct = EXCLUDED.avg_humidity_pct,
                    avg_wind_speed_m_s = EXCLUDED.avg_wind_speed_m_s,
                    avg_solar_irradiance_kwh_m2 = EXCLUDED.avg_solar_irradiance_kwh_m2,
                    cooling_degree_days = EXCLUDED.cooling_degree_days,
                    heating_degree_days = EXCLUDED.heating_degree_days,
                    avg_wet_bulb_temp_c = EXCLUDED.avg_wet_bulb_temp_c,
                    avg_precipitation_mm = EXCLUDED.avg_precipitation_mm,
                    avg_surface_pressure_kpa = EXCLUDED.avg_surface_pressure_kpa,
                    updated_at = NOW()
            """, (
                market, state, lat, lon,
                avg_temp, max_temp, min_temp,
                humidity, wind, solar,
                round(cdd, 1) if cdd else None,
                round(hdd, 1) if hdd else None,
                dew_point,  # dew point as proxy for wet bulb
                precip, pressure
            ))
            conn.commit()
            loaded += 1
            print(f"    ✓ {market}: {avg_temp}°C avg, CDD={round(cdd,0)}, solar={solar} kWh/m²/day")
        except Exception as e:
            conn.rollback()
            print(f"    [ERR] {market}: {e}")
        
        time.sleep(1)  # NASA rate limit
    
    print(f"  [DONE] NASA POWER: {loaded} markets loaded, {skipped} already had data\n")
    return loaded


# ──────────────────────────────────────────────
# 8. PeeringDB IX Completion
# ──────────────────────────────────────────────

def load_peeringdb_ix(conn):
    """Load PeeringDB Internet Exchange points."""
    print("\n" + "="*60)
    print("STEP 8: PeeringDB Internet Exchanges")
    print("="*60)
    
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS peeringdb_ix (
            id SERIAL PRIMARY KEY,
            ix_id INT,
            name TEXT,
            name_long TEXT,
            city TEXT,
            country TEXT,
            region TEXT,
            media TEXT,
            proto_unicast BOOLEAN,
            proto_multicast BOOLEAN,
            proto_ipv6 BOOLEAN,
            net_count INT,
            fac_count INT,
            website TEXT,
            tech_email TEXT,
            policy_general TEXT,
            latitude FLOAT,
            longitude FLOAT,
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(ix_id)
        )
    """)
    conn.commit()
    
    # PeeringDB API - no auth needed for basic IX list
    url = "https://www.peeringdb.com/api/ix?depth=0&limit=0"
    headers = {
        "User-Agent": "DCHub-Discovery/2.0 (dchub.cloud)",
        "Accept": "application/json"
    }
    
    print("  Fetching PeeringDB IX list...")
    data = api_get(url, headers=headers, timeout=60)
    
    if not data or "data" not in data:
        print("  [WARN] PeeringDB rate limited or unavailable. Try again in 1 hour.")
        return 0
    
    records = data["data"]
    loaded = 0
    
    for r in records:
        try:
            cur.execute("""
                INSERT INTO peeringdb_ix
                (ix_id, name, name_long, city, country, region, media,
                 proto_unicast, proto_multicast, proto_ipv6,
                 net_count, fac_count, website, tech_email, policy_general,
                 latitude, longitude)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (ix_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    net_count = EXCLUDED.net_count,
                    fac_count = EXCLUDED.fac_count
            """, (
                r.get("id"),
                r.get("name", ""),
                r.get("name_long", ""),
                r.get("city", ""),
                r.get("country", ""),
                r.get("region_continent", ""),
                r.get("media", ""),
                r.get("proto_unicast", False),
                r.get("proto_multicast", False),
                r.get("proto_ipv6", False),
                r.get("net_count", 0),
                r.get("fac_count", 0),
                r.get("website", ""),
                r.get("tech_email", ""),
                r.get("policy_general", ""),
                r.get("latitude"),
                r.get("longitude")
            ))
            loaded += 1
        except Exception as e:
            conn.rollback()
            if loaded == 0:
                print(f"  [ERR] {e}")
                print(f"  Keys: {list(r.keys())}")
            continue
    
    conn.commit()
    print(f"  [DONE] PeeringDB IX: {loaded} exchanges loaded\n")
    return loaded


# ──────────────────────────────────────────────
# 10. EIA RTO Hourly Grid Operations (latest)
# ──────────────────────────────────────────────

def load_eia_rto_grid(conn):
    """Load latest EIA RTO hourly grid operations data."""
    print("\n" + "="*60)
    print("STEP 10: EIA RTO Hourly Grid Operations")
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
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rto_respondent ON eia_rto_hourly(respondent)")
    conn.commit()
    
    # Get latest fuel mix by BA
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
        print("  [WARN] No data from EIA RTO API")
        return 0
    
    records = data["response"].get("data", [])
    loaded = 0
    
    # Clear old data and replace with fresh
    cur.execute("TRUNCATE TABLE eia_rto_hourly")
    
    for r in records:
        try:
            val = r.get("value")
            if val is not None:
                val = float(val)
            cur.execute("""
                INSERT INTO eia_rto_hourly
                (period, respondent, respondent_name, fueltype, type_name, value, units)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (
                r.get("period", ""),
                r.get("respondent", ""),
                r.get("respondent-name") or r.get("respondentName", ""),
                r.get("fueltype") or r.get("fueltypeid", ""),
                r.get("type-name") or r.get("fueltypeDescription", ""),
                val,
                r.get("value-units") or "MWh"
            ))
            loaded += 1
        except Exception as e:
            conn.rollback()
            if loaded == 0:
                print(f"  [ERR] {e}")
                print(f"  Keys: {list(r.keys())}")
            continue
    
    conn.commit()
    print(f"  [DONE] EIA RTO Hourly: {loaded} records loaded\n")
    return loaded


# ──────────────────────────────────────────────
# MASTER RUNNER
# ──────────────────────────────────────────────

def main():
    start_time = time.time()
    
    print("╔" + "═"*58 + "╗")
    print("║  DC Hub Master Discovery Enhancement v2.0               ║")
    print("║  Loading ALL identified data gaps into Neon              ║")
    print("╚" + "═"*58 + "╝")
    print(f"\nStarted: {datetime.now().isoformat()}")
    print(f"Database: {'SET' if DATABASE_URL else 'MISSING!'}")
    print(f"EIA Key: {EIA_API_KEY[:8]}...")
    
    if not DATABASE_URL:
        print("\n[FATAL] Set NEON_DATABASE_URL or DATABASE_URL first!")
        sys.exit(1)
    
    conn = get_db_connection()
    results = {}
    
    # Step 0: Diagnose substations mystery
    try:
        diagnose_substations(conn)
    except Exception as e:
        print(f"  [ERR] Substation diagnosis failed: {e}")
    
    # Step 1: EIA Generators
    try:
        results["eia_generators"] = load_eia_generators(conn)
    except Exception as e:
        print(f"  [ERR] EIA Generators failed: {e}")
        traceback.print_exc()
        results["eia_generators"] = f"FAILED: {e}"
    
    # Step 2: EIA Gas Storage
    try:
        results["eia_gas_storage"] = load_eia_gas_storage(conn)
    except Exception as e:
        print(f"  [ERR] EIA Gas Storage failed: {e}")
        results["eia_gas_storage"] = f"FAILED: {e}"
    
    # Step 3: EIA Gas Consumption
    try:
        results["eia_gas_consumption"] = load_eia_gas_consumption(conn)
    except Exception as e:
        print(f"  [ERR] EIA Gas Consumption failed: {e}")
        results["eia_gas_consumption"] = f"FAILED: {e}"
    
    # Step 4: HIFLD Gas Compressor Stations
    try:
        results["hifld_gas_compressors"] = load_hifld_arcgis(
            conn, "hifld_gas_compressors", "gas_compressors",
            "HIFLD Gas Compressor Stations"
        )
    except Exception as e:
        print(f"  [ERR] HIFLD Compressors failed: {e}")
        results["hifld_gas_compressors"] = f"FAILED: {e}"
    
    # Step 5: HIFLD Gas Processing Plants
    try:
        results["hifld_gas_processing"] = load_hifld_arcgis(
            conn, "hifld_gas_processing_plants", "gas_processing",
            "HIFLD Gas Processing Plants"
        )
    except Exception as e:
        print(f"  [ERR] HIFLD Processing Plants failed: {e}")
        results["hifld_gas_processing"] = f"FAILED: {e}"
    
    # Step 6: HIFLD Electric Service Territories
    try:
        results["hifld_service_territories"] = load_hifld_arcgis(
            conn, "hifld_service_territories", "service_territories",
            "HIFLD Electric Retail Service Territories"
        )
    except Exception as e:
        print(f"  [ERR] HIFLD Service Territories failed: {e}")
        results["hifld_service_territories"] = f"FAILED: {e}"
    
    # Step 7: NASA POWER Expansion
    try:
        results["nasa_power_climate"] = load_nasa_power_expansion(conn)
    except Exception as e:
        print(f"  [ERR] NASA POWER failed: {e}")
        results["nasa_power_climate"] = f"FAILED: {e}"
    
    # Step 8: PeeringDB IX
    try:
        results["peeringdb_ix"] = load_peeringdb_ix(conn)
    except Exception as e:
        print(f"  [ERR] PeeringDB failed: {e}")
        results["peeringdb_ix"] = f"FAILED: {e}"
    
    # Step 10: EIA RTO Hourly
    try:
        results["eia_rto_hourly"] = load_eia_rto_grid(conn)
    except Exception as e:
        print(f"  [ERR] EIA RTO failed: {e}")
        results["eia_rto_hourly"] = f"FAILED: {e}"
    
    # ── FINAL REPORT ──
    elapsed = time.time() - start_time
    
    print("\n" + "╔" + "═"*58 + "╗")
    print("║  MASTER DISCOVERY RESULTS                                ║")
    print("╠" + "═"*58 + "╣")
    
    total_new = 0
    for source, count in results.items():
        if isinstance(count, int):
            total_new += count
            status = f"{count:>8,} records"
        else:
            status = f"  {count}"
        print(f"║  {source:<30} {status:>25} ║")
    
    print("╠" + "═"*58 + "╣")
    print(f"║  TOTAL NEW RECORDS: {total_new:>10,}                          ║")
    print(f"║  Elapsed: {elapsed:.1f}s                                      ║")
    print("╚" + "═"*58 + "╝")
    
    # Verify final totals
    print("\n── POST-LOAD TABLE VERIFICATION ──")
    cur = conn.cursor()
    for table in [
        "eia_generators", "eia_gas_storage", "eia_gas_consumption",
        "hifld_gas_compressors", "hifld_gas_processing_plants",
        "hifld_service_territories", "nasa_power_climate",
        "peeringdb_ix", "eia_rto_hourly"
    ]:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            print(f"  {table}: {count:,} rows")
        except:
            conn.rollback()
            print(f"  {table}: TABLE NOT FOUND")
    
    conn.close()
    print(f"\nCompleted: {datetime.now().isoformat()}")
    print("🚀 DC Hub Discovery Enhancement v2.0 complete!")


if __name__ == "__main__":
    main()
