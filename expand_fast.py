import psycopg2, psycopg2.extras, requests, time, os
from concurrent.futures import ThreadPoolExecutor, as_completed

conn = psycopg2.connect(os.environ['NEON_DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET statement_timeout = 0')

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

COUNTRIES = [
    ("Greece","GR","EMEA"), ("Bulgaria","BG","EMEA"), ("Serbia","RS","EMEA"),
    ("Croatia","HR","EMEA"), ("Slovakia","SK","EMEA"), ("Slovenia","SI","EMEA"),
    ("Lithuania","LT","EMEA"), ("Latvia","LV","EMEA"), ("Estonia","EE","EMEA"),
    ("Belarus","BY","EMEA"), ("Ukraine","UA","EMEA"), ("Moldova","MD","EMEA"),
    ("Morocco","MA","EMEA"), ("Egypt","EG","EMEA"), ("Nigeria","NG","EMEA"),
    ("Kenya","KE","EMEA"), ("Ghana","GH","EMEA"), ("Tanzania","TZ","EMEA"),
    ("Ethiopia","ET","EMEA"), ("Mozambique","MZ","EMEA"), ("Zambia","ZM","EMEA"),
    ("Zimbabwe","ZW","EMEA"), ("Jordan","JO","EMEA"), ("Kuwait","KW","EMEA"),
    ("Qatar","QA","EMEA"), ("Oman","OM","EMEA"), ("Bahrain","BH","EMEA"),
    ("Pakistan","PK","EMEA"), ("Iraq","IQ","EMEA"), ("Iran","IR","EMEA"),
    ("Bangladesh","BD","APAC"), ("Sri Lanka","LK","APAC"), ("Myanmar","MM","APAC"),
    ("Cambodia","KH","APAC"), ("Laos","LA","APAC"), ("Nepal","NP","APAC"),
    ("Mongolia","MN","APAC"), ("Papua New Guinea","PG","APAC"),
    ("Brunei","BN","APAC"), ("Timor-Leste","TL","APAC"),
]

CHINA_PROVINCES = [
    ("China-Beijing","CN-BJ","APAC","Beijing"),
    ("China-Shanghai","CN-SH","APAC","Shanghai"),
    ("China-Guangdong","CN-GD","APAC","Guangdong"),
    ("China-Sichuan","CN-SC","APAC","Sichuan"),
    ("China-Jiangsu","CN-JS","APAC","Jiangsu"),
    ("China-Zhejiang","CN-ZJ","APAC","Zhejiang"),
    ("China-Shandong","CN-SD","APAC","Shandong"),
    ("China-Henan","CN-HA","APAC","Henan"),
    ("China-Hubei","CN-HB","APAC","Hubei"),
    ("China-Hunan","CN-HN","APAC","Hunan"),
    ("China-Anhui","CN-AH","APAC","Anhui"),
    ("China-Fujian","CN-FJ","APAC","Fujian"),
    ("China-Liaoning","CN-LN","APAC","Liaoning"),
    ("China-Heilongjiang","CN-HL","APAC","Heilongjiang"),
    ("China-Jilin","CN-JL","APAC","Jilin"),
    ("China-Shaanxi","CN-SN","APAC","Shaanxi"),
    ("China-Yunnan","CN-YN","APAC","Yunnan"),
    ("China-Xinjiang","CN-XJ","APAC","Xinjiang"),
    ("China-Inner Mongolia","CN-NM","APAC","Inner Mongolia"),
    ("China-Tibet","CN-XZ","APAC","Tibet"),
]

COLS = ['osm_id','osm_type','infra_type','country','iso_code','region','name','operator',
        'voltage_kv','cables','circuits','frequency_hz','substation_type','gas_substance',
        'location','start_date','lat','lon','fetched_at']

sql = ('INSERT INTO infrastructure (' + ','.join(COLS) +
       ') VALUES %s ON CONFLICT (osm_type, osm_id) DO NOTHING')

def build_query(iso, timeout=150):
    return f"""[out:json][timeout:{timeout}];
rel["ISO3166-1:alpha2"="{iso}"]["admin_level"="2"];
map_to_area->.c;
(
  way["power"="line"]["voltage"~"[0-9]{{5,}}"](area.c);
  node["power"="substation"](area.c);
  way["power"="substation"](area.c);
  way["man_made"="pipeline"]["substance"~"gas"](area.c);
);
out center tags;"""

def build_province_query(province_name, timeout=120):
    return f"""[out:json][timeout:{timeout}];
area["name:en"="{province_name}"]["admin_level"="4"]->.c;
(
  way["power"="line"]["voltage"~"[0-9]{{5,}}"](area.c);
  node["power"="substation"](area.c);
  way["power"="substation"](area.c);
  way["man_made"="pipeline"]["substance"~"gas"](area.c);
);
out center tags;"""

def call_overpass(query, endpoint, timeout=180):
    for attempt in range(3):
        try:
            r = requests.post(endpoint, data={"data": query},
                              timeout=timeout,
                              headers={"User-Agent": "EMEA-APAC-Infra-Fetcher/2.0"})
            if r.status_code == 429:
                time.sleep(60)
                continue
            r.raise_for_status()
            return r.json().get("elements", [])
        except Exception as e:
            print(f"    {endpoint[:40]} attempt {attempt+1}: {e}")
            time.sleep(15)
    return []

def to_row(el, country, iso, region):
    tags = el.get("tags", {})
    center = el.get("center", {})
    power, man_made, substance = tags.get("power",""), tags.get("man_made",""), tags.get("substance","")
    if power == "line": itype = "transmission_line"
    elif power == "substation": itype = "substation"
    elif man_made == "pipeline" and "gas" in substance.lower(): itype = "gas_pipeline"
    else: return None
    try:
        v = int(tags.get("voltage","").split(";")[0].replace(",","").strip())
        vkv = None if v > 2000000000 else v
    except: vkv = None
    return (el.get("id"), el.get("type",""), itype, country, iso, region,
            tags.get("name",""), tags.get("operator",""), vkv,
            tags.get("cables",""), tags.get("circuits",""), tags.get("frequency",""),
            tags.get("substation",""), tags.get("substance",""), tags.get("location",""),
            tags.get("start_date",""),
            center.get("lat") or el.get("lat"),
            center.get("lon") or el.get("lon"),
            "2026-04-09T00:00:00Z")

def fetch_and_insert(args):
    country, iso, region, query, endpoint = args
    elements = call_overpass(query, endpoint)
    rows = [r for el in elements if (r := to_row(el, country, iso, region))]
    if rows:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=2000)
    return country, len(rows)

# Build all jobs, round-robin across 3 endpoints
jobs = []
for i, (country, iso, region) in enumerate(COUNTRIES):
    jobs.append((country, iso, region, build_query(iso), ENDPOINTS[i % 3]))

for i, (country, iso, region, province_name) in enumerate(CHINA_PROVINCES):
    jobs.append((country, iso, region, build_province_query(province_name), ENDPOINTS[i % 3]))

print(f"Running {len(jobs)} jobs across 3 Overpass endpoints in parallel...")
grand_total = 0

# 3 workers = 3 parallel fetches, one per endpoint
with ThreadPoolExecutor(max_workers=3) as ex:
    futures = {ex.submit(fetch_and_insert, job): job for job in jobs}
    for future in as_completed(futures):
        country, count = future.result()
        grand_total += count
        print(f"  ✅ {country:<25} {count:,} records  (total: {grand_total:,})")

print(f"\nDone — {grand_total:,} new records added")
cur.execute('SELECT COUNT(*) FROM infrastructure')
print(f"Grand total in Neon: {cur.fetchone()[0]:,}")
conn.close()
