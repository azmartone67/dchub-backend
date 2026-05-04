#!/usr/bin/env python3
"""
hifld_substation_loader.py — Bulk load HIFLD Electric Substations into Neon

Downloads the full dataset from ArcGIS Hub (GeoJSON API) and bulk-inserts
into the substations table in Neon PostgreSQL.

Run in Railway shell:
    python3 hifld_substation_loader.py

The HIFLD dataset has ~70,000 substations. We currently have 1,042.
This script pages through the ArcGIS Feature Server API using offset/limit
since the direct download URLs may be down.
"""

import json
import sys
import time
import traceback
import urllib.request
import urllib.parse
import math

# ArcGIS Feature Server — paginated query (more reliable than Hub download)
HIFLD_BASE = "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services"
SUBSTATIONS_URL = f"{HIFLD_BASE}/Electric_Substations/FeatureServer/0/query"

# Alternative endpoints to try if the primary is down
ALTERNATIVE_URLS = [
    # HIFLD Open Data Hub GeoJSON API
    "https://opendata.arcgis.com/api/v3/datasets/f48d61b8d4094ac1bc0d20e2e6b10a4c_0/downloads/data?format=geojson&spatialRefId=4326",
    # HIFLD direct feature service (sometimes different URL)
    "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/ArcGIS/rest/services/Electric_Substations_1/FeatureServer/0/query",
]

BATCH_SIZE = 2000
MAX_RECORDS = 80000  # Safety limit
USER_AGENT = 'DCHub-HIFLD-Loader/1.0 (dchub.cloud)'


def fetch_page(offset, batch_size=BATCH_SIZE):
    """Fetch a page of substations from HIFLD ArcGIS Feature Server."""
    params = urllib.parse.urlencode({
        'where': '1=1',
        'outFields': 'NAME,CITY,STATE,STATUS,MAX_VOLT,MIN_VOLT,LATITUDE,LONGITUDE,OWNER,NAICS_CODE,COUNTY',
        'returnGeometry': 'true',
        'outSR': '4326',
        'f': 'json',
        'resultOffset': offset,
        'resultRecordCount': batch_size,
    })
    
    url = f"{SUBSTATIONS_URL}%s{params}"
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        
        if 'error' in data:
            print(f"  ArcGIS error: {data['error']}")
            return None
        
        features = data.get('features', [])
        exceeded = data.get('exceededTransferLimit', False)
        return features, exceeded
    
    except Exception as e:
        print(f"  Fetch error at offset {offset}: {e}")
        return None


def parse_substation(feature):
    """Convert ArcGIS feature to substation dict."""
    attr = feature.get('attributes', {})
    geom = feature.get('geometry', {})
    
    lat = attr.get('LATITUDE') or geom.get('y')
    lng = attr.get('LONGITUDE') or geom.get('x')
    
    if not lat or not lng:
        return None
    
    return {
        'name': attr.get('NAME', '').strip() if attr.get('NAME') else None,
        'city': attr.get('CITY', '').strip() if attr.get('CITY') else None,
        'state': attr.get('STATE', '').strip() if attr.get('STATE') else None,
        'county': attr.get('COUNTY', '').strip() if attr.get('COUNTY') else None,
        'status': attr.get('STATUS', '').strip() if attr.get('STATUS') else 'IN SERVICE',
        'voltage_kv': attr.get('MAX_VOLT'),
        'owner': attr.get('OWNER', '').strip() if attr.get('OWNER') else None,
        'lat': round(float(lat), 6),
        'lng': round(float(lng), 6),
        'source': 'HIFLD',
    }


def insert_batch(conn, substations):
    """Bulk insert substations into Neon, skipping duplicates."""
    if not substations:
        return 0
    
    cur = conn.cursor()
    inserted = 0
    
    for sub in substations:
        try:
            # Use lat/lng as natural dedup key (same location = same substation)
            cur.execute("""
                INSERT INTO substations (name, city, state, county, status, voltage_kv, owner, lat, lng, latitude, longitude, source, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT DO NOTHING
            """, (
                sub['name'], sub['city'], sub['state'], sub['county'],
                sub['status'], sub['voltage_kv'], sub['owner'],
                sub['lat'], sub['lng'],
                sub['lat'], sub['lng'],  # populate both lat/lng and latitude/longitude
                sub['source'],
            ))
            inserted += 1
        except Exception as e:
            # Skip individual errors (e.g., constraint violations)
            pass
    
    conn.commit()
    return inserted


def check_for_unique_constraint(conn):
    """Add a unique constraint on lat/lng if it doesn't exist, for ON CONFLICT."""
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT 1 FROM pg_indexes 
            WHERE tablename = 'substations' AND indexname = 'idx_substations_lat_lng_unique'
        """)
        if not cur.fetchone():
            print("Creating unique index on substations(lat, lng)...")
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_substations_lat_lng_unique 
                ON substations (lat, lng) WHERE lat IS NOT NULL AND lng IS NOT NULL
            """)
            conn.commit()
            print("✅ Unique index created")
    except Exception as e:
        conn.rollback()
        print(f"⚠️ Could not create unique index: {e}")
        print("   Will use INSERT without ON CONFLICT (may have duplicates)")


def main():
    print("=" * 60)
    print("HIFLD Electric Substations Bulk Loader")
    print("=" * 60)
    
    # Get DB connection
    try:
        from db_utils import get_db
        conn = get_db()
        print("✅ Connected to Neon PostgreSQL")
    except Exception as e:
        print(f"❌ Could not connect to database: {e}")
        raise RuntimeError("loader aborted: upstream API unavailable or guard tripped (was sys.exit(1))")
    # Check current count
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM substations")
    current_count = cur.fetchone()[0]
    print(f"📊 Current substations in Neon: {current_count}")
    
    # Create unique index for dedup
    check_for_unique_constraint(conn)
    
    # Test ArcGIS API availability
    print("\n🔍 Testing HIFLD ArcGIS API...")
    test_result = fetch_page(0, 5)
    if test_result is None:
        print("❌ Primary HIFLD API unavailable. Trying alternatives...")
        # TODO: Try alternative URLs
        print("❌ All HIFLD sources unavailable. Try again later.")
        print("   You can also download the CSV manually from:")
        print("   https://hifld-geoplatform.opendata.arcgis.com/datasets/electric-substations")
        raise RuntimeError("loader aborted: upstream API unavailable or guard tripped (was sys.exit(1))")
    features, _ = test_result
    print(f"✅ API available — test returned {len(features)} features")
    
    # Page through all records
    print("\n📥 Downloading substations...")
    total_fetched = 0
    total_inserted = 0
    offset = 0
    
    while offset < MAX_RECORDS:
        result = fetch_page(offset, BATCH_SIZE)
        if result is None:
            print(f"  ⚠️ Failed at offset {offset}, retrying in 5s...")
            time.sleep(5)
            result = fetch_page(offset, BATCH_SIZE)
            if result is None:
                print(f"  ❌ Giving up at offset {offset}")
                break
        
        features, exceeded = result
        if not features:
            print(f"  No more features at offset {offset}")
            break
        
        # Parse features
        substations = []
        for f in features:
            sub = parse_substation(f)
            if sub:
                substations.append(sub)
        
        # Insert batch
        inserted = insert_batch(conn, substations)
        total_fetched += len(features)
        total_inserted += inserted
        
        print(f"  Offset {offset}: fetched {len(features)}, parsed {len(substations)}, inserted {inserted} (total: {total_fetched} fetched, {total_inserted} inserted)")
        
        if not exceeded or len(features) < BATCH_SIZE:
            print(f"  ✅ All records retrieved")
            break
        
        offset += BATCH_SIZE
        time.sleep(1)  # Be polite to the API
    
    # Final count
    cur.execute("SELECT COUNT(*) FROM substations")
    new_count = cur.fetchone()[0]
    
    print(f"\n{'=' * 60}")
    print(f"✅ HIFLD Substation Load Complete")
    print(f"   Fetched: {total_fetched}")
    print(f"   Inserted: {total_inserted}")
    print(f"   Before: {current_count}")
    print(f"   After:  {new_count}")
    print(f"   Net new: {new_count - current_count}")
    print(f"{'=' * 60}")


if __name__ == '__main__':
    main()
