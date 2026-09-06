"""
DC Hub — Land & Power Infrastructure Crawler
═══════════════════════════════════════════════
Automated ingestion of public energy infrastructure data to keep
DC Hub's Land & Power map current and growing.

Sources:
  - EIA-860:  Power plants (capacity, fuel type, status, coordinates)
  - EIA-923:  Monthly generation by plant
  - HIFLD:    Substations, transmission lines (US homeland infrastructure)
  - EIA NG:   Natural gas pipeline mileage by state/operator

Tables updated:
  - power_plants          (name, capacity_mw, fuel_type, lat, lon, operator, status)
  - substations           (name, voltage_kv, lat, lng, operator, state)
  - transmission_lines    (name, voltage_kv, from_sub, to_sub, length_miles, operator)
  - gas_pipelines         (name, operator, diameter_in, length_miles, state, commodity)
  - land_power_sync_log   (source, records_fetched, records_upserted, errors, duration_s)

Schedule: Daily 03:00 UTC via crawler_scheduler.py
Run manually: POST /api/jobs/land-power-sync (admin key required)

v1.0 — March 2026
"""

import os
import csv
import io
import json
import logging
import time
import zipfile
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

# error_detail strings built here are PERSISTED (land_power_sync_log) and then
# SERVED VERBATIM by the public /api/land-power/status route — and requests
# puts the full URL, query string included, into its exception text. That
# pairing published the complete EIA API key for months (rotated 2026-08-07).
from routes._iso_common import scrub_secrets

logger = logging.getLogger('dchub-land-power')

# ─────────────────────────────────────────────────────────────
# DATA SOURCE URLS
# ─────────────────────────────────────────────────────────────

# EIA-860: Annual Electric Generator Report (plant-level data)
# Updated annually, supplemented quarterly
# ★★★ 2026-07-31: was .../facility-fuel/data/, which returned HTTP 400 on EVERY
# call. Measured against the live API: `facility-fuel` does NOT expose
# nameplate-capacity-mw at all — its data columns are generation /
# gross-generation / total-consumption / *-btu / average-heat-content, and its
# plant facet is `plantCode`. Asking it for nameplate-capacity-mw is a malformed
# request, so the crawler could never have worked against this route.
#
# `operating-generator-capacity` is the EIA-860 inventory route and carries what
# this crawler actually needs: nameplate-capacity-mw, latitude, longitude,
# county, status, technology, prime_mover_code, sector, plantName, entityName —
# and its facet IS spelled `plantid`.
#
# ★ That also re-diagnoses #1923. The dedup step was "fixed" there by accepting
# plantCode/plantcode/plant_id spellings, on the theory that EIA had renamed the
# field. It had not: the crawler was querying the WRONG ROUTE, and the accepted
# spellings were the wrong route's field names. The multi-spelling fallback is
# kept below purely as a belt-and-braces guard, but on the correct route the
# FIRST key hits every row.
EIA_860_PLANTS_URL = "https://api.eia.gov/v2/electricity/operating-generator-capacity/data/"

# HIFLD Open Data: Homeland Infrastructure Foundation-Level Data
# Public GeoJSON endpoints — no API key needed
HIFLD_SUBSTATIONS_URL = "https://opendata.arcgis.com/api/v3/datasets/8cb9ba99d67a45e2a5bc0d3d7c2e5d16_0/downloads/data?format=geojson&spatialRefId=4326"
HIFLD_TRANSMISSION_URL = "https://opendata.arcgis.com/api/v3/datasets/70512b03fe994c6393107cc9946e5c22_0/downloads/data?format=geojson&spatialRefId=4326"

# EIA Natural Gas: Interstate pipeline data
EIA_NG_PIPELINES_URL = "https://api.eia.gov/v2/natural-gas/trans/ann/data/"

# Rate limiting
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT = 60
MAX_RETRIES = 3

# EIA API key (free — register at eia.gov)
EIA_API_KEY = os.environ.get("EIA_API_KEY", "")

HEADERS = {
    'User-Agent': 'DCHub-Intelligence/1.0 (dchub.cloud; data-center-research)',
    'Accept': 'application/json',
}


# ─────────────────────────────────────────────────────────────
# TABLE CREATION
# ─────────────────────────────────────────────────────────────

def init_land_power_tables(get_db):
    """Create/update land & power tables in PostgreSQL (Neon)."""
    conn = None
    try:
        conn = _ingest_conn(get_db)
        c = conn.cursor()

        # Power plants (EIA-860)
        c.execute("""
            CREATE TABLE IF NOT EXISTS power_plants (
                id SERIAL PRIMARY KEY,
                eia_plant_id VARCHAR(20),
                name VARCHAR(500),
                operator VARCHAR(500),
                state VARCHAR(10),
                county VARCHAR(200),
                city VARCHAR(200),
                lat DOUBLE PRECISION,
                lon DOUBLE PRECISION,
                capacity_mw DOUBLE PRECISION DEFAULT 0,
                fuel_type VARCHAR(100),
                fuel_category VARCHAR(100),
                prime_mover VARCHAR(50),
                status VARCHAR(50),
                operating_year INTEGER,
                sector VARCHAR(100),
                source VARCHAR(50) DEFAULT 'eia-860',
                last_updated TIMESTAMP DEFAULT NOW(),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS power_plants_eia_id_uniq
            ON power_plants (eia_plant_id)
        """)

        # Substations (HIFLD)
        c.execute("""
            CREATE TABLE IF NOT EXISTS substations (
                id SERIAL PRIMARY KEY,
                -- ★2026-07-30: these three were hifld_id / lon / last_updated
                -- here and in _upsert_substations, and NONE of them exist on the
                -- live table (36 columns; verified via information_schema).
                -- IF NOT EXISTS made this block a no-op against the real table,
                -- so the drift never surfaced here — it surfaced as an INSERT
                -- that could not run. Renamed to match live so a FRESH deploy
                -- creates a table this module can actually write to.
                -- NB: this DDL is a subset — the live table carries 36 columns
                -- (capacity_mva, naics_*, val_*, owner, ...). Do not treat it as
                -- the schema of record; information_schema is.
                hifld_objectid VARCHAR(50),
                name VARCHAR(500),
                operator VARCHAR(500),
                state VARCHAR(10),
                county VARCHAR(200),
                city VARCHAR(200),
                lat DOUBLE PRECISION,
                lng DOUBLE PRECISION,
                voltage_kv DOUBLE PRECISION DEFAULT 0,
                max_voltage_kv DOUBLE PRECISION DEFAULT 0,
                min_voltage_kv DOUBLE PRECISION DEFAULT 0,
                sub_type VARCHAR(100),
                status VARCHAR(50) DEFAULT 'operational',
                lines_count INTEGER DEFAULT 0,
                source VARCHAR(50) DEFAULT 'hifld',
                updated_at TIMESTAMP DEFAULT NOW(),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # ★ The index NAME still says hifld_id while its COLUMN is
        # hifld_objectid — that is deliberate and matches live exactly
        # (`substations_hifld_id_uniq ON public.substations (hifld_objectid)`).
        # Renaming the index would be a second migration for no gain; the stale
        # NAME is in fact the artifact that recorded the rename. Live also
        # carries two further unique indexes on the same column
        # (substations_hifld_objectid_uniq, idx_substations_hifld_oid).
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS substations_hifld_id_uniq
            ON substations (hifld_objectid)
        """)

        # Transmission lines (HIFLD)
        c.execute("""
            CREATE TABLE IF NOT EXISTS transmission_lines (
                id SERIAL PRIMARY KEY,
                hifld_id VARCHAR(50),
                name VARCHAR(500),
                operator VARCHAR(500),
                voltage_kv DOUBLE PRECISION DEFAULT 0,
                from_sub VARCHAR(500),
                to_sub VARCHAR(500),
                length_miles DOUBLE PRECISION DEFAULT 0,
                state VARCHAR(10),
                status VARCHAR(50) DEFAULT 'operational',
                line_type VARCHAR(100),
                source VARCHAR(50) DEFAULT 'hifld',
                last_updated TIMESTAMP DEFAULT NOW(),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS transmission_lines_hifld_id_uniq
            ON transmission_lines (hifld_id)
        """)

        # Gas pipelines — table may already exist from autonomous_brain.py
        c.execute("""
            CREATE TABLE IF NOT EXISTS gas_pipelines (
                id SERIAL PRIMARY KEY,
                name VARCHAR(500),
                operator VARCHAR(500),
                diameter_in DOUBLE PRECISION DEFAULT 0,
                length_miles DOUBLE PRECISION DEFAULT 0,
                state VARCHAR(10),
                commodity VARCHAR(100) DEFAULT 'natural_gas',
                status VARCHAR(50) DEFAULT 'operational',
                lat DOUBLE PRECISION,
                lon DOUBLE PRECISION,
                source VARCHAR(50) DEFAULT 'eia-ng',
                last_updated TIMESTAMP DEFAULT NOW(),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Use existing unique index if present
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS gas_pipelines_name_operator_uniq
            ON gas_pipelines (name, operator)
        """)

        # Sync log — tracks each crawler run
        c.execute("""
            CREATE TABLE IF NOT EXISTS land_power_sync_log (
                id SERIAL PRIMARY KEY,
                source VARCHAR(100),
                records_fetched INTEGER DEFAULT 0,
                records_upserted INTEGER DEFAULT 0,
                records_skipped INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                error_detail TEXT,
                duration_seconds DOUBLE PRECISION DEFAULT 0,
                run_type VARCHAR(20) DEFAULT 'incremental',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        conn.commit()
        logger.info("✅ Land & Power tables initialized")
    except Exception as e:
        logger.error(f"❌ Error initializing land_power tables: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────
# HTTP HELPERS
# ─────────────────────────────────────────────────────────────

def _ingest_conn(get_db):
    """A connection the INGEST OWNS for its whole run — never the web pool.

    ★★★ THE 60-SECOND FORCED RECLAIM. main.py's pool documents it in its own
    words: "Leaked idle-in-tx connections are reaped by Neon (20s), held-but-
    ACTIVE ones by the app's 60s FORCED RECLAIM." Every crawler here holds its
    connection across a paged fetch plus a bulk write — measured 130-160s on
    production — so the pool reclaims it mid-run and the next statement raises

        psycopg2.InterfaceError: connection already closed

    That is why the fetch always succeeded and the write never landed, and why
    power_plants stayed at 66 rows through the route fix (#1990), the reporter
    fix (#1994), the endpoint fix (#1996), the per-source runner (#1999) and the
    batched insert (#2003). Each was a real defect; none of them was THIS one.

    The web pool is sized and policed for sub-second request handlers. A
    minutes-long ingest is not that workload and must not borrow from it — it
    connects directly and closes in its own finally. This is the same rule the
    @contextmanager GC-close trap arrived at from the other direction: the
    caller must fully own the connection for its lifetime.

    Falls back to the pool only when no DSN is configured, so nothing breaks in
    environments that never set one.
    """
    dsn = (os.environ.get('DATABASE_URL')
           or os.environ.get('NEON_DATABASE_URL')
           or '').strip()
    if dsn:
        import psycopg2 as _pg
        conn = _pg.connect(dsn, connect_timeout=15,
                           keepalives=1, keepalives_idle=30,
                           keepalives_interval=10, keepalives_count=3)
        logger.info("  🔌 ingest owns a direct connection (pool bypassed — "
                    "the 60s forced reclaim would kill a multi-minute crawl)")
        return conn
    logger.warning("  ⚠️  no DATABASE_URL — falling back to the pooled "
                   "connection; a crawl over 60s will be reclaimed mid-run")
    return get_db()


def _fetch_json(url, params=None, retries=MAX_RETRIES, headers=None):
    """Fetch JSON with retry + rate limiting.

    headers= exists so a credential rides in a header: params= lands in the
    QUERY STRING, which every proxy logs. EIA reads X-Api-Key (verified
    2026-09-06). See tests/test_no_provider_key_in_url.py.
    """
    for attempt in range(retries):
        try:
            time.sleep(REQUEST_DELAY_SECONDS)
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT,
                                headers={**HEADERS, **(headers or {})})
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️  Fetch attempt {attempt + 1}/{retries} failed for {url}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                raise
    return None


# ─────────────────────────────────────────────────────────────
# SELF-HEALING ARCGIS SOURCE RESOLUTION
# ─────────────────────────────────────────────────────────────
# ★★★ WHY THIS EXISTS. Both HIFLD feeds were pinned to a single hardcoded
# opendata.arcgis.com dataset URL. Those URLs went HTTP 500 ("Item does not
# exist or is inaccessible") and STAYED dead for four months while
# /api/land-power/status reported "healthy". Every run recorded fetched=0,
# errors=1 — the evidence was there and nothing read it.
#
# A single pinned URL is a single point of silent failure for a feed nobody
# watches. These layers get republished under new org/service ids regularly, so
# the crawler now RESOLVES its endpoint at run time from an ordered candidate
# list and records which one it picked and why.
#
# ★★ VALIDATION IS COUNT *AND* FIELDS, NEVER HTTP 200. Measured 2026-07-31 while
# finding these: one substation candidate answers 200 with a perfectly valid
# FeatureServer carrying every expected field — and 128 rows, against a national
# layer of 75,328. "It responded" is not "it is the right layer". A candidate
# must clear a row floor AND expose the fields the parser reads, or the next
# outage is a silent 99.8% data loss instead of a visible zero.
#
# ★ Ordering is by preference, but a candidate below the floor is REJECTED, not
# ranked — measured, the transmission layer exists at 89,744 features on one org
# and 52,244 on another (the smaller one is a different population, close to
# transmission_lines_eia's 56,108, not the 94,626 maintained layer). Picking the
# first that merely responds would have quietly swapped the population.
#
# ★★ SH52-057 (2026-08-12) — THE TABLE ITSELF NOW LIVES IN util/hifld_layers.py.
# This module had worked out which transmission layer is real and defended it
# with the floor below; infrastructure_discovery.py meanwhile carried its OWN
# hardcoded URL pointing at the 52,244-feature layer this floor exists to
# reject, and fed the fiber-route lane from it. The definition moved to one
# importable place so the two cannot disagree again. The resolver, the floor
# and the field assertions below are UNCHANGED and still live here.
from util.hifld_layers import HIFLD_LAYERS as _ARCGIS_LAYERS  # noqa: E402


def _arcgis_probe(url):
    """(count, fields, error) for one candidate. Never raises.

    Uses the module's own _fetch_json (retry + rate limit + REQUEST_TIMEOUT); it
    takes no timeout kwarg, and passing one silently turned every probe into a
    TypeError verdict — which the resolver then reported as "no usable
    endpoint". Caught on the first live run precisely because the verdict list
    names the real exception per candidate instead of collapsing to a bare
    failure.
    """
    try:
        meta = _fetch_json(url, params={'f': 'json'})
        if not isinstance(meta, dict) or meta.get('error'):
            return None, None, f"metadata error: {str(meta.get('error'))[:120]}"
        fields = {f.get('name') for f in (meta.get('fields') or [])}
        cnt = _fetch_json(url + '/query', params={
            'where': '1=1', 'returnCountOnly': 'true', 'f': 'json'})
        if not isinstance(cnt, dict) or cnt.get('count') is None:
            return None, fields, "count query returned no count"
        return int(cnt['count']), fields, None
    except Exception as e:
        return None, None, f"{type(e).__name__}: {str(e)[:120]}"


def _resolve_arcgis_layer(key):
    """Pick the first candidate that clears BOTH the row floor and the fields.

    Returns (url, count, note). Raises RuntimeError listing every candidate's
    verdict if none qualify — so the sync log records WHY the whole layer is
    unavailable, not just that it is.
    """
    spec = _ARCGIS_LAYERS[key]
    verdicts = []
    for url in spec['candidates']:
        count, fields, err = _arcgis_probe(url)
        short = url.split('/services/')[-1][:48]
        if err:
            verdicts.append(f"{short}: {err}")
            continue
        missing = [f for f in spec['required_fields'] if f not in (fields or ())]
        if missing:
            verdicts.append(f"{short}: rows={count} but MISSING fields {missing}")
            continue
        if count < spec['min_rows']:
            verdicts.append(
                f"{short}: only {count} rows, below the {spec['min_rows']} floor "
                f"— a valid endpoint serving the wrong population")
            continue
        note = (f"resolved to {short} — {count} features, all "
                f"{len(spec['required_fields'])} required fields present"
                + (f"; rejected earlier candidates: {'; '.join(verdicts)}"
                   if verdicts else ""))
        logger.info(f"  🔎 {spec['label']}: {note}")
        return url, count, note
    raise RuntimeError(
        f"no usable endpoint for {spec['label']} — every candidate failed "
        f"validation (row floor {spec['min_rows']}, required fields "
        f"{list(spec['required_fields'])}): " + " | ".join(verdicts))


def _fetch_arcgis_geojson(url, page_size=2000, max_features=200000):
    """Page a FeatureServer as GeoJSON into the shape the parsers already expect.

    ArcGIS f=geojson returns features[].properties + features[].geometry, which
    is byte-for-byte the structure the substation/transmission parsers already
    read from the old opendata download — so nothing downstream changes.
    """
    feats = []
    offset = 0
    while True:
        page = _fetch_json(url + '/query', params={
            'where': '1=1', 'outFields': '*', 'f': 'geojson',
            'returnGeometry': 'true',
            'resultOffset': offset, 'resultRecordCount': page_size,
        })
        got = (page or {}).get('features') or []
        feats.extend(got)
        if len(got) < page_size or len(feats) >= max_features:
            break
        offset += page_size
        time.sleep(0.2)
    return {'type': 'FeatureCollection', 'features': feats}


def _fetch_geojson_stream(url, retries=MAX_RETRIES):
    """Fetch large GeoJSON files with streaming to conserve memory."""
    for attempt in range(retries):
        try:
            time.sleep(REQUEST_DELAY_SECONDS)
            resp = requests.get(url, headers=HEADERS, timeout=300, stream=True)
            resp.raise_for_status()
            # Read in chunks, then parse
            content = b""
            for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
                content += chunk
            return json.loads(content)
        except Exception as e:
            logger.warning(f"⚠️  GeoJSON fetch attempt {attempt + 1}/{retries} failed: {e}")
            if attempt < retries - 1:
                time.sleep(5)
            else:
                raise
    return None


# ─────────────────────────────────────────────────────────────
# FUEL TYPE CLASSIFICATION
# ─────────────────────────────────────────────────────────────

FUEL_CATEGORIES = {
    # Renewables
    'SUN': 'solar', 'WND': 'wind', 'WAT': 'hydro', 'GEO': 'geothermal',
    'WDS': 'biomass', 'BLQ': 'biomass', 'WDL': 'biomass', 'AB': 'biomass',
    'OBG': 'biogas', 'LFG': 'landfill_gas', 'OBL': 'biomass',
    # Fossil
    'NG': 'natural_gas', 'DFO': 'oil', 'RFO': 'oil', 'KER': 'oil',
    'PC': 'petroleum_coke', 'JF': 'jet_fuel', 'WO': 'oil',
    'SUB': 'coal', 'BIT': 'coal', 'LIG': 'coal', 'RC': 'coal',
    'ANT': 'coal', 'SC': 'coal', 'WC': 'coal',
    # Nuclear
    'NUC': 'nuclear', 'UR': 'nuclear',
    # Storage
    'MWH': 'battery_storage', 'BAT': 'battery_storage',
    # Other
    'PUR': 'purchased_steam', 'WH': 'waste_heat', 'TDF': 'tire_derived',
    'MSW': 'municipal_waste', 'OTH': 'other', 'OG': 'other_gas',
    'BFG': 'blast_furnace_gas', 'SG': 'syngas', 'H2': 'hydrogen',
}

def classify_fuel(fuel_code):
    """Map EIA fuel code to human-readable category."""
    if not fuel_code:
        return 'unknown'
    return FUEL_CATEGORIES.get(fuel_code.strip().upper(), 'other')


# ─────────────────────────────────────────────────────────────
# CRAWLER 1: POWER PLANTS (EIA-860 via EIA Open Data API)
# ─────────────────────────────────────────────────────────────

def _eia_latest_period():
    """Newest period operating-generator-capacity publishes, e.g. '2026-05'.

    Asked rather than assumed: EIA publishes on its own cadence, and hardcoding
    a period is how a crawler silently pins itself to a stale month (the
    year-pin class). Returns None on any failure, and the caller REFUSES to
    crawl rather than falling back to an unbounded all-periods window.
    """
    try:
        data = _fetch_json(EIA_860_PLANTS_URL,
                           headers={'X-Api-Key': EIA_API_KEY}, params={
            'frequency': 'monthly',
            'data[0]': 'nameplate-capacity-mw',
            'sort[0][column]': 'period',
            'sort[0][direction]': 'desc',
            'offset': 0,
            'length': 1,
        })
        rows = ((data or {}).get('response') or {}).get('data') or []
        return (rows[0].get('period') or '').strip() or None
    except Exception as e:
        logger.error(f"❌ could not read latest EIA period: {e}")
        return None


def crawl_power_plants(get_db, full_refresh=False):
    """
    Fetch power plant data from EIA API v2.
    Uses facility-fuel endpoint for plant-level capacity and fuel data.
    """
    started = time.time()
    fetched = 0
    upserted = 0
    errors = 0
    error_detail = []
    # ★★★ MUST be bound HERE, not inside the try below.
    # Regression shipped in #1990 and caught in production the same hour: this
    # counter was initialised inside the try, but the trailing _log_sync() that
    # REPORTS it sits after the try. So any exception before the initialisation
    # — i.e. exactly the upstream-failure case the log exists to record — hit
    # `NameError: dropped_no_plant_id` on the reporting line, which propagated
    # out of this function and was swallowed by run_land_power_sync's
    # per-step except. Net effect: a FAILING EIA crawl logged NOTHING AT ALL,
    # while the sibling crawlers (whose counters are bound at the top) logged
    # their failures normally. #1990's whole purpose was to make this feed's
    # failures visible; it made them invisible instead.
    #
    # ★ THE GENERAL RULE: a reporting call placed after a try must not depend on
    # any name bound inside that try. Bind every counter the reporter reads at
    # function scope, before the first thing that can raise.
    dropped_no_plant_id = 0
    sample_dropped_keys = None

    if not EIA_API_KEY:
        msg = "EIA_API_KEY not set — skipping power plant crawl. Get free key at eia.gov/opendata"
        logger.warning(f"⚠️  {msg}")
        _log_sync(get_db, 'eia-860-plants', 0, 0, 0, 1, msg, time.time() - started)
        return

    logger.info("🔌 Starting power plant crawl (EIA-860)...")

    conn = None
    try:
        # Paginate through EIA API
        offset = 0
        page_size = 5000
        all_plants = []

        # ★ frequency=monthly, NOT annual: operating-generator-capacity is only
        # published monthly, and `annual` is rejected. Pinning start=end=<latest>
        # matters more than it looks — without it the route returns EVERY period
        # it has ever published (4,780,710 rows measured 2026-07-31) and the
        # 50,000-row safety cap below would silently truncate to whichever
        # months happened to sort first. One period is 28,103 generator rows.
        _period = _eia_latest_period()
        if not _period:
            raise RuntimeError(
                "could not determine the latest EIA period — refusing to crawl "
                "an unbounded multi-period window")
        logger.info(f"  📅 EIA operating-generator-capacity period: {_period}")

        while True:
            params = {
                'frequency': 'monthly',
                'data[0]': 'nameplate-capacity-mw',
                'data[1]': 'latitude',
                'data[2]': 'longitude',
                'data[3]': 'county',
                'start': _period,
                'end': _period,
                'sort[0][column]': 'plantid',
                'sort[0][direction]': 'asc',
                'offset': offset,
                'length': page_size,
            }

            data = _fetch_json(EIA_860_PLANTS_URL, params=params,
                               headers={'X-Api-Key': EIA_API_KEY})
            if not data or 'response' not in data:
                break

            records = data['response'].get('data', [])
            if not records:
                break

            all_plants.extend(records)
            fetched += len(records)
            offset += page_size

            logger.info(f"  📊 Fetched {fetched} plant records so far...")

            # Safety limit — EIA has ~11,000 plants
            if offset > 50000 or len(records) < page_size:
                break

        logger.info(f"  📊 Total plant records fetched: {fetched}")

        # Deduplicate by plant_id (keep latest)
        #
        # 2026-07-29: this loop is why `power_plants` holds 66 rows for the whole
        # United States. It keyed only on rec['plantid'] and `continue`d on
        # anything without it — and the EIA v2 facility-fuel response does not
        # use that spelling for essentially any row. The 2026-03-30 run fetched
        # 55,000 records, upserted 66, and reported errors=0, because errors is
        # only incremented by the INSERT handler below: the other 54,934 never
        # reached it. /api/land-power/status called that outcome "healthy".
        #
        # Two fixes: accept the spellings EIA actually returns, and COUNT the
        # drops so a silent 99.9% loss can never again be reported as success.
        PLANT_ID_KEYS = ('plantid', 'plantCode', 'plantcode', 'plant_id',
                         'plantId', 'plant_code')
        plant_map = {}
        # (dropped_no_plant_id / sample_dropped_keys are bound at function scope
        # above — see the note there. Do NOT re-initialise them here.)
        for rec in all_plants:
            pid = ''
            for _k in PLANT_ID_KEYS:
                v = rec.get(_k)
                if v not in (None, ''):
                    pid = str(v)
                    break
            if not pid:
                dropped_no_plant_id += 1
                if sample_dropped_keys is None and isinstance(rec, dict):
                    sample_dropped_keys = sorted(rec.keys())[:15]
                continue
            # ★★ A ROW IS A GENERATOR, NOT A PLANT. operating-generator-capacity
            # publishes one row per generating UNIT: measured 2026-07-31, a
            # 5,000-row sample covered 1,371 distinct plants — 3.65 units per
            # plant. The previous logic kept the single highest-capacity unit and
            # wrote it as the plant, which under-states plant capacity by that
            # factor. Units are SUMMED per plantid instead.
            #
            # ★ STATUS IS NOT A PLANT PROPERTY. 131 of those 1,371 plants carry
            # MIXED unit statuses (OP/SB/OS/OA in the same plant), so there is no
            # single status to assert. We record the operating share instead:
            # `status` = 'OP' only when EVERY unit is OP, else 'MIXED', and the
            # counts ride along so a caller can see the split rather than trust a
            # flattened label.
            existing = plant_map.get(pid)
            cap = _safe_float(rec.get('nameplate-capacity-mw', 0)) or 0.0
            st = (rec.get('status') or '').strip().upper()
            if not existing:
                rec['_units'] = 1
                rec['_units_op'] = 1 if st == 'OP' else 0
                rec['_cap_sum'] = cap
                rec['_statuses'] = {st} if st else set()
                plant_map[pid] = rec
            else:
                existing['_units'] += 1
                existing['_units_op'] += 1 if st == 'OP' else 0
                existing['_cap_sum'] += cap
                if st:
                    existing['_statuses'].add(st)
                # keep the identity fields from the largest unit (they are
                # plant-level and identical across units, but this is stable)
                if cap > (_safe_float(existing.get('nameplate-capacity-mw', 0)) or 0.0):
                    for _f in ('plantName', 'entityName', 'stateid', 'county',
                               'latitude', 'longitude', 'technology',
                               'energy_source_code', 'prime_mover_code', 'sector'):
                        if rec.get(_f) not in (None, ''):
                            existing[_f] = rec[_f]
                    existing['nameplate-capacity-mw'] = rec.get('nameplate-capacity-mw')

        # Upsert into database
        conn = _ingest_conn(get_db)
        cur = conn.cursor()

        # ★★★ BATCHED, NOT ONE ROUND-TRIP PER PLANT. Measured 2026-07-31: the
        # per-row loop this replaces ran 13,000 individual INSERTs and the crawl
        # died at ~136s with `connection already closed` — the connection cannot
        # survive that many sequential round-trips plus a ~100s paged fetch, so
        # the fetch always succeeded and the WRITE never landed. power_plants sat
        # at 66 rows through every fix in this chain because of this line, not
        # because of the route.
        #
        # This is the identical lesson routes/hosting_capacity_ingest.py already
        # recorded: "Single-row round-trips took ~20ms each (60k rows ~ 20 min).
        # execute_values batches + per-source commits make progress durable."
        # Same repo, same failure, solved once already — worth grepping for
        # execute_values before writing another per-row upsert loop.
        from psycopg2.extras import execute_values
        # ★ `last_updated` is deliberately NOT in this column list. The per-row
        # statement this replaced ended `VALUES (%s ... %s, NOW())` — 15
        # placeholders plus a literal NOW() for a 16th column. execute_values
        # builds each tuple from the row alone, so keeping the column while
        # supplying 15 values raised, on EVERY chunk:
        #     INSERT has more target columns than expressions
        # The column carries DEFAULT NOW() in the DDL and the ON CONFLICT clause
        # sets it explicitly, so dropping it is correct on both paths: inserts
        # take the default, updates take NOW(). Column count and tuple width must
        # be equal — asserted in tests/test_land_power_insert_shape.py, because
        # a mismatch here fails only at execute time, against the live database.
        _PLANT_SQL = """
            INSERT INTO power_plants (
                eia_plant_id, name, operator, state, county, city,
                lat, lon, capacity_mw, fuel_type, fuel_category,
                prime_mover, status, sector, source
            ) VALUES %s
            ON CONFLICT (eia_plant_id) DO UPDATE SET
                name = EXCLUDED.name,
                operator = EXCLUDED.operator,
                capacity_mw = EXCLUDED.capacity_mw,
                fuel_type = EXCLUDED.fuel_type,
                fuel_category = EXCLUDED.fuel_category,
                status = EXCLUDED.status,
                last_updated = NOW()
        """
        rows = []
        for pid, rec in plant_map.items():
            rows.append((
                pid,
                _safe_str(rec.get('plantName', '')),
                _safe_str(rec.get('entityName', '')),
                _safe_str(rec.get('stateid', '')),
                _safe_str(rec.get('county', '')),
                '',
                _safe_float(rec.get('latitude')),
                _safe_float(rec.get('longitude')),
                round(rec.get('_cap_sum', 0.0), 3),
                _safe_str(rec.get('energy_source_code', '')),
                classify_fuel(rec.get('energy_source_code', '')),
                _safe_str(rec.get('prime_mover_code', '')),
                ('OP' if rec.get('_units')
                 and rec.get('_units_op') == rec.get('_units')
                 else ('MIXED' if len(rec.get('_statuses') or ()) > 1
                       else (sorted(rec.get('_statuses') or ['UNK'])[0]))),
                _safe_str(rec.get('sectorName', '')),
                'eia-860',
            ))
        # Commit per chunk so a connection lost midway leaves the rows already
        # written durable, instead of discarding the whole crawl.
        _CHUNK = 2000
        for k in range(0, len(rows), _CHUNK):
            chunk = rows[k:k + _CHUNK]
            try:
                execute_values(cur, _PLANT_SQL, chunk, page_size=500)
                conn.commit()
                upserted += len(chunk)
            except Exception as e:
                errors += 1
                try:
                    conn.rollback()
                except Exception:
                    pass
                if len(error_detail) < 10:
                    error_detail.append(
                        f"plants chunk {k}-{k + len(chunk)}: {str(e)[:120]}")
        conn.commit()
        logger.info(f"✅ Power plants: {upserted} upserted, {errors} errors")

        # A record dropped before the INSERT never touches `errors`, so a total
        # collapse used to look like a clean run. Make it loud and make it
        # countable.
        if dropped_no_plant_id:
            pct = (dropped_no_plant_id / fetched * 100) if fetched else 0.0
            msg = (f"DROPPED {dropped_no_plant_id} of {fetched} EIA records "
                   f"({pct:.1f}%) — no plant id under any of {PLANT_ID_KEYS}. "
                   f"Sample keys on a dropped record: {sample_dropped_keys}")
            logger.error("❌ " + msg)
            error_detail.append(msg[:400])
            if pct > 50:
                # More than half the payload discarded is a failed run, not a
                # partial one. Count it so no caller reads this as healthy.
                errors += 1

    except Exception as e:
        errors += 1
        error_detail.append(f"Fatal: {str(e)[:200]}")
        logger.error(f"❌ Power plant crawl failed: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

    duration = time.time() - started
    # ★ `skipped` is DROPPED RECORDS ONLY — not (fetched - upserted).
    # fetched counts GENERATORS and upserted counts PLANTS, so their difference
    # is the units-per-plant fold (measured 3.65x), which is correct behaviour,
    # not loss. Reporting it as "skipped" published a 73% loss rate on a healthy
    # run and would have made the real 99.9% loss of the #1923 era indistinguishable
    # from normal. dropped_no_plant_id is the only genuine drop counter.
    _log_sync(get_db, 'eia-860-plants', fetched, upserted, dropped_no_plant_id, errors,
              '; '.join(error_detail) if error_detail else None, duration)


# ─────────────────────────────────────────────────────────────
# CRAWLER 2: SUBSTATIONS (HIFLD Open Data)
# ─────────────────────────────────────────────────────────────

class LandPowerWriteBlocked(Exception):
    """A crawler deliberately refused to write. NOT a bug and NOT a crash.

    Distinct from Exception so the reporter can say "blocked" instead of
    "Fatal", and so a `except Exception` added later cannot silently swallow a
    refusal into the generic error path and make it look like a transient
    failure that will fix itself on the next cycle. It will not; only resolving
    identity will.
    """


# ★ ONE CONSTANT, ONE DECISION. Deliberately not an environment variable:
# routes/substation_ingest.py refuses the same write with a hard-coded 409, and
# a guard with an env escape hatch is a guard that gets flipped at 3am to make a
# red dashboard go green. Re-enabling is a code change with a review, in both
# places at once. See the block in crawl_substations() for the measurements.
SUBSTATION_WRITES_BLOCKED = True


def crawl_substations(get_db, full_refresh=False):
    """
    Fetch substation data from HIFLD (Homeland Infrastructure Foundation).
    Public GeoJSON — no API key needed.
    """
    started = time.time()
    # Bound HERE, not inside the try — a reporter must never read a name
    # its own try owns (the #1994 NameError-while-reporting regression).
    source_note = None
    fetched = 0
    upserted = 0
    errors = 0
    error_detail = []

    logger.info("⚡ Starting substation crawl (HIFLD)...")

    conn = None
    try:
        # Resolve the endpoint at run time instead of trusting a pin — the old
        # hardcoded opendata.arcgis.com URL has been HTTP 500 since at least
        # 2026-03-29 and every run recorded fetched=0 while /status said healthy.
        _url, _count, _note = _resolve_arcgis_layer('hifld-substations')
        source_note = _note
        geojson = _fetch_arcgis_geojson(_url)
        if not geojson or 'features' not in geojson:
            raise ValueError("No features in HIFLD substations response")

        features = geojson['features']
        fetched = len(features)
        logger.info(f"  ⚡ Fetched {fetched} substations (endpoint reported {_count})")

        # ★★★ WRITES ARE BLOCKED — this is the SECOND DOOR to a write that
        # routes/substation_ingest.py already refuses with 409 "writes disabled
        # pending an identity strategy" (landed 2026-07-31). That guard was put
        # on the admin route only, and THIS path was left open, so the 04:30
        # dispatcher walked straight past it every night.
        #
        # Do not remove this block to "make the feed work". Measured
        # 2026-08-02 against production, the numbers /status reports are:
        #
        #     fetched=75,328  upserted=2,000  errors=73,328  duration=4,740s
        #
        # and EVERY ONE of those three counts is misleading:
        #
        # · upserted=2,000 IS FICTION — NOTHING WAS WRITTEN. _ingest_conn hands
        #   back a plain psycopg2 connection (autocommit=False). The first
        #   UniqueViolation aborts the transaction; `conn.commit()` on an
        #   aborted transaction silently ROLLS BACK. Verified on the live table:
        #   0 rows created and 0 rows updated on 2026-08-02, count unmoved at
        #   126,846. Four months of "upserted" totals in this log are the same
        #   lie — a counter incremented in Python, never reconciled with COMMIT.
        # · errors=73,328 IS ONE ERROR, NOT 73,328. Row 2,001 raised the real
        #   UniqueViolation; rows 2,002..75,328 each raised
        #   InFailedSqlTransaction ("commands ignored until end of transaction
        #   block") against the already-dead transaction. A cascade reported as
        #   a population. (_upsert_substations now isolates rows — see there.)
        # · duration=4,740s IS 73,327 ROUND-TRIPS TO NEON THAT COULD NOT
        #   SUCCEED, 15x the dispatcher's 300s budget. The per-source step is
        #   non-fatal by design, but the request keeps running server-side after
        #   curl gives up at 300s: hifld-transmission has not logged a run since
        #   2026-07-31, because this one holds the worker for 79 minutes.
        #
        # ★ WHY 2,000 EXACTLY — it is not a coincidence, and it is the proof
        # that ON CONFLICT (hifld_objectid) is arbitrating a key that does not
        # identify anything. The 2026-07-31 canary in substation_ingest.py ran
        # with cap=2000; of those, 1,330 matched on (name, lat, lng) and had
        # their held twin's hifld_objectid CORRECTED to the real upstream ID.
        # The 670 it inserted were reverted; those 1,330 corrections were NOT.
        # Live today: exactly 1,330 rows carry hifld_objectid in 107,655..110,133,
        # all stamped 2026-07-31 03:04:47. So on 2026-08-02 upstream rows
        # 1..2,000 all "succeeded" (1,330 hit ON CONFLICT DO UPDATE on those
        # corrected ids, 670 inserted into the gap the revert left) and row
        # 2,001 — ID 110135, one past the canary's reach — found no corrected
        # id, inserted, and collided. The blocking row is id=4623,
        # hifld_objectid=2025, name='UNKNOWN110135'. The run's success count is
        # precisely the width of a canary that ran two days earlier.
        #
        # ★ THE ARBITER IS NOT THE BUG. ON CONFLICT (hifld_objectid) is valid
        # and matches a real full index (substations_hifld_id_uniq). The bug is
        # that this table carries EIGHT unique indexes and ON CONFLICT
        # arbitrates ONE; a violation of any other still raises. Nor is this
        # the partial-index trap: substations_name_lat_lng_uniq is NOT partial
        # (two siblings are — ix_substations_name_lat_lng and
        # idx_substations_hifld_oid — so that trap is live in this table, just
        # not what fires here).
        #
        # ★★ NEITHER CANDIDATE KEY IDENTIFIES A SUBSTATION TODAY:
        #   · hifld_objectid holds an ArcGIS export ROW NUMBER for 78,356 of
        #     79,686 held rows (1..79,687). Re-export in a different order and
        #     it names a different substation.
        #   · (name, lat, lng) is QUANTIZED — lat/lng are `real` (float4).
        #     Upstream ships geometry at full double precision; the March load
        #     stored the LATITUDE/LONGITUDE attributes rounded to 6dp. The same
        #     physical point collides or not depending on whether the two
        #     representations land on the same float4. Measured over 6,000
        #     upstream rows: 67.3% collide, 32.7% do not. An identity key that
        #     is a coin flip on floating-point rounding is not an identity key.
        #   · And the layer carries UNKNOWN<id> placeholders where the held data
        #     has validated names ('UNKNOWN107657' vs held 'HOLCOMBE'), which is
        #     why the canary's 670 inserts were duplicates with WORSE names.
        #     Extrapolated to 75,328 rows: ~25,000 duplicate substations, and
        #     "126k substations" is a published headline figure.
        #
        # ★★★ 2026-08-12 (SH52-056) — IDENTITY IS RESOLVED. THE BACKFILL IS NOT.
        # Everything above stays true about WHY this was blocked; what changed
        # is that the missing piece now exists and the blocker has moved.
        #
        # RESOLVED: substations.hifld_id holds the upstream HIFLD `ID` under a
        # partial unique index (migrations/2026-08-12_substation_hifld_id_
        # identity.sql). Measured on the live layer 2026-08-12 by paging all
        # 75,328 records: ID populated 75,328/75,328, 75,327 DISTINCT, values
        # namespaced at 107,655+ while OBJECTID is 1..N and matched ID on 0 of
        # 2,000 sampled rows. hifld_objectid is left exactly as it is.
        #
        # ALSO RESOLVED: the "~25,000 duplicates under UNKNOWN<id> names" figure
        # above came from extrapolating a canary that matched on
        # (name, lat, lng). 38,479 of 75,328 upstream names (51.1%) are
        # `UNKNOWN` + the row's own ID, so that match was always going to miss.
        # Matching on coordinate alone, the true picture is:
        #     71,006 upstream assets link to a held row
        #      4,212 genuinely new   <- the actual recovery
        #      8,552 held keys upstream no longer lists (report, never delete)
        #        235 ambiguous coordinate keys (skip; they need a human)
        #
        # STILL BLOCKING, and it is a bounded UPDATE rather than a design
        # question: 78,356 held rows have hifld_id NULL, so keyed on the new
        # column every upstream record reads as new. Run the link pass in the
        # migration, then clear this flag and the 409 in
        # routes/substation_ingest.py TOGETHER — this crawler is the second
        # door and was left open once already.
        if SUBSTATION_WRITES_BLOCKED:
            raise LandPowerWriteBlocked(
                f"fetched {fetched} rows and wrote none — writes to "
                f"`substations` are blocked pending the hifld_id link "
                f"backfill. Identity is RESOLVED (upstream HIFLD `ID` -> "
                f"substations.hifld_id, 75,328/75,328 populated and 75,327 "
                f"distinct), but 78,356 of 79,686 held rows still have "
                f"hifld_id NULL, so a full run would insert ~75,000 rows "
                f"beside the rows they duplicate. Recovery once linked is "
                f"+4,212 genuinely new substations, measured 2026-08-12. "
                f"POST /api/v1/admin/ingest/substations?reconcile_report=1 "
                f"re-measures it. See "
                f"migrations/2026-08-12_substation_hifld_id_identity.sql."
            )

        conn = _ingest_conn(get_db)
        cur = conn.cursor()
        batch = []

        for feat in features:
            props = feat.get('properties', {})
            geom = feat.get('geometry', {})
            coords = geom.get('coordinates', [None, None]) if geom else [None, None]

            hifld_id = str(props.get('ID', props.get('OBJECTID', '')))
            if not hifld_id:
                continue

            batch.append((
                hifld_id,
                _safe_str(props.get('NAME', '')),
                _safe_str(props.get('OWNER', props.get('OPERATOR', ''))),
                _safe_str(props.get('STATE', '')),
                _safe_str(props.get('COUNTY', '')),
                _safe_str(props.get('CITY', '')),
                _safe_float(coords[1]) if len(coords) > 1 else None,  # lat
                _safe_float(coords[0]) if len(coords) > 0 else None,  # lon
                _safe_float(props.get('MAX_VOLT', props.get('VOLTAGE', 0))),
                _safe_float(props.get('MAX_VOLT', 0)),
                _safe_float(props.get('MIN_VOLT', 0)),
                _safe_str(props.get('TYPE', '')),
                _safe_str(props.get('STATUS', 'operational')),
                _safe_int(props.get('LINES', 0)),
            ))

            # Batch insert every 1000
            if len(batch) >= 1000:
                u, e, msg = _upsert_substations(cur, batch)
                upserted += u
                errors += e
                if msg and not error_detail:
                    error_detail.append(f"row upsert: {msg}")
                batch = []

        # Final batch
        if batch:
            u, e, msg = _upsert_substations(cur, batch)
            upserted += u
            errors += e
            if msg and not error_detail:
                error_detail.append(f"row upsert: {msg}")

        conn.commit()
        # A run that fetched rows and wrote NONE is a failure, not a quiet
        # success — say so at ERROR with the reason, and never let the caller
        # read `upserted=0, errors=N` as a healthy no-op. (The sibling
        # eia-860-plants run reported errors=0 while dropping 54,934 of 55,000
        # records, which is exactly how a dead table got published for months.)
        if fetched and not upserted:
            logger.error(f"❌ Substations: fetched {fetched}, upserted ZERO "
                         f"({errors} row errors). First: {error_detail[0] if error_detail else 'n/a'}")
        else:
            logger.info(f"✅ Substations: {upserted} upserted, {errors} errors")

    except LandPowerWriteBlocked as e:
        # A deliberate refusal, not a crash — so it is NOT logged as "Fatal".
        # It still counts as a failed run: this feed has never landed a row and
        # /status must keep saying so. Recorded as errors=1 so that no consumer
        # keying off `errors` alone can read a blocked feed as healthy, and so
        # `verdict` stays never_succeeded rather than drifting to a flattering
        # zero. The fetch above DID run and IS the useful part — it exercises
        # the self-healing endpoint resolution daily, which is the check that
        # would catch the next dead-URL outage.
        errors += 1
        error_detail.append(f"BLOCKED: {str(e)[:400]}")
        logger.error(f"⛔ Substations: {e}")
    except Exception as e:
        errors += 1
        error_detail.append(f"Fatal: {str(e)[:200]}")
        logger.error(f"❌ Substation crawl failed: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

    duration = time.time() - started
    _log_sync(get_db, 'hifld-substations', fetched, upserted, fetched - upserted, errors,
              ('; '.join(error_detail) if error_detail
               else source_note), duration)


def _upsert_substations(cur, batch):
    """Batch upsert substations. Returns (upserted_count, error_count, first_error).

    ★ 2026-07-30 — THREE of the sixteen columns this statement named DO NOT EXIST
    on the live `substations` table. Verified against information_schema, not the
    repo DDL (the house rule: the live table is the truth):

        named here     live column       status
        hifld_id       hifld_objectid    ABSENT — renamed
        lon            lng               ABSENT — renamed
        last_updated   updated_at        ABSENT — renamed

    …and `ON CONFLICT (hifld_id)` named the same absent column. So every row in
    every batch would raise UndefinedColumn. The smoking gun is still in the
    schema: the unique index is NAMED `substations_hifld_id_uniq` but is defined
    ON `hifld_objectid` — the column was renamed and the index name and this
    crawler were both left behind.

    ★★ THIS IS THE SECOND FAULT, NOT THE ONE THAT FIRES. Establish which error
    actually reaches the caller before naming a cause (the lesson from #1933,
    where a client-side binding error masked an UndefinedColumn I had blamed).
    The upstream ArcGIS dataset is GONE, so the fetch dies long before any SQL:

        HIFLD_SUBSTATIONS_URL -> HTTP 500
        {"errors":{"message":"Item does not exist or is inaccessible."}}

    ★★★ 2026-08-02 — THE PARAGRAPH BELOW WAS TRUE WHEN WRITTEN AND IS NOW FALSE.
    "This statement has NEVER executed against the live table" held only until
    the endpoint self-healing in #1996 revived the fetch. It has now executed:
    on 2026-08-02 this function ran 75,328 times against production. Nothing
    landed, and the run reported `upserted=2,000` anyway. Both facts are this
    function's doing, and both are fixed here:

    · THE COUNTER LIED. `upserted += 1` counts a cur.execute() that returned
      without raising — which is NOT the same as a row that survives to COMMIT.
      The caller holds ONE transaction for all 76 batches. The first
      UniqueViolation aborted it, and `conn.commit()` on an aborted transaction
      silently ROLLS BACK. So 2,000 rows were counted, reported, and discarded.
      Verified on the live table: 0 created, 0 updated that day.
    · THE ERROR COUNT LIED. Once the transaction is aborted, every subsequent
      execute raises InFailedSqlTransaction — so 73,327 healthy rows were
      recorded as individual failures of their own. One real error was reported
      as 73,328, and cost 4,740 seconds of round-trips that could not succeed.

    Both come from the same omission: a per-row try/except around a statement
    that shares a transaction with every other row gives the APPEARANCE of row
    isolation with none of the substance. The fix is a real SAVEPOINT per row —
    then a failure rolls back exactly that row, the transaction stays live, and
    `upserted` counts rows that are genuinely still there at COMMIT.

    ★ Reachable only if SUBSTATION_WRITES_BLOCKED is cleared — crawl_substations
    refuses before it gets here. Kept correct rather than deleted so that
    whoever resolves identity inherits a function that counts honestly.

    and land_power_sync_log agrees — every `hifld-substations` run records
    fetched=0, upserted=0, errors=1, "Fatal: 500 Server Error", most recently
    2026-03-30. So this statement has NEVER executed against the live table and
    the column drift has never once fired. It is repaired here as a LANDMINE
    REMOVAL: whoever revives the feed must not also have to rediscover this.

    ★ The table is NOT stale despite that. `substations` holds 126,842 rows
    (79,686 with source='HIFLD') maintained by hifld_substation_loader.py, which
    writes `lng` correctly — updated_at as recent as 2026-07-30. This crawler is
    superseded, not load-bearing. Do not "fix the columns" and conclude the
    crawler works; it still cannot fetch.
    """
    upserted = 0
    errors = 0
    first_error = None
    for row in batch:
        # ★ A REAL SAVEPOINT, NOT try/except THEATRE. Without this, one
        # UniqueViolation poisons the caller's whole transaction: every later
        # row raises InFailedSqlTransaction and the eventual commit() throws
        # away the rows that DID work. try/except alone cannot isolate a
        # statement that shares a transaction — only a savepoint can.
        # (SAVEPOINT requires a transaction block, which is exactly what
        # _ingest_conn gives us: a plain psycopg2 connection, autocommit=False.
        # Do not "simplify" this by setting autocommit — see
        # reference_psycopg2_savepoint_autocommit_trap.)
        try:
            cur.execute("SAVEPOINT sub_row")
        except Exception as e:
            # The transaction is already unusable, or this cursor is a stub.
            # Either way, keep the loop's accounting honest and move on.
            errors += 1
            if first_error is None:
                first_error = str(e)[:200]
            continue
        try:
            cur.execute("""
                INSERT INTO substations (
                    hifld_objectid, name, operator, state, county, city,
                    lat, lng, voltage_kv, max_voltage_kv, min_voltage_kv,
                    sub_type, status, lines_count, source, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'hifld', NOW() ON CONFLICT DO NOTHING)
                ON CONFLICT (hifld_objectid)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    operator = EXCLUDED.operator,
                    voltage_kv = EXCLUDED.voltage_kv,
                    max_voltage_kv = EXCLUDED.max_voltage_kv,
                    min_voltage_kv = EXCLUDED.min_voltage_kv,
                    lines_count = EXCLUDED.lines_count,
                    status = EXCLUDED.status,
                    updated_at = NOW()
            """, row)
            cur.execute("RELEASE SAVEPOINT sub_row")
            upserted += 1
        except Exception as e:
            # Was `except Exception as e: errors += 1` — `e` bound and never
            # read, so a 100%-failing batch was indistinguishable from a
            # 100%-succeeding one in everything except a count nobody surfaced.
            # Carry the first message out so the caller can log WHAT failed.
            errors += 1
            if first_error is None:
                first_error = str(e)[:200]
            # Undo just this row. Without it the next iteration's SAVEPOINT
            # raises too and the whole batch degrades to the cascade this
            # function used to report as 73,328 separate errors.
            try:
                cur.execute("ROLLBACK TO SAVEPOINT sub_row")
            except Exception:
                pass
    return upserted, errors, first_error


# ─────────────────────────────────────────────────────────────
# CRAWLER 3: TRANSMISSION LINES (HIFLD Open Data)
# ─────────────────────────────────────────────────────────────

def crawl_transmission_lines(get_db, full_refresh=False):
    """
    Fetch transmission line data from HIFLD.
    Public GeoJSON — no API key needed.
    """
    started = time.time()
    # Bound HERE, not inside the try — a reporter must never read a name
    # its own try owns (the #1994 NameError-while-reporting regression).
    source_note = None
    fetched = 0
    upserted = 0
    errors = 0
    error_detail = []

    logger.info("🔗 Starting transmission line crawl (HIFLD)...")

    conn = None
    try:
        _url, _count, _note = _resolve_arcgis_layer('hifld-transmission')
        source_note = _note
        geojson = _fetch_arcgis_geojson(_url)
        if not geojson or 'features' not in geojson:
            raise ValueError("No features in HIFLD transmission response")

        features = geojson['features']
        fetched = len(features)
        logger.info(f"  🔗 Fetched {fetched} transmission lines")

        conn = _ingest_conn(get_db)
        cur = conn.cursor()
        batch = []

        for feat in features:
            props = feat.get('properties', {})

            hifld_id = str(props.get('ID', props.get('OBJECTID', '')))
            if not hifld_id:
                continue

            # Calculate length from geometry if available
            length_miles = _safe_float(
                props.get('SHAPE_Length',
                    props.get('SHAPE_Leng',
                        props.get('Shape__Length',
                            props.get('LENGTH', 0)))))
            # HIFLD sometimes gives length in meters, convert
            if length_miles and length_miles > 10000:
                length_miles = length_miles * 0.000621371  # meters to miles

            batch.append((
                hifld_id,
                _safe_str(props.get('ID', '')),
                _safe_str(props.get('OWNER', props.get('OPERATOR', ''))),
                _safe_float(props.get('VOLTAGE', 0)),
                _safe_str(props.get('SUB_1', '')),
                _safe_str(props.get('SUB_2', '')),
                length_miles,
                _safe_str(props.get('STATE', '')),
                _safe_str(props.get('STATUS', 'operational')),
                _safe_str(props.get('TYPE', '')),
            ))

            if len(batch) >= 1000:
                u, e = _upsert_transmission(cur, batch)
                upserted += u
                errors += e
                batch = []

        if batch:
            u, e = _upsert_transmission(cur, batch)
            upserted += u
            errors += e

        conn.commit()
        logger.info(f"✅ Transmission lines: {upserted} upserted, {errors} errors")

    except Exception as e:
        errors += 1
        error_detail.append(f"Fatal: {str(e)[:200]}")
        logger.error(f"❌ Transmission line crawl failed: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

    duration = time.time() - started
    _log_sync(get_db, 'hifld-transmission', fetched, upserted, fetched - upserted, errors,
              ('; '.join(error_detail) if error_detail
               else source_note), duration)


def _upsert_transmission(cur, batch):
    """Batch upsert transmission lines."""
    upserted = 0
    errors = 0
    for row in batch:
        try:
            cur.execute("""
                INSERT INTO transmission_lines (
                    hifld_id, name, operator, voltage_kv, from_sub, to_sub,
                    length_miles, state, status, line_type, source, last_updated
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'hifld', NOW() ON CONFLICT DO NOTHING)
                ON CONFLICT (hifld_id)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    operator = EXCLUDED.operator,
                    voltage_kv = EXCLUDED.voltage_kv,
                    length_miles = EXCLUDED.length_miles,
                    status = EXCLUDED.status,
                    last_updated = NOW()
            """, row)
            upserted += 1
        except Exception as e:
            errors += 1
    return upserted, errors


# ─────────────────────────────────────────────────────────────
# CRAWLER 4: GAS PIPELINES (EIA Natural Gas API)
# ─────────────────────────────────────────────────────────────

def crawl_gas_pipelines(get_db, full_refresh=False):
    """
    Fetch gas pipeline operator/state data from EIA API v2.
    This supplements the existing gas_pipelines table with
    fresh operator and capacity data.
    """
    started = time.time()
    fetched = 0
    upserted = 0
    errors = 0
    error_detail = []

    if not EIA_API_KEY:
        msg = "EIA_API_KEY not set — skipping gas pipeline crawl"
        logger.warning(f"⚠️  {msg}")
        _log_sync(get_db, 'eia-ng-pipelines', 0, 0, 0, 1, msg, time.time() - started)
        return

    logger.info("🔥 Starting gas pipeline crawl (EIA NG)...")

    conn = None
    try:
        offset = 0
        page_size = 5000
        all_records = []

        while True:
            params = {
                'frequency': 'annual',
                'data[0]': 'value',
                'facets[process][]': ['FPR'],  # Pipeline receipts
                'sort[0][column]': 'period',
                'sort[0][direction]': 'desc',
                'offset': offset,
                'length': page_size,
            }

            data = _fetch_json(EIA_NG_PIPELINES_URL, params=params,
                               headers={'X-Api-Key': EIA_API_KEY})
            if not data or 'response' not in data:
                break

            records = data['response'].get('data', [])
            if not records:
                break

            all_records.extend(records)
            fetched += len(records)
            offset += page_size

            if offset > 20000 or len(records) < page_size:
                break

        logger.info(f"  🔥 Fetched {fetched} gas pipeline records")

        # Aggregate by pipeline/operator
        pipe_map = {}
        for rec in all_records:
            key = (
                _safe_str(rec.get('series-description', rec.get('duoarea', ''))),
                _safe_str(rec.get('area-name', ''))
            )
            if key not in pipe_map:
                pipe_map[key] = rec

        conn = _ingest_conn(get_db)
        cur = conn.cursor()

        for (name, area), rec in pipe_map.items():
            if not name:
                continue
            try:
                cur.execute("""
                    INSERT INTO gas_pipelines (name, operator, state, commodity, source, last_updated)
                    VALUES (%s, %s, %s, %s, 'eia-ng', NOW() ON CONFLICT DO NOTHING)
                    ON CONFLICT (name, operator)
                    DO UPDATE SET
                        state = COALESCE(EXCLUDED.state, gas_pipelines.state),
                        last_updated = NOW()
                """, (
                    name[:500],
                    _safe_str(rec.get('area-name', ''))[:500],
                    _safe_str(rec.get('stateid', ''))[:10],
                    'natural_gas',
                ))
                upserted += 1
            except Exception as e:
                errors += 1
                if len(error_detail) < 10:
                    error_detail.append(f"Pipeline {name[:50]}: {str(e)[:100]}")

        conn.commit()
        logger.info(f"✅ Gas pipelines: {upserted} upserted, {errors} errors")

    except Exception as e:
        errors += 1
        error_detail.append(f"Fatal: {str(e)[:200]}")
        logger.error(f"❌ Gas pipeline crawl failed: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

    duration = time.time() - started
    _log_sync(get_db, 'eia-ng-pipelines', fetched, upserted, fetched - upserted, errors,
              '; '.join(error_detail) if error_detail else None, duration)


# ─────────────────────────────────────────────────────────────
# MARKET POWER PROFILES (auto-generated per market)
# ─────────────────────────────────────────────────────────────

def generate_market_power_profiles(get_db):
    """
    Auto-generate power infrastructure summaries for each DC Hub market.
    Calculates substation density, transmission capacity, pipeline access,
    and nearest power plant stats.
    """
    started = time.time()
    conn = None
    try:
        conn = _ingest_conn(get_db)
        cur = conn.cursor()

        # Create profiles table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS market_power_profiles (
                id SERIAL PRIMARY KEY,
                market VARCHAR(200) UNIQUE,
                state VARCHAR(10),
                substation_count INTEGER DEFAULT 0,
                avg_voltage_kv DOUBLE PRECISION DEFAULT 0,
                max_voltage_kv DOUBLE PRECISION DEFAULT 0,
                transmission_line_count INTEGER DEFAULT 0,
                total_transmission_miles DOUBLE PRECISION DEFAULT 0,
                gas_pipeline_count INTEGER DEFAULT 0,
                power_plant_count INTEGER DEFAULT 0,
                total_generation_mw DOUBLE PRECISION DEFAULT 0,
                solar_mw DOUBLE PRECISION DEFAULT 0,
                wind_mw DOUBLE PRECISION DEFAULT 0,
                natural_gas_mw DOUBLE PRECISION DEFAULT 0,
                nuclear_mw DOUBLE PRECISION DEFAULT 0,
                coal_mw DOUBLE PRECISION DEFAULT 0,
                battery_storage_mw DOUBLE PRECISION DEFAULT 0,
                renewable_pct DOUBLE PRECISION DEFAULT 0,
                power_readiness_score INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT NOW()
            )
        """)

        # DC Hub market → state mapping
        MARKET_STATES = {
            'Northern Virginia': 'VA', 'Dallas-Fort Worth': 'TX', 'Phoenix': 'AZ',
            'Chicago': 'IL', 'Atlanta': 'GA', 'Portland': 'OR', 'Salt Lake City': 'UT',
            'Columbus': 'OH', 'Northern California': 'CA', 'Southern California': 'CA',
            'New York Metro': 'NJ', 'Seattle': 'WA', 'Denver': 'CO', 'Houston': 'TX',
            'Minneapolis': 'MN', 'Las Vegas': 'NV', 'Kansas City': 'MO',
            'Sacramento': 'CA', 'San Antonio': 'TX', 'Austin': 'TX',
            'Nashville': 'TN', 'Charlotte': 'NC', 'Raleigh-Durham': 'NC',
            'Tampa Bay': 'FL', 'Miami': 'FL', 'Pittsburgh': 'PA',
            'St. Louis': 'MO', 'Indianapolis': 'IN', 'Omaha': 'NE',
            'Des Moines': 'IA', 'Reno': 'NV', 'Boise': 'ID',
            'Albuquerque': 'NM', 'Hillsboro': 'OR', 'Quincy': 'WA',
            'Papillion': 'NE', 'Council Bluffs': 'IA', 'Elk Grove': 'CA',
            'Prineville': 'OR', 'The Dalles': 'OR', 'Moses Lake': 'WA',
            'Cheyenne': 'WY',
        }

        profiles_updated = 0

        for market, state in MARKET_STATES.items():
            try:
                # Substation stats
                cur.execute("""
                    SELECT COUNT(*), COALESCE(AVG(voltage_kv), 0), COALESCE(MAX(max_voltage_kv), 0)
                    FROM substations WHERE state = %s
                """, (state,))
                sub_count, avg_volt, max_volt = cur.fetchone()

                # Transmission stats
                cur.execute("""
                    SELECT COUNT(*), COALESCE(SUM(length_miles), 0)
                    FROM transmission_lines WHERE state = %s
                """, (state,))
                tx_count, tx_miles = cur.fetchone()

                # Gas pipeline stats
                cur.execute("""
                    SELECT COUNT(*) FROM gas_pipelines WHERE state = %s
                """, (state,))
                gas_count = cur.fetchone()[0]

                # Power plant stats by fuel category
                cur.execute("""
                    SELECT
                        COUNT(*),
                        COALESCE(SUM(capacity_mw), 0),
                        COALESCE(SUM(CASE WHEN fuel_category = 'solar' THEN capacity_mw ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN fuel_category = 'wind' THEN capacity_mw ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN fuel_category = 'natural_gas' THEN capacity_mw ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN fuel_category = 'nuclear' THEN capacity_mw ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN fuel_category = 'coal' THEN capacity_mw ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN fuel_category = 'battery_storage' THEN capacity_mw ELSE 0 END), 0)
                    FROM power_plants WHERE state = %s
                """, (state,))
                pp_count, total_mw, solar, wind, ng, nuc, coal, batt = cur.fetchone()

                # Calculate renewable percentage
                renewable_mw = (solar or 0) + (wind or 0)
                renewable_pct = (renewable_mw / total_mw * 100) if total_mw > 0 else 0

                # Power readiness score (0-100)
                score = _calculate_power_score(
                    sub_count, avg_volt, tx_count, tx_miles,
                    gas_count, total_mw, renewable_pct
                )

                # Upsert profile
                cur.execute("""
                    INSERT INTO market_power_profiles (
                        market, state, substation_count, avg_voltage_kv, max_voltage_kv,
                        transmission_line_count, total_transmission_miles, gas_pipeline_count,
                        power_plant_count, total_generation_mw, solar_mw, wind_mw,
                        natural_gas_mw, nuclear_mw, coal_mw, battery_storage_mw,
                        renewable_pct, power_readiness_score, last_updated
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
                    ON CONFLICT (market)
                    DO UPDATE SET
                        substation_count = EXCLUDED.substation_count,
                        avg_voltage_kv = EXCLUDED.avg_voltage_kv,
                        max_voltage_kv = EXCLUDED.max_voltage_kv,
                        transmission_line_count = EXCLUDED.transmission_line_count,
                        total_transmission_miles = EXCLUDED.total_transmission_miles,
                        gas_pipeline_count = EXCLUDED.gas_pipeline_count,
                        power_plant_count = EXCLUDED.power_plant_count,
                        total_generation_mw = EXCLUDED.total_generation_mw,
                        solar_mw = EXCLUDED.solar_mw,
                        wind_mw = EXCLUDED.wind_mw,
                        natural_gas_mw = EXCLUDED.natural_gas_mw,
                        nuclear_mw = EXCLUDED.nuclear_mw,
                        coal_mw = EXCLUDED.coal_mw,
                        battery_storage_mw = EXCLUDED.battery_storage_mw,
                        renewable_pct = EXCLUDED.renewable_pct,
                        power_readiness_score = EXCLUDED.power_readiness_score,
                        last_updated = NOW()
                """, (
                    market, state, sub_count, avg_volt, max_volt,
                    tx_count, tx_miles, gas_count,
                    pp_count, total_mw, solar, wind,
                    ng, nuc, coal, batt,
                    renewable_pct, score,
                ))
                profiles_updated += 1

            except Exception as e:
                logger.warning(f"⚠️  Error generating profile for {market}: {e}")

        conn.commit()
        logger.info(f"✅ Market power profiles: {profiles_updated} markets updated")

    except Exception as e:
        logger.error(f"❌ Market power profiles failed: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()


def _calculate_power_score(sub_count, avg_volt, tx_count, tx_miles,
                           gas_count, total_mw, renewable_pct):
    """
    Calculate Power Readiness Score (0-100) for a market.
    Weighted composite of infrastructure density and capacity.
    """
    score = 0

    # Substation density (0-25 points)
    # 100+ substations = full marks
    score += min(25, (sub_count or 0) / 100 * 25)

    # Voltage tier (0-15 points)
    # 345kV+ = full marks
    if avg_volt and avg_volt >= 345:
        score += 15
    elif avg_volt and avg_volt >= 230:
        score += 12
    elif avg_volt and avg_volt >= 138:
        score += 8
    elif avg_volt and avg_volt >= 69:
        score += 4

    # Transmission capacity (0-20 points)
    # 1000+ miles of transmission = full marks
    score += min(20, (tx_miles or 0) / 1000 * 20)

    # Gas pipeline access (0-10 points)
    score += min(10, (gas_count or 0) / 20 * 10)

    # Generation capacity (0-20 points)
    # 10,000 MW+ = full marks
    score += min(20, (total_mw or 0) / 10000 * 20)

    # Renewable percentage bonus (0-10 points)
    score += min(10, (renewable_pct or 0) / 50 * 10)

    return min(100, int(score))


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _safe_str(val, default=''):
    """Safely convert to string."""
    if val is None:
        return default
    return str(val).strip()[:500]

def _safe_float(val, default=None):
    """Safely convert to float."""
    if val is None or val == '':
        return default
    try:
        return float(str(val).replace(',', ''))
    except (ValueError, TypeError):
        return default

def _safe_int(val, default=0):
    """Safely convert to int."""
    if val is None or val == '':
        return default
    try:
        return int(float(str(val).replace(',', '')))
    except (ValueError, TypeError):
        return default


def _log_sync(get_db, source, fetched, upserted, skipped, errors, detail, duration):
    """Log a sync run to land_power_sync_log."""
    # error_detail is served by the public /status route, so no credential may
    # be persisted. The status route scrubs again on the way out — rows written
    # by older deploys (heroic-reprieve still runs a pre-fix monolith against
    # this same table) don't pass through this function.
    if detail:
        detail = scrub_secrets(detail)
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO land_power_sync_log
            (source, records_fetched, records_upserted, records_skipped, errors, error_detail, duration_seconds)
            VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
        """, (source, fetched, upserted, skipped, errors, detail, round(duration, 2)))
        conn.commit()
    except Exception as e:
        logger.warning(f"⚠️  Could not log sync: {e}")
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────
# MASTER RUNNER (called by crawler_scheduler.py or manual trigger)
# ─────────────────────────────────────────────────────────────

def run_land_power_sync(get_db, full_refresh=False):
    """
    Master function to run all land & power crawlers sequentially.
    Called by crawler_scheduler.py at 03:00 UTC daily.

    Args:
        get_db: Database connection factory function
        full_refresh: If True, re-fetch all data (vs incremental)
    """
    started = time.time()
    logger.info("=" * 60)
    logger.info("🗺️  LAND & POWER SYNC STARTING")
    logger.info(f"   Mode: {'full refresh' if full_refresh else 'incremental'}")
    logger.info("=" * 60)

    # Step 0: Ensure tables exist
    init_land_power_tables(get_db)

    # Step 1: Power plants (EIA) — needs API key
    try:
        crawl_power_plants(get_db, full_refresh)
    except Exception as e:
        logger.error(f"❌ Power plants crawl failed: {e}")

    # Step 2: Substations (HIFLD) — no key needed
    try:
        crawl_substations(get_db, full_refresh)
    except Exception as e:
        logger.error(f"❌ Substations crawl failed: {e}")

    # Step 3: Transmission lines (HIFLD) — no key needed
    try:
        crawl_transmission_lines(get_db, full_refresh)
    except Exception as e:
        logger.error(f"❌ Transmission lines crawl failed: {e}")

    # Step 4: Gas pipelines (EIA) — needs API key
    try:
        crawl_gas_pipelines(get_db, full_refresh)
    except Exception as e:
        logger.error(f"❌ Gas pipelines crawl failed: {e}")

    # Step 5: Generate market power profiles from all collected data
    try:
        generate_market_power_profiles(get_db)
    except Exception as e:
        logger.error(f"❌ Market power profiles failed: {e}")

    duration = time.time() - started
    logger.info("=" * 60)
    logger.info(f"🗺️  LAND & POWER SYNC COMPLETE — {duration:.1f}s total")
    logger.info("=" * 60)


# ─────────────────────────────────────────────────────────────
# API ROUTE REGISTRATION (add to main.py)
# ─────────────────────────────────────────────────────────────

def register_land_power_routes(app, get_db, require_admin):
    """
    Register Flask routes for land & power data.
    Call from main.py: register_land_power_routes(app, get_db, require_admin)
    """
    from flask import jsonify, request

    @app.route('/api/land-power/sync', methods=['POST'])
    @require_admin
    def trigger_land_power_sync():
        """Manual trigger for land & power sync."""
        import threading
        full = request.args.get('full', 'false').lower() == 'true'
        t = threading.Thread(
            target=run_land_power_sync, args=(get_db, full),
            daemon=True, name='land-power-sync'
        )
        t.start()
        return jsonify({
            "status": "started",
            "mode": "full" if full else "incremental",
            "message": "Land & Power sync running in background"
        })

    @app.route('/api/jobs/land-power-sync', methods=['POST'])
    def job_land_power_sync():
        """Scheduler entry point — matches the /api/jobs/* dispatcher convention.

        ★★★ WHY THIS EXISTS. dchub-scheduler.py declares
        `land_power_sync_incremental` at 04:30 in its JOBS dict, and that entire
        34-job dict is DEAD CODE: nothing invokes the file (every reference
        outside it is a comment or a doc), railway.json runs start_web.sh, and
        3 days of HTTP logs showed exactly ONE POST to /api/land-power/sync — a
        manual one. The land-power feeds therefore last ran 2026-03-30.
        .github/workflows/dchub-jobs.yml IS live and succeeding hourly, so this
        route exposes the sync under the convention that dispatcher already
        speaks (/api/jobs/<name>, X-API-Key) rather than adding a new secret or
        reviving 34 dormant jobs — several of which send email and post publicly.

        ★★ IT REPORTS THE PREVIOUS RUN'S OUTCOME, NOT THIS ONE'S. The crawl takes
        longer than the dispatcher's 300s budget, so it must be spawned — and a
        200 that means "a thread was created" is exactly the flattering-green
        that let this feed die unnoticed. So the response carries the CURRENT
        /status verdict, which describes the run before this one. A scheduler log
        line that says `previous_status: red` is a real signal; `status: started`
        is not.
        """
        provided = {(request.headers.get(h) or '').strip()
                    for h in ('X-API-Key', 'X-Admin-Key', 'X-Internal-Key')}
        expected = {(os.environ.get(k) or '').strip()
                    for k in ('DCHUB_ADMIN_KEY', 'DCHUB_INTERNAL_KEY')}
        expected.discard('')
        if expected and not (provided & expected):
            return jsonify(error='unauthorized'), 401

        full = request.args.get('full', 'false').lower() == 'true'
        prev = None
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT source, MAX(created_at) FILTER (
                           WHERE errors = 0 AND records_upserted > 0)
                  FROM land_power_sync_log GROUP BY source
            """)
            prev = {r[0]: (str(r[1]) if r[1] else None) for r in cur.fetchall()}
        except Exception as e:
            prev = {'_error': str(e)[:120]}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

        # ★★★ PER-SOURCE AND SYNCHRONOUS BY DEFAULT — this is the whole point.
        #
        # A spawned background thread on this service CANNOT SURVIVE. Measured
        # 2026-07-31: the web service redeployed 12 times in 63 minutes
        # (02:06 02:18 02:21 02:26 02:29 02:33 02:35 02:46 02:52 02:59 03:03
        # 03:09) — roughly every 5 minutes, because many sessions merge PRs into
        # this repo all day. A sync fired at 03:01:54 was killed by the 03:03
        # deploy about 70 seconds in and wrote NOTHING. That is why the only
        # rows this log has ever held are fast-fail errors (~13s, inside the
        # window): anything that takes minutes has never once completed.
        #
        # So the unit of work is ONE SOURCE, run INSIDE the request:
        #     plants        15 pages    ~18s + fetch
        #     substations   38 pages    ~46s + fetch
        #     transmission  45 pages    ~54s + fetch
        # Each fits the dispatcher's 300s budget and a typical deploy gap; the
        # four chained together do not. A source killed mid-flight simply retries
        # next cycle, and /status shows it as stale meanwhile.
        #
        # ★ The response carries the crawl's REAL result, so the GitHub Actions
        # log shows what happened instead of "spawned".
        _RUNNERS = {
            'eia-860-plants': crawl_power_plants,
            'hifld-substations': crawl_substations,
            'hifld-transmission': crawl_transmission_lines,
            'eia-ng-pipelines': crawl_gas_pipelines,
        }
        source = (request.args.get('source') or '').strip()
        if source:
            if source not in _RUNNERS:
                return jsonify(error='unknown_source',
                               known=sorted(_RUNNERS)), 400
            started_at = time.time()
            # Take the floor from the DATABASE clock, not the app's — the log's
            # created_at is written by NOW() on the server.
            _run_started_at = None
            try:
                _c0 = get_db()
                _cur0 = _c0.cursor()
                _cur0.execute("SELECT NOW()")
                _run_started_at = _cur0.fetchone()[0]
                _c0.close()
            except Exception:
                pass
            try:
                _RUNNERS[source](get_db, full)
                ok = True
                err = None
            except Exception as e:
                ok = False
                err = str(e)[:300]
            # Report from the LOG, not from the return value — the crawlers
            # return None and the log row is the durable record.
            row = None
            conn2 = None
            try:
                conn2 = get_db()
                c2 = conn2.cursor()
                # ★ BOUNDED TO THIS RUN. Without the created_at floor this
                # read the most recent row for the source — which, when the
                # crawl raises BEFORE _log_sync, is the PREVIOUS run's row. It
                # reported a four-hour-old "facility-fuel HTTP 400" as the
                # outcome of a crawl that had actually failed with `connection
                # already closed`, sending me to diagnose a route that was
                # already fixed. A result block must never be able to show a
                # row the run did not write.
                c2.execute("""
                    SELECT records_fetched, records_upserted, records_skipped,
                           errors, error_detail
                      FROM land_power_sync_log
                     WHERE source = %s AND created_at >= COALESCE(%s, '-infinity'::timestamp)
                     ORDER BY created_at DESC LIMIT 1
                """, (source, _run_started_at))
                r = c2.fetchone()
                if r:
                    row = {'fetched': r[0], 'upserted': r[1], 'skipped': r[2],
                           'errors': r[3], 'detail': (r[4] or '')[:300]}
                else:
                    row = {'_no_row': (
                        'this run wrote NO log row — it raised before _log_sync. '
                        'See `exception`. Deliberately not falling back to the '
                        'previous row, which would misreport an older failure '
                        'as this one.')}
            except Exception:
                pass
            finally:
                if conn2:
                    try:
                        conn2.close()
                    except Exception:
                        pass
            status_code = 200 if (ok and row and not row.get('errors')) else 500
            return jsonify({
                'source': source,
                'completed': ok,
                'elapsed_s': round(time.time() - started_at, 1),
                'result': row,
                'exception': err,
                'previous_last_success': prev.get(source) if isinstance(prev, dict) else None,
            }), status_code

        # No ?source= — spawn the whole chain. Kept for manual use and clearly
        # labelled: on this service it will very likely be killed by a deploy.
        import threading
        threading.Thread(target=run_land_power_sync, args=(get_db, full),
                         daemon=True, name='land-power-job').start()
        return jsonify({
            'status': 'spawned',
            'mode': 'full' if full else 'incremental',
            'previous_last_success': prev,
            'note': ("This 202 means a crawl STARTED, not that it worked, and on "
                     "this service a multi-minute background thread is usually "
                     "killed by the next deploy (12 in 63 minutes, measured). "
                     "Prefer ?source=<key>, which runs ONE crawler synchronously "
                     "and returns its real result. Read /api/land-power/status "
                     "for the verdict."),
        }), 202

    @app.route('/api/land-power/status')
    def land_power_status():
        """Get sync status and stats."""
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()

            # ★★★ 2026-07-31: `status` was the literal string "healthy",
            # returned unconditionally. It said healthy for FOUR MONTHS while
            # every feed was failing: eia-860-plants HTTP 400 (wrong route),
            # hifld-substations and hifld-transmission HTTP 500 (dead dataset
            # URLs). A status field that cannot say "red" is not a status field,
            # and this one is the only surface that would ever have shown the
            # outage. It is now COMPUTED from the age of each source's last
            # SUCCESSFUL run.
            #
            # ★ LAST RUN != LAST SUCCESS. A source failing every night has a
            # fresh last_run and stale data, which is the exact shape that hid
            # this. Both are reported, and the verdict keys off last_success.
            cur.execute("""
                SELECT DISTINCT ON (source)
                    source, records_fetched, records_upserted, errors,
                    duration_seconds, created_at, error_detail
                FROM land_power_sync_log
                ORDER BY source, created_at DESC
            """)
            _last_run = {r[0]: r for r in cur.fetchall()}
            cur.execute("""
                SELECT DISTINCT ON (source) source, created_at, records_upserted
                FROM land_power_sync_log
                WHERE errors = 0 AND records_upserted > 0
                ORDER BY source, created_at DESC
            """)
            _last_ok = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

            # Sources this crawler is SUPPOSED to run. A source that has never
            # logged at all must appear as never_run, not be absent — an absent
            # row is indistinguishable from a healthy one.
            _EXPECTED = ('eia-860-plants', 'hifld-substations',
                         'hifld-transmission', 'eia-ng-pipelines')
            _STALE_AFTER_DAYS = 7

            import datetime as _dt
            _now = _dt.datetime.now(_dt.timezone.utc)

            def _age_days(ts):
                if ts is None:
                    return None
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_dt.timezone.utc)
                return round((_now - ts).total_seconds() / 86400.0, 1)

            syncs = []
            for src in _EXPECTED:
                r = _last_run.get(src)
                ok_ts, ok_rows = _last_ok.get(src, (None, None))
                age = _age_days(ok_ts)
                if r is None:
                    verdict = "never_run"
                elif ok_ts is None:
                    verdict = "never_succeeded"
                elif age is not None and age > _STALE_AFTER_DAYS:
                    verdict = "stale"
                else:
                    verdict = "ok"
                syncs.append({
                    "source": src,
                    "verdict": verdict,
                    "last_run": str(r[5]) if r else None,
                    "last_success": str(ok_ts) if ok_ts else None,
                    "last_success_age_days": age,
                    "last_success_rows": ok_rows,
                    "fetched": r[1] if r else None,
                    "upserted": r[2] if r else None,
                    "errors": r[3] if r else None,
                    "duration_s": r[4] if r else None,
                    # Scrub BEFORE truncating: rows persisted by pre-scrub
                    # deploys still carry raw URLs, and this route is public.
                    "last_error": (scrub_secrets(str(r[6]))[:300]
                                   if r and r[6] else None),
                })

            # Table counts
            counts = {}
            for table in ['power_plants', 'substations', 'transmission_lines', 'gas_pipelines']:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = cur.fetchone()[0]

            _bad = [x["source"] for x in syncs if x["verdict"] != "ok"]
            overall = "healthy" if not _bad else (
                "red" if len(_bad) == len(_EXPECTED) else "degraded")
            return jsonify({
                "status": overall,
                "unhealthy_sources": _bad,
                "stale_after_days": _STALE_AFTER_DAYS,
                "status_basis": (
                    "computed from the age of each source's last SUCCESSFUL run "
                    "(errors=0 AND records_upserted>0), NOT from last_run — a "
                    "source failing nightly has a fresh last_run and stale data. "
                    "red = every expected source unhealthy, degraded = some. "
                    "Was hardcoded \"healthy\" until 2026-07-31 and reported "
                    "healthy through a four-month total outage."),
                "tables": counts,
                "latest_syncs": syncs,
            })
        except Exception as e:
            return jsonify({"status": "error", "error": str(e)}), 500
        finally:
            if conn:
                conn.close()

    @app.route('/api/land-power/market-profiles')
    def market_profiles():
        """Get all market power profiles."""
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT market, state, substation_count, avg_voltage_kv,
                       transmission_line_count, total_transmission_miles,
                       gas_pipeline_count, power_plant_count, total_generation_mw,
                       solar_mw, wind_mw, natural_gas_mw, nuclear_mw,
                       renewable_pct, power_readiness_score, last_updated
                FROM market_power_profiles
                ORDER BY power_readiness_score DESC
            """)
            # Phase plant-count-truth (2026-07-29): every generation member of
            # these stored profiles was computed from the `power_plants` table,
            # which holds 66 rows for the entire United States (see
            # crawl_power_plants above). Houston came out at 1 power plant /
            # 86.9 MW / renewable_pct 0, and Houston, San Antonio and Austin
            # carry IDENTICAL values because the builder aggregates
            # `WHERE state = %s` and stores a STATE roll-up under a MARKET name.
            #
            # Those members cannot be annotated into correctness, so they are
            # not published: null plus a reason, per house rule (an unmeasured
            # figure is never 0). power_readiness_score is computed FROM them,
            # so it is withheld too — a score derived from a 0.5%-loaded table
            # is not a measurement. The substation / transmission / gas members
            # are unaffected and still published.
            _GEN_REASON = (
                "withheld: computed from the `power_plants` table, which holds "
                "66 rows for the whole US (~0.5% of the 13,446-plant EIA fleet "
                "in power_plants_eia) as of the 2026-03-30 build. Not zero — "
                "unmeasured. Re-derive from power_plants_eia before publishing."
            )
            _SCORE_REASON = (
                "withheld: power_readiness_score is a function of "
                "total_generation_mw and renewable_pct, both of which are "
                "withheld above."
            )
            profiles = []
            for r in cur.fetchall():
                profiles.append({
                    "market": r[0], "state": r[1],
                    "substations": r[2], "avg_voltage_kv": round(r[3] or 0, 1),
                    "transmission_lines": r[4], "transmission_miles": round(r[5] or 0, 1),
                    "gas_pipelines": r[6],
                    "power_plants": None,
                    "total_mw": None,
                    "solar_mw": None, "wind_mw": None,
                    "natural_gas_mw": None, "nuclear_mw": None,
                    "renewable_pct": None,
                    "power_readiness_score": None,
                    "last_updated": str(r[15]),
                })
            return jsonify({
                "markets": profiles,
                "count": len(profiles),
                "unmeasured": {
                    "power_plants": _GEN_REASON,
                    "total_mw": _GEN_REASON,
                    "solar_mw": _GEN_REASON,
                    "wind_mw": _GEN_REASON,
                    "natural_gas_mw": _GEN_REASON,
                    "nuclear_mw": _GEN_REASON,
                    "renewable_pct": _GEN_REASON,
                    "power_readiness_score": _SCORE_REASON,
                },
                "basis": {
                    "geographic": "each row's infrastructure members are a "
                                  "STATE-level roll-up (WHERE state = ...) "
                                  "stored under a market name, so every market "
                                  "in the same state carries identical values. "
                                  "They are not metro-level figures.",
                    "freshness": "profiles are a stored build, not live — see "
                                 "each row's last_updated.",
                },
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            if conn:
                conn.close()

    @app.route('/api/land-power/market-profile/<market>')
    def market_profile_detail(market):
        """Get detailed power profile for one market."""
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM market_power_profiles WHERE market = %s", (market,))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Market not found"}), 404

            state = row[2]

            # Phase plant-count-truth (2026-07-29) — this endpoint had THREE
            # faults, all measured live against production before the fix:
            #
            # 1. WRONG TABLE. The plant list read `power_plants`, which holds
            #    66 rows for the entire United States. It is not a
            #    differently-scoped population: it is the SAME EIA-860 plant
            #    population as the healthy 13,446-row `power_plants_eia`,
            #    loaded to ~0.5% because crawl_power_plants keyed its dedup on
            #    rec['plantid'] — a spelling the EIA v2 facility-fuel response
            #    does not return — and silently dropped 54,934 of 55,000
            #    fetched records while reporting errors=0 (fixed in #1923; the
            #    66 rows are the pre-fix 2026-03-30 build and are what is live
            #    today).
            #    What that published: of the 42 markets in
            #    market_power_profiles, 34 got an EMPTY `large_power_plants`
            #    list, because only 13 states have any row at all in the stub
            #    and only 17 rows nationwide clear capacity_mw > 100. Austin,
            #    Dallas-Fort Worth, Houston and San Antonio all got [] — the
            #    real figure from power_plants_eia is 437 Texas plants over
            #    100 MW. The other 8 markets got 1-2 rows where the EIA table
            #    holds 24-241. An empty list is not "no large plants nearby",
            #    and it was published with no way for a caller to tell the
            #    difference. Repointed to power_plants_eia.
            #
            # 2. HARD 500. The substations query selected `lon`. The live
            #    `substations` table has no `lon` column — it is `lng` (36
            #    columns, verified against information_schema). So this
            #    endpoint returned
            #      {"error": "column \"lon\" does not exist ..."}  HTTP 500
            #    for EVERY market, and the CF worker turned that into a 503
            #    "Backend unreachable". Fixing fault 1 alone would have
            #    changed nothing a caller could see, because the response
            #    never got built. power_plants_eia also spells it `lng`, so
            #    both queries below now read `lng` — the published JSON key
            #    stays "lon" (that is the existing contract, and it is a
            #    longitude either way). Do not "fix" the key back to the
            #    column name.
            #
            # 3. STATE ROLL-UP UNDER A MARKET NAME — DISCLOSED, NOT FIXED.
            #    Both queries filter `WHERE state = %s`, so every market in a
            #    state returns identical lists. That is not repairable here:
            #    market_power_profiles carries no geography but `state` (20
            #    columns, no centroid, no county, no CBSA), and there is no
            #    market->metro table in the database to join to. Re-deriving
            #    at metro scope needs a market geography source that does not
            #    exist yet, so the scope is now stated in the response
            #    instead of implied by the route name, and the sibling markets
            #    that return the identical payload are named explicitly. This
            #    matches the disclosure the plural /market-profiles endpoint
            #    makes (#1923) rather than silently keeping a state figure
            #    labelled as a market one.
            cur.execute("""
                SELECT name, utility_name, nameplate_capacity_mw, primary_fuel,
                       lat, lng
                FROM power_plants_eia
                WHERE state = %s AND nameplate_capacity_mw > 100
                ORDER BY nameplate_capacity_mw DESC
                LIMIT 20
            """, (state,))
            large_plants = [
                {"name": r[0], "operator": r[1], "mw": r[2], "fuel": r[3], "lat": r[4], "lon": r[5]}
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT name, voltage_kv, max_voltage_kv, lat, lng
                FROM substations
                WHERE state = %s AND voltage_kv >= 230
                ORDER BY voltage_kv DESC
                LIMIT 20
            """, (state,))
            high_voltage_subs = [
                {"name": r[0], "voltage_kv": r[1], "max_voltage_kv": r[2], "lat": r[3], "lon": r[4]}
                for r in cur.fetchall()
            ]

            # Name the markets that share this state, so "these numbers are a
            # state roll-up" is checkable rather than a claim. Best-effort: a
            # failure here must not take the endpoint down again.
            siblings = []
            try:
                cur.execute("""
                    SELECT market FROM market_power_profiles
                    WHERE state = %s AND market <> %s
                    ORDER BY market
                """, (state, market))
                siblings = [r[0] for r in cur.fetchall()]
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                siblings = None

            _SCOPE = (
                f"STATE roll-up for {state}, not a metro figure. Both lists "
                f"below are `WHERE state = '{state}'`, so every market in "
                f"{state} returns byte-identical lists. Disclosed, not fixed: "
                f"market_power_profiles carries no geography but `state`, and "
                f"there is no market->metro mapping to re-derive from."
            )
            return jsonify({
                "market": market,
                "state": state,
                "large_power_plants": large_plants,
                "high_voltage_substations": high_voltage_subs,
                "basis": {
                    "geographic_scope": _SCOPE,
                    "identical_for_markets": siblings,
                    "large_power_plants": (
                        "power_plants_eia (13,446 US EIA-860 plant records), "
                        "nameplate_capacity_mw > 100, top 20 by nameplate. "
                        "NOT the bare `power_plants` table, which is a 66-row "
                        "stub of the same population."
                    ),
                    "operating_status": (
                        "NOT ASSERTED. power_plants_eia has no `status` column, "
                        "so whether a plant is currently operating is an "
                        "upstream property we cannot state per row. This list "
                        "is EIA-860 plant records over 100 MW nameplate, not a "
                        "list of operating plants. (The previous 66-row source "
                        "did carry `status` and never filtered on it either, so "
                        "no assertion is lost here — it was never made.)"
                    ),
                    "high_voltage_substations": (
                        "substations, voltage_kv >= 230, top 20 by voltage."
                    ),
                },
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            if conn:
                conn.close()

    logger.info("✅ Land & Power routes registered")
