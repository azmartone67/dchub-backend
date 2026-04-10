import psycopg2, psycopg2.extras, requests, csv, time, os

conn = psycopg2.connect(os.environ['NEON_DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET statement_timeout = 0')

MISSED = [
    ("Germany","DE","EMEA"), ("France","FR","EMEA"), ("Italy","IT","EMEA"),
    ("Japan","JP","APAC"), ("India","IN","APAC"),
    ("Hong Kong","HK","APAC"), ("Indonesia","ID","APAC"),
]

OVERPASS = "https://overpass-api.de/api/interpreter"

COLS = ['osm_id','osm_type','infra_type','country','iso_code','region','name','operator',
        'voltage_kv','cables','circuits','frequency_hz','substation_type','gas_substance',
        'location','start_date','lat','lon','fetched_at']

sql = ('INSERT INTO infrastructure (' + ','.join(COLS) +
       ') VALUES %s ON CONFLICT (osm_type, osm_id) DO NOTHING')

def fetch(iso, qtype, timeout=180):
    queries = {
        'lines': f'[out:json][timeout:{timeout}];\nrel["ISO3166-1:alpha2"="{iso}"]["admin_level"="2"];\nmap_to_area->.c;\nway["power"="line"]["voltage"~"[0-9]{{5,}}"](area.c);\nout center tags;',
        'subs':  f'[out:json][timeout:{timeout}];\nrel["ISO3166-1:alpha2"="{iso}"]["admin_level"="2"];\nmap_to_area->.c;\n(node["power"="substation"](area.c);way["power"="substation"](area.c););\nout center tags;',
        'gas':   f'[out:json][timeout:{timeout}];\nrel["ISO3166-1:alpha2"="{iso}"]["admin_level"="2"];\nmap_to_area->.c;\nway["man_made"="pipeline"]["substance"~"gas"](area.c);\nout center tags;',
    }
    els = []
    for name, q in queries.items():
        for attempt in range(3):
            try:
                r = requests.post(OVERPASS, data={"data": q}, timeout=220)
                r.raise_for_status()
                els += r.json().get("elements", [])
                print(f'    {name}: {len(r.json().get("elements",[]))} elements')
                time.sleep(5)
                break
            except Exception as e:
                print(f'    {name} attempt {attempt+1} failed: {e}')
                time.sleep(30)
    return els

def to_row(el, country, iso, region):
    tags = el.get("tags", {})
    center = el.get("center", {})
    power, man_made, substance = tags.get("power",""), tags.get("man_made",""), tags.get("substance","")
    if power == "line": itype = "transmission_line"
    elif power == "substation": itype = "substation"
    elif man_made == "pipeline" and "gas" in substance.lower(): itype = "gas_pipeline"
    else: return None
    raw_v = tags.get("voltage","")
    try:
        v = int(raw_v.split(";")[0].replace(",","").strip())
        vkv = None if v > 2000000000 else v
    except: vkv = None
    return (el.get("id"), el.get("type",""), itype, country, iso, region,
            tags.get("name",""), tags.get("operator",""), vkv,
            tags.get("cables",""), tags.get("circuits",""), tags.get("frequency",""),
            tags.get("substation",""), tags.get("substance",""), tags.get("location",""),
            tags.get("start_date",""), center.get("lat") or el.get("lat"),
            center.get("lon") or el.get("lon"), "2026-04-09T00:00:00Z")

total_added = 0
for country, iso, region in MISSED:
    print(f'\nFetching {country} ({iso})...')
    elements = fetch(iso, None)
    rows = [r for el in elements if (r := to_row(el, country, iso, region))]
    if rows:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=2000)
        total_added += len(rows)
        print(f'  → {len(rows):,} records inserted')
    time.sleep(5)

print(f'\nTotal added: {total_added:,}')
cur.execute('SELECT COUNT(*) FROM infrastructure')
print(f'Grand total in Neon: {cur.fetchone()[0]:,}')
conn.close()
