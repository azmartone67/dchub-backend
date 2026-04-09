"""
EMEA & APAC Power + Gas Infrastructure Fetcher
================================================
Fetches transmission lines, substations, and gas pipelines
for all EMEA and APAC countries via OpenStreetMap Overpass API.

Output: CSV files + SQL ready for Neon PostgreSQL

Run on Replit:
  pip install requests psycopg2-binary
  python fetch_emea_apac_infrastructure.py

Set env vars:
  NEON_DATABASE_URL=postgresql://user:pass@host/db
  (or leave unset to just generate CSV + SQL files)
"""

import os
import csv
import json
import time
import logging
import requests
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Overpass API endpoints (tried in order) ──────────────────────────────────
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

# ── Countries to fetch ───────────────────────────────────────────────────────
EMEA_COUNTRIES = [
    # (display_name, ISO3166-1 alpha-2, region)
    ("Germany",              "DE", "EMEA"),
    ("France",               "FR", "EMEA"),
    ("United Kingdom",       "GB", "EMEA"),
    ("Netherlands",          "NL", "EMEA"),
    ("Belgium",              "BE", "EMEA"),
    ("Spain",                "ES", "EMEA"),
    ("Italy",                "IT", "EMEA"),
    ("Sweden",               "SE", "EMEA"),
    ("Norway",               "NO", "EMEA"),
    ("Denmark",              "DK", "EMEA"),
    ("Finland",              "FI", "EMEA"),
    ("Austria",              "AT", "EMEA"),
    ("Switzerland",          "CH", "EMEA"),
    ("Poland",               "PL", "EMEA"),
    ("Czech Republic",       "CZ", "EMEA"),
    ("Portugal",             "PT", "EMEA"),
    ("Ireland",              "IE", "EMEA"),
    ("Romania",              "RO", "EMEA"),
    ("Hungary",              "HU", "EMEA"),
    ("United Arab Emirates", "AE", "EMEA"),
    ("Saudi Arabia",         "SA", "EMEA"),
    ("South Africa",         "ZA", "EMEA"),
    ("Israel",               "IL", "EMEA"),
    ("Turkey",               "TR", "EMEA"),
]

APAC_COUNTRIES = [
    ("Singapore",    "SG", "APAC"),
    ("Australia",    "AU", "APAC"),
    ("Japan",        "JP", "APAC"),
    ("South Korea",  "KR", "APAC"),
    ("India",        "IN", "APAC"),
    ("Malaysia",     "MY", "APAC"),
    ("Hong Kong",    "HK", "APAC"),
    ("New Zealand",  "NZ", "APAC"),
    ("Indonesia",    "ID", "APAC"),
    ("Thailand",     "TH", "APAC"),
    ("Taiwan",       "TW", "APAC"),
    ("Philippines",  "PH", "APAC"),
    ("Vietnam",      "VN", "APAC"),
]

ALL_COUNTRIES = EMEA_COUNTRIES + APAC_COUNTRIES

# ── Overpass query builder ───────────────────────────────────────────────────
def build_query(iso_code: str, timeout: int = 120) -> str:
    """
    Fetch:
      - Transmission lines (power=line, all voltages — filter later)
      - Substations (power=substation)
      - Gas pipelines (man_made=pipeline, substance contains gas)
    OSM stores the country ISO code under "ISO3166-1:alpha2", not "ISO3166-1".
    We use rel->map_to_area which is the most reliable country selector.
    """
    return f"""
[out:json][timeout:{timeout}];
rel["ISO3166-1:alpha2"="{iso_code}"]["admin_level"="2"];
map_to_area->.country;
(
  way["power"="line"](area.country);
  node["power"="substation"](area.country);
  way["power"="substation"](area.country);
  relation["power"="substation"](area.country);
  way["man_made"="pipeline"]["substance"~"gas"](area.country);
  relation["man_made"="pipeline"]["substance"~"gas"](area.country);
);
out center tags;
""".strip()


def call_overpass(query: str, retries: int = 3) -> list:
    """Try each Overpass endpoint until one succeeds."""
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(retries):
            try:
                resp = requests.post(
                    endpoint,
                    data={"data": query},
                    timeout=150,
                    headers={"User-Agent": "EMEA-APAC-Infra-Fetcher/1.0"},
                )
                resp.raise_for_status()
                return resp.json().get("elements", [])
            except requests.exceptions.HTTPError as e:
                if resp.status_code == 429:
                    wait = 60 * (attempt + 1)
                    log.warning(f"Rate limited on {endpoint}, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    log.warning(f"{endpoint} HTTP error: {e}")
                    break
            except Exception as e:
                log.warning(f"{endpoint} attempt {attempt+1}: {e}")
                time.sleep(10)
    return []


# ── Element classifier ───────────────────────────────────────────────────────
def classify_element(el: dict, country: str, iso: str, region: str) -> dict | None:
    tags   = el.get("tags", {})
    osm_id = el.get("id")
    osm_tp = el.get("type", "")           # node / way / relation
    center = el.get("center", {})

    power   = tags.get("power", "")
    man_made = tags.get("man_made", "")
    substance = tags.get("substance", "")

    if power == "line":
        infra_type = "transmission_line"
    elif power == "substation":
        infra_type = "substation"
    elif man_made == "pipeline" and "gas" in substance.lower():
        infra_type = "gas_pipeline"
    else:
        return None

    lat = center.get("lat") or el.get("lat")
    lon = center.get("lon") or el.get("lon")

    # Voltage: normalize to integer kV where possible
    raw_voltage = tags.get("voltage", "")
    voltage_kv  = None
    if raw_voltage:
        try:
            # OSM stores voltage in volts, e.g. "400000"
            v = int(raw_voltage.split(";")[0].replace(",", "").strip())
            voltage_kv = v // 1000 if v > 1000 else v
        except ValueError:
            voltage_kv = None

    return {
        "osm_id":           osm_id,
        "osm_type":         osm_tp,
        "infra_type":       infra_type,
        "country":          country,
        "iso_code":         iso,
        "region":           region,
        "name":             tags.get("name", tags.get("ref", "")),
        "operator":         tags.get("operator", ""),
        "voltage_kv":       voltage_kv,
        "cables":           tags.get("cables", ""),
        "circuits":         tags.get("circuits", ""),
        "frequency_hz":     tags.get("frequency", ""),
        "substation_type":  tags.get("substation", ""),
        "gas_substance":    tags.get("substance", ""),
        "location":         tags.get("location", ""),
        "start_date":       tags.get("start_date", ""),
        "lat":              lat,
        "lon":              lon,
        "fetched_at":       datetime.utcnow().isoformat() + "Z",
    }


# ── Main fetch loop ──────────────────────────────────────────────────────────
def fetch_all() -> list:
    all_records = []
    seen_ids    = set()

    for name, iso, region in ALL_COUNTRIES:
        log.info(f"Fetching {name} ({iso}) [{region}]...")
        query    = build_query(iso)
        elements = call_overpass(query)
        log.info(f"  → {len(elements)} raw elements")

        new_count = 0
        for el in elements:
            rec = classify_element(el, name, iso, region)
            if rec is None:
                continue
            uid = (rec["osm_type"], rec["osm_id"])
            if uid in seen_ids:
                continue          # deduplicate cross-border elements
            seen_ids.add(uid)
            all_records.append(rec)
            new_count += 1

        log.info(f"  → {new_count} new records (total: {len(all_records)})")
        time.sleep(3)             # polite delay between countries

    return all_records


# ── CSV writer ───────────────────────────────────────────────────────────────
FIELDS = [
    "osm_id", "osm_type", "infra_type", "country", "iso_code", "region",
    "name", "operator", "voltage_kv", "cables", "circuits", "frequency_hz",
    "substation_type", "gas_substance", "location", "start_date",
    "lat", "lon", "fetched_at",
]

def write_csvs(records: list, out_dir: str = "."):
    os.makedirs(out_dir, exist_ok=True)

    # One combined CSV
    combined_path = os.path.join(out_dir, "emea_apac_infrastructure.csv")
    with open(combined_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(records)
    log.info(f"Wrote {len(records)} rows → {combined_path}")

    # Per-type CSVs
    for infra_type in ("transmission_line", "substation", "gas_pipeline"):
        subset = [r for r in records if r["infra_type"] == infra_type]
        if not subset:
            continue
        path = os.path.join(out_dir, f"{infra_type}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(subset)
        log.info(f"  {infra_type}: {len(subset)} rows → {path}")

    # Per-region CSVs
    for region in ("EMEA", "APAC"):
        subset = [r for r in records if r["region"] == region]
        if not subset:
            continue
        path = os.path.join(out_dir, f"{region.lower()}_infrastructure.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(subset)
        log.info(f"  {region}: {len(subset)} rows → {path}")

    return combined_path


# ── SQL / Neon loader ────────────────────────────────────────────────────────
SQL_SCHEMA = """
-- ============================================================
-- EMEA / APAC Power & Gas Infrastructure Schema for Neon DB
-- ============================================================
CREATE TABLE IF NOT EXISTS infrastructure (
    id              SERIAL PRIMARY KEY,
    osm_id          BIGINT,
    osm_type        VARCHAR(16),          -- node / way / relation
    infra_type      VARCHAR(32) NOT NULL, -- transmission_line / substation / gas_pipeline
    country         VARCHAR(64),
    iso_code        CHAR(2),
    region          VARCHAR(8),           -- EMEA / APAC
    name            TEXT,
    operator        TEXT,
    voltage_kv      INTEGER,              -- kV for transmission/substations
    cables          VARCHAR(16),
    circuits        VARCHAR(16),
    frequency_hz    VARCHAR(16),
    substation_type VARCHAR(64),
    gas_substance   VARCHAR(64),
    location        VARCHAR(64),
    start_date      VARCHAR(32),
    lat             DOUBLE PRECISION,
    lon             DOUBLE PRECISION,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (osm_type, osm_id)             -- prevent duplicates on re-import
);

CREATE INDEX IF NOT EXISTS idx_infra_country    ON infrastructure (country);
CREATE INDEX IF NOT EXISTS idx_infra_region     ON infrastructure (region);
CREATE INDEX IF NOT EXISTS idx_infra_type       ON infrastructure (infra_type);
CREATE INDEX IF NOT EXISTS idx_infra_voltage    ON infrastructure (voltage_kv);
CREATE INDEX IF NOT EXISTS idx_infra_location   ON infrastructure (lat, lon);
"""

def escape_sql(val) -> str:
    if val is None or val == "":
        return "NULL"
    s = str(val).replace("'", "''")
    return f"'{s}'"

def write_sql(records: list, out_dir: str = ".") -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "emea_apac_infrastructure.sql")

    with open(path, "w", encoding="utf-8") as f:
        f.write(SQL_SCHEMA)
        f.write("\n-- Data inserts\n")
        f.write("BEGIN;\n\n")

        BATCH = 500
        for i in range(0, len(records), BATCH):
            batch = records[i : i + BATCH]
            rows  = []
            for r in batch:
                row = (
                    f"({escape_sql(r['osm_id'])}, {escape_sql(r['osm_type'])}, "
                    f"{escape_sql(r['infra_type'])}, {escape_sql(r['country'])}, "
                    f"{escape_sql(r['iso_code'])}, {escape_sql(r['region'])}, "
                    f"{escape_sql(r['name'])}, {escape_sql(r['operator'])}, "
                    f"{'NULL' if r['voltage_kv'] is None else int(r['voltage_kv'])}, "
                    f"{escape_sql(r['cables'])}, {escape_sql(r['circuits'])}, "
                    f"{escape_sql(r['frequency_hz'])}, {escape_sql(r['substation_type'])}, "
                    f"{escape_sql(r['gas_substance'])}, {escape_sql(r['location'])}, "
                    f"{escape_sql(r['start_date'])}, "
                    f"{'NULL' if r['lat'] is None else r['lat']}, "
                    f"{'NULL' if r['lon'] is None else r['lon']}, "
                    f"{escape_sql(r['fetched_at'])})"
                )
                rows.append(row)

            cols = (
                "osm_id, osm_type, infra_type, country, iso_code, region, "
                "name, operator, voltage_kv, cables, circuits, frequency_hz, "
                "substation_type, gas_substance, location, start_date, lat, lon, fetched_at"
            )
            f.write(
                f"INSERT INTO infrastructure ({cols})\n"
                f"VALUES\n  " + ",\n  ".join(rows) +
                "\nON CONFLICT (osm_type, osm_id) DO NOTHING;\n\n"
            )

        f.write("COMMIT;\n")

    log.info(f"Wrote SQL → {path}")
    return path


# ── Optional: push directly to Neon ─────────────────────────────────────────
def push_to_neon(records: list, database_url: str):
    try:
        import psycopg2
    except ImportError:
        log.error("psycopg2 not installed. Run: pip install psycopg2-binary")
        return

    log.info(f"Connecting to Neon: {database_url[:40]}...")
    conn = psycopg2.connect(database_url)
    cur  = conn.cursor()

    # Create schema
    cur.execute(SQL_SCHEMA)
    conn.commit()
    log.info("Schema created/verified.")

    # Insert in batches
    inserted = 0
    BATCH = 200
    for i in range(0, len(records), BATCH):
        batch = records[i : i + BATCH]
        data  = [
            (
                r["osm_id"], r["osm_type"], r["infra_type"], r["country"],
                r["iso_code"], r["region"], r["name"], r["operator"],
                r["voltage_kv"], r["cables"], r["circuits"], r["frequency_hz"],
                r["substation_type"], r["gas_substance"], r["location"],
                r["start_date"], r["lat"], r["lon"], r["fetched_at"],
            )
            for r in batch
        ]
        cur.executemany(
            """
            INSERT INTO infrastructure (
                osm_id, osm_type, infra_type, country, iso_code, region,
                name, operator, voltage_kv, cables, circuits, frequency_hz,
                substation_type, gas_substance, location, start_date,
                lat, lon, fetched_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (osm_type, osm_id) DO NOTHING
            """,
            data,
        )
        conn.commit()
        inserted += len(batch)
        log.info(f"  Inserted batch {i//BATCH + 1}: {inserted}/{len(records)}")

    cur.close()
    conn.close()
    log.info(f"Done. {inserted} records pushed to Neon.")


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    OUT_DIR      = "infrastructure_output"
    DATABASE_URL = os.environ.get("NEON_DATABASE_URL", "")

    log.info("=" * 60)
    log.info("EMEA + APAC Infrastructure Fetcher")
    log.info(f"Countries: {len(ALL_COUNTRIES)}")
    log.info(f"Output dir: {OUT_DIR}")
    log.info("=" * 60)

    records = fetch_all()

    log.info(f"\nTotal unique infrastructure records: {len(records)}")

    # Breakdown
    for infra_type in ("transmission_line", "substation", "gas_pipeline"):
        count = sum(1 for r in records if r["infra_type"] == infra_type)
        log.info(f"  {infra_type:<20}: {count:,}")
    for region in ("EMEA", "APAC"):
        count = sum(1 for r in records if r["region"] == region)
        log.info(f"  {region:<20}: {count:,}")

    # Write CSVs
    write_csvs(records, OUT_DIR)

    # Write SQL
    write_sql(records, OUT_DIR)

    # Push to Neon if URL provided
    if DATABASE_URL:
        push_to_neon(records, DATABASE_URL)
    else:
        log.info("\nTip: Set NEON_DATABASE_URL env var to push directly to Neon.")
        log.info("     export NEON_DATABASE_URL='postgresql://user:pass@host/db'")
        log.info("     python fetch_emea_apac_infrastructure.py")
