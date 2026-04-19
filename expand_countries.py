"""
Expansion script — additional EMEA + APAC countries
Run in a separate shell while retry_big.py is still going.
China is split by province to avoid timeouts.

Usage:
  export NEON_DATABASE_URL='postgresql://neondb_owner:...@ep-old-waterfall-aa2rwjzs.westus3.azure.neon.tech/neondb?sslmode=require'
  python3 expand_countries.py
"""

import psycopg2, psycopg2.extras, requests, time, os

conn = psycopg2.connect(os.environ['NEON_DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET statement_timeout = 0')

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

# ── Countries to add ─────────────────────────────────────────────────────────
EXPANSION_COUNTRIES = [
    # Eastern Europe
    ("Greece",      "GR", "EMEA"),
    ("Bulgaria",    "BG", "EMEA"),
    ("Serbia",      "RS", "EMEA"),
    ("Croatia",     "HR", "EMEA"),
    ("Slovakia",    "SK", "EMEA"),
    ("Slovenia",    "SI", "EMEA"),
    ("Lithuania",   "LT", "EMEA"),
    ("Latvia",      "LV", "EMEA"),
    ("Estonia",     "EE", "EMEA"),
    ("Belarus",     "BY", "EMEA"),
    ("Ukraine",     "UA", "EMEA"),
    ("Moldova",     "MD", "EMEA"),
    # Africa
    ("Morocco",     "MA", "EMEA"),
    ("Egypt",       "EG", "EMEA"),
    ("Nigeria",     "NG", "EMEA"),
    ("Kenya",       "KE", "EMEA"),
    ("Ghana",       "GH", "EMEA"),
    ("Tanzania",    "TZ", "EMEA"),
    ("Ethiopia",    "ET", "EMEA"),
    ("Mozambique",  "MZ", "EMEA"),
    ("Zambia",      "ZM", "EMEA"),
    ("Zimbabwe",    "ZW", "EMEA"),
    # Middle East
    ("Jordan",      "JO", "EMEA"),
    ("Kuwait",      "KW", "EMEA"),
    ("Qatar",       "QA", "EMEA"),
    ("Oman",        "OM", "EMEA"),
    ("Bahrain",     "BH", "EMEA"),
    ("Pakistan",    "PK", "EMEA"),
    ("Iraq",        "IQ", "EMEA"),
    ("Iran",        "IR", "EMEA"),
    # APAC
    ("Bangladesh",  "BD", "APAC"),
    ("Sri Lanka",   "LK", "APAC"),
    ("Myanmar",     "MM", "APAC"),
    ("Cambodia",    "KH", "APAC"),
    ("Laos",        "LA", "APAC"),
    ("Nepal",       "NP", "APAC"),
    ("Mongolia",    "MN", "APAC"),
    ("Papua New Guinea", "PG", "APAC"),
    ("Brunei",      "BN", "APAC"),
    ("Timor-Leste", "TL", "APAC"),
]

# China split by province ISO codes (avoids timeout)
CHINA_PROVINCES = [
    ("China-Beijing",    "CN-BJ", "APAC", "Beijing"),
    ("China-Shanghai",   "CN-SH", "APAC", "Shanghai"),
    ("China-Guangdong",  "CN-GD", "APAC", "Guangdong"),
    ("China-Sichuan",    "CN-SC", "APAC", "Sichuan"),
    ("China-Jiangsu",    "CN-JS", "APAC", "Jiangsu"),
    ("China-Zhejiang",   "CN-ZJ", "APAC", "Zhejiang"),
    ("China-Shandong",   "CN-SD", "APAC", "Shandong"),
    ("China-Henan",      "CN-HA", "APAC", "Henan"),
    ("China-Hubei",      "CN-HB", "APAC", "Hubei"),
    ("China-Hunan",      "CN-HN", "APAC", "Hunan"),
    ("China-Anhui",      "CN-AH", "APAC", "Anhui"),
    ("China-Fujian",     "CN-FJ", "APAC", "Fujian"),
    ("China-Liaoning",   "CN-LN", "APAC", "Liaoning"),
    ("China-Heilongjiang","CN-HL","APAC", "Heilongjiang"),
    ("China-Jilin",      "CN-JL", "APAC", "Jilin"),
    ("China-Shaanxi",    "CN-SN", "APAC", "Shaanxi"),
    ("China-Yunnan",     "CN-YN", "APAC", "Yunnan"),
    ("China-Xinjiang",   "CN-XJ", "APAC", "Xinjiang"),
    ("China-Inner Mongolia","CN-NM","APAC","Inner Mongolia"),
    ("China-Tibet",      "CN-XZ", "APAC", "Tibet"),
]

COLS = ['osm_id','osm_type','infra_type','country','iso_code','region','name','operator',
        'voltage_kv','cables','circuits','frequency_hz','substation_type','gas_substance',
        'location','start_date','lat','lon','fetched_at']

sql = ('INSERT INTO infrastructure (' + ','.join(COLS) +
       ') VALUES %s ON CONFLICT (osm_type, osm_id) DO NOTHING')


def call_overpass(query, timeout=220):
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(3):
            try:
                r = requests.post(endpoint, data={"data": query},
                                  timeout=timeout,
                                  headers={"User-Agent": "EMEA-APAC-Infra-Fetcher/2.0"})
                r.raise_for_status()
                return r.json().get("elements", [])
            except requests.exceptions.HTTPError as e:
                if r.status_code == 429:
                    wait = 60 * (attempt + 1)
                    print(f"    Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"    {endpoint} HTTP error: {e}")
                    break
            except Exception as e:
                print(f"    {endpoint} attempt {attempt+1}: {e}")
                time.sleep(20)
    return []


def fetch_by_iso(iso, timeout=160):
    """Fetch all 3 infra types for a country ISO code."""
    elements = []
    queries = [
        # Transmission lines (voltage filter keeps query size manageable)
        f'[out:json][timeout:{timeout}];\nrel["ISO3166-1:alpha2"="{iso}"]["admin_level"="2"];\nmap_to_area->.c;\nway["power"="line"]["voltage"~"[0-9]{{5,}}"](area.c);\nout center tags;',
        # Substations
        f'[out:json][timeout:{timeout}];\nrel["ISO3166-1:alpha2"="{iso}"]["admin_level"="2"];\nmap_to_area->.c;\n(node["power"="substation"](area.c);way["power"="substation"](area.c););\nout center tags;',
        # Gas pipelines
        f'[out:json][timeout:{timeout}];\nrel["ISO3166-1:alpha2"="{iso}"]["admin_level"="2"];\nmap_to_area->.c;\nway["man_made"="pipeline"]["substance"~"gas"](area.c);\nout center tags;',
    ]
    labels = ["lines", "substations", "gas"]
    for label, q in zip(labels, queries):
        els = call_overpass(q)
        print(f"      {label}: {len(els)}")
        elements += els
        time.sleep(4)
    return elements


def fetch_by_province(province_name, timeout=120):
    """Fetch infra for a Chinese province by name."""
    elements = []
    queries = [
        f'[out:json][timeout:{timeout}];\narea["name:en"="{province_name}"]["admin_level"="4"]->.c;\nway["power"="line"]["voltage"~"[0-9]{{5,}}"](area.c);\nout center tags;',
        f'[out:json][timeout:{timeout}];\narea["name:en"="{province_name}"]["admin_level"="4"]->.c;\n(node["power"="substation"](area.c);way["power"="substation"](area.c););\nout center tags;',
        f'[out:json][timeout:{timeout}];\narea["name:en"="{province_name}"]["admin_level"="4"]->.c;\nway["man_made"="pipeline"]["substance"~"gas"](area.c);\nout center tags;',
    ]
    labels = ["lines", "substations", "gas"]
    for label, q in zip(labels, queries):
        els = call_overpass(q)
        print(f"      {label}: {len(els)}")
        elements += els
        time.sleep(4)
    return elements


def to_row(el, country, iso, region):
    tags     = el.get("tags", {})
    center   = el.get("center", {})
    power    = tags.get("power", "")
    man_made = tags.get("man_made", "")
    substance = tags.get("substance", "")

    if power == "line":           itype = "transmission_line"
    elif power == "substation":   itype = "substation"
    elif man_made == "pipeline" and "gas" in substance.lower(): itype = "gas_pipeline"
    else: return None

    raw_v = tags.get("voltage", "")
    try:
        v = int(raw_v.split(";")[0].replace(",", "").strip())
        vkv = None if v > 2000000000 else v
    except: vkv = None

    return (
        el.get("id"), el.get("type", ""), itype, country, iso, region,
        tags.get("name", ""), tags.get("operator", ""), vkv,
        tags.get("cables", ""), tags.get("circuits", ""),
        tags.get("frequency", ""), tags.get("substation", ""),
        tags.get("substance", ""), tags.get("location", ""),
        tags.get("start_date", ""),
        center.get("lat") or el.get("lat"),
        center.get("lon") or el.get("lon"),
        "2026-04-09T00:00:00Z",
    )


def insert_rows(rows):
    if not rows: return 0
    psycopg2.extras.execute_values(cur, sql, rows, page_size=2000)
    return len(rows)


# ── Run expansion countries ───────────────────────────────────────────────────
grand_total = 0

print("=" * 60)
print(f"Expansion: {len(EXPANSION_COUNTRIES)} countries + {len(CHINA_PROVINCES)} China provinces")
print("=" * 60)

for country, iso, region in EXPANSION_COUNTRIES:
    print(f"\n{country} ({iso}) [{region}]...")
    elements = fetch_by_iso(iso)
    rows = [r for el in elements if (r := to_row(el, country, iso, region))]
    added = insert_rows(rows)
    grand_total += added
    print(f"  → {added:,} records inserted (running total: {grand_total:,})")
    time.sleep(3)

print("\n" + "=" * 60)
print("China provinces...")
print("=" * 60)

for country, iso, region, province_name in CHINA_PROVINCES:
    print(f"\n  {province_name}...")
    elements = fetch_by_province(province_name)
    rows = [r for el in elements if (r := to_row(el, country, iso, region))]
    added = insert_rows(rows)
    grand_total += added
    print(f"  → {added:,} records (running total: {grand_total:,})")
    time.sleep(3)

print(f"\n{'='*60}")
print(f"Expansion complete — {grand_total:,} new records added")
cur.execute('SELECT COUNT(*) FROM infrastructure')
print(f"Grand total in Neon: {cur.fetchone()[0]:,}")
print()
cur.execute("""
    SELECT region, infra_type, COUNT(*)
    FROM infrastructure
    GROUP BY region, infra_type
    ORDER BY region, infra_type
""")
for r in cur.fetchall():
    print(f"  {r[0]:<6} {r[1]:<20} {r[2]:,}")
conn.close()
