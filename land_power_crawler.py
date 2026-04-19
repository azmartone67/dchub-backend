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
  - substations           (name, voltage_kv, lat, lon, operator, state)
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

logger = logging.getLogger('dchub-land-power')

# ─────────────────────────────────────────────────────────────
# DATA SOURCE URLS
# ─────────────────────────────────────────────────────────────

# EIA-860: Annual Electric Generator Report (plant-level data)
# Updated annually, supplemented quarterly
EIA_860_PLANTS_URL = "https://api.eia.gov/v2/electricity/facility-fuel/data/"

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
        conn = get_db()
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
                hifld_id VARCHAR(50),
                name VARCHAR(500),
                operator VARCHAR(500),
                state VARCHAR(10),
                county VARCHAR(200),
                city VARCHAR(200),
                lat DOUBLE PRECISION,
                lon DOUBLE PRECISION,
                voltage_kv DOUBLE PRECISION DEFAULT 0,
                max_voltage_kv DOUBLE PRECISION DEFAULT 0,
                min_voltage_kv DOUBLE PRECISION DEFAULT 0,
                sub_type VARCHAR(100),
                status VARCHAR(50) DEFAULT 'operational',
                lines_count INTEGER DEFAULT 0,
                source VARCHAR(50) DEFAULT 'hifld',
                last_updated TIMESTAMP DEFAULT NOW(),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS substations_hifld_id_uniq
            ON substations (hifld_id)
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

def _fetch_json(url, params=None, retries=MAX_RETRIES):
    """Fetch JSON with retry + rate limiting."""
    for attempt in range(retries):
        try:
            time.sleep(REQUEST_DELAY_SECONDS)
            resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️  Fetch attempt {attempt + 1}/{retries} failed for {url}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                raise
    return None


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

        while True:
            params = {
                'api_key': EIA_API_KEY,
                'frequency': 'annual',
                'data[0]': 'nameplate-capacity-mw',
                'facets[stateid][]': [],  # All states
                'sort[0][column]': 'plantid',
                'sort[0][direction]': 'asc',
                'offset': offset,
                'length': page_size,
            }

            data = _fetch_json(EIA_860_PLANTS_URL, params=params)
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
        plant_map = {}
        for rec in all_plants:
            pid = str(rec.get('plantid', ''))
            if not pid:
                continue
            # Keep the record with highest capacity or most recent
            existing = plant_map.get(pid)
            if not existing:
                plant_map[pid] = rec
            else:
                new_cap = _safe_float(rec.get('nameplate-capacity-mw', 0))
                old_cap = _safe_float(existing.get('nameplate-capacity-mw', 0))
                if new_cap > old_cap:
                    plant_map[pid] = rec

        # Upsert into database
        conn = get_db()
        cur = conn.cursor()

        for pid, rec in plant_map.items():
            try:
                cur.execute("""
                    INSERT INTO power_plants (
                        eia_plant_id, name, operator, state, county, city,
                        lat, lon, capacity_mw, fuel_type, fuel_category,
                        prime_mover, status, sector, source, last_updated
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (eia_plant_id)
                    DO UPDATE SET
                        name = EXCLUDED.name,
                        operator = EXCLUDED.operator,
                        capacity_mw = EXCLUDED.capacity_mw,
                        fuel_type = EXCLUDED.fuel_type,
                        fuel_category = EXCLUDED.fuel_category,
                        status = EXCLUDED.status,
                        last_updated = NOW()
                """, (
                    pid,
                    _safe_str(rec.get('plantName', '')),
                    _safe_str(rec.get('operator', '')),
                    _safe_str(rec.get('stateid', '')),
                    _safe_str(rec.get('county', '')),
                    _safe_str(rec.get('city', '')),
                    _safe_float(rec.get('latitude')),
                    _safe_float(rec.get('longitude')),
                    _safe_float(rec.get('nameplate-capacity-mw', 0)),
                    _safe_str(rec.get('fuel2002', '')),
                    classify_fuel(rec.get('fuel2002', '')),
                    _safe_str(rec.get('reported-prime-mover', '')),
                    _safe_str(rec.get('status', 'OP')),
                    _safe_str(rec.get('sectorName', '')),
                    'eia-860',
                ))
                upserted += 1
            except Exception as e:
                errors += 1
                if len(error_detail) < 10:
                    error_detail.append(f"Plant {pid}: {str(e)[:100]}")

        conn.commit()
        logger.info(f"✅ Power plants: {upserted} upserted, {errors} errors")

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
    _log_sync(get_db, 'eia-860-plants', fetched, upserted, fetched - upserted, errors,
              '; '.join(error_detail) if error_detail else None, duration)


# ─────────────────────────────────────────────────────────────
# CRAWLER 2: SUBSTATIONS (HIFLD Open Data)
# ─────────────────────────────────────────────────────────────

def crawl_substations(get_db, full_refresh=False):
    """
    Fetch substation data from HIFLD (Homeland Infrastructure Foundation).
    Public GeoJSON — no API key needed.
    """
    started = time.time()
    fetched = 0
    upserted = 0
    errors = 0
    error_detail = []

    logger.info("⚡ Starting substation crawl (HIFLD)...")

    conn = None
    try:
        # HIFLD data can be large (~70K features), use streaming
        geojson = _fetch_geojson_stream(HIFLD_SUBSTATIONS_URL)
        if not geojson or 'features' not in geojson:
            raise ValueError("No features in HIFLD substations response")

        features = geojson['features']
        fetched = len(features)
        logger.info(f"  ⚡ Fetched {fetched} substations")

        conn = get_db()
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
                u, e = _upsert_substations(cur, batch)
                upserted += u
                errors += e
                batch = []

        # Final batch
        if batch:
            u, e = _upsert_substations(cur, batch)
            upserted += u
            errors += e

        conn.commit()
        logger.info(f"✅ Substations: {upserted} upserted, {errors} errors")

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
              '; '.join(error_detail) if error_detail else None, duration)


def _upsert_substations(cur, batch):
    """Batch upsert substations. Returns (upserted_count, error_count)."""
    upserted = 0
    errors = 0
    for row in batch:
        try:
            cur.execute("""
                INSERT INTO substations (
                    hifld_id, name, operator, state, county, city,
                    lat, lon, voltage_kv, max_voltage_kv, min_voltage_kv,
                    sub_type, status, lines_count, source, last_updated
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'hifld', NOW())
                ON CONFLICT (hifld_id)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    operator = EXCLUDED.operator,
                    voltage_kv = EXCLUDED.voltage_kv,
                    max_voltage_kv = EXCLUDED.max_voltage_kv,
                    min_voltage_kv = EXCLUDED.min_voltage_kv,
                    lines_count = EXCLUDED.lines_count,
                    status = EXCLUDED.status,
                    last_updated = NOW()
            """, row)
            upserted += 1
        except Exception as e:
            errors += 1
    return upserted, errors


# ─────────────────────────────────────────────────────────────
# CRAWLER 3: TRANSMISSION LINES (HIFLD Open Data)
# ─────────────────────────────────────────────────────────────

def crawl_transmission_lines(get_db, full_refresh=False):
    """
    Fetch transmission line data from HIFLD.
    Public GeoJSON — no API key needed.
    """
    started = time.time()
    fetched = 0
    upserted = 0
    errors = 0
    error_detail = []

    logger.info("🔗 Starting transmission line crawl (HIFLD)...")

    conn = None
    try:
        geojson = _fetch_geojson_stream(HIFLD_TRANSMISSION_URL)
        if not geojson or 'features' not in geojson:
            raise ValueError("No features in HIFLD transmission response")

        features = geojson['features']
        fetched = len(features)
        logger.info(f"  🔗 Fetched {fetched} transmission lines")

        conn = get_db()
        cur = conn.cursor()
        batch = []

        for feat in features:
            props = feat.get('properties', {})

            hifld_id = str(props.get('ID', props.get('OBJECTID', '')))
            if not hifld_id:
                continue

            # Calculate length from geometry if available
            length_miles = _safe_float(props.get('SHAPE_Length', props.get('LENGTH', 0)))
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
              '; '.join(error_detail) if error_detail else None, duration)


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
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'hifld', NOW())
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
                'api_key': EIA_API_KEY,
                'frequency': 'annual',
                'data[0]': 'value',
                'facets[process][]': ['FPR'],  # Pipeline receipts
                'sort[0][column]': 'period',
                'sort[0][direction]': 'desc',
                'offset': offset,
                'length': page_size,
            }

            data = _fetch_json(EIA_NG_PIPELINES_URL, params=params)
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

        conn = get_db()
        cur = conn.cursor()

        for (name, area), rec in pipe_map.items():
            if not name:
                continue
            try:
                cur.execute("""
                    INSERT INTO gas_pipelines (name, operator, state, commodity, source, last_updated)
                    VALUES (%s, %s, %s, %s, 'eia-ng', NOW())
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
        conn = get_db()
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
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
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
    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO land_power_sync_log
            (source, records_fetched, records_upserted, records_skipped, errors, error_detail, duration_seconds)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
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

    @app.route('/api/land-power/status')
    def land_power_status():
        """Get sync status and stats."""
        conn = None
        try:
            conn = get_db()
            cur = conn.cursor()

            # Latest sync per source
            cur.execute("""
                SELECT DISTINCT ON (source)
                    source, records_fetched, records_upserted, errors,
                    duration_seconds, created_at
                FROM land_power_sync_log
                ORDER BY source, created_at DESC
            """)
            syncs = [
                {"source": r[0], "fetched": r[1], "upserted": r[2],
                 "errors": r[3], "duration_s": r[4], "last_run": str(r[5])}
                for r in cur.fetchall()
            ]

            # Table counts
            counts = {}
            for table in ['power_plants', 'substations', 'transmission_lines', 'gas_pipelines']:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                counts[table] = cur.fetchone()[0]

            return jsonify({
                "status": "healthy",
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
            profiles = []
            for r in cur.fetchall():
                profiles.append({
                    "market": r[0], "state": r[1],
                    "substations": r[2], "avg_voltage_kv": round(r[3], 1),
                    "transmission_lines": r[4], "transmission_miles": round(r[5], 1),
                    "gas_pipelines": r[6], "power_plants": r[7],
                    "total_mw": round(r[8], 1),
                    "solar_mw": round(r[9], 1), "wind_mw": round(r[10], 1),
                    "natural_gas_mw": round(r[11], 1), "nuclear_mw": round(r[12], 1),
                    "renewable_pct": round(r[13], 1),
                    "power_readiness_score": r[14],
                    "last_updated": str(r[15]),
                })
            return jsonify({"markets": profiles, "count": len(profiles)})
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

            # Also get nearby power plants
            state = row[2]
            cur.execute("""
                SELECT name, operator, capacity_mw, fuel_category, lat, lon
                FROM power_plants
                WHERE state = %s AND capacity_mw > 100
                ORDER BY capacity_mw DESC
                LIMIT 20
            """, (state,))
            large_plants = [
                {"name": r[0], "operator": r[1], "mw": r[2], "fuel": r[3], "lat": r[4], "lon": r[5]}
                for r in cur.fetchall()
            ]

            cur.execute("""
                SELECT name, voltage_kv, max_voltage_kv, lat, lon
                FROM substations
                WHERE state = %s AND voltage_kv >= 230
                ORDER BY voltage_kv DESC
                LIMIT 20
            """, (state,))
            high_voltage_subs = [
                {"name": r[0], "voltage_kv": r[1], "max_voltage_kv": r[2], "lat": r[3], "lon": r[4]}
                for r in cur.fetchall()
            ]

            return jsonify({
                "market": market,
                "large_power_plants": large_plants,
                "high_voltage_substations": high_voltage_subs,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        finally:
            if conn:
                conn.close()

    logger.info("✅ Land & Power routes registered")
