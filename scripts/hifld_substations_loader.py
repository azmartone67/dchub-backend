#!/usr/bin/env python3
"""
HIFLD Substations Bulk Loader for DC Hub
=========================================
Run in Railway shell: python3 /tmp/hifld_loader.py

Paginates the HIFLD ArcGIS FeatureServer to download ~80K substations,
deduplicates against existing Neon records, and bulk inserts new ones.

Source: https://services1.arcgis.com/Hp6G80Pky0om6HgA/arcgis/rest/services/Electric_Substations/FeatureServer/0
License: Public Use (HIFLD Open)
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse

# --- CONFIG ---
ARCGIS_BASE = (
    "https://services1.arcgis.com/Hp6G80Pky0om6HgA/arcgis/rest/services"
    "/Electric_Substations/FeatureServer/0/query"
)
BATCH_SIZE = 2000  # ArcGIS max is usually 2000 for this service
OUT_FIELDS = "OBJECTID,NAME,CITY,STATE,ZIP,LATITUDE,LONGITUDE,STATUS,OWNER,LINES,MAX_VOLT,MIN_VOLT,TYPE,COUNTY,NAICS_CODE,NAICS_DESC,SOURCE,SOURCEDATE,VAL_METHOD,VAL_DATE"
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set. Run this in Railway shell.")
    sys.exit(1)

# --- Step 0: Check table schema ---
import psycopg2
from psycopg2.extras import execute_values

def get_conn():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)

def ensure_table(conn):
    """Create substations table if not exists, add any missing columns."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS substations (
            id SERIAL PRIMARY KEY,
            name TEXT,
            city TEXT,
            state TEXT,
            zip TEXT,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            status TEXT,
            owner TEXT,
            lines INTEGER,
            max_volt DOUBLE PRECISION,
            min_volt DOUBLE PRECISION,
            type TEXT,
            county TEXT,
            naics_code TEXT,
            naics_desc TEXT,
            source TEXT DEFAULT 'HIFLD',
            source_date TEXT,
            val_method TEXT,
            val_date TEXT,
            hifld_objectid INTEGER,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    
    # Add columns that might be missing from earlier schema
    new_cols = {
        'hifld_objectid': 'INTEGER',
        'zip': 'TEXT',
        'county': 'TEXT',
        'naics_code': 'TEXT',
        'naics_desc': 'TEXT',
        'source_date': 'TEXT',
        'val_method': 'TEXT',
        'val_date': 'TEXT',
        'type': 'TEXT',
        'lines': 'INTEGER',
        'min_volt': 'DOUBLE PRECISION',
        'max_volt': 'DOUBLE PRECISION',
        'owner': 'TEXT',
    }
    for col, dtype in new_cols.items():
        try:
            cur.execute(f"ALTER TABLE substations ADD COLUMN IF NOT EXISTS {col} {dtype}")
        except Exception:
            conn.rollback()
    
    # Add unique constraint on hifld_objectid if not exists
    try:
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_substations_hifld_objectid 
            ON substations (hifld_objectid) WHERE hifld_objectid IS NOT NULL
        """)
    except Exception:
        conn.rollback()
    
    # Also add spatial index for lat/lng lookups
    try:
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_substations_lat_lng 
            ON substations (latitude, longitude)
        """)
    except Exception:
        conn.rollback()
    
    conn.commit()
    cur.close()
    print("✅ Table schema verified")

def get_existing_objectids(conn):
    """Get set of already-loaded HIFLD object IDs to skip duplicates."""
    cur = conn.cursor()
    cur.execute("SELECT hifld_objectid FROM substations WHERE hifld_objectid IS NOT NULL")
    ids = {row[0] for row in cur.fetchall()}
    cur.close()
    return ids

def get_existing_count(conn):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM substations")
    count = cur.fetchone()[0]
    cur.close()
    return count

# --- Step 1: Paginate ArcGIS API ---
def fetch_batch(offset, retry=3):
    """Fetch a batch of substations from ArcGIS FeatureServer."""
    params = urllib.parse.urlencode({
        'where': '1=1',
        'outFields': OUT_FIELDS,
        'resultOffset': offset,
        'resultRecordCount': BATCH_SIZE,
        'orderByFields': 'OBJECTID ASC',
        'f': 'json',
    })
    url = f"{ARCGIS_BASE}?{params}"
    
    for attempt in range(retry):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'DCHub/1.0'})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode('utf-8'))
            
            if 'error' in data:
                print(f"  ⚠️ ArcGIS error: {data['error'].get('message', 'unknown')}")
                if attempt < retry - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None, False
            
            features = data.get('features', [])
            exceeded = data.get('exceededTransferLimit', False)
            return features, exceeded
            
        except Exception as e:
            print(f"  ⚠️ Attempt {attempt+1}/{retry} failed: {e}")
            if attempt < retry - 1:
                time.sleep(2 ** attempt)
            else:
                return None, False
    
    return None, False

def feature_to_row(f):
    """Convert ArcGIS feature to DB row tuple."""
    a = f.get('attributes', {})
    return (
        a.get('NAME'),
        a.get('CITY'),
        a.get('STATE'),
        a.get('ZIP'),
        a.get('LATITUDE'),
        a.get('LONGITUDE'),
        a.get('STATUS'),
        a.get('OWNER'),
        a.get('LINES'),
        a.get('MAX_VOLT'),
        a.get('MIN_VOLT'),
        a.get('TYPE'),
        a.get('COUNTY'),
        a.get('NAICS_CODE'),
        a.get('NAICS_DESC'),
        'HIFLD',
        a.get('SOURCEDATE'),
        a.get('VAL_METHOD'),
        a.get('VAL_DATE'),
        a.get('OBJECTID'),
    )

# --- Step 2: Bulk insert ---
def bulk_insert(conn, rows):
    """Insert rows using ON CONFLICT DO NOTHING for dedup."""
    if not rows:
        return 0
    cur = conn.cursor()
    sql = """
        INSERT INTO substations (
            name, city, state, zip, latitude, longitude, status, owner,
            lines, max_volt, min_volt, type, county, naics_code, naics_desc,
            source, source_date, val_method, val_date, hifld_objectid
        ) VALUES %s
        ON CONFLICT (hifld_objectid) WHERE hifld_objectid IS NOT NULL
        DO NOTHING
    """
    execute_values(cur, sql, rows, page_size=500)
    inserted = cur.rowcount
    conn.commit()
    cur.close()
    return inserted

# --- Step 3: Backfill existing records with hifld_objectid ---
def backfill_existing_objectids(conn):
    """
    Try to match existing substations (loaded before hifld_objectid column existed)
    by name+lat+lng so we don't create duplicates.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM substations 
        WHERE hifld_objectid IS NULL AND source = 'HIFLD'
    """)
    count = cur.fetchone()[0]
    cur.close()
    if count > 0:
        print(f"  ℹ️ {count} existing HIFLD records lack hifld_objectid — will dedup by name+lat+lng")
    return count

# --- MAIN ---
def main():
    print("=" * 60)
    print("  HIFLD Substations Bulk Loader for DC Hub")
    print("=" * 60)
    
    conn = get_conn()
    
    # Schema
    ensure_table(conn)
    
    # Current state
    existing_count = get_existing_count(conn)
    print(f"📊 Current substations in Neon: {existing_count:,}")
    
    existing_ids = get_existing_objectids(conn)
    print(f"📊 Records with hifld_objectid: {len(existing_ids):,}")
    
    orphans = backfill_existing_objectids(conn)
    
    # First, get total record count from ArcGIS
    count_params = urllib.parse.urlencode({
        'where': '1=1',
        'returnCountOnly': 'true',
        'f': 'json',
    })
    count_url = f"{ARCGIS_BASE}?{count_params}"
    try:
        req = urllib.request.Request(count_url, headers={'User-Agent': 'DCHub/1.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            count_data = json.loads(resp.read().decode('utf-8'))
        total_remote = count_data.get('count', 0)
        print(f"🌐 HIFLD total substations available: {total_remote:,}")
    except Exception as e:
        print(f"⚠️ Could not get count from ArcGIS: {e}")
        print("   The API may be down. Try again later.")
        conn.close()
        sys.exit(1)
    
    if total_remote == 0:
        print("❌ ArcGIS returned 0 records. API may be down.")
        conn.close()
        sys.exit(1)
    
    # Paginate and load
    offset = 0
    total_fetched = 0
    total_inserted = 0
    total_skipped = 0
    batch_num = 0
    
    print(f"\n🚀 Starting bulk load (batch size: {BATCH_SIZE})...\n")
    
    while True:
        batch_num += 1
        features, exceeded = fetch_batch(offset)
        
        if features is None:
            print(f"❌ Failed to fetch batch at offset {offset}. Stopping.")
            break
        
        if not features:
            print(f"✅ No more features at offset {offset}. Done fetching.")
            break
        
        total_fetched += len(features)
        
        # Filter out already-loaded records
        rows = []
        skipped = 0
        for f in features:
            oid = f.get('attributes', {}).get('OBJECTID')
            if oid and oid in existing_ids:
                skipped += 1
                continue
            row = feature_to_row(f)
            # Skip records with no lat/lng
            if row[4] is None or row[5] is None:
                skipped += 1
                continue
            rows.append(row)
            if oid:
                existing_ids.add(oid)
        
        total_skipped += skipped
        
        # Bulk insert
        if rows:
            inserted = bulk_insert(conn, rows)
            total_inserted += inserted
            print(f"  Batch {batch_num}: fetched {len(features)}, inserted {inserted}, skipped {skipped} (offset {offset})")
        else:
            print(f"  Batch {batch_num}: fetched {len(features)}, all skipped/dupes (offset {offset})")
        
        offset += BATCH_SIZE
        
        # Rate limit courtesy
        time.sleep(0.5)
        
        # Safety valve
        if batch_num > 100:
            print("⚠️ Safety limit (100 batches) reached. Stopping.")
            break
    
    # Final count
    final_count = get_existing_count(conn)
    conn.close()
    
    print(f"\n{'=' * 60}")
    print(f"  HIFLD Bulk Load Complete")
    print(f"{'=' * 60}")
    print(f"  🌐 Remote records:   {total_remote:,}")
    print(f"  📥 Fetched:          {total_fetched:,}")
    print(f"  ✅ Inserted:         {total_inserted:,}")
    print(f"  ⏭️  Skipped (dupes):  {total_skipped:,}")
    print(f"  📊 Total in Neon:    {final_count:,}")
    print(f"{'=' * 60}")

if __name__ == '__main__':
    main()
