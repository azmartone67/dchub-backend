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

Tables are created with the production column types (transmission_lines per
tests/test_transmission_readers_sql.py, which read them from /api/v1/admin/schema;
gas_pipelines.updated_at and substations.updated_at are `timestamp without
time zone` per information_schema, 2026-09-23). S3/S4 give gas_pipelines and
substations a missing / TEXT column on purpose: the missing-column and text
shapes the fix must report, on tables this file owns.

Set RADAR_SLA_SQL_DSN to run it. CI passes the db-parity service DSN and then
asserts this file did not skip. Owns and recreates only transmission_lines,
gas_pipelines and substations.
"""
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

_OWNED = ("transmission_lines", "gas_pipelines", "substations")

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
         "INSERT INTO gas_pipelines (updated_at) VALUES (NOW() - INTERVAL '900 hours')",
         "INSERT INTO substations (updated_at) VALUES (NOW())")
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
         "INSERT INTO substations (name) VALUES ('SUB ONE')")

    sub = _at(_scan(), "substations")

    assert [f["issue"] for f in sub] == ["sla_column_unmeasurable"], sub
    assert 'column "updated_at" does not exist' in sub[0]["detail"], sub[0]


def test_s4_a_text_column_is_reported_not_fresh(db):
    # facilities.first_seen / discovered_facilities.discovered_at shape: ISO
    # text that MAX() returns as a str.
    _ddl(db, _TX_DDL, _SUB_OK_DDL,
         "CREATE TABLE gas_pipelines (id SERIAL PRIMARY KEY, updated_at TEXT)",
         "INSERT INTO gas_pipelines (updated_at) VALUES ('2026-09-23T03:07:45')")

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
         "INSERT INTO gas_pipelines (updated_at) VALUES (NOW())")
    conn = _FailingAgeQuery("gas_pipelines")
    monkeypatch.setattr(radar, "_db", lambda: conn)

    gas = _at(_scan(), "gas_pipelines")

    assert conn.fired == 1, "the injected failure never ran — this test proves nothing"
    assert [f["issue"] for f in gas] == ["sla_column_unmeasurable"], gas
    assert "raised QueryCanceled" in gas[0]["detail"], gas[0]
