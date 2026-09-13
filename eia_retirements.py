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

Cadence: crawler_scheduler's `eia_retirements` slot (daily, 00:00 UTC) calls
run_if_due(), which refreshes only when EIA has published a newer period than
the table holds, or the last refresh is REFRESH_MAX_AGE_DAYS old.
POST /api/jobs/eia-retirements forces a refresh (manual backfill). Upsert keyed
on (eia_plant_id, generator_id, status) with the explicit three-column ON
CONFLICT (Gemini's trap note: a plant shifting planned_retirement→retired must
not duplicate-key). DDL is CREATE TABLE IF NOT EXISTS inside the tick — NOT in
any boot path (the boot-DDL-storm trap), and via direct psycopg2 (safe_db
silently skips DDL).

★★★ IT RAN ONCE. Until 2026-09-12 this docstring said "monthly via POST
/api/jobs/eia-retirements … external cron caller". Nothing ever called that
route — no workflow, no scheduler slot, no job registry — so the newest ingest
was this module's creation day. On 2026-09-12 /api/v1/retirement-headroom and
the power availability timeline were still serving the 2026-04 filing, and
served-table-freshness read the table frozen at 63 days.

★★ A SCHEDULED REFRESH MUST GET THREE THINGS RIGHT THAT ONE LOAD NEVER HAD TO.
  · WITHDRAWN RETIREMENTS. Every reader serves future-dated planned_retirement
    rows and labels them with MAX(source_month). An upsert-only refresh keeps
    serving a retirement EIA has since withdrawn — phantom headroom, labelled
    with the newest period. After a complete fetch, planned rows the latest
    filing no longer lists are deleted.
  · THE STAMP. ingested_at was only ever set on INSERT, so a refresh that
    re-confirmed every row and inserted none left the table reading frozen.
    Every row a refresh writes is restamped.
  · ONE TRANSACTION. Upsert, prune and the coordinate fallback commit together
    or not at all. A fetch that came back short, a filing listing fewer than
    MIN_KEEP_RATIO of the planned rows held, or a period older than the one
    held is refused before anything is written — a reader sees one filing,
    never parts of two.
Rows with no plant or generator id are skipped: NULLs never conflict, so every
refresh would insert them again.

★ THE OUTCOME IS BEATEN, NOT THE SLOT. crawler_scheduler beats
worker:eia_retirements every night the slot runs, including the nights nothing
was due. This module beats FEED only when a refresh commits (success, rows
upserted) or fails (error), at the monthly cadence tools/deadman/watch.py uses —
so a gate that never opens goes OVERDUE instead of staying green.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import urllib.parse

logger = logging.getLogger(__name__)

EIA_BASE = "https://api.eia.gov/v2/electricity/operating-generator-capacity/data/"

STATUS = "planned_retirement"

# Dead-man ledger feed for the refresh OUTCOME (see the docstring).
FEED = "eia-retirements-refresh"
# What tools/deadman/watch.py gives every monthly producer (gem-refresh,
# planned-generators-ingest): OVERDUE after 2x, ~65 days with no refresh.
CADENCE_HOURS = 780
# EIA-860M is monthly. Refreshing at this age as well re-pulls a period EIA has
# revised, and re-confirms the table against the source through a gap in EIA's
# publishing, so a table that is current never reads frozen.
REFRESH_MAX_AGE_DAYS = 28
# A real month moves the planned-retirement list by a few percent; a broken pull
# moves most of it. A filing listing fewer than this share of the rows held is
# refused rather than pruned against.
MIN_KEEP_RATIO = 0.5

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


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

_STATE_SQL = ("SELECT MAX(source_month), MAX(ingested_at), COUNT(*) "
              "FROM generator_retirements WHERE status = %s")

_UPSERT_SQL = """
    INSERT INTO generator_retirements
      (eia_plant_id, generator_id, plant_name, state, county,
       lat, lng, capacity_mw, fuel_category, prime_mover,
       ba_code, retirement_date, status, source_month)
    VALUES %s
    ON CONFLICT (eia_plant_id, generator_id, status)
    DO UPDATE SET
      retirement_date = EXCLUDED.retirement_date,
      capacity_mw     = EXCLUDED.capacity_mw,
      lat             = COALESCE(EXCLUDED.lat, generator_retirements.lat),
      lng             = COALESCE(EXCLUDED.lng, generator_retirements.lng),
      ba_code         = COALESCE(EXCLUDED.ba_code, generator_retirements.ba_code),
      source_month    = EXCLUDED.source_month,
      ingested_at     = now()
    RETURNING (xmax = 0)
"""

# Planned rows the latest filing no longer lists. The keys it does list arrive
# as two parallel arrays; a row with a NULL key matches none of them and goes
# too, since no refresh could ever update it.
_PRUNE_SQL = """
    DELETE FROM generator_retirements g
     WHERE g.status = %s
       AND NOT EXISTS (
             SELECT 1
               FROM unnest(%s::int[], %s::text[]) AS k(eia_plant_id, generator_id)
              WHERE k.eia_plant_id = g.eia_plant_id
                AND k.generator_id = g.generator_id)
"""

# coords fallback: substations county MEDIAN (never AVG)
_MEDIAN_SQL = """
    UPDATE generator_retirements g SET lat = m.mlat, lng = m.mlng
      FROM (SELECT state, county,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY lat) AS mlat,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY lng) AS mlng
              FROM substations
             WHERE lat IS NOT NULL AND county IS NOT NULL AND county != ''
             GROUP BY state, county) m
     WHERE g.lat IS NULL AND g.state = m.state
       AND lower(coalesce(g.county,'')) = lower(m.county)
"""

_COUNTS_SQL = ("SELECT COUNT(*), COUNT(lat) FROM generator_retirements "
               "WHERE status = %s")


class RefreshRefused(RuntimeError):
    """A guard declined to commit a refresh. Nothing was written."""


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


def fetch_planned_retirements(api_key, period, get_json=None):
    """Page the full generator inventory for `period`; keep rows with a
    planned retirement filed. Returns list of dicts (EIA field names).

    Raises RefreshRefused when fewer rows arrived than the total EIA reported.
    A short fetch must never reach the prune: every retirement on the pages
    that did not arrive would be deleted as withdrawn."""
    get_json = get_json or _get_json
    keep, offset, scanned, total = [], 0, 0, 0
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
        resp = get_json(f"{EIA_BASE}?{q}&{cols}", timeout=90,
                        headers=_eia_headers(api_key))["response"]
        rows = resp.get("data") or []
        total = int(resp.get("total") or 0)
        scanned += len(rows)
        keep.extend(x for x in rows if x.get("planned-retirement-year-month"))
        offset += PAGE
        if offset >= total or not rows:
            break
    if not total or scanned < total:
        raise RefreshRefused(f"EIA {period}: {scanned} of {total} generator rows "
                             "arrived — incomplete fetch, nothing written")
    return keep


def normalize(rows, period):
    """EIA rows -> (records, asserted, stats). Pure.

    records   one upsert tuple per (plant, generator); the last row wins, as it
              did when the loader upserted row by row.
    asserted  every (plant, generator) this filing lists WITH a planned
              retirement — including rows whose month does not parse, so the
              prune leaves those rows as they were rather than deleting a
              retirement over a formatting fault.
    """
    records, asserted = {}, set()
    stats = {"no_key": 0, "bad_month": 0, "duplicates": 0}
    for x in rows:
        ret = str(x.get("planned-retirement-year-month") or "").strip()
        if not ret:
            continue
        try:
            plant = int(x.get("plantid") or 0) or None
        except (TypeError, ValueError):
            plant = None
        gen = str(x.get("generatorid") or "").strip() or None
        if plant is None or gen is None:
            stats["no_key"] += 1
            continue
        key = (plant, gen)
        asserted.add(key)
        if not _MONTH_RE.match(ret):
            stats["bad_month"] += 1
            continue
        if key in records:
            stats["duplicates"] += 1
        records[key] = (
            plant, gen, x.get("plantName"), x.get("stateid"), x.get("county"),
            _f(x.get("latitude")), _f(x.get("longitude")),
            _f(x.get("nameplate-capacity-mw")),
            x.get("technology"), x.get("prime_mover_code"),
            x.get("balancing_authority_code"),
            ret + "-01", STATUS, period,
        )
    return list(records.values()), asserted, stats


def refresh_due(latest_period, table_period, newest_ingest, now,
                max_age_days=REFRESH_MAX_AGE_DAYS):
    """(due, reason). Pure. EIA periods are 'YYYY-MM', which order as text.
    latest_period may be None when EIA could not be asked; the age rule still
    applies."""
    if table_period is None or newest_ingest is None:
        return True, "the table holds no planned retirements"
    if latest_period and latest_period > table_period:
        return True, (f"EIA has published {latest_period}; the table holds "
                      f"{table_period}")
    age_d = (now - newest_ingest).total_seconds() / 86400.0
    if age_d >= max_age_days:
        return True, (f"last refresh {age_d:.1f}d ago (refreshes at "
                      f"{max_age_days}d)")
    return False, (f"not due: the table holds {table_period}, EIA's latest is "
                   f"{latest_period or 'unknown'}, refreshed {age_d:.1f}d ago")


def table_state(cur):
    """(period held, newest ingested_at, planned rows) — (None, None, 0) when
    the table does not exist yet."""
    cur.execute("SELECT to_regclass('generator_retirements') IS NOT NULL")
    if not cur.fetchone()[0]:
        return None, None, 0
    cur.execute(_STATE_SQL, (STATUS,))
    period, newest, n = cur.fetchone()
    return period, newest, int(n or 0)


def refresh(conn, period, records, asserted):
    """ONE transaction: guards, upsert, prune, coordinate fallback. Commits and
    returns counts, or rolls back and raises — never part of a filing."""
    from psycopg2.extras import execute_values

    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            held_period, _newest, held = table_state(cur)
            if not records:
                raise RefreshRefused(f"EIA {period}: no planned retirements parsed")
            if held_period and period < held_period:
                raise RefreshRefused(f"EIA {period} is older than the {held_period} "
                                     "filing already held")
            if held and len(records) < MIN_KEEP_RATIO * held:
                raise RefreshRefused(
                    f"EIA {period} lists {len(records)} planned retirements against "
                    f"{held} held (under {MIN_KEEP_RATIO:.0%}) — refusing to prune on it")
            flags = execute_values(cur, _UPSERT_SQL, records, page_size=500, fetch=True)
            inserted = sum(1 for (is_new,) in flags if is_new)
            keys = sorted(asserted)
            cur.execute(_PRUNE_SQL, (STATUS, [k[0] for k in keys], [k[1] for k in keys]))
            pruned = cur.rowcount
            cur.execute(_MEDIAN_SQL)
            median_fixed = cur.rowcount
            cur.execute(_COUNTS_SQL, (STATUS,))
            total, geocoded = cur.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"upserted": len(records), "inserted": inserted,
            "updated": len(records) - inserted, "pruned": pruned,
            "median_coord_fallbacks": median_fixed, "held_before": held,
            "total_planned": int(total), "geocoded": int(geocoded)}


def _connect(db_url):
    import psycopg2
    return psycopg2.connect(db_url, sslmode="require", connect_timeout=10)


def _read_state(db_url):
    conn = _connect(db_url)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            period, newest, _held = table_state(cur)
        return period, newest
    finally:
        conn.close()


def _beat(beat, status, rows=None, note=None):
    """Record the refresh outcome on the dead-man ledger. Fail-soft: telemetry
    never fails a refresh."""
    try:
        fn = beat
        if fn is None:
            from routes.ingest_runs import record_beat
            fn = record_beat
        fn(FEED, status=status, rows=rows, cad=CADENCE_HOURS,
           note=(note or "")[:280] or None)
    except Exception as e:  # noqa: BLE001
        logger.warning("eia-retirements: outcome beat failed: %s", e)


def run_eia_retirements_ingest(db_url=None, api_key=None, force=True, beat=None,
                               now=None):
    """Fetch the latest EIA-860M period and refresh generator_retirements.

    force=True (POST /api/jobs/eia-retirements) always refreshes; force=False
    (the scheduler, via run_if_due) refreshes only when refresh_due() says so.
    Returns a counts dict with ok=False when the refresh could not run or was
    refused. Never raises — the scheduler slot turns ok=False into an error."""
    db_url = db_url or os.environ.get("DATABASE_URL")
    api_key = api_key or os.environ.get("EIA_API_KEY")
    if not db_url or not api_key:
        logger.error("eia-retirements: missing DATABASE_URL or EIA_API_KEY")
        _beat(beat, "error", note="missing DATABASE_URL or EIA_API_KEY")
        return {"ok": False, "error": "missing_config"}
    now = now or datetime.datetime.now(datetime.timezone.utc)

    period = None
    try:
        lookup_error = None
        try:
            period = _latest_period(api_key)
        except Exception as e:  # noqa: BLE001
            lookup_error = f"EIA period lookup failed: {str(e)[:160]}"
        if force:
            reason = "forced"
        else:
            held_period, newest = _read_state(db_url)
            due, reason = refresh_due(period, held_period, newest, now)
            if not due:
                # An EIA blip on a night nothing was due is not a failure.
                if lookup_error:
                    reason = f"{reason}; {lookup_error}"
                    logger.warning("eia-retirements: %s", reason)
                else:
                    logger.info("eia-retirements: %s", reason)
                return {"ok": True, "due": False, "reason": reason, "period": period}
        if not period:
            raise RuntimeError(lookup_error or "EIA returned no period")

        rows = fetch_planned_retirements(api_key, period)
        records, asserted, stats = normalize(rows, period)
        logger.info("eia-retirements: period %s → %d generators with filed retirement "
                    "(%s)", period, len(records), stats)
        conn = _connect(db_url)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(_DDL)
            out = refresh(conn, period, records, asserted)
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — reported as the outcome, below
        logger.error("eia-retirements: refresh of %s failed: %s", period, e)
        _beat(beat, "error", note=f"EIA {period or '?'}: {str(e)[:220]}")
        return {"ok": False, "error": str(e)[:300], "period": period}

    out.update(ok=True, due=True, reason=reason, period=period,
               skipped=stats["no_key"] + stats["bad_month"], skipped_detail=stats)
    _beat(beat, "success", rows=out["upserted"],
          note=(f"EIA {period}: {out['inserted']} new, {out['updated']} refreshed, "
                f"{out['pruned']} no longer listed; {out['total_planned']} planned held"))
    logger.info("eia-retirements: %s", out)
    return out


def run_if_due(db_url=None, api_key=None):
    """crawler_scheduler slot `eia_retirements`: refresh only when due."""
    return run_eia_retirements_ingest(db_url, api_key, force=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run_eia_retirements_ingest())
