#!/usr/bin/env python3
"""
DC Hub — Midstream Gas Infrastructure Discovery
Fetches compressor stations, gas processing plants, and LNG terminals
from HIFLD ArcGIS REST services.

Requires:
  - DATABASE_URL or NEON_DATABASE_URL env var

Tables populated:
  - gas_compressor_stations
  - gas_processing_plants
  - lng_terminals
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

# HIFLD ArcGIS Feature Service endpoints
HIFLD_SERVICES = {
    "compressor_stations": {
        "url": "https://services1.arcgis.com/Hp6G80Pky0om6HgQ/arcgis/rest/services/Natural_Gas_Compressor_Stations/FeatureServer/0/query",
        "table": "gas_compressor_stations",
        "field_map": {
            "NAME": "name",
            "OPERATOR": "operator",
            "LATITUDE": "latitude",
            "LONGITUDE": "longitude",
            "STATE": "state",
            "COUNTY": "county",
            "HORSEPOWER": "capacity_hp",
            "PIPELINE": "pipeline_name",
            "OBJECTID": "source_id"
        }
    },
    "processing_plants": {
        "url": "https://services1.arcgis.com/Hp6G80Pky0om6HgQ/arcgis/rest/services/Natural_Gas_Processing_Plants/FeatureServer/0/query",
        "table": "gas_processing_plants",
        "field_map": {
            "NAME": "name",
            "OPERATOR": "operator",
            "LATITUDE": "latitude",
            "LONGITUDE": "longitude",
            "STATE": "state",
            "COUNTY": "county",
            "CAPACITY": "capacity_mmcfd",
            "TYPE": "plant_type",
            "OBJECTID": "source_id"
        }
    },
    "lng_terminals": {
        "url": "https://services1.arcgis.com/Hp6G80Pky0om6HgQ/arcgis/rest/services/LNG_Import_Exports_and_Terminals/FeatureServer/0/query",
        "table": "lng_terminals",
        "field_map": {
            "NAME": "name",
            "OPERATOR": "operator",
            "LATITUDE": "latitude",
            "LONGITUDE": "longitude",
            "STATE": "state",
            "COUNTY": "county",
            "CAPACITY": "capacity_bcfd",
            "TYPE": "terminal_type",
            "STATUS": "status",
            "OBJECTID": "source_id"
        }
    }
}

# Alternative HIFLD URLs (NASA mirror) in case primary is down
HIFLD_ALT = {
    "compressor_stations": "https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/1/query",
    "processing_plants": "https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/2/query",
    "lng_terminals": "https://maps.nccs.nasa.gov/mapping/rest/services/hifld_open/energy/FeatureServer/3/query"
}

# ── Helpers ─────────────────────────────────────────────────────────────────

def get_conn():
    if not DATABASE_URL:
        print("ERROR: No DATABASE_URL or NEON_DATABASE_URL set")
        sys.exit(1)
    return psycopg2.connect(DATABASE_URL)

def fetch_arcgis_features(url, where="1=1", max_records=5000):
    """
    Fetch all features from an ArcGIS REST FeatureServer endpoint.
    Handles pagination via resultOffset.
    """
    all_features = []
    offset = 0
    batch_size = 1000
    
    while True:
        params = {
            "where": where,
            "outFields": "*",
            "outSR": 4326,
            "f": "json",
            "resultOffset": offset,
            "resultRecordCount": batch_size
        }
        
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, timeout=60)
                if resp.status_code == 200:
                    break
                print(f"  HTTP {resp.status_code}, retry {attempt+1}")
                time.sleep(2)
            except requests.exceptions.Timeout:
                print(f"  Timeout, retry {attempt+1}")
                time.sleep(3)
            except Exception as e:
                print(f"  Error: {e}, retry {attempt+1}")
                time.sleep(2)
        else:
            print(f"  ✗ Failed after 3 attempts at offset {offset}")
            break
        
        try:
            data = resp.json()
        except:
            print(f"  ✗ Invalid JSON at offset {offset}")
            break
        
        if "error" in data:
            print(f"  ✗ ArcGIS error: {data['error'].get('message', 'Unknown')}")
            break
        
        features = data.get("features", [])
        if not features:
            break
        
        all_features.extend(features)
        print(f"    Fetched {len(features)} at offset {offset} (total: {len(all_features)})")
        
        # Check if we got less than batch_size (last page)
        if len(features) < batch_size:
            break
        
        offset += batch_size
        
        if len(all_features) >= max_records:
            print(f"    Reached max records ({max_records})")
            break
        
        time.sleep(0.3)  # Rate limit courtesy
    
    return all_features

# ── Discovery Functions ─────────────────────────────────────────────────────

def discover_layer(conn, layer_name, config):
    """Discover and upsert a single midstream layer."""
    print(f"\n{'=' * 60}")
    print(f"LAYER: {layer_name}")
    print(f"{'=' * 60}")
    
    url = config["url"]
    table = config["table"]
    field_map = config["field_map"]
    
    # Try primary URL first, then alternative
    print(f"  Source: {url[:80]}...")
    features = fetch_arcgis_features(url)
    
    if not features and layer_name in HIFLD_ALT:
        alt_url = HIFLD_ALT[layer_name]
        print(f"  Primary failed, trying NASA mirror: {alt_url[:80]}...")
        features = fetch_arcgis_features(alt_url)
    
    if not features:
        print(f"  ✗ No features found for {layer_name}")
        return 0
    
    print(f"  → {len(features)} features from API")
    
    cur = conn.cursor()
    inserted = 0
    errors = 0
    
    for feat in features:
        attrs = feat.get("attributes", {})
        geom = feat.get("geometry", {})
        
        # Build record from field map
        record = {}
        for arcgis_field, db_field in field_map.items():
            val = attrs.get(arcgis_field)
            # Also try lowercase
            if val is None:
                val = attrs.get(arcgis_field.lower())
            record[db_field] = val
        
        # Geometry fallback if lat/lng not in attributes
        if not record.get("latitude") and geom:
            record["latitude"] = geom.get("y")
        if not record.get("longitude") and geom:
            record["longitude"] = geom.get("x")
        
        # Skip records without coordinates
        if not record.get("latitude") or not record.get("longitude"):
            continue
        
        source_id = str(record.get("source_id", ""))
        if not source_id:
            continue
        
        # Build dynamic INSERT
        db_fields = [k for k in record.keys() if k != "source_id"]
        db_fields.append("source")
        db_fields.append("source_id")
        
        values = [record.get(f) for f in db_fields[:-2]]
        values.append("HIFLD")
        values.append(source_id)
        
        placeholders = ", ".join(["%s"] * len(values))
        fields_str = ", ".join(db_fields)
        
        # Upsert
        update_parts = ", ".join([f"{f} = EXCLUDED.{f}" for f in db_fields if f not in ("source", "source_id")])
        
        try:
            cur.execute(f"""
                INSERT INTO {table} ({fields_str}, last_updated)
                VALUES ({placeholders}, NOW())
                ON CONFLICT (source, source_id) DO UPDATE
                SET {update_parts}, last_updated = NOW()
            """, values)
            inserted += 1
        except Exception as e:
            if errors < 3:
                print(f"  Insert error: {e}")
            errors += 1
            conn.rollback()
            cur = conn.cursor()
    
    conn.commit()
    
    # Stats
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    total = cur.fetchone()[0]
    
    print(f"  ✓ Upserted {inserted}, Errors: {errors}")
    print(f"  Total in {table}: {total}")
    
    return inserted

# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("DC Hub — Midstream Gas Discovery")
    print(f"Time: {datetime.now().isoformat()}")
    print(f"Database: {DATABASE_URL[:40]}..." if DATABASE_URL else "NO DATABASE")
    print("=" * 60)
    
    if not DATABASE_URL:
        print("ERROR: Set DATABASE_URL or NEON_DATABASE_URL")
        sys.exit(1)
    
    conn = get_conn()
    
    try:
        results = {}
        for layer_name, config in HIFLD_SERVICES.items():
            count = discover_layer(conn, layer_name, config)
            results[layer_name] = count
        
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        for name, count in results.items():
            status = "✓" if count > 0 else "✗"
            print(f"  {status} {name}: {count} records")
        print(f"  Total: {sum(results.values())} records")
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
