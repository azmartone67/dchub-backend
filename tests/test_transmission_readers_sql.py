"""The transmission readers moved off discovered_transmission_lines, run against a
real Postgres (2026-09-13).

site_planner.find_nearest_transmission swallows every SQL error: execute_query
logs it and returns None, and a None falls through to a live ArcGIS fallback. A
wrong column name in the repointed lookup would therefore fail nothing — the
composite score, the site report, analyze and compare would quietly lose their
transmission line. And the KMZ export's type=all shipped a statement Postgres
rejects, on every request; a fake cursor accepts any string. Only a database can
say either statement is right.

  L1  a substation's line comes back from transmission_lines, keyed the way the
      analyze route serves it, with volt_class None
  L2  a higher voltage matched on the OTHER endpoint wins, and a NULL voltage
      never does (Postgres sorts NULLs first under DESC)
  L3  a miss runs without an SQL error and falls through to the fallback
  K1  type=all runs against real tables and exports plants and pipelines only
  K2  the exact request that returned 500 in production now exports
  K3  the summary runs and publishes no transmission count
  C1  control: Postgres rejects the statement type=all used to send, with and
      without a market, so this file can catch the defect it exists for
  C2  control: a statement that cannot run comes back None AND reaches the
      error log the L tests read, so their no-SQL-error check can fail

Tables are created with the column lists and types production reported through
/api/v1/admin/schema on 2026-09-13 (transmission_lines, discovered_power_plants,
discovered_pipelines). substations carries only the three columns step 1 reads,
at their production types. discovered_transmission_lines is dropped and never
created, so any read of it raises — and every L test fails on a logged SQL error.

Set TRANSMISSION_READERS_SQL_DSN to run it. CI passes the db-parity service DSN
and then asserts this file did not skip. Owns and recreates only
transmission_lines, substations, discovered_power_plants and discovered_pipelines.
"""
import logging
import os
import sys
import xml.etree.ElementTree as ET

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("TRANSMISSION_READERS_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="TRANSMISSION_READERS_SQL_DSN not set — no Postgres to run against")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

KML = "{http://www.opengis.net/kml/2.2}"

DDL = (
    "DROP TABLE IF EXISTS discovered_transmission_lines",
    "DROP TABLE IF EXISTS transmission_lines",
    "DROP TABLE IF EXISTS substations",
    "DROP TABLE IF EXISTS discovered_power_plants",
    "DROP TABLE IF EXISTS discovered_pipelines",
    """CREATE TABLE transmission_lines (
           id SERIAL PRIMARY KEY, hifld_id VARCHAR(50), name VARCHAR(500),
           operator VARCHAR(500), voltage_kv DOUBLE PRECISION,
           from_sub VARCHAR(500), to_sub VARCHAR(500),
           length_miles DOUBLE PRECISION, state VARCHAR(10), status VARCHAR(50),
           line_type VARCHAR(100), source VARCHAR(50),
           last_updated TIMESTAMP DEFAULT NOW(), created_at TIMESTAMP DEFAULT NOW())""",
    "CREATE TABLE substations (name TEXT, lat REAL, lng REAL)",
    """CREATE TABLE discovered_power_plants (
           id TEXT, name TEXT, fuel_type TEXT, capacity_mw REAL,
           generation_mwh REAL, operator TEXT, state TEXT, sector TEXT,
           market TEXT, discovered_at TEXT, last_updated TEXT, source TEXT,
           is_new INTEGER, lat REAL, lng REAL)""",
    """CREATE TABLE discovered_pipelines (
           id TEXT, operator TEXT, pipeline_type TEXT, status TEXT,
           diameter_inches REAL, commodity TEXT, name TEXT, capacity_mdth REAL,
           lat REAL, lng REAL, states_served TEXT, state TEXT, market TEXT,
           discovered_at TEXT, last_updated TEXT, source TEXT, is_new BOOLEAN)""",
)

# Real rows from the EIA source (US_Electric_Power_Transmission_Lines), 2026-09-13:
# (hifld_id, operator, voltage_kv, from_sub, to_sub, status). ASHBURNHAM is the
# Massachusetts substation the ASHBURN% prefix also matches.
ASHBURN_LINES = [
    ("115875", "VIRGINIA ELECTRIC & POWER CO", 230.0, "PLEASANT VIEW", "ASHBURN", "IN SERVICE"),
    ("116346", "FITCHBURG GAS AND ELECTRIC LIGHT COMPANY", 115.0, "TAP139720", "ASHBURNHAM", "IN SERVICE"),
    ("119843", "VIRGINIA ELECTRIC & POWER CO", 230.0, "ASHBURN", "BEAUMEADE", "IN SERVICE"),
]
ASHBURN = ("ASHBURN", 39.0438, -77.4874)
SITE = (39.0440, -77.4870)

# The keys /api/v1/site-planner/analyze serves as `transmission`.
SERVED_KEYS = {"line_name", "voltage_kv", "owner", "status", "volt_class",
               "distance_miles", "matched_substation"}


@pytest.fixture
def db(monkeypatch):
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    for stmt in DDL:
        cur.execute(stmt)
    # site_planner opens its own connection from the environment.
    monkeypatch.setenv("NEON_DATABASE_URL", DSN)
    yield cur
    conn.close()


class _ErrorLog(logging.Handler):
    """What site_planner logs at ERROR — where execute_query puts the SQL errors
    it swallows. Attached to the logger itself: caplog.records read after a
    fixture's yield holds only the TEARDOWN stage, and a check written that way
    never saw an error raised during the test (measured: it let a statement with
    a column that does not exist pass L3)."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def error_log():
    handler = _ErrorLog()
    logger = logging.getLogger("site_planner")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


@pytest.fixture
def no_sql_errors(error_log):
    """execute_query swallows SQL errors into a log line; make that line a failure."""
    yield
    assert not error_log.messages, f"site_planner swallowed an SQL error: {error_log.messages}"


def _seed_lines(cur, lines, substations=(ASHBURN,)):
    cur.executemany("INSERT INTO substations (name, lat, lng) VALUES (%s, %s, %s)",
                    list(substations))
    cur.executemany(
        "INSERT INTO transmission_lines (hifld_id, name, operator, voltage_kv, "
        "from_sub, to_sub, status, line_type, source) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'AC; OVERHEAD', 'eia-arcgis-runner')",
        [(h, op, op, kv, f, t, st) for h, op, kv, f, t, st in lines])


def _no_fallback(lat, lng):
    pytest.fail("a lookup that matched a line reached the live ArcGIS fallback")


def test_l1_the_lookup_reads_the_maintained_table(db, no_sql_errors, monkeypatch):
    import site_planner as sp
    monkeypatch.setattr(sp, "_query_hifld_transmission_live", _no_fallback)
    _seed_lines(db, ASHBURN_LINES)

    tx = sp.find_nearest_transmission(*SITE)

    assert tx is not None
    assert set(tx) == SERVED_KEYS
    assert tx["voltage_kv"] == 230.0
    assert tx["owner"] == "VIRGINIA ELECTRIC & POWER CO"
    assert tx["status"] == "IN SERVICE"
    assert tx["line_name"] in ("PLEASANT VIEW", "ASHBURN")
    assert tx["volt_class"] is None
    assert tx["matched_substation"] == "ASHBURN"
    assert isinstance(tx["distance_miles"], float) and tx["distance_miles"] < 1.0


def test_l2_the_other_endpoint_and_voltage_order_are_honoured(db, no_sql_errors, monkeypatch):
    import site_planner as sp
    monkeypatch.setattr(sp, "_query_hifld_transmission_live", _no_fallback)
    _seed_lines(db, ASHBURN_LINES + [
        ("900001", "NO VOLTAGE ON RECORD", None, "ASHBURN", "NOWHERE", "IN SERVICE"),
        ("900002", "A 500 KV OPERATOR", 500.0, "LOUDOUN", "ASHBURN", "IN SERVICE"),
    ])

    tx = sp.find_nearest_transmission(*SITE)

    assert tx["voltage_kv"] == 500.0
    assert tx["owner"] == "A 500 KV OPERATOR"
    assert tx["line_name"] == "LOUDOUN", "line_name is the matched line's from_sub"


def test_l3_a_miss_runs_cleanly_and_falls_through(db, no_sql_errors, monkeypatch):
    import site_planner as sp
    sentinel = {"from": "fallback"}
    monkeypatch.setattr(sp, "_query_hifld_transmission_live", lambda lat, lng: sentinel)
    _seed_lines(db, ASHBURN_LINES, substations=(("OSM-917634654", 39.0438, -77.4874),))

    assert sp.find_nearest_transmission(*SITE) is sentinel


def test_c2_control_a_swallowed_sql_error_reaches_the_error_log(db, error_log):
    """Anti-vacuity for no_sql_errors: a statement that cannot run comes back as
    None AND lands in the log that fixture reads."""
    import site_planner as sp
    assert sp.execute_query("SELECT owner FROM transmission_lines") is None
    assert any('column "owner" does not exist' in m for m in error_log.messages), \
        error_log.messages


def _real_get_db():
    return psycopg2.connect(DSN)


def _seed_export(cur):
    cur.executemany(
        "INSERT INTO discovered_power_plants (id, name, fuel_type, capacity_mw, "
        "operator, state, market, source, lat, lng) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        [("p1", "Comanche Peak", "Nuclear", 2430.0, "Luminant", "TX", "dallas", "eia", 32.298, -97.785),
         ("p2", "Martin Lake", "Coal", 2250.0, "Luminant", "TX", "dallas", "eia", 32.259, -94.570),
         ("p3", "No Coordinates", "Solar", 10.0, "Unknown & Co", "TX", "dallas", "eia", None, None),
         ("p4", "Byron", "Nuclear", 2347.0, "Constellation", "IL", "chicago", "eia", 42.074, -89.282)])
    cur.executemany(
        "INSERT INTO discovered_pipelines (id, operator, name, capacity_mdth, "
        "diameter_inches, lat, lng, states_served, state, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        [("g1", "Kinder Morgan", "NGPL", 2000.0, 36.0, 32.7, -96.8, "TX,OK", "TX", "Active"),
         ("g2", "Nobody", "No Coordinates", 5.0, 8.0, None, None, "TX", "TX", "Active")])


@pytest.mark.parametrize("market, placemarks", [("dallas", 3), (None, 4)])
def test_k1_type_all_exports_plants_and_pipelines_only(db, monkeypatch, market, placemarks):
    import energy_kmz_export as ek
    monkeypatch.setattr(ek, "get_db", _real_get_db)
    _seed_export(db)

    kml, _total = ek.generate_all_kml(market)

    marks = ET.fromstring(kml.encode("utf-8")).findall(f".//{KML}Placemark")
    assert len(marks) == placemarks
    assert all(m.find(f"{KML}Point") is not None for m in marks), \
        "every exported placemark must carry a position"
    assert "Transmission Lines (" not in kml and "tx-style" not in kml
    assert "Transmission lines are not included" in kml


def _export_client(monkeypatch):
    import flask
    import energy_kmz_export as ek
    monkeypatch.setattr(ek, "get_db", _real_get_db)
    app = flask.Flask(__name__)
    ek.register_kmz_export_routes(app)
    return app.test_client()


def test_k2_the_request_that_returned_500_in_production_now_exports(db, monkeypatch):
    client = _export_client(monkeypatch)
    _seed_export(db)

    r = client.get("/api/energy-discovery/export/kmz?type=all&format=kml&market=dallas")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.mimetype == "application/vnd.google-earth.kml+xml"

    # What the Land & Power "Export KMZ" button opens: type=all, KMZ by default.
    r = client.get("/api/energy-discovery/export/kmz?type=all")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.mimetype == "application/vnd.google-earth.kmz"


def test_k3_the_summary_runs_and_publishes_no_transmission_count(db, monkeypatch):
    client = _export_client(monkeypatch)
    _seed_export(db)

    body = client.get("/api/energy-discovery/export/summary").get_json()

    assert body["success"] is True, body
    assert body["data"]["power_plants"] == 3
    assert body["data"]["pipelines"] == 2
    assert "transmission_lines" not in body["data"]
    assert "transmission" not in body["endpoints"]
    assert body["not_exported"]["transmission_lines"]


# The transmission statement type=all built before 2026-09-13, verbatim.
_OLD_TYPE_ALL = ("SELECT * FROM discovered_transmission_lines WHERE 1=1 "
                 "ORDER BY voltage_kv DESC LIMIT 1000")


@pytest.mark.parametrize("market", [None, "dallas"])
def test_c1_control_postgres_rejects_the_statement_type_all_sent(db, market):
    query, params = _OLD_TYPE_ALL, []
    if market:
        query += " AND market = %s"
        params.append(market)
    query += " ORDER BY voltage_kv DESC LIMIT 500"
    with pytest.raises(psycopg2.errors.SyntaxError):
        db.execute(query, params)
