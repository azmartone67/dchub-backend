#!/usr/bin/env python3
"""
DC Hub — EIA Pricing Discovery
Fetches electricity retail rates, natural gas prices, and gas storage data
from the EIA Open Data API v2.

Requires:
  - DATABASE_URL or NEON_DATABASE_URL env var
  - EIA_API_KEY env var (get free key at https://www.eia.gov/opendata/register.php)

Tables populated:
  - eia_electricity_rates
  - eia_natural_gas_prices
  - eia_gas_storage_weekly
"""

import os
import sys
import json
import time
import requests
import psycopg2
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
EIA_API_KEY = os.environ.get("EIA_API_KEY", "SuphqqIra7G46LHVDwb9CL5n4WYRwLu7ujeFXJMG")
EIA_BASE = "https://api.eia.gov/v2"

# All 50 US states + DC
US_STATES = [
    "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH",
    "NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT",
    "VT","VA","WA","WV","WI","WY"
]

SECTORS = ["COM", "IND", "RES", "ALL"]

GAS_STORAGE_REGIONS = [
    "Lower 48", "East", "Midwest", "Mountain", "Pacific", "South Central",
    "Salt", "Nonsalt"
]

# ── Helpers ─────────────────────────────────────────────────────────────────

def get_conn():
    if not DATABASE_URL:
        print("ERROR: No DATABASE_URL or NEON_DATABASE_URL set")
        sys.exit(1)
    return psycopg2.connect(DATABASE_URL)

def eia_get(endpoint, params=None):
    """Make a request to the EIA API v2 with retry logic."""
    url = f"{EIA_BASE}/{endpoint}"
    base_params = {"api_key": EIA_API_KEY}
    if params:
        base_params.update(params)
    
    for attempt in range(3):
        try:
            resp = requests.get(url, params=base_params, timeout=30)
            if resp.status_code == 429:
                wait = 2 ** (attempt + 1)
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            print(f"  Timeout on attempt {attempt + 1}")
            time.sleep(2)
        except Exception as e:
            print(f"  Error: {e}")
            if attempt == 2:
                return None
            time.sleep(2)
    return None

# ── Electricity Retail Rates ────────────────────────────────────────────────

def fetch_electricity_rates(conn):
    """
    Fetch monthly retail electricity prices by state and sector from EIA.
    Gets the last 12 months for all states and sectors.
    """
    print("\n" + "=" * 60)
    print("ELECTRICITY RETAIL RATES")
    print("=" * 60)
    
    cur = conn.cursor()
    total_inserted = 0
    total_updated = 0
    errors = 0
    
    # Fetch in batches by sector to reduce API calls
    for sector in SECTORS:
        print(f"\n  Sector: {sector}")
        
        data = eia_get("electricity/retail-sales/data", {
            "frequency": "monthly",
            "data[0]": "price",
            "facets[sectorid][]": sector,
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "length": 500,  # Smaller batches to avoid EIA 504 timeouts
            "offset": 0
        })
        
        if not data or "response" not in data:
            print(f"  ✗ No data for sector {sector}")
            errors += 1
            continue
        
        records = data.get("response", {}).get("data", [])
        print(f"  → {len(records)} records from API")
        
        batch = []
        for rec in records:
            state = rec.get("stateid", "").strip()
            price = rec.get("price")
            period = rec.get("period", "").strip()
            
            if not state or not period or price is None:
                continue
            
            # EIA sometimes returns 'W' (withheld) or other strings
            try:
                price = float(price)
            except (TypeError, ValueError):
                continue
            
            # Only keep last 12 months per state
            batch.append((state, sector, price, period))
        
        if batch:
            # Upsert
            for state, sec, price, period in batch:
                try:
                    cur.execute("""
                        INSERT INTO eia_electricity_rates (state, sector, price_cents_kwh, period, retrieved_at)
                        VALUES (%s, %s, %s, %s, NOW())
                        ON CONFLICT (state, sector, period) DO UPDATE
                        SET price_cents_kwh = EXCLUDED.price_cents_kwh,
                            retrieved_at = NOW()
                    """, (state, sec, price, period))
                    total_inserted += 1
                except Exception as e:
                    errors += 1
                    conn.rollback()
                    cur = conn.cursor()
            
            conn.commit()
            print(f"  ✓ Upserted {len(batch)} records for {sector}")
        
        time.sleep(0.5)  # Rate limit courtesy
    
    # Summary
    cur.execute("SELECT COUNT(*) FROM eia_electricity_rates")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT state) FROM eia_electricity_rates")
    states = cur.fetchone()[0]
    cur.execute("SELECT MIN(period), MAX(period) FROM eia_electricity_rates")
    min_p, max_p = cur.fetchone()
    
    print(f"\n  TOTAL: {total} records, {states} states, {min_p} to {max_p}")
    print(f"  Inserted/updated: {total_inserted}, Errors: {errors}")
    
    return total_inserted

# ── Natural Gas Prices ──────────────────────────────────────────────────────

def fetch_natural_gas_prices(conn):
    """
    Fetch natural gas prices by state from EIA.
    Covers citygate, industrial, commercial, and electric power sectors.
    """
    print("\n" + "=" * 60)
    print("NATURAL GAS PRICES")
    print("=" * 60)
    
    cur = conn.cursor()
    total_inserted = 0
    errors = 0
    
    gas_sectors = {
        "PCS": "citygate",
        "PRS": "residential",
        "PIN": "industrial", 
        "PCO": "commercial",
        "PEU": "electric_power"
    }
    
    for eia_code, sector_name in gas_sectors.items():
        print(f"\n  Sector: {sector_name} ({eia_code})")
        
        data = eia_get("natural-gas/pri/sum/data", {
            "frequency": "monthly",
            "data[0]": "value",
            "facets[process][]": eia_code,
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "length": 2000,
            "offset": 0
        })
        
        if not data or "response" not in data:
            print(f"  ✗ No data for {sector_name}")
            errors += 1
            continue
        
        records = data.get("response", {}).get("data", [])
        print(f"  → {len(records)} records from API")
        
        for rec in records:
            # EIA natural gas uses 'area-name' for state
            state = rec.get("area-name", "").strip()
            series_id = rec.get("series", "")
            value = rec.get("value")
            period = rec.get("period", "").strip()
            
            if not state or not period or value is None:
                continue
            
            # Map full state names to abbreviations
            state_abbr = _state_abbr(state)
            if not state_abbr:
                continue
            
            # EIA sometimes returns 'W' (withheld) or other strings
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            
            try:
                cur.execute("""
                    INSERT INTO eia_natural_gas_prices 
                    (state, series_id, price_dollars_mcf, period, sector, retrieved_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (state, sector, period) DO UPDATE
                    SET price_dollars_mcf = EXCLUDED.price_dollars_mcf,
                        series_id = EXCLUDED.series_id,
                        retrieved_at = NOW()
                """, (state_abbr, series_id, value, period, sector_name))
                total_inserted += 1
            except Exception as e:
                errors += 1
                conn.rollback()
                cur = conn.cursor()
        
        conn.commit()
        time.sleep(0.5)
    
    cur.execute("SELECT COUNT(*) FROM eia_natural_gas_prices")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT state) FROM eia_natural_gas_prices")
    states = cur.fetchone()[0]
    
    print(f"\n  TOTAL: {total} records, {states} states")
    print(f"  Inserted/updated: {total_inserted}, Errors: {errors}")
    
    return total_inserted

# ── Gas Storage Weekly ──────────────────────────────────────────────────────

def fetch_gas_storage(conn):
    """
    Fetch weekly natural gas storage data by region from EIA.
    """
    print("\n" + "=" * 60)
    print("GAS STORAGE (WEEKLY)")
    print("=" * 60)
    
    cur = conn.cursor()
    total_inserted = 0
    errors = 0
    
    data = eia_get("natural-gas/stor/wkly/data", {
        "frequency": "weekly",
        "data[0]": "value",
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "length": 500
    })
    
    if not data or "response" not in data:
        print("  ✗ No storage data available")
        return 0
    
    records = data.get("response", {}).get("data", [])
    print(f"  → {len(records)} records from API")
    
    prev_values = {}
    
    for rec in records:
        region = rec.get("area-name", "").strip()
        value = rec.get("value")
        period = rec.get("period", "").strip()
        
        if not region or not period or value is None:
            continue
        
        # EIA sometimes returns 'W' (withheld) or other strings
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        
        # Calculate net change
        key = region
        net_change = None
        if key in prev_values:
            net_change = value - prev_values[key]
        prev_values[key] = value
        
        try:
            cur.execute("""
                INSERT INTO eia_gas_storage_weekly 
                (region, working_gas_bcf, net_change_bcf, period, retrieved_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (region, period) DO UPDATE
                SET working_gas_bcf = EXCLUDED.working_gas_bcf,
                    net_change_bcf = EXCLUDED.net_change_bcf,
                    retrieved_at = NOW()
            """, (region, value, net_change, period))
            total_inserted += 1
        except Exception as e:
            errors += 1
            conn.rollback()
            cur = conn.cursor()
    
    conn.commit()
    
    cur.execute("SELECT COUNT(*) FROM eia_gas_storage_weekly")
    total = cur.fetchone()[0]
    
    print(f"\n  TOTAL: {total} records")
    print(f"  Inserted/updated: {total_inserted}, Errors: {errors}")
    
    return total_inserted

# ── State Abbreviation Helper ───────────────────────────────────────────────

STATE_MAP = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI",
    "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA",
    "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME",
    "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM",
    "New York": "NY", "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI",
    "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "U.S.": "US"
}

def _state_abbr(name):
    """Convert full state name to abbreviation."""
    if len(name) == 2 and name.upper() in US_STATES:
        return name.upper()
    return STATE_MAP.get(name)

# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("DC Hub — EIA Pricing Discovery")
    print(f"Time: {datetime.now().isoformat()}")
    if EIA_API_KEY:
        print("Credential: configured")
    else:
        print("Credential: MISSING")
    print(f"Database: {DATABASE_URL[:40]}..." if DATABASE_URL else "NO DATABASE")
    print("=" * 60)
    
    if not DATABASE_URL:
        print("ERROR: Set DATABASE_URL or NEON_DATABASE_URL")
        sys.exit(1)
    
    if not EIA_API_KEY:
        print("ERROR: Set EIA_API_KEY")
        sys.exit(1)
    
    conn = get_conn()
    
    try:
        elec = fetch_electricity_rates(conn)
        gas = fetch_natural_gas_prices(conn)
        storage = fetch_gas_storage(conn)
        
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"  Electricity rates: {elec} records")
        print(f"  Natural gas prices: {gas} records")
        print(f"  Gas storage: {storage} records")
        print(f"  Total: {elec + gas + storage} records")
        print("=" * 60)
        
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
