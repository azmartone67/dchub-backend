#!/usr/bin/env python3
"""
DC Hub — EIA Generator Re-Seed with Coordinates
=================================================
Fetches plant-level generator data from EIA API v2 (Form 860/860M)
and upserts into the eia_generators table in Neon WITH lat/lng.

The existing 45,343 rows have 0 coordinates — this script replaces
them with deduplicated plant-level records that include latitude,
longitude, fuel type, capacity, operator, state, and balancing authority.

Strategy:
  1. Query EIA API v2 /electricity/operating-generator-capacity/data
     with plantid, plantName, latitude, longitude, nameplate-capacity-mw,
     energy_source_desc, balancing_authority_code, stateid, status
  2. Paginate in 5,000-row chunks (API max per request)
  3. Aggregate generators to plant-level (sum capacity, pick primary fuel)
  4. TRUNCATE + batch-insert into eia_generators (clean slate)

Usage:
  # In Railway shell:
  EIA_API_KEY=SuphqqIra7G46LHVDwb9CL5n4WYRwLu7ujeFXJMG \
  NEON_DATABASE_URL="postgresql://..." \
  python3 eia_generator_reseed.py

  # Or with defaults from env:
  python3 eia_generator_reseed.py
"""

import os
import sys
import json
import time
import logging
import psycopg2
from psycopg2.extras import execute_values
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('eia-reseed')

# ═══════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════

EIA_API_KEY = os.environ.get('EIA_API_KEY', 'SuphqqIra7G46LHVDwb9CL5n4WYRwLu7ujeFXJMG')
DB_URL = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
EIA_BASE = 'https://api.eia.gov/v2/electricity/operating-generator-capacity/data/'
PAGE_SIZE = 5000  # EIA API max
MAX_PAGES = 100   # Safety limit (500K rows max)


# ═══════════════════════════════════════════════════════════
# EIA API FETCHER
# ═══════════════════════════════════════════════════════════

def fetch_eia_page(offset=0):
    """Fetch one page of generator data from EIA API v2."""
    params = (
        f"%sapi_key={EIA_API_KEY}"
        f"&frequency=monthly"
        f"&data[0]=nameplate-capacity-mw"
        f"&data[1]=latitude"
        f"&data[2]=longitude"
        f"&sort[0][column]=plantid"
        f"&sort[0][direction]=asc"
        f"&offset={offset}"
        f"&length={PAGE_SIZE}"
    )
    url = EIA_BASE + params
    
    req = Request(url, headers={
        'User-Agent': 'DCHub/3.0 (+https://dchub.cloud)',
        'Accept': 'application/json',
    })
    
    try:
        with urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            response = data.get('response', {})
            rows = response.get('data', [])
            total = response.get('total', 0)
            return rows, total
    except (HTTPError, URLError) as e:
        log.error(f"EIA API error at offset {offset}: {e}")
        return [], 0
    except Exception as e:
        log.error(f"Unexpected error at offset {offset}: {e}")
        return [], 0


def fetch_all_generators():
    """Paginate through all EIA generator records."""
    all_rows = []
    offset = 0
    total = None
    page = 0
    
    while page < MAX_PAGES:
        log.info(f"  Fetching page {page + 1} (offset {offset})...")
        rows, reported_total = fetch_eia_page(offset)
        
        if total is None:
            total = int(reported_total) if reported_total else 0
            log.info(f"  EIA reports {total} total generator records")
        
        if not rows:
            log.info(f"  No more rows at offset {offset}")
            break
        
        all_rows.extend(rows)
        log.info(f"  Got {len(rows)} rows (total so far: {len(all_rows):,})")
        
        offset += PAGE_SIZE
        page += 1
        
        if offset >= total:
            break
        
        # Rate limit courtesy
        time.sleep(0.5)
    
    log.info(f"✅ Fetched {len(all_rows):,} total generator records from EIA")
    return all_rows


# ═══════════════════════════════════════════════════════════
# PLANT-LEVEL AGGREGATION
# ═══════════════════════════════════════════════════════════

def aggregate_to_plants(raw_rows):
    """Aggregate generator-level records to plant-level.
    
    EIA data has one row per generator. A plant can have 10+ generators.
    We aggregate: sum capacity, pick primary fuel (highest MW), keep coords.
    """
    plants = defaultdict(lambda: {
        'plant_id': None,
        'name': None,
        'lat': None,
        'lng': None,
        'state': None,
        'county': None,
        'operator': None,
        'balancing_authority': None,
        'status': None,
        'fuel_types': defaultdict(float),  # fuel -> total MW
        'total_capacity_mw': 0.0,
        'generator_count': 0,
    })
    
    skipped_no_id = 0
    skipped_no_coords = 0
    
    for row in raw_rows:
        plant_id = row.get('plantid') or row.get('plantId')
        if not plant_id:
            skipped_no_id += 1
            continue
        
        plant_id = str(plant_id)
        p = plants[plant_id]
        
        # Core identity (take first non-null)
        if not p['plant_id']:
            p['plant_id'] = plant_id
        if not p['name']:
            p['name'] = row.get('plantName') or row.get('plant_name') or f'Plant {plant_id}'
        
        # Coordinates — take first valid pair
        lat = _safe_float(row.get('latitude'))
        lng = _safe_float(row.get('longitude'))
        if lat and lng and abs(lat) <= 90 and abs(lng) <= 180:
            if not p['lat']:
                p['lat'] = lat
                p['lng'] = lng
        
        # Metadata
        if not p['state']:
            p['state'] = row.get('stateid') or row.get('state')
        if not p['county']:
            p['county'] = row.get('county')
        if not p['operator']:
            p['operator'] = row.get('entityName') or row.get('entity_name')
        if not p['balancing_authority']:
            p['balancing_authority'] = row.get('balancing_authority_code') or row.get('ba_code')
        if not p['status']:
            p['status'] = row.get('status') or 'OP'
        
        # Capacity aggregation
        mw = _safe_float(row.get('nameplate-capacity-mw') or row.get('nameplate_capacity_mw'))
        if mw and mw > 0:
            fuel = row.get('energy_source_desc') or row.get('technology') or 'Unknown'
            p['fuel_types'][fuel] += mw
            p['total_capacity_mw'] += mw
        
        p['generator_count'] += 1
    
    # Determine primary fuel type per plant
    result = []
    for pid, p in plants.items():
        if not p['lat'] or not p['lng']:
            skipped_no_coords += 1
            continue
        
        # Primary fuel = highest total MW
        if p['fuel_types']:
            primary_fuel = max(p['fuel_types'], key=p['fuel_types'].get)
        else:
            primary_fuel = 'Unknown'
        
        result.append({
            'plant_id': p['plant_id'],
            'name': (p['name'] or '')[:200],
            'lat': round(p['lat'], 6),
            'lng': round(p['lng'], 6),
            'state': (p['state'] or '')[:5],
            'county': (p['county'] or '')[:100],
            'operator': (p['operator'] or '')[:200],
            'balancing_authority': (p['balancing_authority'] or '')[:20],
            'fuel_type': primary_fuel[:100],
            'capacity_mw': round(p['total_capacity_mw'], 2),
            'generator_count': p['generator_count'],
            'status': (p['status'] or 'OP')[:20],
        })
    
    log.info(f"  Aggregated to {len(result):,} plants with coordinates")
    log.info(f"  Skipped: {skipped_no_id} (no plant ID), {skipped_no_coords} (no coordinates)")
    
    return result


def _safe_float(val):
    """Safely convert a value to float, returning None on failure."""
    if val is None:
        return None
    try:
        f = float(val)
        return f if f != 0 else None
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════
# DATABASE UPSERT
# ═══════════════════════════════════════════════════════════

def ensure_table(conn):
    """Ensure eia_generators table has the right schema with lat/lng."""
    cur = conn.cursor()
    
    # Check if lat column exists
    cur.execute("""
        SELECT column_name FROM information_schema.columns 
        WHERE table_name = 'eia_generators' AND column_name = 'lat'
    """)
    has_lat = cur.fetchone() is not None
    
    if not has_lat:
        log.info("  Adding lat/lng columns to eia_generators...")
        for col_sql in [
            "ALTER TABLE eia_generators ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION",
            "ALTER TABLE eia_generators ADD COLUMN IF NOT EXISTS lng DOUBLE PRECISION",
            "ALTER TABLE eia_generators ADD COLUMN IF NOT EXISTS county TEXT",
            "ALTER TABLE eia_generators ADD COLUMN IF NOT EXISTS operator TEXT",
            "ALTER TABLE eia_generators ADD COLUMN IF NOT EXISTS balancing_authority TEXT",
            "ALTER TABLE eia_generators ADD COLUMN IF NOT EXISTS generator_count INTEGER DEFAULT 1",
            "ALTER TABLE eia_generators ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'OP'",
        ]:
            try:
                cur.execute(col_sql)
            except Exception:
                conn.rollback()
        conn.commit()
    
    # Create spatial index if not exists
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_eia_gen_lat_lng ON eia_generators(lat, lng)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_eia_gen_state ON eia_generators(state)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_eia_gen_fuel ON eia_generators(fuel_type)")
        conn.commit()
    except Exception:
        conn.rollback()
    
    cur.close()


def upsert_plants(conn, plants):
    """Truncate and batch-insert all plants."""
    cur = conn.cursor()
    
    # Truncate old data (the 45K rows with no coords)
    cur.execute("TRUNCATE TABLE eia_generators")
    log.info("  Truncated eia_generators (old data had no coordinates)")
    
    # Batch insert
    insert_sql = """
        INSERT INTO eia_generators 
        (plant_id, name, lat, lng, state, county, operator, balancing_authority, 
         fuel_type, capacity_mw, generator_count, status)
        VALUES %s
    """
    
    values = [(
        p['plant_id'], p['name'], p['lat'], p['lng'],
        p['state'], p['county'], p['operator'], p['balancing_authority'],
        p['fuel_type'], p['capacity_mw'], p['generator_count'], p['status'],
    ) for p in plants]
    
    # Insert in chunks of 500
    inserted = 0
    for i in range(0, len(values), 500):
        chunk = values[i:i+500]
        try:
            execute_values(cur, insert_sql, chunk,
                template='(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)')
            inserted += len(chunk)
        except Exception as e:
            log.error(f"  Batch insert error at chunk {i}: {e}")
            conn.rollback()
    
    conn.commit()
    cur.close()
    
    log.info(f"  ✅ Inserted {inserted:,} plants into eia_generators")
    return inserted


def verify(conn):
    """Post-insert verification."""
    cur = conn.cursor()
    
    cur.execute("SELECT COUNT(*) FROM eia_generators")
    total = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM eia_generators WHERE lat IS NOT NULL AND lng IS NOT NULL")
    with_coords = cur.fetchone()[0]
    
    cur.execute("SELECT state, COUNT(*), ROUND(SUM(capacity_mw)::numeric, 0) FROM eia_generators GROUP BY state ORDER BY SUM(capacity_mw) DESC LIMIT 10")
    top_states = cur.fetchall()
    
    cur.execute("SELECT fuel_type, COUNT(*), ROUND(SUM(capacity_mw)::numeric, 0) FROM eia_generators GROUP BY fuel_type ORDER BY SUM(capacity_mw) DESC LIMIT 10")
    top_fuels = cur.fetchall()
    
    cur.close()
    
    log.info(f"\n{'='*60}")
    log.info(f"  VERIFICATION")
    log.info(f"{'='*60}")
    log.info(f"  Total plants:      {total}")
    log.info(f"  With coordinates:  {with_coords:,} ({round(with_coords/total*100, 1) if total else 0}%)")
    log.info(f"\n  Top 10 states by capacity:")
    for state, count, mw in top_states:
        log.info(f"    {state:<5} {count:>5} plants  {mw:>10} MW")
    log.info(f"\n  Top 10 fuel types:")
    for fuel, count, mw in top_fuels:
        log.info(f"    {fuel:<30} {count:>5} plants  {mw:>10} MW")
    log.info(f"{'='*60}")
    
    return total, with_coords


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    if not DB_URL:
        log.error("No NEON_DATABASE_URL or DATABASE_URL set")
        sys.exit(1)
    if not EIA_API_KEY:
        log.error("No EIA_API_KEY set")
        sys.exit(1)
    
    log.info("=" * 60)
    log.info("  DC Hub — EIA Generator Re-Seed")
    log.info("  Source: EIA API v2 (Form 860/860M)")
    log.info(f"  Target: eia_generators table in Neon")
    log.info("=" * 60)
    
    # Step 1: Fetch from EIA
    log.info("\n📡 Step 1: Fetching generator data from EIA API v2...")
    raw_rows = fetch_all_generators()
    if not raw_rows:
        log.error("No data returned from EIA API")
        sys.exit(1)
    
    # Step 2: Aggregate to plant level
    log.info("\n🔧 Step 2: Aggregating to plant-level with coordinates...")
    plants = aggregate_to_plants(raw_rows)
    if not plants:
        log.error("No plants with coordinates after aggregation")
        sys.exit(1)
    
    # Step 3: Upsert to Neon
    log.info("\n💾 Step 3: Upserting to Neon eia_generators table...")
    conn = psycopg2.connect(DB_URL, connect_timeout=30)
    try:
        ensure_table(conn)
        inserted = upsert_plants(conn, plants)
        
        # Step 4: Verify
        log.info("\n🔍 Step 4: Verifying...")
        total, with_coords = verify(conn)
        
        log.info(f"\n✅ DONE — {total} plants with {with_coords:,} coordinates in eia_generators")
        log.info(f"   Previous: 45,343 rows with 0 coordinates")
        log.info(f"   Now:      {total} plants with {with_coords:,} coordinates ({round(with_coords/total*100, 1) if total else 0}%)")
    finally:
        conn.close()


if __name__ == '__main__':
    main()
