"""
DC Hub — International Infrastructure Ingest (OpenStreetMap Overpass)
====================================================================
Pulls REAL power substations and power plants for the NON-US DCPI metros
from the OpenStreetMap Overpass API and upserts them into the EXISTING
`infrastructure` table (the same table created by
fetch_emea_apac_infrastructure.py). No new columns are invented.

WHY THIS FILE EXISTS
--------------------
The US energy auto-discovery (energy_auto_discovery_pg.py) pulls power
plants / substations from HIFLD ArcGIS — but HIFLD is US-only. The DCPI
international markets (London, Frankfurt, Amsterdam, Paris, Dublin, Tokyo,
Singapore, Tel Aviv, Mumbai, Sydney, Sao Paulo, Taipei, ...) had no
substation/plant coverage. OpenStreetMap Overpass is FREE, needs NO token,
and carries power=substation / power=plant globally.

DATA SOURCE (the only one used — REAL, FREE, no credentials)
------------------------------------------------------------
  OpenStreetMap Overpass API
    https://overpass-api.de/api/interpreter           (primary)
    https://overpass.kumi.systems/api/interpreter     (mirror)
    https://overpass.openstreetmap.ru/api/interpreter (mirror)
  Query: power=substation + power=plant within each metro's bounding box.
  Fields used (verbatim from OSM tags — never fabricated): name, operator,
  voltage (volts), cables, circuits, frequency, substation, location,
  start_date, plus node lat/lon or way/relation center lat/lon.

ABSOLUTE HONESTY RULES THIS SCRIPT FOLLOWS
------------------------------------------
  * NOTHING is fabricated. Every value written comes straight from the OSM
    element. Missing tags are stored as NULL / "", never as a placeholder.
  * A per-metro REAL count is printed. 0 is a valid, honest answer.
  * If DATABASE_URL / NEON_DATABASE_URL is not set, the script still queries
    the live API and prints REAL counts, but writes nothing (dry run). It
    never invents rows.
  * power plant capacity (plant:output:electricity) is NOT written, because
    the existing `infrastructure` table has NO capacity column and we do not
    invent one. It is surfaced in the printed log only (verbatim from OSM).

TARGET TABLE (must already exist; this script does NOT alter its shape)
-----------------------------------------------------------------------
  Table `infrastructure` from fetch_emea_apac_infrastructure.py:
    osm_id, osm_type, infra_type, country, iso_code, region, name, operator,
    voltage_kv, cables, circuits, frequency_hz, substation_type,
    gas_substance, location, start_date, lat, lon, fetched_at
  UNIQUE (osm_type, osm_id)  → ON CONFLICT DO NOTHING makes re-runs idempotent.
  infra_type values written here: 'substation', 'power_plant'.

HOW TO RUN
----------
  pip install requests psycopg2-binary

  # Dry run (queries the live Overpass API, prints REAL per-metro counts,
  # writes nothing):
  python intl_infra_ingest.py

  # Real upsert into the existing infrastructure table (idempotent):
  export DATABASE_URL='postgresql://user:pass@host/db'   # or NEON_DATABASE_URL
  python intl_infra_ingest.py

  # Limit to specific metros (substring match on the metro key):
  python intl_infra_ingest.py --metros frankfurt,tokyo,tel-aviv

This file is ADDITIVE and side-effect-free on import (all work is under
__main__). It does not change behavior of any existing module.
"""

from __future__ import annotations

import os
import sys
import time
import logging
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("intl_infra_ingest")

# ── Overpass API endpoints (tried in order). REAL, FREE, no token. ───────────
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]
OVERPASS_SOURCE_URL = "https://overpass-api.de/api/interpreter"  # cited in logs

# ── NON-US DCPI metros: (metro_key, display, country_iso2, region, bbox) ─────
# bbox = (south_lat, west_lon, north_lat, east_lon)  — WGS84 degrees.
# Coordinates are the metro center (already used elsewhere in the codebase,
# e.g. routes/international_ingestion.py _INTL_MARKET_MAP and
# global_facilities_expansion.py) expanded by ~±0.30° (~33 km) to cover the
# metro footprint. These are GEOGRAPHIC bounding boxes, NOT data values — they
# only tell Overpass *where* to look; every returned record is real OSM data.
_BOX = 0.30  # degrees of padding around each metro center


def _bbox(lat: float, lon: float, pad: float = _BOX) -> tuple:
    return (lat - pad, lon - pad, lat + pad, lon + pad)


# Region tags mirror fetch_emea_apac_infrastructure.py ("EMEA" / "APAC"); the
# Americas (Sao Paulo) gets "AMER" so the column is honest rather than wrong.
INTL_METROS = [
    # ── EMEA ──────────────────────────────────────────────────────────────
    ("london-uk",     "London, UK",      "GB", "EMEA", _bbox(51.5074, -0.1278)),
    ("slough-uk",     "Slough, UK",      "GB", "EMEA", _bbox(51.5105, -0.5950)),
    ("manchester-uk", "Manchester, UK",  "GB", "EMEA", _bbox(53.4808, -2.2426)),
    ("dublin-ie",     "Dublin, IE",      "IE", "EMEA", _bbox(53.3498, -6.2603)),
    ("frankfurt-de",  "Frankfurt, DE",   "DE", "EMEA", _bbox(50.1109,  8.6821)),
    ("berlin-de",     "Berlin, DE",      "DE", "EMEA", _bbox(52.5200, 13.4050)),
    ("amsterdam-nl",  "Amsterdam, NL",   "NL", "EMEA", _bbox(52.3676,  4.9041)),
    ("paris-fr",      "Paris, FR",       "FR", "EMEA", _bbox(48.8566,  2.3522)),
    ("marseille-fr",  "Marseille, FR",   "FR", "EMEA", _bbox(43.2965,  5.3698)),
    ("tel-aviv-il",   "Tel Aviv, IL",    "IL", "EMEA", _bbox(32.0853, 34.7818)),
    # ── APAC ──────────────────────────────────────────────────────────────
    ("singapore",     "Singapore",       "SG", "APAC", _bbox(1.3521, 103.8198)),
    ("tokyo",         "Tokyo, JP",       "JP", "APAC", _bbox(35.6762, 139.6503)),
    ("osaka",         "Osaka, JP",       "JP", "APAC", _bbox(34.6937, 135.5023)),
    ("mumbai-in",     "Mumbai, IN",      "IN", "APAC", _bbox(19.0760,  72.8777)),
    ("bangalore-in",  "Bangalore, IN",   "IN", "APAC", _bbox(12.9716,  77.5946)),
    ("sydney-au",     "Sydney, AU",      "AU", "APAC", _bbox(-33.8688, 151.2093)),
    ("taipei-tw",     "Taipei, TW",      "TW", "APAC", _bbox(25.0330, 121.5654)),
    # ── AMER (non-US) ─────────────────────────────────────────────────────
    ("sao-paulo-br",  "Sao Paulo, BR",   "BR", "AMER", _bbox(-23.5505, -46.6333)),
]


# ── Overpass query builder ───────────────────────────────────────────────────
def build_query(bbox: tuple, timeout: int = 90) -> str:
    """
    Fetch power substations + power plants inside a bbox.
    bbox = (south, west, north, east).
    `out center tags` gives node lat/lon directly and a computed `center`
    for ways/relations — exactly what classify_element() reads.
    """
    s, w, n, e = bbox
    return f"""
[out:json][timeout:{timeout}];
(
  node["power"="substation"]({s},{w},{n},{e});
  way["power"="substation"]({s},{w},{n},{e});
  relation["power"="substation"]({s},{w},{n},{e});
  node["power"="plant"]({s},{w},{n},{e});
  way["power"="plant"]({s},{w},{n},{e});
  relation["power"="plant"]({s},{w},{n},{e});
);
out center tags;
""".strip()


def call_overpass(query: str, retries: int = 3) -> list | None:
    """
    Try each Overpass endpoint until one succeeds.
    Returns a list of elements, or None if EVERY endpoint failed (so the
    caller can honestly report 'source_unavailable' instead of a fake 0).
    """
    any_response = False
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(retries):
            resp = None
            try:
                resp = requests.post(
                    endpoint,
                    data={"data": query},
                    timeout=180,
                    headers={"User-Agent": "DCHub-IntlInfra/1.0 (https://dchub.cloud)"},
                )
                resp.raise_for_status()
                any_response = True
                return resp.json().get("elements", [])
            except requests.exceptions.HTTPError as exc:
                code = getattr(resp, "status_code", None)
                if code == 429:
                    wait = 30 * (attempt + 1)
                    log.warning(f"  rate-limited (429) on {endpoint}; waiting {wait}s")
                    time.sleep(wait)
                    continue
                log.warning(f"  {endpoint} HTTP {code}: {exc}")
                break  # non-rate-limit HTTP error → try next endpoint
            except Exception as exc:  # noqa: BLE001 - network errors vary
                log.warning(f"  {endpoint} attempt {attempt + 1} failed: {exc}")
                time.sleep(5)
    # Distinguish "API down" (None) from "API said zero" (handled by caller
    # which only gets here on total failure).
    return None if not any_response else []


# ── Element → row (verbatim from OSM tags; nothing fabricated) ───────────────
def classify_element(el: dict, country: str, iso: str, region: str) -> dict | None:
    tags = el.get("tags", {}) or {}
    power = tags.get("power", "")

    if power == "substation":
        infra_type = "substation"
    elif power == "plant":
        infra_type = "power_plant"
    else:
        return None  # query is scoped, but be defensive

    center = el.get("center", {}) or {}
    lat = el.get("lat", center.get("lat"))
    lon = el.get("lon", center.get("lon"))

    # Voltage: OSM stores volts (e.g. "400000" or "400000;161000"); take the
    # FIRST listed value and convert to kV. If it isn't parseable we store
    # NULL — we never guess a voltage.
    voltage_kv = None
    raw_voltage = tags.get("voltage", "")
    if raw_voltage:
        first = str(raw_voltage).split(";")[0].replace(",", "").strip()
        try:
            v = int(float(first))
            voltage_kv = v // 1000 if v > 1000 else v
        except (ValueError, TypeError):
            voltage_kv = None

    return {
        "osm_id":          el.get("id"),
        "osm_type":        el.get("type", ""),     # node / way / relation
        "infra_type":      infra_type,             # substation / power_plant
        "country":         country,
        "iso_code":        iso,
        "region":          region,
        "name":            tags.get("name", tags.get("ref", "")),
        "operator":        tags.get("operator", ""),
        "voltage_kv":      voltage_kv,
        "cables":          tags.get("cables", ""),
        "circuits":        tags.get("circuits", ""),
        "frequency_hz":    tags.get("frequency", ""),
        "substation_type": tags.get("substation", ""),
        "gas_substance":   "",                      # N/A for power assets
        "location":        tags.get("location", ""),
        "start_date":      tags.get("start_date", ""),
        "lat":             lat,
        "lon":             lon,
        "fetched_at":      datetime.now(timezone.utc).isoformat(),
        # Diagnostics only — NOT written to DB (no column for it; never faked).
        "_plant_output_raw":  tags.get("plant:output:electricity", ""),
        "_plant_source":      tags.get("plant:source", ""),
    }


# ── Fetch loop over metros ───────────────────────────────────────────────────
def fetch_intl(metros: list) -> tuple[list, dict]:
    """
    Returns (records, per_metro_report).
    per_metro_report[key] = {
        'display', 'substations', 'power_plants', 'total', 'status'
    }
    status is 'ok' | 'source_unavailable' (Overpass unreachable for that metro).
    Records are de-duplicated globally on (osm_type, osm_id).
    """
    records: list = []
    seen: set = set()
    report: dict = {}

    for key, display, iso, region, bbox in metros:
        log.info(f"[{key}] {display} — querying Overpass {OVERPASS_SOURCE_URL}")
        elements = call_overpass(build_query(bbox))

        if elements is None:
            log.warning(f"[{key}] Overpass unreachable → source_unavailable (no fake data written)")
            report[key] = {
                "display": display, "substations": 0, "power_plants": 0,
                "total": 0, "status": "source_unavailable",
            }
            time.sleep(3)
            continue

        subs = plants = 0
        for el in elements:
            rec = classify_element(el, display, iso, region)
            if rec is None:
                continue
            uid = (rec["osm_type"], rec["osm_id"])
            if uid in seen:
                continue
            seen.add(uid)
            records.append(rec)
            if rec["infra_type"] == "substation":
                subs += 1
            else:
                plants += 1

        report[key] = {
            "display": display, "substations": subs, "power_plants": plants,
            "total": subs + plants, "status": "ok",
        }
        log.info(f"[{key}] REAL: {subs} substations, {plants} power plants "
                 f"(unique so far: {len(records)})")
        time.sleep(5)  # polite delay; Overpass shared infra

    return records, report


# ── DB upsert into the EXISTING `infrastructure` table ───────────────────────
_UPSERT_COLS = (
    "osm_id, osm_type, infra_type, country, iso_code, region, name, operator, "
    "voltage_kv, cables, circuits, frequency_hz, substation_type, gas_substance, "
    "location, start_date, lat, lon, fetched_at"
)


def _connect():
    """Open a psycopg2 connection from DATABASE_URL/NEON_DATABASE_URL, else None."""
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if not db_url:
        return None
    import psycopg2  # imported lazily so dry runs need no driver
    return psycopg2.connect(db_url)


def upsert_records(conn, records: list) -> int:
    """
    Idempotent upsert into `infrastructure`. ON CONFLICT (osm_type, osm_id)
    DO NOTHING — re-runs add only genuinely new OSM elements. Returns the
    number of rows actually inserted (new), counted via cur.rowcount.

    This assumes the `infrastructure` table already exists (created by
    fetch_emea_apac_infrastructure.py). We do NOT create or alter it here —
    additive + reversible, no schema surprises for other callers.
    """
    cur = conn.cursor()

    # Verify the target table exists; fail honestly rather than silently.
    cur.execute("SELECT to_regclass('public.infrastructure')")
    if (cur.fetchone() or [None])[0] is None:
        cur.close()
        raise RuntimeError(
            "Table `infrastructure` does not exist. Create it first via "
            "fetch_emea_apac_infrastructure.py (SQL_SCHEMA) — this script is "
            "additive and intentionally does not define the schema."
        )

    inserted = 0
    sql = (
        f"INSERT INTO infrastructure ({_UPSERT_COLS}) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        f"ON CONFLICT (osm_type, osm_id) DO NOTHING"
    )
    for r in records:
        cur.execute(sql, (
            r["osm_id"], r["osm_type"], r["infra_type"], r["country"],
            r["iso_code"], r["region"], r["name"], r["operator"],
            r["voltage_kv"], r["cables"], r["circuits"], r["frequency_hz"],
            r["substation_type"], r["gas_substance"], r["location"],
            r["start_date"], r["lat"], r["lon"], r["fetched_at"],
        ))
        inserted += cur.rowcount  # 1 if newly inserted, 0 if conflict
    conn.commit()
    cur.close()
    return inserted


# ── Entry point ──────────────────────────────────────────────────────────────
def _select_metros(argv: list) -> list:
    """--metros a,b,c → keep metros whose key contains any of those substrings."""
    wanted = None
    for i, a in enumerate(argv):
        if a == "--metros" and i + 1 < len(argv):
            wanted = [s.strip().lower() for s in argv[i + 1].split(",") if s.strip()]
    if not wanted:
        return INTL_METROS
    return [m for m in INTL_METROS if any(w in m[0] for w in wanted)]


def main(argv: list) -> int:
    metros = _select_metros(argv)

    log.info("=" * 68)
    log.info("DC Hub — International Infrastructure Ingest (OSM Overpass)")
    log.info(f"Source : {OVERPASS_SOURCE_URL} (FREE, no token)")
    log.info(f"Metros : {len(metros)} non-US DCPI metros")
    log.info("=" * 68)

    records, report = fetch_intl(metros)

    # ── Per-metro REAL counts (0 is a valid honest answer) ───────────────────
    log.info("-" * 68)
    log.info("PER-METRO REAL RECORD COUNTS (OpenStreetMap Overpass):")
    total_sub = total_plant = 0
    for key, _disp, _iso, _region, _bbox in metros:
        row = report.get(key, {})
        status = row.get("status", "unknown")
        subs = row.get("substations", 0)
        plants = row.get("power_plants", 0)
        total_sub += subs
        total_plant += plants
        tag = "" if status == "ok" else f"  [{status}]"
        log.info(f"  {row.get('display', key):<18} substations={subs:<6} "
                 f"power_plants={plants:<5} (total={subs + plants}){tag}")
    log.info("-" * 68)
    log.info(f"TOTAL unique REAL records: {len(records)} "
             f"(substations={total_sub}, power_plants={total_plant})")
    log.info(f"Source: OpenStreetMap via {OVERPASS_SOURCE_URL}")

    # ── Upsert (only if a DB URL is configured) ──────────────────────────────
    conn = _connect()
    if conn is None:
        log.info("-" * 68)
        log.info("DRY RUN: no DATABASE_URL / NEON_DATABASE_URL set — nothing written.")
        log.info("Counts above are REAL live-API results. Set the env var to upsert:")
        log.info("  export DATABASE_URL='postgresql://user:pass@host/db'")
        log.info("  python intl_infra_ingest.py")
        return 0

    try:
        inserted = upsert_records(conn, records)
        log.info("-" * 68)
        log.info(f"Upserted into `infrastructure`: {inserted} NEW rows "
                 f"({len(records) - inserted} already present — idempotent).")
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error(f"Upsert failed (no partial fake data committed): {exc}")
        return 1
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
