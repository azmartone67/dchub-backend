"""
quarterly_refresh.py — Global Infrastructure OSM Refresh
=========================================================
Re-pulls all EMEA/APAC transmission lines, substations, and gas pipelines
from OpenStreetMap and upserts into the Neon `infrastructure` table.

Existing rows are skipped (ON CONFLICT DO NOTHING on osm_type + osm_id),
so only net-new OSM features are added each quarter.

Usage (Replit shell):
  export NEON_DATABASE_URL='postgresql://neondb_owner:...@ep-old-waterfall-aa2rwjzs.westus3.azure.neon.tech/neondb?sslmode=require'
  python3 quarterly_refresh.py

Estimated run time: 3-5 hours (rate-limit friendly)
"""

import psycopg2, psycopg2.extras, requests, time, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

conn = psycopg2.connect(os.environ['NEON_DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
cur.execute('SET statement_timeout = 0')

FETCHED_AT = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

COLS = ['osm_id','osm_type','infra_type','country','iso_code','region','name','operator',
        'voltage_kv','cables','circuits','frequency_hz','substation_type','gas_substance',
        'location','start_date','lat','lon','fetched_at']

SQL = ('INSERT INTO infrastructure (' + ','.join(COLS) +
       ') VALUES %s ON CONFLICT (osm_type, osm_id) DO NOTHING')

# ── Full country list ─────────────────────────────────────────────────────────
ALL_COUNTRIES = [
    # Western Europe
    ("Germany",       "DE", "EMEA"),
    ("France",        "FR", "EMEA"),
    ("Italy",         "IT", "EMEA"),
    ("Spain",         "ES", "EMEA"),
    ("Portugal",      "PT", "EMEA"),
    ("Netherlands",   "NL", "EMEA"),
    ("Belgium",       "BE", "EMEA"),
    ("Switzerland",   "CH", "EMEA"),
    ("Austria",       "AT", "EMEA"),
    ("Sweden",        "SE", "EMEA"),
    ("Norway",        "NO", "EMEA"),
    ("Denmark",       "DK", "EMEA"),
    ("Finland",       "FI", "EMEA"),
    ("Poland",        "PL", "EMEA"),
    ("Czech Republic","CZ", "EMEA"),
    ("Hungary",       "HU", "EMEA"),
    ("Romania",       "RO", "EMEA"),
    ("Turkey",        "TR", "EMEA"),
    ("United Kingdom","GB", "EMEA"),
    ("Ireland",       "IE", "EMEA"),
    ("South Africa",  "ZA", "EMEA"),
    ("Israel",        "IL", "EMEA"),
    ("Saudi Arabia",  "SA", "EMEA"),
    ("UAE",           "AE", "EMEA"),
    # Eastern Europe
    ("Greece",        "GR", "EMEA"),
    ("Bulgaria",      "BG", "EMEA"),
    ("Serbia",        "RS", "EMEA"),
    ("Croatia",       "HR", "EMEA"),
    ("Slovakia",      "SK", "EMEA"),
    ("Slovenia",      "SI", "EMEA"),
    ("Lithuania",     "LT", "EMEA"),
    ("Latvia",        "LV", "EMEA"),
    ("Estonia",       "EE", "EMEA"),
    ("Belarus",       "BY", "EMEA"),
    ("Ukraine",       "UA", "EMEA"),
    ("Moldova",       "MD", "EMEA"),
    # Africa
    ("Morocco",       "MA", "EMEA"),
    ("Egypt",         "EG", "EMEA"),
    ("Nigeria",       "NG", "EMEA"),
    ("Kenya",         "KE", "EMEA"),
    ("Ghana",         "GH", "EMEA"),
    ("Tanzania",      "TZ", "EMEA"),
    ("Ethiopia",      "ET", "EMEA"),
    ("Mozambique",    "MZ", "EMEA"),
    ("Zambia",        "ZM", "EMEA"),
    ("Zimbabwe",      "ZW", "EMEA"),
    # Middle East
    ("Jordan",        "JO", "EMEA"),
    ("Kuwait",        "KW", "EMEA"),
    ("Qatar",         "QA", "EMEA"),
    ("Oman",          "OM", "EMEA"),
    ("Bahrain",       "BH", "EMEA"),
    ("Pakistan",      "PK", "EMEA"),
    ("Iraq",          "IQ", "EMEA"),
    ("Iran",          "IR", "EMEA"),
    # APAC
    ("Japan",         "JP", "APAC"),
    ("South Korea",   "KR", "APAC"),
    ("India",         "IN", "APAC"),
    ("Australia",     "AU", "APAC"),
    ("New Zealand",   "NZ", "APAC"),
    ("Singapore",     "SG", "APAC"),
    ("Malaysia",      "MY", "APAC"),
    ("Thailand",      "TH", "APAC"),
    ("Indonesia",     "ID", "APAC"),
    ("Philippines",   "PH", "APAC"),
    ("Vietnam",       "VN", "APAC"),
    ("Taiwan",        "TW", "APAC"),
    ("Hong Kong",     "HK", "APAC"),
    ("Bangladesh",    "BD", "APAC"),
    ("Sri Lanka",     "LK", "APAC"),
    ("Myanmar",       "MM", "APAC"),
    ("Cambodia",      "KH", "APAC"),
    ("Laos",          "LA", "APAC"),
    ("Nepal",         "NP", "APAC"),
    ("Mongolia",      "MN", "APAC"),
    ("Papua New Guinea","PG","APAC"),
    ("Brunei",        "BN", "APAC"),
    ("Timor-Leste",   "TL", "APAC"),
]

# China via bounding boxes (avoids timeout, proven to work)
CHINA_REGIONS = [
    ("China-Northeast", "CN-NE", "APAC", "36,115,54,135"),
    ("China-North",     "CN-N",  "APAC", "35,100,42,116"),
    ("China-East",      "CN-E",  "APAC", "24,108,38,122"),
    ("China-South",     "CN-S",  "APAC", "18,105,26,116"),
    ("China-Central",   "CN-C",  "APAC", "25,100,36,112"),
    ("China-West",      "CN-W",  "APAC", "22,96,36,106"),
    ("China-Northwest", "CN-NW", "APAC", "35,73,50,100"),
]


def call_overpass(query, endpoint, timeout=180):
    for attempt in range(3):
        try:
            r = requests.post(endpoint, data={"data": query},
                              timeout=timeout,
                              headers={"User-Agent": "Global-Infra-Refresh/3.0"})
            r.raise_for_status()
            return r.json().get("elements", [])
        except requests.exceptions.HTTPError as e:
            if r.status_code == 429:
                wait = 90 * (attempt + 1)
                print(f"    Rate limited on {endpoint}, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"    HTTP {r.status_code} on {endpoint}: {e}")
                break
        except Exception as e:
            print(f"    {endpoint} attempt {attempt+1}: {e}")
            time.sleep(20)
    return []


def fetch_country(iso, timeout=160):
    """Fetch lines, substations, gas for a country ISO."""
    queries = [
        f'[out:json][timeout:{timeout}];\nrel["ISO3166-1:alpha2"="{iso}"]["admin_level"="2"];\nmap_to_area->.c;\nway["power"="line"]["voltage"~"[0-9]{{5,}}"](area.c);\nout center tags;',
        f'[out:json][timeout:{timeout}];\nrel["ISO3166-1:alpha2"="{iso}"]["admin_level"="2"];\nmap_to_area->.c;\n(node["power"="substation"](area.c);way["power"="substation"](area.c););\nout center tags;',
        f'[out:json][timeout:{timeout}];\nrel["ISO3166-1:alpha2"="{iso}"]["admin_level"="2"];\nmap_to_area->.c;\nway["man_made"="pipeline"]["substance"~"gas"](area.c);\nout center tags;',
    ]
    labels = ["lines", "substations", "gas"]
    elements = []
    for i, (label, q) in enumerate(zip(labels, queries)):
        endpoint = OVERPASS_ENDPOINTS[i % len(OVERPASS_ENDPOINTS)]
        els = call_overpass(q, endpoint)
        print(f"      {label}: {len(els)}")
        elements += els
        time.sleep(5)
    return elements


def fetch_china_bbox(bbox_str, timeout=120):
    """Fetch infra for a China bounding box (S,W,N,E)."""
    s, w, n, e = bbox_str.split(',')
    queries = [
        f'[out:json][timeout:{timeout}];\nway["power"="line"]["voltage"~"[0-9]{{5,}}"]({s},{w},{n},{e});\nout center tags;',
        f'[out:json][timeout:{timeout}];\n(node["power"="substation"]({s},{w},{n},{e});way["power"="substation"]({s},{w},{n},{e}););\nout center tags;',
        f'[out:json][timeout:{timeout}];\nway["man_made"="pipeline"]["substance"~"gas"]({s},{w},{n},{e});\nout center tags;',
    ]
    labels = ["lines", "substations", "gas"]
    elements = []
    for i, (label, q) in enumerate(zip(labels, queries)):
        endpoint = OVERPASS_ENDPOINTS[i % len(OVERPASS_ENDPOINTS)]
        els = call_overpass(q, endpoint)
        print(f"      {label}: {len(els)}")
        elements += els
        time.sleep(5)
    return elements


def to_row(el, country, iso, region):
    tags      = el.get("tags", {})
    center    = el.get("center", {})
    power     = tags.get("power", "")
    man_made  = tags.get("man_made", "")
    substance = tags.get("substance", "")

    if power == "line":           itype = "transmission_line"
    elif power == "substation":   itype = "substation"
    elif man_made == "pipeline" and "gas" in substance.lower(): itype = "gas_pipeline"
    else: return None

    raw_v = tags.get("voltage", "")
    try:
        v = int(str(raw_v).split(";")[0].replace(",", "").strip())
        vkv = None if v > 2_000_000_000 else v
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
        FETCHED_AT,
    )


def insert_rows(rows):
    if not rows: return 0
    psycopg2.extras.execute_values(cur, SQL, rows, page_size=2000)
    return len(rows)


# ── Main refresh loop ─────────────────────────────────────────────────────────
print("=" * 60)
print(f"Quarterly Refresh — {FETCHED_AT}")
print(f"{len(ALL_COUNTRIES)} countries + {len(CHINA_REGIONS)} China regions")
print("=" * 60)

grand_total = 0

for country, iso, region in ALL_COUNTRIES:
    print(f"\n{country} ({iso}) [{region}]...")
    elements = fetch_country(iso)
    rows = [r for el in elements if (r := to_row(el, country, iso, region))]
    added = insert_rows(rows)
    grand_total += added
    print(f"  → {added:,} new records (running total: {grand_total:,})")
    time.sleep(3)

print("\n" + "=" * 60)
print("China regions (bounding box)...")
print("=" * 60)

for name, iso, region, bbox in CHINA_REGIONS:
    print(f"\n  {name}...")
    elements = fetch_china_bbox(bbox)
    rows = [r for el in elements if (r := to_row(el, "China", iso, region))]
    added = insert_rows(rows)
    grand_total += added
    print(f"  → {added:,} new records (running total: {grand_total:,})")
    time.sleep(3)

print(f"\n{'='*60}")
print(f"Refresh complete — {grand_total:,} net-new records added")

cur.execute('SELECT COUNT(*) FROM infrastructure')
print(f"Total OSM infrastructure rows in Neon: {cur.fetchone()[0]:,}")

cur.execute('SELECT COUNT(*) FROM global_infrastructure')
print(f"Total global_infrastructure view rows:  {cur.fetchone()[0]:,}")

print()
cur.execute("""
    SELECT region, infra_type, COUNT(*)
    FROM global_infrastructure
    GROUP BY region, infra_type
    ORDER BY region, infra_type
""")
for row in cur.fetchall():
    print(f"  {row[0]:<6} {row[1]:<20} {row[2]:,}")

conn.close()
print("\nDone.")
