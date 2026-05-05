"""
load_hifld_transmission.py — US Transmission Lines from HIFLD
==============================================================
Pulls the full HIFLD Electric Power Transmission Lines dataset
(public ArcGIS REST API, no key required) and loads into Neon.

Usage (Replit shell):
  export NEON_DATABASE_URL='postgresql://neondb_owner:...@ep-old-waterfall-aa2rwjzs.westus3.azure.neon.tech/neondb?sslmode=require'
  python3 load_hifld_transmission.py
"""

import psycopg2, psycopg2.extras, requests, time, os, json

BASE_URL = (
    "https://services1.arcgis.com/Hp6G80Pky0om7QvQ/arcgis/rest/services/"
    "Electric_Power_Transmission_Lines/FeatureServer/0"
)

# ── Step 1: discover actual field names ──────────────────────────────────────
print("Fetching service metadata...")
meta = requests.get(f"{BASE_URL}?f=json", timeout=30).json()
all_fields = [f["name"] for f in meta.get("fields", [])]
print(f"Available fields ({len(all_fields)}): {all_fields}")

# Map desired columns to whatever the service actually calls them
# (HIFLD field names vary slightly between vintages)
FIELD_MAP = {
    "hifld_id":     ["OBJECTID"],
    "name":         ["NAME", "LINE_NAME"],
    "operator":     ["OWNER", "OPERATOR"],
    "voltage_kv":   ["VOLTAGE", "KV"],
    "from_sub":     ["SUB_1", "FROM_SUB", "FROM_SUBSTATION"],
    "to_sub":       ["SUB_2", "TO_SUB", "TO_SUBSTATION"],
    "length_miles": ["MILES", "SHAPE_Length", "LENGTH_MI"],
    "state":        ["STATE", "STATE_NAME", "ST"],
    "status":       ["STATUS"],
    "line_type":    ["TYPE", "LINE_TYPE"],
}

def pick_field(candidates):
    for c in candidates:
        if c in all_fields:
            return c
    return None

resolved = {col: pick_field(candidates) for col, candidates in FIELD_MAP.items()}
print("\nField mapping:")
for col, field in resolved.items():
    print(f"  {col:<15} → {field}")

missing = [col for col, field in resolved.items() if field is None]
if missing:
    print(f"\nWARNING: Could not find fields for: {missing}")

out_fields = ",".join(f for f in resolved.values() if f)

# ── Step 2: connect to Neon ──────────────────────────────────────────────────
conn = psycopg2.connect(os.environ['NEON_DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET statement_timeout = 0')
cur.execute('TRUNCATE TABLE transmission_lines RESTART IDENTITY')
print("\nTable truncated, starting load...")

SQL = """
INSERT INTO transmission_lines
  (hifld_id, name, operator, voltage_kv, from_sub, to_sub,
   length_miles, state, status, line_type, source, last_updated)
VALUES %s
ON CONFLICT DO NOTHING
"""

def safe_float(v):
    try:
        f = float(v)
        return None if f < 0 else f
    except:
        return None

def get_val(attrs, col):
    field = resolved.get(col)
    return attrs.get(field) if field else None

def fetch_page(offset):
    params = {
        "where":             "1=1",
        "outFields":         out_fields,
        "f":                 "json",
        "resultOffset":      offset,
        "resultRecordCount": 2000,
        "returnGeometry":    "false",
    }
    for attempt in range(4):
        try:
            r = requests.get(f"{BASE_URL}/query", params=params, timeout=90)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                print(f"  API error: {data['error']}")
                print(f"  Params used: {params}")
                return []
            return data.get("features", [])
        except Exception as e:
            print(f"  Attempt {attempt+1} failed at offset {offset}: {e}")
            time.sleep(10 * (attempt + 1))
    return []

grand_total = 0
offset = 0

while True:
    features = fetch_page(offset)
    if not features:
        break

    rows = []
    for f in features:
        a = f.get("attributes", {})
        rows.append((
            get_val(a, "hifld_id"),
            str(get_val(a, "name") or "")[:255] or None,
            str(get_val(a, "operator") or "")[:255] or None,
            safe_float(get_val(a, "voltage_kv")),
            str(get_val(a, "from_sub") or "")[:255] or None,
            str(get_val(a, "to_sub") or "")[:255] or None,
            safe_float(get_val(a, "length_miles")),
            str(get_val(a, "state") or "")[:2] or None,
            str(get_val(a, "status") or "")[:64] or None,
            str(get_val(a, "line_type") or "")[:128] or None,
            "HIFLD",
            None,
        ))

    if rows:
        psycopg2.extras.execute_values(cur, SQL, rows, page_size=2000)
        grand_total += len(rows)

    print(f"  Offset {offset:>6} — fetched {len(features):,}  |  total: {grand_total:,}")

    if len(features) < 2000:
        break

    offset += 2000
    time.sleep(1)

print(f"\nDone — {grand_total:,} transmission lines loaded.")

cur.execute("SELECT COUNT(*), MIN(voltage_kv), MAX(voltage_kv) FROM transmission_lines")
cnt, mn, mx = cur.fetchone()
print(f"DB count: {cnt:,}  |  voltage range: {mn} – {mx} kV")

cur.execute("""
    SELECT state, COUNT(*) AS lines
    FROM transmission_lines
    GROUP BY state ORDER BY lines DESC LIMIT 10
""")
print("\nTop 10 states:")
for row in cur.fetchall():
    print(f"  {row[0]}: {row[1]:,}")

conn.close()
