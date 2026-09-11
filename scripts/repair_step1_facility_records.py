#!/usr/bin/env python3
"""
repair_step1_facility_records.py — correct two owner-approved facility records
==============================================================================
Data QA 2026-09-11. Dry run by default; --apply writes; --rollback FILE undoes.

THE TWO CORRECTIONS
-------------------
1. Anthropic New York AI Campus
   slug anthropic-fluidstack-anthropic-new-york-ai-campus-9df234ed  (hash8 9df234ed)
   The city, the point 40.7128, -74.0060 (the New York City centroid), the
   market and the 200 MW were typed into project_seeder.py:129-145 with no
   source. Anthropic's 2025-11-12 announcement names New York State only:
     https://www.anthropic.com/news/anthropic-invests-50-billion-in-american-ai-infrastructure
   discovered_facilities rows (slug OR hash8) still carrying the seeded point
   (ROUND(latitude,4)=40.7128 AND ROUND(longitude,4)=-74.0060):
     latitude, longitude, city, market, power_mw -> NULL   (absent, not a placeholder)
     source_url -> the announcement;  last_updated -> this run's UTC stamp
     status (Planned), state, country and address are NOT touched.
   Legacy `facilities` rows: ONLY where a coordinate pair still carries the
   seeded point, null the same fields where the columns exist.
   --include-carrier-links (OFF by default): export every
   carrier_facility_presence row pointing at the corrected ids into the
   rollback file, then delete them.

2. Compass Goodyear Campus (Phoenix)
   slug compass-datacenters-compass-goodyear-campus-phoenix-dc9da94a  (hash8 dc9da94a)
   power  212 MW. https://www.compassdatacenters.com/data-center-markets/
          (modified 2026-08-21) lists a 212 MW Goodyear campus.
   point  33.4418, -112.3795, the centroid of OpenStreetMap way 723116259
          "Compass Datacenters Goodyear Campus".
          © OpenStreetMap contributors, ODbL, way 723116259
          The stored 33.4353, -112.3587 lies 1.56 km outside the campus.
   discovered_facilities rows (slug OR hash8) still at power_mw = 100:
     power_mw -> 212, latitude -> 33.4418, longitude -> -112.3795,
     source_url -> the markets page, last_updated -> this run's UTC stamp.
   Legacy `facilities` rows, column by column: power_mw -> 212 only where it is
   still 100; a coordinate pair -> the OSM centroid only where it still holds
   the old point.
   raw_data gains a `manual_correction` object recording both sources ONLY when
   the live column is json/jsonb. Every DDL in this repo declares it TEXT, so
   normally it is left alone and the provenance lives here and in the
   rollback file.

last_updated is ONE UTC timestamp computed by this script and bound as a
parameter (the `...Z` wire shape of utc_clock.utc_iso_z), not SQL NOW(): the
rollback file is written before the write and must record the exact value, so
both the fresh-connection read-back and a later --rollback can match it.

WHY EVERY SIBLING, AND WHY THE LEGACY ROW
-----------------------------------------
 * The API serves the MOST COMPLETE row among all rows sharing
   LEFT(MD5(provider|name),8) (main.py:23675-23733). Fix one sibling and an
   uncorrected twin becomes the anchor.
 * The linked legacy row's power_mw resurfaces through
   COALESCE(df.power_mw, f.power_mw): /api/v1/map (main.py:7126),
   /api/v1/facilities/slug/<slug> (main.py:37334), routes/d1_sync.py:251,
   routes/vectorize_sync.py:140.
 * coordinates_status is computed, not stored (routes/provenance.py:477-514):
   NULL latitude/longitude publishes `unknown`.
 * /api/v1/mcp/backfill-empty-city refills an empty city (and market) only
   while coordinates exist (routes/mcp_funnel_diag.py:486-511), so the city,
   the market and the point go NULL together.
 * The API's "on-site" carriers are carrier_facility_presence rows joined on
   the discovered id AND merged_facility_id (main.py:23705-23731). Nulling the
   coordinates does not remove them.

COLUMN NAMES: taken from the DDL, then probed live (pg_attribute) at run time
-----------------------------------------------------------------------------
 discovered_facilities  market (routes/discovery_routes.py:191, api_server.py:2143).
                        The API's "region" is `market AS region` (main.py:23712);
                        neither DDL has a region column. latitude/longitude
                        (discovery_routes.py:192-193), power_mw :194,
                        source_url :197, raw_data TEXT :198, last_updated TEXT
                        :201, merged_facility_id TEXT :205.
 facilities (legacy)    region (discovery_engine_v3.py:154, discovery_nexus.py:201);
                        market (declared in no DDL here, but read from
                        `facilities` by main.py:23758 and by
                        util/facility_ner_noindex.py:194, whose columns are
                        noted as verified against the live schema);
                        latitude/longitude (discovery_engine_v3.py:155-156) and
                        lat/lon (migrations/002_discovery_tables.sql:163-164,
                        util/facility_ner_noindex.py:196).
 carrier_facility_presence
                        dchub_facility_id TEXT (carrier_facility_ingestion.py:96),
                        holding BOTH id spaces (main.py:23625-23637).
 A candidate column that does not exist on the live table is skipped and reported.

NEVER WRITTEN
-------------
canonical_slug (frozen), name and provider (the API's hash lookup keys on
them), is_duplicate (a visibility flag, not a dedup mechanism) and
duplicate_of_id. The planner and every statement builder raise
FrozenColumnError: a real exception, not an `assert`, which `python -O` strips.

SAFETY
------
 * Dry run by default: prints every candidate row, the plan and the exact SQL.
 * More than 6 facility rows changing for ONE correction aborts before any
   write. (Carrier rows are not counted: the research counts 263 carriers on
   the Anthropic record, so counting them would make the flag unusable.)
 * --apply and --rollback refuse when DATABASE_URL is the read replica: the
   same URL, or the same host/port/database, as NEON_REPLICA_URL or
   DATABASE_READ_URL (the replica pair routes/_conn_provenance.py:42-53 and
   main.py:1382-1385 read). No existing repair script had a replica guard.
 * --apply writes the rollback JSON (the prior value of every column touched,
   plus every carrier row it will delete) BEFORE the first UPDATE, and refuses
   to overwrite an existing file.
 * ONE transaction: autocommit off, SET LOCAL lock and statement timeouts.
   Every UPDATE is a compare-and-swap on exactly the values the rollback file
   recorded; any rowcount other than the expected one rolls everything back.
 * After COMMIT every corrected row is re-read on a FRESH connection and
   compared with the plan. Any mismatch exits non-zero.

USAGE (DATABASE_URL must point at the PRIMARY)
-----
    python3 scripts/repair_step1_facility_records.py                     # dry run
    python3 scripts/repair_step1_facility_records.py --apply [--include-carrier-links] [--rollback-out FILE]
    python3 scripts/repair_step1_facility_records.py --rollback FILE

Exit: 0 ok · 1 write or read-back failure · 2 refused (env, replica, schema,
      rollback file, frozen column) · 3 row cap exceeded · 4 a correction
      matched no rows (wrong database?)

If a write fails with SQLSTATE 25006 (read-only transaction) on the pooled
endpoint, see routes/_session_dsn.py: use the DIRECT (non -pooler) endpoint.

RUNBOOK AFTER --apply (auth header NAME only: X-Admin-Key)
---------------------------------------------------------
1. Purge Cloudflare (routes/cf_purge.py:94-110), once per slug:
     POST https://dchub.cloud/api/v1/cf/purge        header: X-Admin-Key
     {"urls": ["https://dchub.cloud/facilities/<slug>",
               "https://dchub.cloud/facilities/<slug>.json",
               "https://dchub.cloud/api/v1/facilities/<slug>",
               "https://dchub.cloud/api/v1/facility/<slug>",
               "https://dchub.cloud/api/v1/facilities/by-slug/<slug>",
               "https://dchub.cloud/api/v1/facilities/slug/<slug>"]}
   /facilities/<slug>.json is routes/facility_profile_page.py:2003 and
   /api/v1/facilities/slug/<slug> is main.py:37314. by-slug is fetched by the
   edge worker (PATCHES/dchubapiproxy-*.js); no Flask route defines it.
2. Rebuild the sitemap snapshot (main.py:33915):
     POST https://dchub.cloud/api/v1/admin/sitemap/rebuild-snapshot   header: X-Admin-Key
3. Expected:
   * Anthropic: robots noindex because contentless (util/thin_content.py:107-116,
     routes/facility_profile_page.py:1143-1145), and dropped from the sitemap
     (util/thin_content.py:119-181, main.py:33466). BOTH hold only if EVERY row
     carrying the slug is left with no power, no coordinates, no real city AND
     NO ADDRESS. address is not part of this correction; the dry run prints it
     and predicts the outcome. (util/facility_ner_noindex.py:225-229 is a
     second noindex path for legacy source='news_pipeline' rows, and it also
     needs sqft empty.)
   * Compass: 212 MW at the OSM point.
4. Known leftovers:
   * The D1 failover mirror reads only rows WITH coordinates
     (routes/d1_sync.py:256-257), so it stops refreshing the Anthropic row.
     That row is deleted only by the prune after a FULLY successful pass
     (d1_sync.py:319-324, 355-368). Until then D1 keeps the old point and
     200 MW.
   * The Anthropic page body can still name a New York market: with no city
     match and no coordinates the DCPI lookup falls back to the most recent
     market in the state (routes/facility_profile_page.py:482-489).
   * Carrier rows deleted by --include-carrier-links are not re-attached to the
     real New York buildings their facility_pdb_id describes.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import struct
import sys
from collections import namedtuple
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FORMAT = "repair_step1_facility_records/v1"

DISCOVERED = "discovered_facilities"
LEGACY = "facilities"
CARRIER = "carrier_facility_presence"
TABLES = (DISCOVERED, LEGACY, CARRIER)

# Never written, in any table, by any path (apply or rollback).
FROZEN_COLUMNS = frozenset({"canonical_slug", "name", "provider",
                            "is_duplicate", "duplicate_of_id"})

MAX_ROWS_PER_CORRECTION = 6

REPLICA_ENV_VARS = ("NEON_REPLICA_URL", "DATABASE_READ_URL")

# routes/facility_slug.py::hash_sql('') — pinned equal by the test suite.
HASH8_SQL = "LEFT(MD5(COALESCE(provider,'')||'|'||COALESCE(name,'')),8)"

TX_SETTINGS = ("SET LOCAL lock_timeout = '5s'",
               "SET LOCAL statement_timeout = '120s'")

# Coordinate columns per table, from the DDL (see the docstring).
COORD_PAIRS = {DISCOVERED: (("latitude", "longitude"),),
               LEGACY: (("latitude", "longitude"), ("lat", "lon"))}
# The market/region column(s) per table.
AREA_COLUMNS = {DISCOVERED: ("market",), LEGACY: ("region", "market")}

DISPLAY_COLUMNS = ("id", "name", "provider", "canonical_slug", "source",
                   "city", "state", "market", "region", "latitude",
                   "longitude", "lat", "lon", "power_mw", "status", "address",
                   "sqft", "source_url", "last_updated", "merged_facility_id",
                   "is_duplicate", "duplicate_of_id", "raw_data")

REQUIRED_COLUMNS = {
    DISCOVERED: ("id", "name", "provider", "latitude", "longitude",
                 "power_mw", "city", "source_url", "last_updated"),
    LEGACY: ("id", "name", "provider"),
    CARRIER: ("id", "dchub_facility_id"),
}

JSON_TYPES = ("json", "jsonb")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_TYPE_NAME = re.compile(r"^[a-z][a-z0-9 ]*(\(\d+(,\d+)?\))?(\[\])?$")

SCHEMA_SQL = ("SELECT a.attname, format_type(a.atttypid, a.atttypmod) "
              "FROM pg_attribute a "
              "WHERE a.attrelid = to_regclass(%s) AND a.attnum > 0 "
              "AND NOT a.attisdropped ORDER BY a.attnum")

PLANNED, ALREADY, CHANGED = "planned", "already applied", "changed"

Change = namedtuple("Change", "table id set prior")
Skip = namedtuple("Skip", "table id reason")


class FrozenColumnError(Exception):
    """A write would touch a frozen column or a table this script may not write."""


class Refused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


# ─── value helpers (pure) ────────────────────────────────────────────────────

def _hash8(provider, name) -> str:
    return hashlib.md5(((provider or "") + "|" + (name or "")).encode("utf-8")).hexdigest()[:8]


def _round4(value):
    """ROUND(value::numeric, 4), half away from zero like Postgres numeric."""
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    except (ArithmeticError, ValueError):
        return None


def _at_point(row, pair, point) -> bool:
    return (_round4(row.get(pair[0])) == point[0]
            and _round4(row.get(pair[1])) == point[1])


def _num_eq(value, number) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)) == Decimal(str(number))
    except (ArithmeticError, ValueError):
        return False


def _float4(value) -> float:
    return struct.unpack("f", struct.pack("f", float(value)))[0]


def _instant(value) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _json_value(value):
    return json.loads(value) if isinstance(value, str) else value


def values_equal(actual, expected, col_type) -> bool:
    """Type-aware equality for plan checks and read-back. NULL equals only NULL."""
    if actual is None or expected is None:
        return actual is None and expected is None
    kind = (col_type or "text").lower()
    try:
        if kind == "real":
            return _float4(actual) == _float4(expected)
        if (kind in ("double precision", "smallint", "integer", "bigint")
                or kind.startswith(("numeric", "decimal"))):
            return Decimal(str(actual)) == Decimal(str(expected))
        if kind.startswith("timestamp") or kind == "date":
            return _instant(actual) == _instant(expected)
        if kind in JSON_TYPES:
            return _json_value(actual) == _json_value(expected)
        if kind == "boolean":
            return bool(actual) == bool(expected)
    except (ArithmeticError, ValueError, TypeError, InvalidOperation):
        return False
    return str(actual) == str(expected)


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _pairs(table, cols):
    return [p for p in COORD_PAIRS[table] if p[0] in cols and p[1] in cols]


# ─── the two corrections (pure data + decisions) ─────────────────────────────

class Correction:
    key = title = slug = hash8 = ""
    carrier_links = False
    provenance: dict = {}

    def describe(self) -> dict:
        return {"key": self.key, "title": self.title, "slug": self.slug,
                "hash8": self.hash8, "provenance": self.provenance}

    def identity(self, table, row, linked) -> str:
        """Why this row is this facility: slug, hash8, and/or a merged link."""
        why = []
        if row.get("canonical_slug") == self.slug:
            why.append("slug")
        if _hash8(row.get("provider"), row.get("name")) == self.hash8:
            why.append("hash8")
        if table == LEGACY and str(row.get("id")) in linked:
            why.append("linked from discovered id "
                       + ",".join(str(i) for i in linked[str(row.get("id"))]))
        return "+".join(why)

    def decide(self, table, row, cols, stamp):
        """-> (PLANNED, {column: new value}) or (ALREADY|CHANGED, {})."""
        raise NotImplementedError

    def finish(self, table, row, cols, target, plan):
        """Hook: add provenance / notes to a PLANNED row's target."""

    def skipped_legacy_warning(self, row, linked_from):
        return None


class AnthropicNewYork(Correction):
    key = "anthropic_new_york_ai_campus"
    title = "Anthropic New York AI Campus"
    slug = "anthropic-fluidstack-anthropic-new-york-ai-campus-9df234ed"
    hash8 = "9df234ed"
    carrier_links = True
    SOURCE_URL = ("https://www.anthropic.com/news/"
                  "anthropic-invests-50-billion-in-american-ai-infrastructure")
    SEEDED_POINT = (Decimal("40.7128"), Decimal("-74.0060"))
    provenance = {
        "date": "2026-09-11",
        "source_url": SOURCE_URL,
        "finding": ("Anthropic's 2025-11-12 announcement names New York State "
                    "only. The city, the point 40.7128,-74.0060, the market and "
                    "200 MW were hand-seeded (project_seeder.py) with no source."),
    }

    def _nulled(self, table, cols):
        return [c for c in ("city", *AREA_COLUMNS[table], "power_mw") if c in cols]

    def decide(self, table, row, cols, stamp):
        pairs = _pairs(table, cols)
        seeded = [p for p in pairs if _at_point(row, p, self.SEEDED_POINT)]
        if seeded:
            target = {}
            for pair in (pairs if table == DISCOVERED else seeded):
                target[pair[0]] = None
                target[pair[1]] = None
            for col in self._nulled(table, cols):
                target[col] = None
            if table == DISCOVERED:
                target["source_url"] = self.SOURCE_URL
                target["last_updated"] = stamp
            return PLANNED, target
        settled = (all(row.get(c) is None for p in pairs for c in p)
                   and all(row.get(c) is None for c in self._nulled(table, cols))
                   and (table != DISCOVERED or row.get("source_url") == self.SOURCE_URL))
        return (ALREADY if settled else CHANGED), {}

    def skipped_legacy_warning(self, row, linked_from):
        if linked_from and row.get("power_mw") not in (None, 0, 0.0):
            return (f"facilities id={row.get('id')} is linked from discovered id "
                    f"{linked_from} but does not carry the seeded point, so it is "
                    f"NOT corrected; its power_mw={row.get('power_mw')!r} still "
                    "resurfaces through COALESCE(df.power_mw, f.power_mw) once the "
                    "discovered power_mw is NULL (main.py:7126, main.py:37334, "
                    "routes/d1_sync.py:251, routes/vectorize_sync.py:140)")
        return None


class CompassGoodyear(Correction):
    key = "compass_goodyear_campus_phoenix"
    title = "Compass Goodyear Campus (Phoenix)"
    slug = "compass-datacenters-compass-goodyear-campus-phoenix-dc9da94a"
    hash8 = "dc9da94a"
    SOURCE_URL = "https://www.compassdatacenters.com/data-center-markets/"
    OSM_ATTRIBUTION = "© OpenStreetMap contributors, ODbL, way 723116259"
    OLD_POWER, NEW_POWER = 100, 212
    OLD_POINT = (Decimal("33.4353"), Decimal("-112.3587"))
    NEW_POINT = (33.4418, -112.3795)
    provenance = {
        "date": "2026-09-11",
        "power_mw": {
            "value": 212, "source_url": SOURCE_URL,
            "finding": ("The Compass markets page (modified 2026-08-21) lists a "
                        "212 MW Goodyear campus.")},
        "coordinates": {
            "latitude": 33.4418, "longitude": -112.3795,
            "source_url": "https://www.openstreetmap.org/way/723116259",
            "method": ("centroid of OpenStreetMap way 723116259 "
                       "'Compass Datacenters Goodyear Campus'"),
            "attribution": OSM_ATTRIBUTION,
            "replaced": ("33.4353, -112.3587, which lies 1.56 km outside the "
                         "campus")},
    }

    def _new_point_dec(self):
        return tuple(Decimal(str(v)) for v in self.NEW_POINT)

    def decide(self, table, row, cols, stamp):
        pairs = _pairs(table, cols)
        if table == DISCOVERED:
            if _num_eq(row.get("power_mw"), self.OLD_POWER):
                return PLANNED, {"power_mw": self.NEW_POWER,
                                 "latitude": self.NEW_POINT[0],
                                 "longitude": self.NEW_POINT[1],
                                 "source_url": self.SOURCE_URL,
                                 "last_updated": stamp}
            settled = (_num_eq(row.get("power_mw"), self.NEW_POWER)
                       and _at_point(row, ("latitude", "longitude"), self._new_point_dec())
                       and row.get("source_url") == self.SOURCE_URL)
            return (ALREADY if settled else CHANGED), {}
        target = {}
        if "power_mw" in cols and _num_eq(row.get("power_mw"), self.OLD_POWER):
            target["power_mw"] = self.NEW_POWER
        for pair in pairs:
            if _at_point(row, pair, self.OLD_POINT):
                target[pair[0]], target[pair[1]] = self.NEW_POINT
        if target:
            return PLANNED, target
        settled = (_num_eq(row.get("power_mw"), self.NEW_POWER)
                   or any(_at_point(row, p, self._new_point_dec()) for p in pairs))
        return (ALREADY if settled else CHANGED), {}

    def finish(self, table, row, cols, target, plan):
        if (table == DISCOVERED and "latitude" in target
                and not _at_point(row, ("latitude", "longitude"), self.OLD_POINT)):
            plan.notes.append(
                f"{table} id={row.get('id')}: stored point "
                f"({row.get('latitude')}, {row.get('longitude')}) is not the "
                "researched 33.4353, -112.3587; it is replaced anyway because "
                "the precondition is power_mw = 100")
        if not any(c in target for p in COORD_PAIRS[table] for c in p):
            return
        col_type = cols.get("raw_data")
        if col_type is None:
            return
        if col_type not in JSON_TYPES:
            note = (f"{table}.raw_data is {col_type}, not json/jsonb: left untouched; "
                    "the OSM provenance is recorded in the script docstring and the "
                    "rollback file")
            if note not in plan.notes:
                plan.notes.append(note)
            return
        old = row.get("raw_data")
        try:
            doc = {} if old in (None, "") else _json_value(old)
        except ValueError:
            plan.notes.append(f"{table} id={row.get('id')}: raw_data is not "
                              "parseable JSON; left untouched")
            return
        if not isinstance(doc, dict):
            plan.notes.append(f"{table} id={row.get('id')}: raw_data is not a "
                              "JSON object; left untouched")
            return
        merged = dict(doc)
        merged["manual_correction"] = self.provenance
        target["raw_data"] = json.dumps(merged, ensure_ascii=False)


CORRECTIONS = (AnthropicNewYork(), CompassGoodyear())


# ─── planning (PURE: fetched rows in, changes out, no DB access) ─────────────

class CorrectionPlan:
    def __init__(self, correction):
        self.correction = correction
        self.changes = []      # [Change(table, id, {column: new}, {column: old})]
        self.skipped = []      # [Skip(table, id, reason)]
        self.warnings = []
        self.notes = []
        self.carrier_ids = []  # dchub_facility_id values whose carrier rows may go
        self.rows_after = []   # [(table, row with this plan applied)]


def _check_frozen(table, columns):
    bad = sorted(set(columns) & FROZEN_COLUMNS)
    if bad:
        raise FrozenColumnError(f"refusing to write frozen column(s) {bad} on {table}")


def _linked_ids(discovered_rows):
    """legacy facilities id (text) -> discovered ids whose merged_facility_id names it."""
    linked = {}
    for row in discovered_rows:
        merged = row.get("merged_facility_id")
        if merged not in (None, ""):
            linked.setdefault(str(merged), []).append(row.get("id"))
    return linked


def _id_key(row):
    return str(row.get("id"))


def plan_correction(correction, discovered_rows, legacy_rows, schema, stamp):
    """Fetched rows -> CorrectionPlan. Pure, so the decision is unit-tested.

    A row that no longer carries the value being corrected is SKIPPED, as
    `already applied` (it holds the corrected value) or `changed` (it holds
    something else, which this script has no evidence about).
    """
    plan = CorrectionPlan(correction)
    linked = _linked_ids(discovered_rows)
    carrier_ids = set()
    for table, rows in ((DISCOVERED, discovered_rows), (LEGACY, legacy_rows)):
        cols = schema.get(table) or {}
        for row in sorted(rows, key=_id_key):
            row_id = row.get("id")
            if not correction.identity(table, row, linked):
                plan.skipped.append(Skip(table, row_id, "not this facility "
                                         "(no slug, hash8 or merged link)"))
                continue
            state, target = correction.decide(table, row, cols, stamp)
            after = dict(row)
            if state == PLANNED:
                correction.finish(table, row, cols, target, plan)
                _check_frozen(table, target)
                new = {c: v for c, v in target.items()
                       if not values_equal(row.get(c), v, cols.get(c))}
                if new:
                    plan.changes.append(Change(table, row_id, new,
                                               {c: row.get(c) for c in new}))
                    after.update(new)
                else:
                    state = ALREADY
                    plan.skipped.append(Skip(table, row_id, ALREADY))
            elif state == ALREADY:
                plan.skipped.append(Skip(table, row_id, ALREADY))
            else:
                plan.skipped.append(Skip(table, row_id, "changed: no longer "
                                         "carries the value being corrected"))
                if table == LEGACY:
                    warning = correction.skipped_legacy_warning(
                        row, linked.get(str(row_id)))
                    if warning:
                        plan.warnings.append(warning)
            plan.rows_after.append((table, after))
            if correction.carrier_links and state in (PLANNED, ALREADY):
                carrier_ids.add(str(row_id))
                merged = row.get("merged_facility_id")
                if table == DISCOVERED and merged not in (None, ""):
                    carrier_ids.add(str(merged))
    plan.carrier_ids = sorted(carrier_ids)
    return plan


# ─── statement builders (pure) — the only place write SQL is made ────────────

def _ident(name):
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"not a plain column name: {name!r}")
    return name


def _cas(col, col_type):
    """`col still holds %s`, compared in the column's own type.

    CAST to the declared type makes a float4 column compare as float4, a
    numeric(10,6) as numeric, and so on. json has no equality operator and
    jsonb normalises its text, so JSON compares semantically as jsonb.
    """
    if col_type in JSON_TYPES:
        return f"{col}::jsonb IS NOT DISTINCT FROM CAST(%s AS jsonb)"
    if not col_type or not _TYPE_NAME.match(col_type):
        raise ValueError(f"unexpected column type {col_type!r} for {col}")
    return f"{col} IS NOT DISTINCT FROM CAST(%s AS {col_type})"


def build_update(table, row_id, new_values, expected_values, types):
    """UPDATE <table> SET <new> WHERE id = <id> AND every column still holds <expected>."""
    if table not in (DISCOVERED, LEGACY):
        raise FrozenColumnError(f"refusing to UPDATE {table}: only {DISCOVERED} "
                                f"and {LEGACY} rows are corrected")
    _check_frozen(table, new_values)
    _check_frozen(table, expected_values)
    cols = [_ident(c) for c in new_values]
    if not cols or set(cols) != set(expected_values):
        raise ValueError(f"{table} id={row_id}: SET and compare-and-swap columns differ")
    params = ([new_values[c] for c in cols] + [row_id]
              + [expected_values[c] for c in cols])
    where = " AND ".join(["id = %s"] + [_cas(c, types.get(c)) for c in cols])
    sql = (f"UPDATE {table} SET " + ", ".join(f"{c} = %s" for c in cols)
           + f" WHERE {where}")
    return sql, params


def build_delete(table, row_ids, facility_ids):
    if table != CARRIER:
        raise FrozenColumnError(f"refusing to DELETE from {table}: only "
                                f"{CARRIER} rows may be deleted")
    if not row_ids or not facility_ids:
        raise ValueError("refusing an empty DELETE target")
    return (f"DELETE FROM {CARRIER} WHERE id = ANY(%s) "
            f"AND dchub_facility_id = ANY(%s::text[])",
            [list(row_ids), [str(i) for i in facility_ids]])


def build_insert(table, row):
    if table != CARRIER:
        raise FrozenColumnError(f"refusing to INSERT into {table}: only deleted "
                                f"{CARRIER} rows are re-inserted")
    _check_frozen(table, row)
    cols = [_ident(c) for c in row]
    return (f"INSERT INTO {CARRIER} (" + ", ".join(cols) + ") VALUES ("
            + ", ".join("%s" for _ in cols) + ")",
            [row[c] for c in cols])


def apply_statements(plans, schema, carrier_rows, carrier_ids):
    """Every WRITE --apply executes, in order: (sql, params, expected rowcount).

    TX_SETTINGS run first, inside the same transaction, from _begin().
    """
    stmts = []
    for plan in plans:
        for change in plan.changes:
            sql, params = build_update(change.table, change.id, change.set,
                                       change.prior, schema[change.table])
            stmts.append((sql, params, 1))
    if carrier_rows:
        sql, params = build_delete(CARRIER, [r["id"] for r in carrier_rows],
                                   carrier_ids)
        stmts.append((sql, params, len(carrier_rows)))
    return stmts


# ─── read queries (pure builders) ────────────────────────────────────────────

def _select_list(types, wanted):
    out = []
    for col in wanted:
        if col in types:
            out.append(f"{_ident(col)}::text AS {col}" if types[col] in JSON_TYPES
                       else _ident(col))
    return ", ".join(out)


def _display_columns(types):
    # raw_data is only read when it can be written (json/jsonb).
    return [c for c in DISPLAY_COLUMNS
            if c != "raw_data" or types.get("raw_data") in JSON_TYPES]


def discovered_candidates_query(schema, correction):
    types = schema[DISCOVERED]
    where, params = f"{HASH8_SQL} = %s", [correction.hash8]
    if "canonical_slug" in types:
        where, params = (f"canonical_slug = %s OR {where}",
                         [correction.slug, correction.hash8])
    return (f"SELECT {_select_list(types, _display_columns(types))} "
            f"FROM {DISCOVERED} WHERE {where}", params)


def legacy_candidates_query(schema, correction, linked_ids):
    types = schema[LEGACY]
    where = f"id::text = ANY(%s::text[]) OR {HASH8_SQL} = %s"
    params = [sorted(linked_ids), correction.hash8]
    if "canonical_slug" in types:
        where += " OR canonical_slug = %s"
        params.append(correction.slug)
    return (f"SELECT {_select_list(types, _display_columns(types))} "
            f"FROM {LEGACY} WHERE {where}", params)


def carrier_rows_query(schema, facility_ids):
    types = schema[CARRIER]
    return (f"SELECT {_select_list(types, list(types))} FROM {CARRIER} "
            f"WHERE dchub_facility_id = ANY(%s::text[])", [sorted(facility_ids)])


def readback_query(table, columns, types, ids):
    missing = [c for c in columns if c not in types]
    if missing:
        raise ValueError(f"{table} no longer has column(s) {missing}; cannot verify")
    wanted = [c for c in columns if c != "id"]
    return (f"SELECT id, {_select_list(types, wanted)} FROM {table} "
            f"WHERE id::text = ANY(%s::text[])", [[str(i) for i in ids]])


def rollback_payload(plans, schema, carrier_rows, carrier_ids, stamp, include_links):
    types = {}
    for plan in plans:
        for change in plan.changes:
            types.setdefault(change.table, {}).update(
                {c: schema[change.table].get(c) for c in change.set})
    if carrier_rows:
        types[CARRIER] = dict(schema[CARRIER])
    return {
        "format": FORMAT,
        "generated_at_utc": stamp,
        "last_updated_written": stamp,
        "undo": "python3 scripts/repair_step1_facility_records.py --rollback <this file>",
        "include_carrier_links": bool(include_links),
        "corrections": [plan.correction.describe() for plan in plans],
        "column_types": types,
        "changes": [{"correction": plan.correction.key, "table": change.table,
                     "id": _jsonable(change.id), "set": _jsonable(change.set),
                     "prior": _jsonable(change.prior)}
                    for plan in plans for change in plan.changes],
        "carrier_facility_ids": sorted(carrier_ids) if carrier_rows else [],
        "carrier_rows": [_jsonable(r) for r in carrier_rows],
    }


# ─── safety rails that need the environment or the filesystem ────────────────

def _endpoint(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if not host:
        return None
    return (host, port, (parsed.path or "").strip("/"))


def replica_refusal(environ):
    """A message (naming variables, never values) when DATABASE_URL is the replica."""
    dsn = (environ.get("DATABASE_URL") or "").strip()
    for name in REPLICA_ENV_VARS:
        replica = (environ.get(name) or "").strip()
        if not replica:
            continue
        same = replica == dsn
        if not same:
            endpoint = _endpoint(replica)
            same = endpoint is not None and endpoint == _endpoint(dsn)
        if same:
            return (f"REFUSING: DATABASE_URL points at the same endpoint as {name}, "
                    "the read replica. Point DATABASE_URL at the PRIMARY and re-run.")
    return None


def write_rollback_file(path, payload):
    """Create (never overwrite) and fsync the rollback file."""
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


# ─── database layer ──────────────────────────────────────────────────────────

class WriteAborted(Exception):
    """A statement touched an unexpected number of rows; the transaction was rolled back."""


def _connect(dsn):
    import psycopg2
    return psycopg2.connect(dsn, sslmode="require", connect_timeout=10)


def _quiet_rollback(conn):
    try:
        conn.rollback()
    except Exception as exc:  # noqa: BLE001 — the original error is what matters
        print(f"   (rollback itself failed: {type(exc).__name__})", file=sys.stderr)


def _quiet_close(conn):
    try:
        conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"   (close failed: {type(exc).__name__})", file=sys.stderr)


def _rows(cur):
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def probe_schema(cur):
    """{table: {column: format_type}} for the tables unqualified SQL resolves to."""
    schema = {}
    for table in TABLES:
        cur.execute(SCHEMA_SQL, [table])
        cols = {name: col_type for name, col_type in cur.fetchall()}
        if cols:
            schema[table] = cols
    return schema


def require_columns(schema, include_links):
    tables = (DISCOVERED, LEGACY) + ((CARRIER,) if include_links else ())
    for table in tables:
        if table not in schema:
            if table == LEGACY:
                print(f"NOTE: table {LEGACY} not found; legacy rows are not considered")
                continue
            raise Refused(2, f"table {table} not found")
        missing = [c for c in REQUIRED_COLUMNS[table] if c not in schema[table]]
        if missing:
            raise Refused(2, f"{table} lacks required column(s) {missing}")


def fetch_candidates(cur, schema, correction):
    sql, params = discovered_candidates_query(schema, correction)
    cur.execute(sql, params)
    discovered = sorted(_rows(cur), key=_id_key)
    legacy = []
    if LEGACY in schema:
        sql, params = legacy_candidates_query(schema, correction,
                                              _linked_ids(discovered))
        cur.execute(sql, params)
        legacy = sorted(_rows(cur), key=_id_key)
    return discovered, legacy


def verify(connect, dsn, expected, schema, carrier_absent=(), carrier_present=()):
    """Re-read on a FRESH connection and compare. expected: [(table, id, {col: value})].

    A fresh connection is the point: a write that was never committed is still
    visible to the connection that made it, and invisible to every other.
    """
    mismatches = []
    conn = connect(dsn)
    try:
        cur = conn.cursor()
        grouped = {}
        for table, row_id, values in expected:
            grouped.setdefault(table, []).append((row_id, values))
        for table, items in grouped.items():
            types = schema[table]
            columns = sorted({c for _, values in items for c in values})
            sql, params = readback_query(table, columns, types, [i for i, _ in items])
            cur.execute(sql, params)
            found = {str(r["id"]): r for r in _rows(cur)}
            for row_id, values in items:
                row = found.get(str(row_id))
                if row is None:
                    mismatches.append(f"{table} id={row_id}: not found on read-back")
                    continue
                for col, want in values.items():
                    if not values_equal(row.get(col), want, types.get(col)):
                        mismatches.append(f"{table} id={row_id} {col}: expected "
                                          f"{want!r}, read back {row.get(col)!r}")
        if carrier_absent:
            cur.execute(f"SELECT id FROM {CARRIER} WHERE id = ANY(%s)",
                        [list(carrier_absent)])
            for row in _rows(cur):
                mismatches.append(f"{CARRIER} id={row['id']}: still present after DELETE")
        if carrier_present:
            types = schema[CARRIER]
            cols = sorted({c for r in carrier_present for c in r if c != "id"})
            sql, params = readback_query(CARRIER, cols, types,
                                         [r["id"] for r in carrier_present])
            cur.execute(sql, params)
            found = {str(r["id"]): r for r in _rows(cur)}
            for want in carrier_present:
                got = found.get(str(want["id"]))
                if got is None:
                    mismatches.append(f"{CARRIER} id={want['id']}: not re-inserted")
                    continue
                for col in cols:
                    if not values_equal(got.get(col), want.get(col), types.get(col)):
                        mismatches.append(f"{CARRIER} id={want['id']} {col}: expected "
                                          f"{want.get(col)!r}, read back {got.get(col)!r}")
        _quiet_rollback(conn)
    finally:
        _quiet_close(conn)
    return mismatches


def report_verification(mismatches, what):
    if mismatches:
        print(f"\nREAD-BACK MISMATCH on a fresh connection ({len(mismatches)}):",
              file=sys.stderr)
        for line in mismatches:
            print(f"   {line}", file=sys.stderr)
        return 1
    print(f"\nread-back OK on a fresh connection: {what}")
    return 0


# ─── printing ────────────────────────────────────────────────────────────────

def _fmt(value, limit=80):
    text = "NULL" if value is None else repr(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def print_candidates(correction, discovered, legacy, schema):
    linked = _linked_ids(discovered)
    print(f"\n== {correction.title}")
    print(f"   slug={correction.slug}  hash8={correction.hash8}")
    print(f"   candidates: {len(discovered)} in {DISCOVERED}, {len(legacy)} in {LEGACY}")
    for table, rows in ((DISCOVERED, discovered), (LEGACY, legacy)):
        cols = schema.get(table) or {}
        for row in rows:
            match = correction.identity(table, row, linked) or "NO MATCH"
            link = (f"merged_facility_id={_fmt(row.get('merged_facility_id'))}"
                    if table == DISCOVERED else
                    f"linked_from_discovered={linked.get(str(row.get('id')), [])}")
            area = " ".join(f"{c}={_fmt(row.get(c))}"
                            for c in AREA_COLUMNS[table] if c in cols)
            coords = " ".join(f"{a},{b}={_fmt(row.get(a))},{_fmt(row.get(b))}"
                              for a, b in _pairs(table, cols))
            print(f"   - {table} id={row.get('id')!r} [match: {match}]")
            print(f"       name={_fmt(row.get('name'))} provider={_fmt(row.get('provider'))}"
                  f" status={_fmt(row.get('status'))}")
            print(f"       city={_fmt(row.get('city'))} {area} {coords}"
                  f" power_mw={_fmt(row.get('power_mw'))}")
            print(f"       address={_fmt(row.get('address'))} {link}"
                  f" is_duplicate={_fmt(row.get('is_duplicate'))}")


def _load_thin_content():
    path = os.path.join(ROOT, "util", "thin_content.py")
    spec = importlib.util.spec_from_file_location("_repair_step1_thin_content", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "evidence", None)):
        raise AttributeError("util/thin_content.py has no evidence()")
    return module


def predict_page_outcome(plan):
    """What util/thin_content.evidence says about the rows left carrying the slug."""
    try:
        thin = _load_thin_content()
    except Exception as exc:  # noqa: BLE001 — said out loud, never silently
        return [f"PAGE OUTCOME: not predicted; util/thin_content.py did not load "
                f"({type(exc).__name__})"]
    live = [(t, r) for t, r in plan.rows_after
            if r.get("canonical_slug") == plan.correction.slug
            and (t == LEGACY or not r.get("is_duplicate"))]
    if not live:
        return ["PAGE OUTCOME: not predicted; no live candidate row carries the frozen slug"]
    carrying = []
    for table, row in live:
        facts = [k for k, v in thin.evidence(row).items() if v]
        if facts:
            carrying.append(f"{table} id={row.get('id')!r}: {', '.join(facts)}")
    if carrying:
        return ["PAGE OUTCOME after plan: NOT contentless; evidence remains on "
                + "; ".join(carrying)]
    return [f"PAGE OUTCOME after plan: all {len(live)} live row(s) carrying the slug "
            "are contentless -> robots noindex and dropped from the sitemap"]


def print_plan(plan):
    print(f"   PLAN: {len(plan.changes)} row(s) change "
          f"(cap {MAX_ROWS_PER_CORRECTION} per correction)")
    for change in plan.changes:
        print(f"     UPDATE {change.table} id={change.id!r}")
        for col, new in change.set.items():
            print(f"         {col:<14} {_fmt(change.prior[col])} -> {_fmt(new)}")
    for skip in plan.skipped:
        print(f"   SKIPPED {skip.table} id={skip.id!r}: {skip.reason}")
    for warning in plan.warnings:
        print(f"   WARNING: {warning}")
    for note in plan.notes:
        print(f"   NOTE: {note}")
    for line in predict_page_outcome(plan):
        print(f"   {line}")


def print_carrier_summary(carrier_ids, carrier_rows, include_links, schema):
    if not carrier_ids:
        return
    print(f"\n== {CARRIER}")
    if CARRIER not in schema:
        print("   table not found; no carrier links to report")
        return
    per_id = {}
    for row in carrier_rows:
        key = str(row.get("dchub_facility_id"))
        per_id[key] = per_id.get(key, 0) + 1
    carriers = len({r.get("carrier_name") for r in carrier_rows if r.get("carrier_name")})
    print(f"   {len(carrier_rows)} row(s), {carriers} distinct carrier(s), reference the "
          f"corrected id(s) {carrier_ids}; rows per id: {per_id}")
    if include_links:
        print("   --include-carrier-links: exported to the rollback file, then DELETED")
    else:
        print("   --include-carrier-links not given: left in place")


def print_sql(stmts, executing):
    label = "ONE transaction" if executing else "dry run: NOT executed"
    print(f"\n== PLANNED SQL ({len(TX_SETTINGS) + len(stmts)} statement(s), {label})")
    for sql in TX_SETTINGS:
        print(f"   {sql};")
    for sql, params, expect in stmts:
        print(f"   {sql};")
        print(f"       params={json.dumps(_jsonable(params), ensure_ascii=False)}"
              f"  expected_rowcount={expect}")


# ─── the two runs ────────────────────────────────────────────────────────────

def _begin(conn):
    """Explicit transaction: autocommit OFF, so SET LOCAL binds every later statement."""
    conn.autocommit = False
    cur = conn.cursor()
    for sql in TX_SETTINGS:
        cur.execute(sql)
    return cur


def _execute_writes(conn, cur, stmts, undo_hint):
    try:
        for sql, params, expect in stmts:
            cur.execute(sql, params)
            if cur.rowcount != expect:
                raise WriteAborted(f"expected {expect} row(s), got {cur.rowcount}: {sql[:90]}")
        conn.commit()
    except Exception as exc:
        _quiet_rollback(conn)
        raise WriteAborted(f"{type(exc).__name__}: {exc}. Rolled back; nothing was "
                           f"committed. {undo_hint}") from exc


def run_repair(connect, dsn, args, clock):
    moment = clock().astimezone(timezone.utc)
    stamp = moment.isoformat().replace("+00:00", "Z")
    conn = connect(dsn)
    try:
        cur = _begin(conn)
        schema = probe_schema(cur)
        require_columns(schema, args.include_carrier_links)
        plans, unmatched = [], []
        for correction in CORRECTIONS:
            discovered, legacy = fetch_candidates(cur, schema, correction)
            print_candidates(correction, discovered, legacy, schema)
            plan = plan_correction(correction, discovered, legacy, schema, stamp)
            print_plan(plan)
            plans.append(plan)
            if not discovered and not legacy:
                unmatched.append(correction.title)
        carrier_ids = sorted({i for plan in plans for i in plan.carrier_ids})
        carrier_rows = []
        if carrier_ids and CARRIER in schema:
            sql, params = carrier_rows_query(schema, carrier_ids)
            cur.execute(sql, params)
            carrier_rows = sorted(_rows(cur), key=_id_key)
        print_carrier_summary(carrier_ids, carrier_rows, args.include_carrier_links, schema)
        to_delete = carrier_rows if args.include_carrier_links else []
        stmts = apply_statements(plans, schema, to_delete, carrier_ids)
        print_sql(stmts, args.apply)

        if unmatched:
            _quiet_rollback(conn)
            print(f"\nABORT: no candidate rows matched {unmatched}. Is DATABASE_URL the "
                  "production database? Nothing written.", file=sys.stderr)
            return 4
        over = [f"{p.correction.title}: {len(p.changes)} rows" for p in plans
                if len(p.changes) > MAX_ROWS_PER_CORRECTION]
        if over:
            _quiet_rollback(conn)
            print(f"\nABORT: more than {MAX_ROWS_PER_CORRECTION} rows would change for "
                  f"one correction ({'; '.join(over)}). Nothing written.", file=sys.stderr)
            return 3
        if not args.apply:
            _quiet_rollback(conn)
            print("\nDRY RUN: nothing written. Re-run with --apply to write.")
            return 0
        if not stmts:
            _quiet_rollback(conn)
            print("\nNothing to write: every correction is already applied.")
            return 0

        path = args.rollback_out or os.path.join(
            os.getcwd(), "repair_step1_facility_records_rollback_"
            + moment.strftime("%Y%m%dT%H%M%SZ") + ".json")
        payload = rollback_payload(plans, schema, to_delete, carrier_ids, stamp,
                                   args.include_carrier_links)
        try:
            write_rollback_file(path, payload)
        except OSError as exc:
            raise Refused(2, f"could not create the rollback file {path} "
                             f"({type(exc).__name__}); nothing written") from exc
        print(f"\nrollback file written BEFORE any write: {path}")
        _execute_writes(conn, cur, stmts,
                        f"{path} describes a change that did not happen.")
        print("committed.")
    except BaseException:
        _quiet_rollback(conn)
        raise
    finally:
        _quiet_close(conn)

    expected = [(ch.table, ch.id, ch.set) for plan in plans for ch in plan.changes]
    mismatches = verify(connect, dsn, expected, schema,
                        carrier_absent=[r["id"] for r in to_delete])
    code = report_verification(mismatches, f"{len(expected)} row(s) match the plan; "
                                           f"{len(to_delete)} carrier row(s) confirmed gone")
    print(f"undo: python3 scripts/repair_step1_facility_records.py --rollback {path}")
    return code


def run_rollback(connect, dsn, path):
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise Refused(2, f"cannot read rollback file {path} ({type(exc).__name__})") from exc
    if not isinstance(payload, dict) or payload.get("format") != FORMAT:
        raise Refused(2, f"{path} is not a {FORMAT} rollback file")
    changes = payload.get("changes") or []
    carrier_rows = payload.get("carrier_rows") or []
    if not changes and not carrier_rows:
        print("rollback file lists no changes; nothing to do")
        return 0
    conn = connect(dsn)
    try:
        cur = _begin(conn)
        schema = probe_schema(cur)
        # Every statement is BUILT before the first write, so a tampered file
        # (a frozen column, an unknown table) is refused with nothing written.
        stmts = []
        for change in changes:
            table = change.get("table")
            types = schema.get(table)
            if types is None:
                raise Refused(2, f"table {table!r} named in the rollback file was not found")
            prior, applied = change.get("prior") or {}, change.get("set") or {}
            unknown = sorted(c for c in prior if c not in types)
            if unknown:
                raise Refused(2, f"{table} has no column(s) {unknown}")
            sql, params = build_update(table, change.get("id"), prior, applied, types)
            stmts.append((sql, params, 1))
        if carrier_rows:
            if CARRIER not in schema:
                raise Refused(2, f"table {CARRIER} not found; cannot re-insert carrier rows")
            unknown = sorted({c for row in carrier_rows for c in row
                              if c not in schema[CARRIER]})
            if unknown:
                raise Refused(2, f"{CARRIER} has no column(s) {unknown}")
            inserts = [build_insert(CARRIER, row) for row in carrier_rows]
            cur.execute(f"SELECT id FROM {CARRIER} WHERE id = ANY(%s)",
                        [[row["id"] for row in carrier_rows]])
            clash = [row["id"] for row in _rows(cur)]
            if clash:
                raise Refused(2, f"{CARRIER} already holds id(s) {clash[:10]}; "
                                 "refusing to re-insert over them")
            stmts.extend((sql, params, 1) for sql, params in inserts)
        print_sql(stmts, True)
        _execute_writes(conn, cur, stmts,
                        "Nothing was restored; a row may have changed since --apply.")
        print("rollback committed.")
    except BaseException:
        _quiet_rollback(conn)
        raise
    finally:
        _quiet_close(conn)

    expected = [(c["table"], c["id"], c["prior"]) for c in changes]
    mismatches = verify(connect, dsn, expected, schema, carrier_present=carrier_rows)
    return report_verification(mismatches, f"{len(expected)} row(s) hold their prior "
                                           f"values; {len(carrier_rows)} carrier row(s) "
                                           "re-inserted")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Correct the Anthropic New York AI Campus and Compass Goodyear "
                    "Campus records. Dry run by default.")
    parser.add_argument("--apply", action="store_true",
                        help="write (the default is a dry run)")
    parser.add_argument("--include-carrier-links", action="store_true",
                        help="also export and DELETE carrier_facility_presence rows "
                             "for the corrected Anthropic ids")
    parser.add_argument("--rollback-out", metavar="FILE",
                        help="rollback file path (default: timestamped, in the "
                             "working directory)")
    parser.add_argument("--rollback", metavar="FILE",
                        help="undo a previous --apply from its rollback file")
    args = parser.parse_args(argv)
    if args.rollback and (args.apply or args.include_carrier_links or args.rollback_out):
        parser.error("--rollback cannot be combined with --apply, "
                     "--include-carrier-links or --rollback-out")
    return args


def _utc_now():
    return datetime.now(timezone.utc)


def main(argv=None, *, connect=None, environ=None, clock=None):
    args = parse_args(argv)
    environ = os.environ if environ is None else environ
    connect = connect or _connect
    clock = clock or _utc_now
    dsn = (environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("DATABASE_URL is not set; it must point at the PRIMARY database",
              file=sys.stderr)
        return 2
    if args.apply or args.rollback:
        refusal = replica_refusal(environ)
        if refusal:
            print(refusal, file=sys.stderr)
            return 2
    try:
        if args.rollback:
            return run_rollback(connect, dsn, args.rollback)
        return run_repair(connect, dsn, args, clock)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return exc.code
    except FrozenColumnError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except WriteAborted as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — reported with its type, never swallowed
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
