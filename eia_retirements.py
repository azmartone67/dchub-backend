"""EIA-860M planned-retirement ingest (2026-07-11, Gemini co-design round 3).

The data layer behind get_retirement_headroom: retiring generators = concrete
near-term transmission headroom events (a plant going offline frees injection
capacity at its POI), from FILED EIA-860M data — deterministic, not forecast.

Source: EIA API v2 electricity/operating-generator-capacity (monthly, ~28k
generator rows/period). We keep only rows with a non-null
planned-retirement-year-month. Coordinates + county + balancing_authority_code
come straight from EIA (no join needed); rows missing coords fall back to the
substations county MEDIAN centroid (MEDIAN not AVG — the market-coords Gulf-
skew lesson). Values arrive as STRINGS (".9") — cast defensively.

Cadence: monthly via POST /api/jobs/eia-retirements (jobs_routes.py, external
cron caller) + manual backfill anytime. Upsert keyed on
(eia_plant_id, generator_id, status) with the explicit three-column ON CONFLICT
(Gemini's trap note: a plant shifting planned_retirement→retired must not
duplicate-key). DDL is CREATE TABLE IF NOT EXISTS inside the tick — NOT in any
boot path (the boot-DDL-storm trap), and via direct psycopg2 (safe_db silently
skips DDL).
"""

from __future__ import annotations

import os
import json
import logging
import urllib.request
import urllib.parse

logger = logging.getLogger(__name__)

EIA_BASE = "https://api.eia.gov/v2/electricity/operating-generator-capacity/data/"


def _eia_headers(api_key):
    """EIA reads X-Api-Key. A key in the query string is written verbatim
    into every proxy/gateway log it passes through — see
    tests/test_no_provider_key_in_url.py."""
    return {"X-Api-Key": api_key} if api_key else {}
PAGE = 5000

_DDL = """
CREATE TABLE IF NOT EXISTS generator_retirements (
  id             SERIAL PRIMARY KEY,
  eia_plant_id   INTEGER,
  generator_id   TEXT,
  plant_name     TEXT,
  state          TEXT,
  county         TEXT,
  lat            DOUBLE PRECISION,
  lng            DOUBLE PRECISION,
  capacity_mw    DOUBLE PRECISION,
  fuel_category  TEXT,
  prime_mover    TEXT,
  ba_code        TEXT,
  retirement_date DATE,
  status         TEXT,
  source_month   TEXT,
  ingested_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE (eia_plant_id, generator_id, status)
);
CREATE INDEX IF NOT EXISTS idx_gen_ret_date ON generator_retirements (retirement_date);
CREATE INDEX IF NOT EXISTS idx_gen_ret_state ON generator_retirements (state);
CREATE INDEX IF NOT EXISTS idx_gen_ret_ba ON generator_retirements (ba_code);
"""


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except Exception:
        return None


def _get_json(url, timeout=90, tries=3, headers=None):
    """GET with retry — the EIA API and DNS both flake transiently; a monthly
    cron must ride through it."""
    import time

    import requests   # house rule: requests, not urllib, on Railway — urllib's
                      # default UA is what Cloudflare 1010-blocks (2026-08-10)
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, timeout=timeout, headers=headers or {})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def _latest_period(api_key):
    url = "https://api.eia.gov/v2/electricity/operating-generator-capacity/"
    return _get_json(url, timeout=30,
                     headers=_eia_headers(api_key))["response"]["endPeriod"]


def fetch_planned_retirements(api_key, period):
    """Page the full generator inventory for `period`; keep rows with a
    planned retirement filed. Returns list of dicts (EIA field names)."""
    keep, offset = [], 0
    while True:
        params = {
            "frequency": "monthly",
            "start": period, "end": period,
            "length": str(PAGE), "offset": str(offset),
        }
        q = urllib.parse.urlencode(params)
        # data[] columns must repeat — urlencode(doseq) mangles brackets, build manually
        cols = "&".join("data[]=" + c for c in (
            "nameplate-capacity-mw", "planned-retirement-year-month",
            "latitude", "longitude", "county"))
        resp = _get_json(f"{EIA_BASE}?{q}&{cols}", timeout=90,
                         headers=_eia_headers(api_key))["response"]
        rows = resp.get("data") or []
        keep.extend(x for x in rows if x.get("planned-retirement-year-month"))
        offset += PAGE
        if offset >= int(resp.get("total") or 0) or not rows:
            break
    return keep


def run_eia_retirements_ingest(db_url=None, api_key=None):
    """The tick: fetch latest EIA-860M period, upsert planned retirements,
    median-fallback missing coords. Returns counts dict (logged — the
    swallowed-write house rule)."""
    import psycopg2
    db_url = db_url or os.environ.get("DATABASE_URL")
    api_key = api_key or os.environ.get("EIA_API_KEY")
    if not db_url or not api_key:
        logger.error("eia-retirements: missing DATABASE_URL or EIA_API_KEY")
        return {"ok": False, "error": "missing_config"}

    period = _latest_period(api_key)
    rows = fetch_planned_retirements(api_key, period)
    logger.info("eia-retirements: period %s → %d generators with filed retirement",
                period, len(rows))

    conn = psycopg2.connect(db_url, sslmode="require", connect_timeout=10)
    conn.autocommit = True
    up, skipped = 0, 0
    with conn.cursor() as cur:
        cur.execute(_DDL)
        for x in rows:
            try:
                ret = (x.get("planned-retirement-year-month") or "").strip()
                if not ret:
                    skipped += 1
                    continue
                cur.execute("""
                    INSERT INTO generator_retirements
                      (eia_plant_id, generator_id, plant_name, state, county,
                       lat, lng, capacity_mw, fuel_category, prime_mover,
                       ba_code, retirement_date, status, source_month)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'planned_retirement',%s)
                    ON CONFLICT (eia_plant_id, generator_id, status)
                    DO UPDATE SET
                      retirement_date = EXCLUDED.retirement_date,
                      capacity_mw     = EXCLUDED.capacity_mw,
                      lat             = COALESCE(EXCLUDED.lat, generator_retirements.lat),
                      lng             = COALESCE(EXCLUDED.lng, generator_retirements.lng),
                      ba_code         = COALESCE(EXCLUDED.ba_code, generator_retirements.ba_code),
                      source_month    = EXCLUDED.source_month
                """, (
                    int(x.get("plantid") or 0) or None,
                    str(x.get("generatorid") or "").strip() or None,
                    x.get("plantName"), x.get("stateid"), x.get("county"),
                    _f(x.get("latitude")), _f(x.get("longitude")),
                    _f(x.get("nameplate-capacity-mw")),
                    x.get("technology"), x.get("prime_mover_code"),
                    x.get("balancing_authority_code"),
                    ret + "-01", period,
                ))
                up += 1
            except Exception as e:
                skipped += 1
                logger.warning("eia-retirements: row skipped (%s %s): %s",
                               x.get("plantid"), x.get("generatorid"), str(e)[:120])
        # coords fallback: substations county MEDIAN (never AVG)
        cur.execute("""
            UPDATE generator_retirements g SET lat = m.mlat, lng = m.mlng
              FROM (SELECT state, county,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY lat) AS mlat,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY lng) AS mlng
                      FROM substations
                     WHERE lat IS NOT NULL AND county IS NOT NULL AND county != ''
                     GROUP BY state, county) m
             WHERE g.lat IS NULL AND g.state = m.state
               AND lower(coalesce(g.county,'')) = lower(m.county)
        """)
        median_fixed = cur.rowcount
        cur.execute("SELECT COUNT(*), COUNT(lat) FROM generator_retirements WHERE status='planned_retirement'")
        total, geocoded = cur.fetchone()
    conn.close()
    out = {"ok": True, "period": period, "upserted": up, "skipped": skipped,
           "median_coord_fallbacks": median_fixed,
           "total_planned": int(total), "geocoded": int(geocoded)}
    logger.info("eia-retirements: %s", out)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_eia_retirements_ingest())
