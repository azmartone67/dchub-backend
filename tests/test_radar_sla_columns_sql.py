"""check_data_freshness_sla_breach against a real Postgres (2026-09-23).

The radar's table-age SLA row for transmission_lines read `updated_at`, a column
the table does not have (production information_schema, 2026-09-23: id,
hifld_id, name, operator, voltage_kv, from_sub, to_sub, length_miles, state,
status, line_type, source, last_updated, created_at). MAX(updated_at) raised
UndefinedColumn, the row's `except Exception: continue` swallowed it, and the
row could never breach. The only visible trace was the query-tracking cursor
marking every run of the detector "degraded" in brain_detector_runs — 337
degraded and 0 completed runs from 2026-09-13 07:25 to 2026-09-23 06:01.
Two more rows were blind without even that: facilities.first_seen and
discovered_facilities.discovered_at are TEXT, MAX() hands back a str, and
`.tzinfo` raised in Python, where nothing tracks it.

The row now reads last_updated — the column routes/transmission_ingest.py, the
table's one writer since #5314, stamps NOW() on every row of its weekly EIA
full-replace — and a row the radar cannot measure is REPORTED as
sla_column_unmeasurable instead of passing as fresh.

  S1  rows written by the shipped EIA ingest route read as fresh: no finding
      at table:transmission_lines, while a stale sibling table in the SAME run
      breaches (so the silence is a measurement, not a dead detector)
  S2  the same rows aged past 720h breach — and the breach the radar produces,
      fed to the shipped autopilot action, escalates (None, None) rather than
      firing a refresh endpoint
  S3  an SLA column that does not exist is reported as unmeasurable, never as
      fresh and never as a breach
  S4  an SLA column that is TEXT is reported as unmeasurable, never as fresh
  S5  an age query that raises is reported as unmeasurable, never as fresh
      (the injected failure is counted, so the test cannot pass without it)

Second change, same day: the two TEXT rows are measured, and a missing table is
reported. facilities.first_seen stays TEXT and is cast, from an allowlist
(_SLA_ISO_TEXT_COLUMNS) — S4 still holds for any other TEXT column.
discovered_facilities is read from first_seen, a timestamptz DEFAULT now().
Neither table has an autonomous refresh any more, so a breach escalates. The
dcpi_scores row is gone: that table has never existed in production, and the
radar skipped a missing table without a word.

  S6  facilities.first_seen in the production mix of ISO shapes reads as fresh
      — no finding at all, where it used to be unmeasurable
  S7  aged, with a naive newest value, it breaches at the right age even when
      the radar's session TimeZone is UTC+14: a naive string is UTC, never
      session-local. The breach escalates.
  S8  aged, with an offset newest value (-10:00), it breaches at the age the
      offset says, not at its wall-clock digits
  S9  a value that does not parse is reported unmeasurable, and the rest of
      the scan still measures (a stale sibling breaches in the same run)
  S10 discovered_facilities rows written by the competitor-gap crawler's
      writer (news_facility_extractor.insert_discovered_facility, which sets
      no first_seen — the column default does) read as fresh; aged 30h they
      breach at 30h, and the breach escalates
  S11 an SLA row naming a table that does not exist is reported as
      unmeasurable, not skipped

Tables are created with the production column types (transmission_lines per
tests/test_transmission_readers_sql.py, which read them from /api/v1/admin/schema;
gas_pipelines.updated_at and substations.updated_at are `timestamp without
time zone` per information_schema, 2026-09-23; facilities.first_seen TEXT and
the discovered_facilities columns the writer touches per information_schema,
2026-09-23). S3/S4 give gas_pipelines and substations a missing / TEXT column
on purpose: the missing-column and text shapes the fix must report, on tables
this file owns.

Set RADAR_SLA_SQL_DSN to run it. CI passes the db-parity service DSN and then
asserts this file did not skip. Owns and recreates only transmission_lines,
gas_pipelines, substations, facilities and discovered_facilities.
"""
import datetime as _dt
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

psycopg2 = pytest.importorskip("psycopg2")
pytest.importorskip("flask")

DSN = os.environ.get("RADAR_SLA_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="RADAR_SLA_SQL_DSN not set — no Postgres to run against")

_OWNED = ("transmission_lines", "gas_pipelines", "substations",
          "facilities", "discovered_facilities")

# transmission_lines exactly as production reports it (see module docstring).
_TX_DDL = """CREATE TABLE transmission_lines (
       id SERIAL PRIMARY KEY, hifld_id VARCHAR(50), name VARCHAR(500),
       operator VARCHAR(500), voltage_kv DOUBLE PRECISION,
       from_sub VARCHAR(500), to_sub VARCHAR(500),
       length_miles DOUBLE PRECISION, state VARCHAR(10), status VARCHAR(50),
       line_type VARCHAR(100), source VARCHAR(50),
       last_updated TIMESTAMP DEFAULT NOW(), created_at TIMESTAMP DEFAULT NOW())"""
_GAS_OK_DDL = "CREATE TABLE gas_pipelines (id SERIAL PRIMARY KEY, updated_at TIMESTAMP)"
_SUB_OK_DDL = "CREATE TABLE substations (id SERIAL PRIMARY KEY, updated_at TIMESTAMP)"
_GAS_STALE = "INSERT INTO gas_pipelines (updated_at) VALUES (NOW() ON CONFLICT DO NOTHING - INTERVAL '900 hours')"
_FAC_DDL = ("CREATE TABLE facilities (id TEXT PRIMARY KEY, name TEXT, source TEXT, "
            "first_seen TEXT, last_updated TEXT)")
# The discovered_facilities columns insert_discovered_facility and its dedup
# probe touch, with production's types, nullability and defaults.
_DISC_DDL = """CREATE TABLE discovered_facilities (
       id SERIAL PRIMARY KEY, source TEXT NOT NULL, name TEXT NOT NULL,
       provider TEXT, city TEXT, state TEXT, country TEXT,
       latitude REAL, longitude REAL, power_mw REAL, sqft TEXT, status TEXT,
       source_url TEXT, discovered_at TEXT NOT NULL,
       confidence_score REAL DEFAULT 0.5, notes TEXT,
       investment_usd BIGINT, acreage INTEGER, canonical_slug TEXT,
       first_seen TIMESTAMPTZ DEFAULT now(),
       last_updated TIMESTAMP DEFAULT now())"""

# UTC+14, no DST: a radar that read a naive string in its session TimeZone
# would be 14 hours off.
_FAR_TZ = "Pacific/Kiritimati"

# Rows in the runner's body shape: [hifld_id, name, operator, voltage_kv,
# from_sub, to_sub, status, line_type, length_miles, state]. The route refuses
# a full-replace whose rows mostly lack length_miles/state (#5319).
_EIA_ROWS = [
    ["100001", "LINE A", "ACME POWER", 345, "SUB ONE", "SUB TWO", "IN SERVICE", "AC; OVERHEAD", 12.4, "TX"],
    ["100002", "LINE B", "ACME POWER", 138, "SUB TWO", "SUB THREE", "IN SERVICE", "AC; OVERHEAD", 3.1, "TX"],
    ["100003", "LINE C", "OTHER CO", 500, "SUB FOUR", "SUB FIVE", "IN SERVICE", "AC; OVERHEAD", 48.0, "OK"],
]


def _connect():
    c = psycopg2.connect(DSN)
    c.autocommit = True
    return c


@pytest.fixture
def db(monkeypatch):
    """Recreate the owned tables empty and point the radar's _db() at them."""
    from routes import brain_consistency_radar as radar
    c = _connect()
    with c.cursor() as cur:
        for t in _OWNED:
            cur.execute(f"DROP TABLE IF EXISTS {t}")
    monkeypatch.setattr(radar, "_db", _connect)
    yield c
    c.close()


def _ddl(c, *stmts):
    with c.cursor() as cur:
        for s in stmts:
            cur.execute(s)


def _one(c, sql, params=None):
    with c.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _ingest(monkeypatch, rows):
    """POST rows through the shipped EIA ingest route, as the weekly runner does.
    Only the connection's sslmode is overridden: the throwaway Postgres has no
    TLS. The route's own SQL runs unchanged."""
    from flask import Flask
    from routes import transmission_ingest as ti
    real_connect = psycopg2.connect
    monkeypatch.setattr(ti.psycopg2, "connect",
                        lambda dsn, **kw: real_connect(DSN))
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "test-admin-key")
    app = Flask(__name__)
    ti.register_transmission_ingest(app)
    resp = app.test_client().post(
        "/api/v1/admin/ingest/transmission-lines",
        data=json.dumps({"rows": rows}),
        headers={"X-Admin-Key": "test-admin-key", "Content-Type": "application/json"})
    body = resp.get_json()
    assert resp.status_code == 200 and body.get("ok"), body
    assert body.get("inserted") == len(rows), body
    return body


def _scan():
    from routes import brain_consistency_radar as radar
    return radar.check_data_freshness_sla_breach()


def _at(findings, table):
    return [f for f in findings if f.get("url") == f"table:{table}"]


def test_s1_rows_the_eia_ingest_writes_read_as_fresh(db, monkeypatch):
    _ddl(db, _TX_DDL, _GAS_OK_DDL, _SUB_OK_DDL,
         "INSERT INTO gas_pipelines (updated_at) VALUES (NOW() ON CONFLICT DO NOTHING - INTERVAL '900 hours')",
         "INSERT INTO substations (updated_at) VALUES (NOW() ON CONFLICT DO NOTHING)")
    _ingest(monkeypatch, _EIA_ROWS)
    n, nulls = _one(db, "SELECT COUNT(*), COUNT(*) FILTER (WHERE last_updated IS NULL) "
                        "FROM transmission_lines")
    assert (n, nulls) == (len(_EIA_ROWS), 0), "the ingest did not stamp last_updated"

    findings = _scan()

    assert _at(findings, "transmission_lines") == [], _at(findings, "transmission_lines")
    # Control: the same run DID read the database — the stale sibling breaches.
    gas = _at(findings, "gas_pipelines")
    assert [f["issue"] for f in gas] == ["data_freshness_sla_breach"], findings


def test_s2_aged_rows_breach_and_the_breach_escalates(db, monkeypatch):
    from routes import brain_autopilot
    _ddl(db, _TX_DDL)
    _ingest(monkeypatch, _EIA_ROWS)
    _ddl(db, "UPDATE transmission_lines SET last_updated = NOW() - INTERVAL '800 hours'")

    tx = _at(_scan(), "transmission_lines")

    assert [f["issue"] for f in tx] == ["data_freshness_sla_breach"], tx
    assert 790 <= tx[0]["count"] <= 820, tx[0]
    # The premise for arming this row: a breach reaches a human, it does not
    # fire the retired TRUNCATE-and-reload refresh.
    assert brain_autopilot._action_data_freshness_breach(tx[0]) == (None, None)


def test_s3_a_missing_column_is_reported_not_fresh(db):
    _ddl(db, _TX_DDL, _GAS_OK_DDL,
         "CREATE TABLE substations (id SERIAL PRIMARY KEY, name TEXT)",
         "INSERT INTO substations (name) VALUES ('SUB ONE') ON CONFLICT DO NOTHING")

    sub = _at(_scan(), "substations")

    assert [f["issue"] for f in sub] == ["sla_column_unmeasurable"], sub
    assert 'column "updated_at" does not exist' in sub[0]["detail"], sub[0]


def test_s4_a_text_column_is_reported_not_fresh(db):
    # facilities.first_seen / discovered_facilities.discovered_at shape: ISO
    # text that MAX() returns as a str.
    _ddl(db, _TX_DDL, _SUB_OK_DDL,
         "CREATE TABLE gas_pipelines (id SERIAL PRIMARY KEY, updated_at TEXT)",
         "INSERT INTO gas_pipelines (updated_at) VALUES ('2026-09-23T03:07:45') ON CONFLICT DO NOTHING")

    gas = _at(_scan(), "gas_pipelines")

    assert [f["issue"] for f in gas] == ["sla_column_unmeasurable"], gas
    assert 'column "updated_at" is text, not a timestamp' in gas[0]["detail"], gas[0]


class _FailingAgeQuery:
    """A real connection whose age query for one table raises, as a statement
    timeout would. Every other statement runs against Postgres."""

    def __init__(self, table):
        self.table, self.fired, self._real = table, 0, _connect()

    def cursor(self):
        return _FailingCursor(self, self._real.cursor())

    def close(self):
        self._real.close()


class _FailingCursor:
    def __init__(self, owner, real):
        self._owner, self._real = owner, real

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.close()

    def execute(self, sql, params=None):
        if sql.startswith("SELECT MAX(") and sql.endswith(f"FROM {self._owner.table}"):
            self._owner.fired += 1
            raise psycopg2.errors.QueryCanceled(
                "canceling statement due to statement timeout")
        return self._real.execute(sql, params)

    def fetchone(self):
        return self._real.fetchone()


def test_s5_an_age_query_that_raises_is_reported_not_fresh(db, monkeypatch):
    from routes import brain_consistency_radar as radar
    _ddl(db, _TX_DDL, _SUB_OK_DDL, _GAS_OK_DDL,
         "INSERT INTO gas_pipelines (updated_at) VALUES (NOW() ON CONFLICT DO NOTHING)")
    conn = _FailingAgeQuery("gas_pipelines")
    monkeypatch.setattr(radar, "_db", lambda: conn)

    gas = _at(_scan(), "gas_pipelines")

    assert conn.fired == 1, "the injected failure never ran — this test proves nothing"
    assert [f["issue"] for f in gas] == ["sla_column_unmeasurable"], gas
    assert "raised QueryCanceled" in gas[0]["detail"], gas[0]


def _utc(hours_ago):
    return _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours_ago)


def _radar_session_tz(monkeypatch, tz):
    """Point the radar's _db() at connections whose session TimeZone is tz."""
    from routes import brain_consistency_radar as radar

    def _conn():
        c = psycopg2.connect(DSN, options=f"-c TimeZone={tz}")
        c.autocommit = True
        return c

    probe = _conn()
    try:
        assert _one(probe, "SHOW TimeZone") == (tz,), "the session TimeZone did not take"
    finally:
        probe.close()
    monkeypatch.setattr(radar, "_db", _conn)


def _facilities(c, *first_seen):
    with c.cursor() as cur:
        for i, v in enumerate(first_seen):
            cur.execute("INSERT INTO facilities (id, name, source, first_seen) "
                        "VALUES (%s, %s, 'dchub_pipeline', %s)", (f"f{i}", f"Facility {i}", v))


def test_s6_facilities_iso_text_in_production_shapes_reads_fresh(db, monkeypatch):
    # Every shape facilities.first_seen holds in production (2026-09-23):
    # date-only (1,404 rows), naive with microseconds (14,166), naive to the
    # second (15), Z (28), +00 with a fraction (72), and NULL.
    _ddl(db, _FAC_DDL, _GAS_OK_DDL, _GAS_STALE)
    _facilities(db, _utc(0).strftime("%Y-%m-%d"),
                _utc(2).strftime("%Y-%m-%dT%H:%M:%S.%f"),
                _utc(50).strftime("%Y-%m-%d %H:%M:%S"),
                _utc(60).strftime("%Y-%m-%dT%H:%M:%SZ"),
                _utc(70).strftime("%Y-%m-%d %H:%M:%S.%f") + "+00",
                None, "")
    _radar_session_tz(monkeypatch, _FAR_TZ)

    findings = _scan()

    assert _at(findings, "facilities") == [], _at(findings, "facilities")
    gas = _at(findings, "gas_pipelines")
    assert [f["issue"] for f in gas] == ["data_freshness_sla_breach"], findings


def test_s7_aged_naive_newest_breaches_at_its_utc_age(db, monkeypatch):
    from routes import brain_autopilot
    _ddl(db, _FAC_DDL)
    _facilities(db, _utc(400).strftime("%Y-%m-%dT%H:%M:%S.%f"),
                _utc(430).strftime("%Y-%m-%d %H:%M:%S.%f") + "+00",
                _utc(450).strftime("%Y-%m-%dT%H:%M:%SZ"),
                _utc(500).strftime("%Y-%m-%d"))
    _radar_session_tz(monkeypatch, _FAR_TZ)

    fac = _at(_scan(), "facilities")

    assert [f["issue"] for f in fac] == ["data_freshness_sla_breach"], fac
    assert 398 <= fac[0]["count"] <= 402, fac[0]
    # The owner's call (2026-09-23): no autonomous osm-crawl for this table.
    assert brain_autopilot._action_data_freshness_breach(fac[0]) == (None, None)


def test_s8_aged_offset_newest_breaches_at_the_offsets_age(db, monkeypatch):
    _ddl(db, _FAC_DDL)
    at_minus_10 = _utc(400).astimezone(_dt.timezone(_dt.timedelta(hours=-10)))
    _facilities(db, at_minus_10.strftime("%Y-%m-%dT%H:%M:%S") + "-10:00",
                _utc(450).strftime("%Y-%m-%dT%H:%M:%S.%f"))
    _radar_session_tz(monkeypatch, _FAR_TZ)

    fac = _at(_scan(), "facilities")

    assert [f["issue"] for f in fac] == ["data_freshness_sla_breach"], fac
    assert 398 <= fac[0]["count"] <= 402, fac[0]


def test_s9_an_unparseable_value_is_reported_and_the_scan_goes_on(db):
    _ddl(db, _FAC_DDL, _GAS_OK_DDL, _GAS_STALE)
    _facilities(db, _utc(1).strftime("%Y-%m-%dT%H:%M:%S.%f"), "not a date")

    findings = _scan()

    fac = _at(findings, "facilities")
    assert [f["issue"] for f in fac] == ["sla_column_unmeasurable"], fac
    assert "raised InvalidDatetimeFormat" in fac[0]["detail"], fac[0]
    # gas_pipelines comes after facilities in SLAS: still measured.
    gas = _at(findings, "gas_pipelines")
    assert [f["issue"] for f in gas] == ["data_freshness_sla_breach"], findings


def _stage_competitor_gap(conn, *names):
    """Stage leads through the competitor-gap crawler's own shaping and its
    writer — the path behind 3,977 of the 4,197 discovered_facilities rows
    added in the 30 days to 2026-09-23."""
    from news_facility_extractor import insert_discovered_facility
    from routes.competitor_gap_crawler import _to_discovered_facility
    ids = []
    for n in names:
        cand = {"name": n, "operator": "Acme Data", "city": "Phoenix", "state": "AZ",
                "country": "US", "address": "1 Main St",
                "source_url": "https://www.cloudscene.com/data-center/"
                              + n.lower().replace(" ", "-")}
        ids.append(insert_discovered_facility(conn, _to_discovered_facility(cand, "cloudscene")))
    return ids


def test_s10_discovery_rows_the_competitor_gap_writer_stages(db):
    from routes import brain_autopilot
    _ddl(db, _DISC_DDL)
    writer = psycopg2.connect(DSN)
    try:
        ids = _stage_competitor_gap(writer, "Acme Phoenix DC1", "Acme Phoenix DC2")
    finally:
        writer.close()
    assert all(isinstance(i, int) for i in ids), ids
    n, stamped = _one(db, "SELECT COUNT(*), COUNT(first_seen) FROM discovered_facilities")
    assert (n, stamped) == (2, 2), "the writer's rows carry no first_seen"

    assert _at(_scan(), "discovered_facilities") == []

    _ddl(db, "UPDATE discovered_facilities SET first_seen = NOW() - INTERVAL '30 hours'")
    disc = _at(_scan(), "discovered_facilities")

    assert [f["issue"] for f in disc] == ["data_freshness_sla_breach"], disc
    assert 29 <= disc[0]["count"] <= 31, disc[0]
    assert brain_autopilot._action_data_freshness_breach(disc[0]) == (None, None)


def test_s11_a_missing_table_is_reported_not_skipped(db):
    _ddl(db, _TX_DDL, _GAS_OK_DDL)  # no substations table at all

    sub = _at(_scan(), "substations")

    assert [f["issue"] for f in sub] == ["sla_column_unmeasurable"], sub
    assert 'table "substations" does not exist' in sub[0]["detail"], sub[0]
