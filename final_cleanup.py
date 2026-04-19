import psycopg2, psycopg2.extras, requests, time, os

conn = psycopg2.connect(os.environ['NEON_DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET statement_timeout = 0')

ENDPOINT = "https://overpass-api.de/api/interpreter"
ENDPOINT2 = "https://overpass.kumi.systems/api/interpreter"

COLS = ['osm_id','osm_type','infra_type','country','iso_code','region','name','operator',
        'voltage_kv','cables','circuits','frequency_hz','substation_type','gas_substance',
        'location','start_date','lat','lon','fetched_at']
sql = ('INSERT INTO infrastructure (' + ','.join(COLS) +
       ') VALUES %s ON CONFLICT (osm_type, osm_id) DO NOTHING')

def fetch(query):
    for ep in [ENDPOINT, ENDPOINT2]:
        for attempt in range(3):
            try:
                r = requests.post(ep, data={"data": query}, timeout=200,
                                  headers={"User-Agent": "Infra-Final/1.0"})
                if r.status_code == 429:
                    time.sleep(60); continue
                r.raise_for_status()
                return r.json().get("elements", [])
            except Exception as e:
                print(f"  {ep[:35]} attempt {attempt+1}: {e}")
                time.sleep(15)
    return []

def to_row(el, country, iso, region):
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
    return (el.get("id"), el.get("type",""), it, country, iso, region,
            tags.get("name",""), tags.get("operator",""), vkv,
            tags.get("cables",""), tags.get("circuits",""), tags.get("frequency",""),
            tags.get("substation",""), tags.get("substance",""), tags.get("location",""),
            tags.get("start_date",""), center.get("lat") or el.get("lat"),
            center.get("lon") or el.get("lon"), "2026-04-09T00:00:00Z")

def run(name, iso, region, query):
    els = fetch(query)
    rows = [r for el in els if (r := to_row(el, name, iso, region))]
    if rows: psycopg2.extras.execute_values(cur, sql, rows, page_size=2000)
    print(f"  {name}: {len(rows):,}")
    return len(rows)

total = 0

# Missing countries
print("=== Missing countries ===")
for name, iso, region in [("Bahrain","BH","EMEA"),("Iran","IR","EMEA"),
                           ("Myanmar","MM","APAC"),("Nepal","NP","APAC"),("Brunei","BN","APAC")]:
    q = f"""[out:json][timeout:150];
rel["ISO3166-1:alpha2"="{iso}"]["admin_level"="2"];
map_to_area->.c;
(way["power"="line"]["voltage"~"[0-9]{{5,}}"](area.c);
node["power"="substation"](area.c);way["power"="substation"](area.c);
way["man_made"="pipeline"]["substance"~"gas"](area.c););
out center tags;"""
    total += run(name, iso, region, q)
    time.sleep(5)

# China — use bounding boxes per region instead of province names
print("\n=== China by region ===")
CHINA_BBOX = [
    ("China-North",    "CN-N",  [("37,105,43,117"),("38,111,43,122")]),
    ("China-Northeast","CN-NE", [("38,118,53,135")]),
    ("China-East",     "CN-E",  [("24,108,38,123")]),
    ("China-South",    "CN-S",  [("18,105,26,117")]),
    ("China-Central",  "CN-C",  [("25,100,35,112")]),
    ("China-West",     "CN-W",  [("26,78,40,104")]),
    ("China-Northwest","CN-NW", [("35,73,49,106")]),
]
for name, iso, bboxes in CHINA_BBOX:
    count = 0
    for bbox in bboxes:
        s,w,n,e = bbox.split(",")
        q = f"""[out:json][timeout:150];
(way["power"="line"]["voltage"~"[0-9]{{5,}}"]({bbox});
node["power"="substation"]({bbox});way["power"="substation"]({bbox});
way["man_made"="pipeline"]["substance"~"gas"]({bbox}););
out center tags;"""
        els = fetch(q)
        rows = [r for el in els if (r := to_row(el, name, iso, "APAC"))]
        if rows: psycopg2.extras.execute_values(cur, sql, rows, page_size=2000)
        count += len(rows)
        time.sleep(5)
    print(f"  {name}: {count:,}")
    total += count

print(f"\nTotal added: {total:,}")
cur.execute('SELECT COUNT(*) FROM infrastructure')
print(f"Grand total in Neon: {cur.fetchone()[0]:,}")
conn.close()
