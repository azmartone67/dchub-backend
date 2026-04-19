#!/usr/bin/env python3
"""
HIFLD Substations CSV Loader for DC Hub
========================================
Run in Railway shell:
  python3 /tmp/hifld_csv_loader.py

Downloads Substations.csv from GitHub repo and bulk inserts into Neon.
Deduplicates on hifld_objectid using ON CONFLICT DO NOTHING.
"""

import os, sys, csv, io, time, subprocess

DATABASE_URL = os.environ.get("DATABASE_URL")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
CSV_URL = "https://raw.githubusercontent.com/azmartone67/dchub-backend/main/scripts/Substations.csv"

if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set. Run in Railway shell.")
    sys.exit(1)

import psycopg2
from psycopg2.extras import execute_values

def get_conn():
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)

def ensure_table(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS substations (
            id SERIAL PRIMARY KEY,
            name TEXT, city TEXT, state TEXT, zip TEXT,
            latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
            status TEXT, owner TEXT, lines INTEGER,
            max_volt DOUBLE PRECISION, min_volt DOUBLE PRECISION,
            type TEXT, county TEXT, naics_code TEXT, naics_desc TEXT,
            source TEXT DEFAULT 'HIFLD', source_date TEXT,
            val_method TEXT, val_date TEXT,
            hifld_objectid INTEGER,
            country TEXT DEFAULT 'USA',
            county_fips TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    # Add any missing columns
    for col, dtype in {
        'hifld_objectid': 'INTEGER', 'zip': 'TEXT', 'county': 'TEXT',
        'naics_code': 'TEXT', 'naics_desc': 'TEXT', 'source_date': 'TEXT',
        'val_method': 'TEXT', 'val_date': 'TEXT', 'type': 'TEXT',
        'lines': 'INTEGER', 'min_volt': 'DOUBLE PRECISION',
        'max_volt': 'DOUBLE PRECISION', 'owner': 'TEXT',
        'country': 'TEXT', 'county_fips': 'TEXT',
    }.items():
        try:
            cur.execute(f"ALTER TABLE substations ADD COLUMN IF NOT EXISTS {col} {dtype}")
        except Exception:
            conn.rollback()
    try:
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_substations_hifld_oid ON substations (hifld_objectid) WHERE hifld_objectid IS NOT NULL")
    except Exception:
        conn.rollback()
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_substations_lat_lng ON substations (latitude, longitude)")
    except Exception:
        conn.rollback()
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_substations_state ON substations (state)")
    except Exception:
        conn.rollback()
    conn.commit()
    cur.close()
    print("  Table schema verified")

def safe_int(v):
    if not v or v.strip() == '': return None
    try: return int(float(v))
    except: return None

def safe_float(v):
    if not v or v.strip() == '': return None
    try: return float(v)
    except: return None

def download_csv():
    """Download CSV from GitHub using curl."""
    print("  Downloading CSV from GitHub...")
    cmd = ['curl', '-sL', CSV_URL]
    if GITHUB_TOKEN:
        cmd = ['curl', '-sL', '-H', f'Authorization: token {GITHUB_TOKEN}', CSV_URL]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        print(f"  ERROR: curl failed: {result.stderr}")
        sys.exit(1)
    data = result.stdout
    if not data or data.startswith('<!DOCTYPE') or data.startswith('404'):
        print("  ERROR: Got HTML or 404. Check GITHUB_TOKEN and URL.")
        sys.exit(1)
    lines = data.strip().split('\n')
    print(f"  Downloaded {len(lines):,} lines ({len(data):,} bytes)")
    return data

def main():
    print("=" * 60)
    print("  HIFLD Substations CSV Loader — DC Hub")
    print("=" * 60)

    conn = get_conn()
    ensure_table(conn)

    # Current count
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM substations")
    before_count = cur.fetchone()[0]
    cur.close()
    print(f"  Current substations in Neon: {before_count:,}")

    # Download
    csv_data = download_csv()

    # Parse CSV
    reader = csv.DictReader(io.StringIO(csv_data))
    
    batch = []
    batch_size = 2000
    total_parsed = 0
    total_inserted = 0
    total_skipped = 0
    batch_num = 0

    print(f"\n  Loading into Neon (batch size: {batch_size})...\n")

    for row in reader:
        total_parsed += 1
        
        lat = safe_float(row.get('LATITUDE'))
        lng = safe_float(row.get('LONGITUDE'))
        oid = safe_int(row.get('OBJECTID'))
        
        # Skip rows with no coordinates
        if lat is None or lng is None:
            total_skipped += 1
            continue

        batch.append((
            row.get('NAME', '').strip() or None,
            row.get('CITY', '').strip() or None,
            row.get('STATE', '').strip() or None,
            row.get('ZIP', '').strip() or None,
            lat, lng,
            row.get('STATUS', '').strip() or None,
            None,  # owner (not in CSV)
            safe_int(row.get('LINES')),
            safe_float(row.get('MAX_VOLT')),
            safe_float(row.get('MIN_VOLT')),
            row.get('TYPE', '').strip() or None,
            row.get('COUNTY', '').strip() or None,
            row.get('NAICS_CODE', '').strip() or None,
            row.get('NAICS_DESC', '').strip() or None,
            'HIFLD',
            row.get('SOURCEDATE', '').strip() or None,
            row.get('VAL_METHOD', '').strip() or None,
            row.get('VAL_DATE', '').strip() or None,
            oid,
            row.get('COUNTRY', 'USA').strip() or 'USA',
            row.get('COUNTYFIPS', '').strip() or None,
        ))

        if len(batch) >= batch_size:
            batch_num += 1
            inserted = flush_batch(conn, batch)
            total_inserted += inserted
            print(f"  Batch {batch_num}: parsed {len(batch)}, inserted {inserted} (total: {total_parsed:,})")
            batch = []

    # Final batch
    if batch:
        batch_num += 1
        inserted = flush_batch(conn, batch)
        total_inserted += inserted
        print(f"  Batch {batch_num}: parsed {len(batch)}, inserted {inserted} (total: {total_parsed:,})")

    # Final count
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM substations")
    after_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT state) FROM substations WHERE state IS NOT NULL")
    state_count = cur.fetchone()[0]
    cur.close()
    conn.close()

    print(f"\n{'=' * 60}")
    print(f"  HIFLD CSV Load Complete")
    print(f"{'=' * 60}")
    print(f"  CSV rows parsed:  {total_parsed:,}")
    print(f"  Inserted:         {total_inserted:,}")
    print(f"  Skipped (no GPS): {total_skipped:,}")
    print(f"  Before:           {before_count:,}")
    print(f"  After:            {after_count:,}")
    print(f"  Net new:          {after_count - before_count:,}")
    print(f"  States covered:   {state_count}")
    print(f"{'=' * 60}")

def flush_batch(conn, rows):
    cur = conn.cursor()
    sql = """
        INSERT INTO substations (
            name, city, state, zip, latitude, longitude, status, owner,
            lines, max_volt, min_volt, type, county, naics_code, naics_desc,
            source, source_date, val_method, val_date, hifld_objectid,
            country, county_fips
        ) VALUES %s
        ON CONFLICT (hifld_objectid) WHERE hifld_objectid IS NOT NULL
        DO NOTHING
    """
    execute_values(cur, sql, rows, page_size=500)
    inserted = cur.rowcount
    conn.commit()
    cur.close()
    return inserted

if __name__ == '__main__':
    main()
