#!/usr/bin/env python3
"""
DC Hub Rankings Data Ingestion
===============================
Standalone script — bypasses Flask app, connects directly to Neon.

Run: python3 ingest_rankings_data.py

Actions:
  1. Ingest EIA gas pipelines (3,289 records) into gas_pipelines table
  2. Expand fiber routes with 30+ additional carrier routes
  3. Fix VA/MD bounding box for Ashburn
  4. Fix any remaining state='Texas' → 'TX' in facilities
"""

import os
import sys
import json
import time
import math
import requests
import psycopg2

# ============================================================
# DATABASE CONNECTION
# ============================================================
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set")
    sys.exit(1)

# Strip sslmode if pooler doesn't support it, add connect_timeout
def get_conn():
    url = DATABASE_URL
    if 'connect_timeout' not in url:
        url += ('&' if '?' in url else '?') + 'connect_timeout=15'
    return psycopg2.connect(url)

print("🔌 Connecting to Neon...")
conn = get_conn()
conn.autocommit = True
cur = conn.cursor()
print("✅ Connected")

# ============================================================
# STATE BOUNDING BOXES (fixed VA/MD overlap for Ashburn)
# ============================================================
STATE_BOUNDS = {
    'VA': (36.5, -83.7, 39.5, -75.2),   # CHECK FIRST — Ashburn (38.95, -77.45) is VA
    'MD': (38.0, -79.5, 39.7, -75.0),   # Narrower — checked AFTER VA
    'AL': (30.2, -88.5, 35.0, -84.9), 'AZ': (31.3, -114.8, 37.0, -109.0),
    'AR': (33.0, -94.6, 36.5, -89.6), 'CA': (32.5, -124.4, 42.0, -114.1),
    'CO': (37.0, -109.1, 41.0, -102.0), 'CT': (41.0, -73.7, 42.1, -71.8),
    'DE': (38.5, -75.8, 39.8, -75.0), 'FL': (24.5, -87.6, 31.0, -80.0),
    'GA': (30.4, -85.6, 35.0, -80.8), 'ID': (42.0, -117.2, 49.0, -111.0),
    'IL': (37.0, -91.5, 42.5, -87.0), 'IN': (37.8, -88.1, 41.8, -84.8),
    'IA': (40.4, -96.6, 43.5, -90.1), 'KS': (37.0, -102.1, 40.0, -94.6),
    'KY': (36.5, -89.6, 39.1, -82.0), 'LA': (29.0, -94.0, 33.0, -89.0),
    'ME': (43.1, -71.1, 47.5, -67.0),
    'MA': (41.2, -73.5, 42.9, -69.9), 'MI': (41.7, -90.4, 48.3, -82.1),
    'MN': (43.5, -97.2, 49.4, -89.5), 'MS': (30.2, -91.7, 35.0, -88.1),
    'MO': (36.0, -95.8, 40.6, -89.1), 'MT': (44.4, -116.0, 49.0, -104.0),
    'NE': (40.0, -104.1, 43.0, -95.3), 'NV': (35.0, -120.0, 42.0, -114.0),
    'NH': (42.7, -72.6, 45.3, -70.7), 'NJ': (38.9, -75.6, 41.4, -73.9),
    'NM': (31.3, -109.1, 37.0, -103.0), 'NY': (40.5, -79.8, 45.0, -71.9),
    'NC': (33.8, -84.3, 36.6, -75.5), 'ND': (45.9, -104.0, 49.0, -96.6),
    'OH': (38.4, -84.8, 42.0, -80.5), 'OK': (33.6, -103.0, 37.0, -94.4),
    'OR': (42.0, -124.6, 46.3, -116.5), 'PA': (39.7, -80.5, 42.3, -74.7),
    'RI': (41.1, -71.9, 42.0, -71.1), 'SC': (32.0, -83.4, 35.2, -78.5),
    'SD': (42.5, -104.1, 46.0, -96.4), 'TN': (35.0, -90.3, 36.7, -81.6),
    'TX': (25.8, -106.6, 36.5, -93.5), 'UT': (37.0, -114.1, 42.0, -109.0),
    'VT': (42.7, -73.4, 45.0, -71.5),
    'WA': (45.5, -124.8, 49.0, -116.9), 'WV': (37.2, -82.6, 40.6, -77.7),
    'WI': (42.5, -92.9, 47.1, -86.8), 'WY': (41.0, -111.1, 45.0, -104.1),
    'DC': (38.8, -77.12, 38.99, -76.91),
}

# Priority order — check VA before MD, DC before both
STATE_CHECK_ORDER = ['DC', 'VA', 'MD'] + [k for k in STATE_BOUNDS if k not in ('DC', 'VA', 'MD')]

def lat_lng_to_state(lat, lng):
    if lat is None or lng is None:
        return None
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    for code in STATE_CHECK_ORDER:
        bounds = STATE_BOUNDS.get(code)
        if not bounds:
            continue
        min_lat, min_lng, max_lat, max_lng = bounds
        if min_lat <= lat <= max_lat and min_lng <= lng <= max_lng:
            return code
    return None


# ============================================================
# FIX 1: Texas full-name duplicate
# ============================================================
print("\n" + "="*60)
print("FIX 1: Texas full-name duplicate in facilities")
print("="*60)
cur.execute("SELECT state, COUNT(*) FROM facilities WHERE state IN ('Texas', 'texas') GROUP BY state")
rows = cur.fetchall()
if rows:
    for state, count in rows:
        print(f"  Found {count} facilities with state='{state}'")
    cur.execute("UPDATE facilities SET state = 'TX' WHERE LOWER(state) = 'texas'")
    print(f"  ✅ Fixed — all set to 'TX'")
else:
    print("  ✅ No duplicates found")


# ============================================================
# FIX 2: Ingest EIA gas pipelines
# ============================================================
print("\n" + "="*60)
print("FIX 2: Ingest EIA gas pipelines (3,289 records)")
print("="*60)

EIA_GAS_URL = "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0/query"

def fetch_gas_batch(offset=0, batch_size=2000):
    """Fetch a batch of gas pipeline features from EIA ArcGIS."""
    params = {
        'where': '1=1',
        'outFields': 'FID,TYPEPIPE,Operator,Status',
        'returnGeometry': 'true',
        'resultOffset': offset,
        'resultRecordCount': batch_size,
        'f': 'json',
        'outSR': '4326',
    }
    resp = requests.get(EIA_GAS_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get('features', [])

# Get total count first
resp = requests.get(EIA_GAS_URL, params={'where': '1=1', 'returnCountOnly': 'true', 'f': 'json'}, timeout=15)
total = resp.json().get('count', 0)
print(f"  EIA ArcGIS reports {total} gas pipeline records")

cur.execute("SELECT COUNT(*) FROM gas_pipelines")
existing = cur.fetchone()[0]
print(f"  Current gas_pipelines table: {existing} rows")

if existing >= total * 0.8:
    print("  ✅ Already populated, skipping")
else:
    # Clear and re-ingest
    if existing > 0:
        cur.execute("DELETE FROM gas_pipelines")
        print(f"  Cleared {existing} existing rows")

    inserted = 0
    offset = 0
    batch_size = 2000

    while offset < total + batch_size:
        print(f"  Fetching batch offset={offset}...", end=" ", flush=True)
        try:
            features = fetch_gas_batch(offset, batch_size)
            if not features:
                print("no more records")
                break

            batch_inserted = 0
            for feat in features:
                attrs = feat.get('attributes', {})
                geom = feat.get('geometry', {})
                operator = attrs.get('Operator', 'Unknown')
                typepipe = attrs.get('TYPEPIPE', 'Interstate')
                status = attrs.get('Status', 'Operating')
                fid = attrs.get('FID', 0)

                # Extract lat/lng from geometry
                lat = lng = None
                if geom:
                    if 'paths' in geom and geom['paths']:
                        path = geom['paths'][0]
                        mid = path[len(path) // 2] if path else None
                        if mid and len(mid) >= 2:
                            lng, lat = mid[0], mid[1]
                    elif 'x' in geom and 'y' in geom:
                        lng, lat = geom['x'], geom['y']

                if not lat or not lng:
                    continue

                state = lat_lng_to_state(lat, lng)
                pipe_type = 'interstate' if 'Interstate' in str(typepipe) else 'intrastate'

                try:
                    cur.execute("""
                        INSERT INTO gas_pipelines (name, operator, pipeline_type, status, lat, lng, state, country, source, source_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (source_id) DO NOTHING
                    """, (
                        f"{operator} ({typepipe})"[:200],
                        str(operator)[:100],
                        pipe_type,
                        'active' if 'operat' in str(status).lower() else str(status)[:50],
                        lat, lng,
                        state or '',
                        'US',
                        'eia',
                        f"eia_gas_{fid}"
                    ))
                    batch_inserted += 1
                except Exception as e:
                    pass  # Skip duplicates

            inserted += batch_inserted
            print(f"{len(features)} fetched, {batch_inserted} inserted (total: {inserted})")
            offset += batch_size
            time.sleep(0.5)  # Be nice to the API

        except Exception as e:
            print(f"error: {e}")
            break

    print(f"  ✅ Gas pipeline ingestion complete: {inserted} records")

# Verify
cur.execute("SELECT state, COUNT(*) FROM gas_pipelines WHERE state IS NOT NULL AND state != '' GROUP BY state ORDER BY count DESC LIMIT 15")
print("\n  Top states by gas pipeline count:")
for state, count in cur.fetchall():
    print(f"    {state}: {count}")


# ============================================================
# FIX 3: Expand fiber routes (30+ additional carrier routes)
# ============================================================
print("\n" + "="*60)
print("FIX 3: Expand fiber routes (additional carrier routes)")
print("="*60)

ADDITIONAL_ROUTES = [
    # More Zayo routes
    {'source_id': 'ZAYO-NYC-BOS', 'provider': 'Zayo', 'name': 'New York - Boston', 'start_location': 'New York, NY', 'end_location': 'Boston, MA', 'start_lat': 40.71, 'start_lng': -74.01, 'end_lat': 42.36, 'end_lng': -71.06, 'distance_miles': 215, 'fiber_count': 576},
    {'source_id': 'ZAYO-LA-SJ', 'provider': 'Zayo', 'name': 'Los Angeles - San Jose', 'start_location': 'Los Angeles, CA', 'end_location': 'San Jose, CA', 'start_lat': 34.05, 'start_lng': -118.24, 'end_lat': 37.34, 'end_lng': -121.89, 'distance_miles': 340, 'fiber_count': 432},
    {'source_id': 'ZAYO-SEA-PDX', 'provider': 'Zayo', 'name': 'Seattle - Portland', 'start_location': 'Seattle, WA', 'end_location': 'Portland, OR', 'start_lat': 47.61, 'start_lng': -122.33, 'end_lat': 45.52, 'end_lng': -122.68, 'distance_miles': 175, 'fiber_count': 432},
    {'source_id': 'ZAYO-DEN-KC', 'provider': 'Zayo', 'name': 'Denver - Kansas City', 'start_location': 'Denver, CO', 'end_location': 'Kansas City, MO', 'start_lat': 39.74, 'start_lng': -104.99, 'end_lat': 39.10, 'end_lng': -94.58, 'distance_miles': 600, 'fiber_count': 288},
    {'source_id': 'ZAYO-CHI-MSP', 'provider': 'Zayo', 'name': 'Chicago - Minneapolis', 'start_location': 'Chicago, IL', 'end_location': 'Minneapolis, MN', 'start_lat': 41.88, 'start_lng': -87.63, 'end_lat': 44.98, 'end_lng': -93.27, 'distance_miles': 410, 'fiber_count': 288},
    # Lumen additional
    {'source_id': 'LUMEN-ATL-MIA', 'provider': 'Lumen', 'name': 'Atlanta - Miami', 'start_location': 'Atlanta, GA', 'end_location': 'Miami, FL', 'start_lat': 33.75, 'start_lng': -84.39, 'end_lat': 25.76, 'end_lng': -80.19, 'distance_miles': 660, 'fiber_count': 432},
    {'source_id': 'LUMEN-DAL-HOU', 'provider': 'Lumen', 'name': 'Dallas - Houston', 'start_location': 'Dallas, TX', 'end_location': 'Houston, TX', 'start_lat': 32.78, 'start_lng': -96.80, 'end_lat': 29.76, 'end_lng': -95.37, 'distance_miles': 240, 'fiber_count': 576},
    {'source_id': 'LUMEN-CHI-COL', 'provider': 'Lumen', 'name': 'Chicago - Columbus', 'start_location': 'Chicago, IL', 'end_location': 'Columbus, OH', 'start_lat': 41.88, 'start_lng': -87.63, 'end_lat': 39.96, 'end_lng': -82.99, 'distance_miles': 350, 'fiber_count': 432},
    {'source_id': 'LUMEN-NYC-PHL', 'provider': 'Lumen', 'name': 'New York - Philadelphia', 'start_location': 'New York, NY', 'end_location': 'Philadelphia, PA', 'start_lat': 40.71, 'start_lng': -74.01, 'end_lat': 39.95, 'end_lng': -75.17, 'distance_miles': 95, 'fiber_count': 576},
    {'source_id': 'LUMEN-PHL-NOVA', 'provider': 'Lumen', 'name': 'Philadelphia - Northern Virginia', 'start_location': 'Philadelphia, PA', 'end_location': 'Ashburn, VA', 'start_lat': 39.95, 'start_lng': -75.17, 'end_lat': 38.95, 'end_lng': -77.45, 'distance_miles': 165, 'fiber_count': 576},
    {'source_id': 'LUMEN-SEA-PDX', 'provider': 'Lumen', 'name': 'Seattle - Portland', 'start_location': 'Seattle, WA', 'end_location': 'Portland, OR', 'start_lat': 47.61, 'start_lng': -122.33, 'end_lat': 45.52, 'end_lng': -122.68, 'distance_miles': 175, 'fiber_count': 288},
    # Crown Castle additional
    {'source_id': 'CC-NOVA-NYC', 'provider': 'Crown Castle', 'name': 'Northern Virginia - New York', 'start_location': 'Ashburn, VA', 'end_location': 'New York, NY', 'start_lat': 38.95, 'start_lng': -77.45, 'end_lat': 40.71, 'end_lng': -74.01, 'distance_miles': 250, 'fiber_count': 432},
    {'source_id': 'CC-CHI-IND', 'provider': 'Crown Castle', 'name': 'Chicago - Indianapolis', 'start_location': 'Chicago, IL', 'end_location': 'Indianapolis, IN', 'start_lat': 41.88, 'start_lng': -87.63, 'end_lat': 39.77, 'end_lng': -86.16, 'distance_miles': 185, 'fiber_count': 288},
    {'source_id': 'CC-DAL-SA', 'provider': 'Crown Castle', 'name': 'Dallas - San Antonio', 'start_location': 'Dallas, TX', 'end_location': 'San Antonio, TX', 'start_lat': 32.78, 'start_lng': -96.80, 'end_lat': 29.42, 'end_lng': -98.49, 'distance_miles': 275, 'fiber_count': 288},
    # Cogent additional
    {'source_id': 'COGENT-NYC-BOS', 'provider': 'Cogent', 'name': 'New York - Boston', 'start_location': 'New York, NY', 'end_location': 'Boston, MA', 'start_lat': 40.71, 'start_lng': -74.01, 'end_lat': 42.36, 'end_lng': -71.06, 'distance_miles': 215, 'fiber_count': 576},
    {'source_id': 'COGENT-LA-SF', 'provider': 'Cogent', 'name': 'Los Angeles - San Francisco', 'start_location': 'Los Angeles, CA', 'end_location': 'San Francisco, CA', 'start_lat': 34.05, 'start_lng': -118.24, 'end_lat': 37.77, 'end_lng': -122.42, 'distance_miles': 380, 'fiber_count': 576},
    {'source_id': 'COGENT-DAL-HOU', 'provider': 'Cogent', 'name': 'Dallas - Houston', 'start_location': 'Dallas, TX', 'end_location': 'Houston, TX', 'start_lat': 32.78, 'start_lng': -96.80, 'end_lat': 29.76, 'end_lng': -95.37, 'distance_miles': 240, 'fiber_count': 576},
    {'source_id': 'COGENT-ATL-MIA', 'provider': 'Cogent', 'name': 'Atlanta - Miami', 'start_location': 'Atlanta, GA', 'end_location': 'Miami, FL', 'start_lat': 33.75, 'start_lng': -84.39, 'end_lat': 25.76, 'end_lng': -80.19, 'distance_miles': 660, 'fiber_count': 576},
    # Windstream additional
    {'source_id': 'WINDSTREAM-ATL-CLT', 'provider': 'Windstream', 'name': 'Atlanta - Charlotte', 'start_location': 'Atlanta, GA', 'end_location': 'Charlotte, NC', 'start_lat': 33.75, 'start_lng': -84.39, 'end_lat': 35.23, 'end_lng': -80.84, 'distance_miles': 245, 'fiber_count': 288},
    {'source_id': 'WINDSTREAM-CLT-NOVA', 'provider': 'Windstream', 'name': 'Charlotte - Northern Virginia', 'start_location': 'Charlotte, NC', 'end_location': 'Ashburn, VA', 'start_lat': 35.23, 'start_lng': -80.84, 'end_lat': 38.95, 'end_lng': -77.45, 'distance_miles': 330, 'fiber_count': 288},
    # Segra additional
    {'source_id': 'SEGRA-CLT-ATL', 'provider': 'Segra', 'name': 'Charlotte - Atlanta', 'start_location': 'Charlotte, NC', 'end_location': 'Atlanta, GA', 'start_lat': 35.23, 'start_lng': -80.84, 'end_lat': 33.75, 'end_lng': -84.39, 'distance_miles': 245, 'fiber_count': 288},
    {'source_id': 'SEGRA-RDU-CLT', 'provider': 'Segra', 'name': 'Raleigh - Charlotte', 'start_location': 'Raleigh, NC', 'end_location': 'Charlotte, NC', 'start_lat': 35.78, 'start_lng': -78.64, 'end_lat': 35.23, 'end_lng': -80.84, 'distance_miles': 170, 'fiber_count': 288},
    # Uniti additional
    {'source_id': 'UNITI-DAL-HOU', 'provider': 'Uniti', 'name': 'Dallas - Houston', 'start_location': 'Dallas, TX', 'end_location': 'Houston, TX', 'start_lat': 32.78, 'start_lng': -96.80, 'end_lat': 29.76, 'end_lng': -95.37, 'distance_miles': 240, 'fiber_count': 144},
    {'source_id': 'UNITI-MEM-ATL', 'provider': 'Uniti', 'name': 'Memphis - Atlanta', 'start_location': 'Memphis, TN', 'end_location': 'Atlanta, GA', 'start_lat': 35.15, 'start_lng': -90.05, 'end_lat': 33.75, 'end_lng': -84.39, 'distance_miles': 390, 'fiber_count': 144},
    {'source_id': 'UNITI-BHM-ATL', 'provider': 'Uniti', 'name': 'Birmingham - Atlanta', 'start_location': 'Birmingham, AL', 'end_location': 'Atlanta, GA', 'start_lat': 33.52, 'start_lng': -86.81, 'end_lat': 33.75, 'end_lng': -84.39, 'distance_miles': 150, 'fiber_count': 144},
    # EXA / GTT
    {'source_id': 'GTT-NOVA-NYC', 'provider': 'GTT', 'name': 'Northern Virginia - New York', 'start_location': 'Ashburn, VA', 'end_location': 'New York, NY', 'start_lat': 38.95, 'start_lng': -77.45, 'end_lat': 40.71, 'end_lng': -74.01, 'distance_miles': 250, 'fiber_count': 432},
    {'source_id': 'GTT-NYC-CHI', 'provider': 'GTT', 'name': 'New York - Chicago', 'start_location': 'New York, NY', 'end_location': 'Chicago, IL', 'start_lat': 40.71, 'start_lng': -74.01, 'end_lat': 41.88, 'end_lng': -87.63, 'distance_miles': 790, 'fiber_count': 432},
    # Consolidated Communications
    {'source_id': 'CCI-MSP-CHI', 'provider': 'Consolidated', 'name': 'Minneapolis - Chicago', 'start_location': 'Minneapolis, MN', 'end_location': 'Chicago, IL', 'start_lat': 44.98, 'start_lng': -93.27, 'end_lat': 41.88, 'end_lng': -87.63, 'distance_miles': 410, 'fiber_count': 288},
    # Sparklight
    {'source_id': 'SPARK-PHX-TUC', 'provider': 'Sparklight', 'name': 'Phoenix - Tucson', 'start_location': 'Phoenix, AZ', 'end_location': 'Tucson, AZ', 'start_lat': 33.45, 'start_lng': -112.07, 'end_lat': 32.22, 'end_lng': -110.93, 'distance_miles': 115, 'fiber_count': 144},
    # FirstLight
    {'source_id': 'FL-BOS-ALB', 'provider': 'FirstLight', 'name': 'Boston - Albany', 'start_location': 'Boston, MA', 'end_location': 'Albany, NY', 'start_lat': 42.36, 'start_lng': -71.06, 'end_lat': 42.65, 'end_lng': -73.76, 'distance_miles': 175, 'fiber_count': 144},
]

cur.execute("SELECT COUNT(*) FROM fiber_routes WHERE start_lat IS NOT NULL")
existing_with_coords = cur.fetchone()[0]
print(f"  Current fiber routes with coordinates: {existing_with_coords}")

inserted_fiber = 0
for route in ADDITIONAL_ROUTES:
    try:
        cur.execute("""
            INSERT INTO fiber_routes (name, provider, route_type, start_location, end_location,
                start_lat, start_lng, end_lat, end_lng, distance_miles, fiber_count, status, source, source_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id) DO UPDATE SET
                start_lat = EXCLUDED.start_lat, start_lng = EXCLUDED.start_lng,
                end_lat = EXCLUDED.end_lat, end_lng = EXCLUDED.end_lng,
                distance_miles = EXCLUDED.distance_miles, fiber_count = EXCLUDED.fiber_count
        """, (
            route['name'], route['provider'], 'long-haul',
            route['start_location'], route['end_location'],
            route['start_lat'], route['start_lng'], route['end_lat'], route['end_lng'],
            route['distance_miles'], route['fiber_count'],
            'active', 'seed', route['source_id']
        ))
        inserted_fiber += 1
    except Exception as e:
        print(f"  ⚠️ Failed to insert {route['source_id']}: {e}")

print(f"  ✅ Inserted/updated {inserted_fiber} additional fiber routes")

# Verify
cur.execute("SELECT COUNT(*) FROM fiber_routes WHERE start_lat IS NOT NULL")
total_fiber = cur.fetchone()[0]
print(f"  Total fiber routes with coordinates: {total_fiber}")


# ============================================================
# SUMMARY
# ============================================================
print("\n" + "="*60)
print("INGESTION COMPLETE — Summary")
print("="*60)

cur.execute("SELECT COUNT(*) FROM gas_pipelines")
gas_count = cur.fetchone()[0]

cur.execute("SELECT COUNT(*) FROM fiber_routes WHERE start_lat IS NOT NULL")
fiber_count = cur.fetchone()[0]

cur.execute("SELECT COUNT(*) FROM facilities WHERE country = 'US' AND state IS NOT NULL AND LENGTH(state) = 2")
fac_count = cur.fetchone()[0]

cur.execute("SELECT COUNT(*) FROM capacity_pipeline WHERE LOWER(status) IN ('construction', 'under_construction', 'under construction')")
constr_count = cur.fetchone()[0]

print(f"  Gas pipelines:     {gas_count:,} records")
print(f"  Fiber routes:      {fiber_count} routes with coordinates")
print(f"  Facilities (US):   {fac_count:,} (state = 2-letter code)")
print(f"  Construction:      {constr_count} projects")
print(f"\n  Rankings API endpoints:")
print(f"    https://dchub.cloud/api/rankings/construction")
print(f"    https://dchub.cloud/api/rankings/power")
print(f"    https://dchub.cloud/api/rankings/gas")
print(f"    https://dchub.cloud/api/rankings/fiber")

conn.close()
print("\n✅ Done!")
