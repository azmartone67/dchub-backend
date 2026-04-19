"""
fix_transmission_state.py — Backfill state + length_miles
==========================================================
Parallel offset-based fetch from HIFLD with geometry, derives
state from midpoint, bulk-updates Neon.

Usage:
  python3 fix_transmission_state.py
"""

import psycopg2, psycopg2.extras, requests, time, os
from concurrent.futures import ThreadPoolExecutor, as_completed

conn = psycopg2.connect(os.environ['NEON_DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET statement_timeout = 0')

BASE_URL = (
    "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/"
    "Electric_Power_Transmission_Lines/FeatureServer/0/query"
)
METERS_PER_MILE = 1609.344
PAGE      = 1000   # smaller = safer for geometry payloads
WORKERS   = 4

# ── Step 1: State polygons ────────────────────────────────────────────────────
print("Fetching US state boundaries (WGS84)...")
sresp = requests.get(
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/0/query",
    params={"where":"1=1","outFields":"STUSAB","returnGeometry":"true",
            "outSR":"4326","geometryPrecision":"4","f":"json","resultRecordCount":"60"},
    timeout=60
).json()

states_polys = []
for feat in sresp.get("features", []):
    abbr  = feat["attributes"]["STUSAB"]
    rings = (feat.get("geometry") or {}).get("rings", [])
    if rings:
        states_polys.append((abbr, rings[0]))
print(f"Loaded {len(states_polys)} state polygons")

# Sanity check — a point in Texas should resolve
def point_in_poly(px, py, poly):
    n, inside, j = len(poly), False, len(poly)-1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if ((yi > py) != (yj > py)) and (px < (xj-xi)*(py-yi)/(yj-yi+1e-12)+xi):
            inside = not inside
        j = i
    return inside

def get_state(lon, lat):
    if lon is None or lat is None: return None
    for abbr, ring in states_polys:
        if point_in_poly(lon, lat, ring): return abbr
    return None

# Quick sanity: Austin TX (-97.74, 30.27) → should be TX
test = get_state(-97.74, 30.27)
print(f"Sanity check — Austin TX → {test}  (expected TX)")
if test != "TX":
    print("WARNING: state lookup may be broken — check CRS of TIGER response")

# ── Step 2: How many total pages? ─────────────────────────────────────────────
cur.execute("SELECT COUNT(*) FROM transmission_lines")
total = cur.fetchone()[0]
pages = (total + PAGE - 1) // PAGE
print(f"\n{total:,} rows → {pages} pages of {PAGE} | {WORKERS} workers\n")

def fetch_page(offset):
    params = {
        "where":             "1=1",
        "outFields":         "OBJECTID,Shape__Length",
        "returnGeometry":    "true",
        "geometryType":      "esriGeometryPolyline",
        "outSR":             "4326",
        "geometryPrecision": "5",
        "f":                 "json",
        "resultOffset":      offset,
        "resultRecordCount": PAGE,
        "returnGeometry":    "true",
    }
    for attempt in range(4):
        try:
            r = requests.get(BASE_URL, params=params, timeout=120)
            data = r.json()
            if "error" in data:
                print(f"  API error offset={offset}: {data['error']}")
                time.sleep(20)
                continue
            return offset, data.get("features", [])
        except Exception as e:
            print(f"  Error offset={offset} attempt {attempt+1}: {e}")
            time.sleep(10 * (attempt+1))
    return offset, []

def process_and_update(offset, features):
    rows = []
    for feat in features:
        oid       = feat["attributes"].get("OBJECTID")
        shape_len = feat["attributes"].get("Shape__Length")
        miles     = round(shape_len / METERS_PER_MILE, 3) if shape_len else None
        paths     = (feat.get("geometry") or {}).get("paths", [])
        lon = lat = None
        if paths and paths[0]:
            coord   = paths[0][len(paths[0]) // 2]
            lon, lat = coord[0], coord[1]
        state = get_state(lon, lat)
        rows.append((miles, state, str(oid)))

    if rows:
        psycopg2.extras.execute_values(
            cur,
            "UPDATE transmission_lines SET length_miles=d.m, state=d.s "
            "FROM (VALUES %s) AS d(m,s,id) "
            "WHERE transmission_lines.hifld_id = d.id",
            rows,
            template="(%s::double precision, %s::varchar, %s::text)",
            page_size=2000,
        )
    return len(rows)

# ── Step 3: Parallel fetch, sequential update ─────────────────────────────────
offsets = list(range(0, total, PAGE))
grand   = 0

with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    futs = {ex.submit(fetch_page, off): off for off in offsets}
    buf  = {}          # offset → features, held until it's their turn
    next_off = 0

    for fut in as_completed(futs):
        offset, features = fut.result()
        buf[offset] = features

        # Flush in-order so DB writes stay sequential
        while next_off in buf:
            feats = buf.pop(next_off)
            n = process_and_update(next_off, feats)
            grand += n
            print(f"  offset {next_off:>6} — {n:>4} rows | total: {grand:,}")
            next_off += PAGE

print(f"\nDone — {grand:,} rows processed")

# ── Verify ────────────────────────────────────────────────────────────────────
cur.execute("""
    SELECT state, COUNT(*) AS n
    FROM transmission_lines WHERE state IS NOT NULL
    GROUP BY state ORDER BY n DESC LIMIT 15
""")
rows = cur.fetchall()
matched = sum(r[1] for r in rows)
print(f"\nTop 15 states ({matched:,} with state):")
for r in rows: print(f"  {r[0]}: {r[1]:,}")

cur.execute("SELECT COUNT(*) FROM transmission_lines WHERE state IS NULL")
print(f"\nNULL state (offshore/territories): {cur.fetchone()[0]:,}")

cur.execute("SELECT ROUND(AVG(length_miles)::numeric,1), MAX(length_miles) FROM transmission_lines WHERE length_miles IS NOT NULL")
avg, mx = cur.fetchone()
print(f"Avg length: {avg} mi | Longest: {mx:.1f} mi")

conn.close()
