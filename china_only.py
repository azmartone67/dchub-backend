import psycopg2, psycopg2.extras, requests, time, os

conn = psycopg2.connect(os.environ['NEON_DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET statement_timeout = 0')

ENDPOINTS = ["https://overpass-api.de/api/interpreter","https://overpass.kumi.systems/api/interpreter"]

COLS = ['osm_id','osm_type','infra_type','country','iso_code','region','name','operator',
        'voltage_kv','cables','circuits','frequency_hz','substation_type','gas_substance',
        'location','start_date','lat','lon','fetched_at']
sql = ('INSERT INTO infrastructure (' + ','.join(COLS) +
       ') VALUES %s ON CONFLICT (osm_type, osm_id) DO NOTHING')

# China divided into 7 bounding boxes covering the whole country
CHINA_REGIONS = [
    ("China-Northeast", "CN-NE", "36,115,54,135"),
    ("China-North",     "CN-N",  "35,100,42,116"),
    ("China-East",      "CN-E",  "24,108,38,122"),
    ("China-South",     "CN-S",  "18,105,26,116"),
    ("China-Central",   "CN-C",  "25,100,36,112"),
    ("China-West",      "CN-W",  "22,96,36,106"),
    ("China-Northwest", "CN-NW", "35,73,50,100"),
]

def fetch(query):
    for ep in ENDPOINTS:
        for attempt in range(3):
            try:
                r = requests.post(ep, data={"data": query}, timeout=200,
                                  headers={"User-Agent": "China-Infra/1.0"})
                if r.status_code == 429:
                    print("  Rate limited, waiting 60s...")
                    time.sleep(60); continue
                r.raise_for_status()
                return r.json().get("elements", [])
            except Exception as e:
                print(f"  {ep[:35]} attempt {attempt+1}: {e}")
                time.sleep(20)
    return []

def to_row(el, country, iso):
    tags = el.get("tags", {}); center = el.get("center", {})
    p = tags.get("power",""); mm = tags.get("man_made",""); sub = tags.get("substance","")
    if p == "line": it = "transmission_line"
    elif p == "substation": it = "substation"
    elif mm == "pipeline" and "gas" in sub.lower(): it = "gas_pipeline"
    else: return None
    try:
        v = int(tags.get("voltage","").split(";")[0].replace(",","").strip())
        vkv = None if v > 2000000000 else v
    except: vkv = None
    return (el.get("id"), el.get("type",""), it, country, iso, "APAC",
            tags.get("name",""), tags.get("operator",""), vkv,
            tags.get("cables",""), tags.get("circuits",""), tags.get("frequency",""),
            tags.get("substation",""), tags.get("substance",""), tags.get("location",""),
            tags.get("start_date",""), center.get("lat") or el.get("lat"),
            center.get("lon") or el.get("lon"), "2026-04-09T00:00:00Z")

grand_total = 0
for name, iso, bbox in CHINA_REGIONS:
    print(f"{name} ({bbox})...")
    q = f"""[out:json][timeout:150];
(way["power"="line"]["voltage"~"[0-9]{{5,}}"]({bbox});
node["power"="substation"]({bbox});
way["power"="substation"]({bbox});
way["man_made"="pipeline"]["substance"~"gas"]({bbox}););
out center tags;"""
    els = fetch(q)
    rows = [r for el in els if (r := to_row(el, name, iso))]
    if rows:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=2000)
    grand_total += len(rows)
    print(f"  → {len(rows):,} records (total: {grand_total:,})")
    time.sleep(8)

print(f"\nChina total: {grand_total:,}")
cur.execute('SELECT COUNT(*) FROM infrastructure')
print(f"Grand total in Neon: {cur.fetchone()[0]:,}")
conn.close()
