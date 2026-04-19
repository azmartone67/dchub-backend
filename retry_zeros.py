import psycopg2, psycopg2.extras, requests, time, os

conn = psycopg2.connect(os.environ['NEON_DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET statement_timeout = 0')

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

ZEROS = [
    ("Lithuania","LT","EMEA"), ("Serbia","RS","EMEA"), ("Slovenia","SI","EMEA"),
    ("Estonia","EE","EMEA"), ("Moldova","MD","EMEA"), ("Kenya","KE","EMEA"),
    ("Ethiopia","ET","EMEA"), ("Nigeria","NG","EMEA"), ("Zimbabwe","ZW","EMEA"),
    ("Tanzania","TZ","EMEA"), ("Qatar","QA","EMEA"), ("Zambia","ZM","EMAC"),
    ("Kuwait","KW","EMEA"), ("Bahrain","BH","EMEA"), ("Iran","IR","EMEA"),
    ("Myanmar","MM","APAC"), ("Nepal","NP","APAC"), ("Brunei","BN","APAC"),
]

COLS = ['osm_id','osm_type','infra_type','country','iso_code','region','name','operator',
        'voltage_kv','cables','circuits','frequency_hz','substation_type','gas_substance',
        'location','start_date','lat','lon','fetched_at']

sql = ('INSERT INTO infrastructure (' + ','.join(COLS) +
       ') VALUES %s ON CONFLICT (osm_type, osm_id) DO NOTHING')

def fetch(iso):
    q = f"""[out:json][timeout:150];
rel["ISO3166-1:alpha2"="{iso}"]["admin_level"="2"];
map_to_area->.c;
(
  way["power"="line"]["voltage"~"[0-9]{{5,}}"](area.c);
  node["power"="substation"](area.c);
  way["power"="substation"](area.c);
  way["man_made"="pipeline"]["substance"~"gas"](area.c);
);
out center tags;"""
    for endpoint in ENDPOINTS:
        for attempt in range(3):
            try:
                r = requests.post(endpoint, data={"data": q}, timeout=180,
                                  headers={"User-Agent": "Infra-Retry/1.0"})
                if r.status_code == 429:
                    print(f"  Rate limited, waiting 60s...")
                    time.sleep(60)
                    continue
                r.raise_for_status()
                return r.json().get("elements", [])
            except Exception as e:
                print(f"  {endpoint[:35]} attempt {attempt+1}: {e}")
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

grand_total = 0
for country, iso, region in ZEROS:
    print(f"{country} ({iso})...")
    els = fetch(iso)
    rows = [r for el in els if (r := to_row(el, country, iso, region))]
    if rows:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=2000)
    grand_total += len(rows)
    print(f"  → {len(rows):,} records (total: {grand_total:,})")
    time.sleep(5)

print(f"\nRetry complete — {grand_total:,} records added")
cur.execute('SELECT COUNT(*) FROM infrastructure')
print(f"Grand total in Neon: {cur.fetchone()[0]:,}")
conn.close()
