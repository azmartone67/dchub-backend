"""The transmission readers moved off discovered_transmission_lines, run against a
real Postgres (2026-09-13).

site_planner.find_nearest_transmission swallows every SQL error: execute_query
logs it and returns None, and a None reads as a miss. A wrong column name in the
repointed lookup would therefore fail nothing — the composite score, the site
report, analyze and compare would quietly lose their transmission line. And the
KMZ export's type=all shipped a statement Postgres rejects, on every request; a
fake cursor accepts any string. Only a database can say either statement is
right.

★ 2026-09-13, step 2 anchored. The lookup used to serve the highest-voltage line
NATIONWIDE whose endpoint started with the nearest substation's first word. It now
takes the substations near the site, matches their names EXACTLY to line
endpoints, and serves a line only where its endpoint names place it at the
substation it is attributed to. L4-L12 pin one rule each, on production rows;
each fails on the prefix lookup or on the mutation that deletes its rule.

★ 2026-09-13, measured. execute_query returns None for a statement that did not
run, and the lookup read that as a miss at each of its three statements, so a
query error was served as "no transmission line near the site".
find_nearest_transmission_measured returns (line, measured);
find_nearest_transmission still returns None for both, as L3, L10, L11 and L12
pin. Those four and L1 also read the measured variant, and L13 fails each
statement in turn.

  L1  a substation's line comes back from transmission_lines, keyed the way the
      analyze route serves it, with volt_class None; ties break on the lowest id;
      the measured variant serves the same line, measured
  L2  a higher voltage matched on the OTHER endpoint wins, and a NULL voltage
      never does (Postgres sorts NULLs first under DESC)
  L3  a miss (a name no endpoint carries, or no substation in range) runs
      without an SQL error, returns None, and opens no network connection; the
      measured variant reports the miss as measured
  L4  a first-word coincidence elsewhere is not served (WEST% -> WEST VERNON, AL)
  L5  a far end found only far away rules the line out (WEST -> BARCOLA, FL)
  L6  when BOTH names also occur far away, finding the pair nearby is not enough
  L7  the nearest ANCHORING substation wins — over a nearer substation no line
      names, and over a farther, higher voltage — and its distance is served
  L8  the EIA 'not available' voltage (-999999) is never served
  L9  a substation stored in mixed case matches its uppercase endpoint name
  L10 a case-folded match with nothing to place the far end is not served, and
      the miss is measured
  L11 an anchor name that also occurs far away, with nothing to place the far
      end, is not served, and the miss is measured
  L12 when the statement that places the endpoint names fails, no unverified
      line is served, and the answer is not measured
  L13 when any one of the lookup's three statements cannot run, nothing is served
      and the answer is not measured; each case fails its statement exactly once
  K1  type=all runs against real tables and exports plants and pipelines only
  K2  the exact request that returned 500 in production now exports
  K3  the summary runs and publishes no transmission count
  C1  control: Postgres rejects the statement type=all used to send, with and
      without a market, so this file can catch the defect it exists for
  C2  control: a statement that cannot run comes back None AND reaches the
      error log the L tests read, so their no-SQL-error check can fail
  C3  control: a connection attempt whose error the caller swallows still lands
      in the record the L tests read, so their no-network check can fail
  C4  control: the prefix statement step 2 ran before, on L4's rows, serves the
      Alabama line — the defect L4 guards is present in its fixture

★ 2026-09-13 — a miss used to fall through to _query_hifld_transmission_live, a
live ArcGIS query that ArcGIS rejected on every call (400: an outFields name the
layer does not have), so it returned None after a network round trip. It was
removed rather than repointed; the end of find_nearest_transmission_measured says
why.
Every L test fails if the lookup reaches the network again, whatever it returns.

Tables are created with the column lists and types production reported through
/api/v1/admin/schema on 2026-09-13 (transmission_lines, discovered_power_plants,
discovered_pipelines). substations carries only the three columns the lookup
reads, at their production types. discovered_transmission_lines is dropped and
never created, so any read of it raises — and every L test fails on a logged SQL
error.

Set TRANSMISSION_READERS_SQL_DSN to run it. CI passes the db-parity service DSN
and then asserts this file did not skip. Owns and recreates only
transmission_lines, substations, discovered_power_plants and discovered_pipelines.
"""
import logging
import os
import socket
import sys
import xml.etree.ElementTree as ET

import pytest

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("TRANSMISSION_READERS_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="TRANSMISSION_READERS_SQL_DSN not set — no Postgres to run against")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests._prod_shaped_db import reset_tables  # noqa: E402

KML = "{http://www.opengis.net/kml/2.2}"

# The tables this file OWNS. On an empty postgres `reset_tables` reports them
# absent and the DDL below creates them; on a prod-shaped branch it TRUNCATEs
# production's own tables and the `IF NOT EXISTS` creates become no-ops, so the
# readers run against the REAL column list. See tests/_prod_shaped_db.py --
# a DROP there raises `DependentObjectsStillExist`, and CASCADE would delete
# production's views out of a branch 19 lanes share.
#
# `discovered_transmission_lines` is NOT in this set on purpose: it is dropped
# and never recreated, so any reader still touching it raises. That drop is an
# assertion, and truncating it would hand such a reader an empty table instead.
MANAGED = ("transmission_lines", "substations", "discovered_power_plants",
           "discovered_pipelines")

DDL = (
    "DROP TABLE IF EXISTS discovered_transmission_lines",
    """CREATE TABLE IF NOT EXISTS transmission_lines (
           id SERIAL PRIMARY KEY, hifld_id VARCHAR(50), name VARCHAR(500),
           operator VARCHAR(500), voltage_kv DOUBLE PRECISION,
           from_sub VARCHAR(500), to_sub VARCHAR(500),
           length_miles DOUBLE PRECISION, state VARCHAR(10), status VARCHAR(50),
           line_type VARCHAR(100), source VARCHAR(50),
           last_updated TIMESTAMP DEFAULT NOW(), created_at TIMESTAMP DEFAULT NOW())""",
    "CREATE TABLE IF NOT EXISTS substations (name TEXT, lat REAL, lng REAL)",
    """CREATE TABLE IF NOT EXISTS discovered_power_plants (
           id TEXT, name TEXT, fuel_type TEXT, capacity_mw REAL,
           generation_mwh REAL, operator TEXT, state TEXT, sector TEXT,
           market TEXT, discovered_at TEXT, last_updated TEXT, source TEXT,
           is_new INTEGER, lat REAL, lng REAL)""",
    """CREATE TABLE IF NOT EXISTS discovered_pipelines (
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

# Production rows for L4-L12 (transmission_lines and substations, read-only dump
# 2026-09-13): substations as (name, lat, lng), lines as above.
WEST_AR = ("WEST", 33.66443, -93.61403)
WEST_CO = ("WEST", 40.40444, -105.11756)
TAP150157 = ("TAP150157", 33.65862, -93.63464)
WEST_VERNON = ("WEST VERNON", 33.65164, -88.257835)  # Alabama
BARCOLA = ("BARCOLA", 27.823994, -81.887856)  # Florida
LINE_WEST_TAP150157 = ("119159", "ENTERGY ARKANSAS INC", 115.0, "WEST", "TAP150157", "IN SERVICE")
LINE_WEST_VERNON = ("100855", "ALABAMA POWER CO", 500.0, "UNKNOWN109244", "WEST VERNON", "IN SERVICE")
LINE_WEST_BARCOLA = ("107410", "NOT AVAILABLE", 230.0, "WEST", "BARCOLA", "IN SERVICE")
LINE_WEST_HORSESHOE = ("205039", "PUBLIC SERVICE CO OF COLORADO", 115.0, "WEST", "HORSESHOE",
                       "NOT AVAILABLE")

ALLEN_NV = ("ALLEN", 36.275608, -115.18578)
ALLEN_IN = ("ALLEN", 41.01433, -84.97763)
LINCOLN_NV = ("LINCOLN", 36.217197, -115.08377)
LINCOLN_IN = ("LINCOLN", 41.070286, -85.06264)
WASHBURN_NV = ("WASHBURN", 36.25287, -115.190414)
WASHBURN_WI = ("WASHBURN", 46.669804, -90.89717)
IRON_MOUNTAIN_NV = ("IRON MOUNTAIN", 36.322582, -115.20829)
LINE_ALLEN_LINCOLN = ("114834", "INDIANA MICHIGAN POWER CO", 138.0, "ALLEN", "LINCOLN", "IN SERVICE")
LINE_WASHBURN_IRON_MOUNTAIN = ("301903", "SALT RIVER PROJECT", 69.0, "WASHBURN", "IRON MOUNTAIN",
                               "NOT AVAILABLE")

ASHBURN_POINT = (39.0438, -77.4874)
ASHBURN_500KV = ("Ashburn 500kV", 39.0438, -77.4874)  # no line endpoint carries this name
ASHBURN_VA = ("ASHBURN", 39.05139, -77.49837)
PLEASANT_VIEW_VA = ("PLEASANT VIEW", 39.07591, -77.528824)
GOOSE_CREEK = ("GOOSE CREEK", 39.07551, -77.53146)
BRAMBLETON = ("BRAMBLETON", 38.96356, -77.54904)
LINE_BRAMBLETON_GOOSE_CREEK = ("165881", "VIRGINIA ELECTRIC & POWER CO", 500.0, "BRAMBLETON",
                               "GOOSE CREEK", "IN SERVICE")

ABERDEEN_MS = ("ABERDEEN", 33.816994, -88.5389)
TAP144796 = ("TAP144796", 33.81673, -88.5373)
UNKNOWN109811 = ("UNKNOWN109811", 33.772106, -88.560196)
TAP144779 = ("TAP144779", 33.767437, -88.56205)
LINE_ABERDEEN_NOT_AVAILABLE = ("134250", "TENNESSEE VALLEY AUTHORITY", -999999.0, "TAP144796",
                               "ABERDEEN", "IN SERVICE")
LINE_UNKNOWN109811 = ("124894", "TENNESSEE VALLEY AUTHORITY", 161.0, "UNKNOWN109811", "TAP144779",
                      "IN SERVICE")

HEIDELBACH_MIXED_CASE = ("Heidelbach", 37.978012, -87.56066)
PIGEON_CREEK_TRANS = ("PIGEON CREEK TRANS", 37.988815, -87.58379)
LINE_HEIDELBACH = ("140725", "SOUTHERN INDIANA GAS & ELEC CO", 138.0, "HEIDELBACH",
                   "PIGEON CREEK TRANS", "IN SERVICE")

ADAMS_MA = ("Adams Substation", 42.654667, -73.1059)
LINE_MAGLEY_ADAMS = ("108004", "INDIANA MICHIGAN POWER CO", 138.0, "MAGLEY SUBSTATION",
                     "ADAMS SUBSTATION", "IN SERVICE")
LINE_ADAMS_PENNVILLE = ("121567", "INDIANA MICHIGAN POWER CO", 138.0, "ADAMS SUBSTATION",
                        "PENNVILLE SUBSTATION", "IN SERVICE")

# The keys /api/v1/site-planner/analyze serves as `transmission`.
SERVED_KEYS = {"line_name", "voltage_kv", "owner", "status", "volt_class",
               "distance_miles", "matched_substation"}


@pytest.fixture
def db(monkeypatch):
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    reset_tables(cur, *MANAGED)
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


def _record_connections(monkeypatch):
    """Refuse and record every outbound connection attempt for one test.

    Patched at the socket layer, so any HTTP client counts, not only requests.
    psycopg2 is unaffected: libpq resolves and connects in C."""
    attempts = []

    def resolve(host, *args, **kwargs):
        attempts.append(host)
        raise OSError(f"network disabled in this test: resolve {host!r}")

    def connect(sock, address, *args, **kwargs):
        attempts.append(address)
        raise OSError(f"network disabled in this test: connect {address!r}")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(socket.socket, "connect", connect)
    return attempts


@pytest.fixture
def no_network(monkeypatch):
    """The lookup must not reach the network. Reads the RECORD, not an exception:
    the removed fallback caught every exception and returned None, so refusing
    the connection alone would have let it pass."""
    attempts = _record_connections(monkeypatch)
    yield
    assert not attempts, f"the transmission lookup opened a network connection: {attempts}"


def _seed_lines(cur, lines, substations=(ASHBURN,)):
    cur.executemany("INSERT INTO substations (name, lat, lng) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    list(substations))
    cur.executemany(
        "INSERT INTO transmission_lines (hifld_id, name, operator, voltage_kv, "
        "from_sub, to_sub, status, line_type, source) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'AC; OVERHEAD', 'eia-arcgis-runner')",
        [(h, op, op, kv, f, t, st) for h, op, kv, f, t, st in lines])


def test_l1_the_lookup_reads_the_maintained_table(db, no_sql_errors, no_network):
    import site_planner as sp
    _seed_lines(db, ASHBURN_LINES)

    tx = sp.find_nearest_transmission(*SITE)

    assert tx is not None
    assert set(tx) == SERVED_KEYS
    assert tx["voltage_kv"] == 230.0
    assert tx["owner"] == "VIRGINIA ELECTRIC & POWER CO"
    assert tx["status"] == "IN SERVICE"
    # Both ASHBURN lines are 230 kV; the tie breaks on the lowest id (115875 is
    # inserted first), so the answer does not depend on the plan.
    assert tx["line_name"] == "PLEASANT VIEW"
    assert tx["volt_class"] is None
    assert tx["matched_substation"] == "ASHBURN"
    assert isinstance(tx["distance_miles"], float) and tx["distance_miles"] < 1.0
    assert sp.find_nearest_transmission_measured(*SITE) == (tx, True)


def test_l2_the_other_endpoint_and_voltage_order_are_honoured(db, no_sql_errors, no_network):
    import site_planner as sp
    _seed_lines(db, ASHBURN_LINES + [
        ("900001", "NO VOLTAGE ON RECORD", None, "ASHBURN", "NOWHERE", "IN SERVICE"),
        ("900002", "A 500 KV OPERATOR", 500.0, "LOUDOUN", "ASHBURN", "IN SERVICE"),
    ])

    tx = sp.find_nearest_transmission(*SITE)

    assert tx["voltage_kv"] == 500.0
    assert tx["owner"] == "A 500 KV OPERATOR"
    assert tx["line_name"] == "LOUDOUN", "line_name is the matched line's from_sub"


# The two ways to reach the end of the lookup: step 2 runs and matches nothing (an
# import placeholder name, the common case), or step 1 finds no substation in its
# box (this site is ~31 mi north of ASHBURN; the default box is ±15 mi).
_MISSES = {
    "placeholder-name": ((("OSM-917634654", 39.0438, -77.4874),), SITE),
    "no-substation-in-range": ((ASHBURN,), (39.5, -77.4874)),
}


@pytest.mark.parametrize("substations, site", list(_MISSES.values()), ids=list(_MISSES))
def test_l3_a_miss_runs_cleanly_and_returns_none(db, no_sql_errors, no_network,
                                                 substations, site):
    import site_planner as sp
    _seed_lines(db, ASHBURN_LINES, substations=substations)

    assert sp.find_nearest_transmission(*site) is None
    assert sp.find_nearest_transmission_measured(*site) == (None, True)


def test_l4_a_first_word_coincidence_elsewhere_is_not_served(db, no_sql_errors, no_network):
    """797 EIA lines start with WEST. The prefix lookup served Arkansas's WEST the
    500 kV Alabama Power line into WEST VERNON, ~330 mi away; WEST's own line is
    Entergy Arkansas's 115 kV line to TAP150157."""
    import site_planner as sp
    # A copy of TAP150157 with no coordinates stands for the auto_discovery rows
    # production holds without any; the distance arithmetic must never see one.
    _seed_lines(db, [LINE_WEST_TAP150157, LINE_WEST_VERNON],
                substations=(WEST_AR, TAP150157, ("TAP150157", None, None), WEST_VERNON))

    tx = sp.find_nearest_transmission(WEST_AR[1], WEST_AR[2])

    assert tx, "the lookup served no line"
    assert (tx["line_name"], tx["voltage_kv"], tx["owner"]) == ("WEST", 115.0, "ENTERGY ARKANSAS INC")
    assert (tx["matched_substation"], tx["distance_miles"]) == ("WEST", 0.0)


def test_l5_a_far_end_found_only_far_away_rules_the_line_out(db, no_sql_errors, no_network):
    """Nine substations are named WEST, in six states, so an exact name is not an
    identity. WEST -> BARCOLA (230 kV) is in Florida: BARCOLA stands ~800 mi from
    Arkansas's WEST. Florida's WEST is left out of the fixture, so the far end is
    the only thing that can rule the line out."""
    import site_planner as sp
    _seed_lines(db, [LINE_WEST_TAP150157, LINE_WEST_BARCOLA],
                substations=(WEST_AR, TAP150157, BARCOLA))

    tx = sp.find_nearest_transmission(WEST_AR[1], WEST_AR[2])

    assert tx, "the lookup served no line"
    assert (tx["voltage_kv"], tx["owner"]) == (115.0, "ENTERGY ARKANSAS INC")


def test_l6_a_nearby_pair_of_names_that_both_occur_elsewhere_is_not_enough(
        db, no_sql_errors, no_network):
    """Nevada has an ALLEN and a LINCOLN 7 mi apart, and Indiana has both too; the
    ALLEN -> LINCOLN line is Indiana Michigan Power's. WASHBURN also occurs far away
    (Wisconsin), but IRON MOUNTAIN occurs only here, so that line stays."""
    import site_planner as sp
    _seed_lines(db, [LINE_ALLEN_LINCOLN, LINE_WASHBURN_IRON_MOUNTAIN],
                substations=(ALLEN_NV, ALLEN_IN, LINCOLN_NV, LINCOLN_IN,
                             WASHBURN_NV, WASHBURN_WI, IRON_MOUNTAIN_NV))

    tx = sp.find_nearest_transmission(ALLEN_NV[1], ALLEN_NV[2])

    assert tx, "the lookup served no line"
    assert (tx["owner"], tx["voltage_kv"]) == ("SALT RIVER PROJECT", 69.0)
    assert (tx["matched_substation"], tx["distance_miles"]) == ("WASHBURN", 1.6)


def test_l7_the_nearest_anchoring_substation_wins_and_its_distance_is_served(
        db, no_sql_errors, no_network):
    """From the Ashburn point: 'Ashburn 500kV' (0.0 mi) is named by no line; ASHBURN
    (0.8 mi) anchors a 230 kV line; GOOSE CREEK (3.2 mi) anchors a 500 kV one. The
    prefix lookup served ASHBURN's line as 'Ashburn 500kV' at 0.0 mi. A substation
    with no name — production holds 53 — stands at the site and must not break it."""
    import site_planner as sp
    _seed_lines(db, [ASHBURN_LINES[0], LINE_BRAMBLETON_GOOSE_CREEK],
                substations=(ASHBURN_500KV, (None, 39.0439, -77.4875), ASHBURN_VA,
                             PLEASANT_VIEW_VA, GOOSE_CREEK, BRAMBLETON))

    tx = sp.find_nearest_transmission(*ASHBURN_POINT)

    assert tx, "the lookup served no line"
    assert (tx["line_name"], tx["voltage_kv"]) == ("PLEASANT VIEW", 230.0)
    assert (tx["matched_substation"], tx["distance_miles"]) == ("ASHBURN", 0.8)


def test_l8_the_eia_not_available_voltage_is_never_served(db, no_sql_errors, no_network):
    """-999999 is EIA's 'not available', on 15,127 production lines. ABERDEEN's
    only line here carries it; the next anchor, 3.3 mi out, has TVA's 161 kV line."""
    import site_planner as sp
    _seed_lines(db, [LINE_ABERDEEN_NOT_AVAILABLE, LINE_UNKNOWN109811],
                substations=(ABERDEEN_MS, TAP144796, UNKNOWN109811, TAP144779))

    tx = sp.find_nearest_transmission(ABERDEEN_MS[1], ABERDEEN_MS[2])

    assert tx, "the lookup served no line"
    assert (tx["voltage_kv"], tx["owner"]) == (161.0, "TENNESSEE VALLEY AUTHORITY")
    assert (tx["matched_substation"], tx["distance_miles"]) == ("UNKNOWN109811", 3.3)


def test_l9_a_mixed_case_substation_matches_its_uppercase_endpoint(db, no_sql_errors, no_network):
    """Every endpoint name is uppercase; 31,016 substation names are not."""
    import site_planner as sp
    _seed_lines(db, [LINE_HEIDELBACH], substations=(HEIDELBACH_MIXED_CASE, PIGEON_CREEK_TRANS))

    tx = sp.find_nearest_transmission(HEIDELBACH_MIXED_CASE[1], HEIDELBACH_MIXED_CASE[2])

    assert tx, "the lookup served no line"
    assert (tx["line_name"], tx["voltage_kv"]) == ("HEIDELBACH", 138.0)
    assert (tx["matched_substation"], tx["distance_miles"]) == ("Heidelbach", 0.0)


def test_l10_a_case_folded_match_with_nothing_to_place_the_far_end_is_not_served(
        db, no_sql_errors, no_network):
    """Massachusetts' 'Adams Substation' folds to ADAMS SUBSTATION, an endpoint of two
    Indiana Michigan Power lines in Indiana, and neither far end is in `substations`.
    Production also holds 'Adams Substation' in seven other states, which the lookup
    would find; they are left out so the as-stored rule is tested alone. It decides
    whenever no other copy is visible, as for ELLIOT SUBSTATION and UNIVERSITY
    SUBSTATION in the measured sample."""
    import site_planner as sp
    _seed_lines(db, [LINE_MAGLEY_ADAMS, LINE_ADAMS_PENNVILLE], substations=(ADAMS_MA,))

    assert sp.find_nearest_transmission(ADAMS_MA[1], ADAMS_MA[2]) is None
    assert sp.find_nearest_transmission_measured(ADAMS_MA[1], ADAMS_MA[2]) == (None, True)


def test_l11_an_anchor_name_found_far_away_with_nothing_to_place_the_far_end_is_not_served(
        db, no_sql_errors, no_network):
    """WEST -> HORSESHOE is Public Service Co of Colorado's. HORSESHOE is not in
    `substations`, and WEST also stands in Colorado, so nothing places that line at
    Arkansas's WEST."""
    import site_planner as sp
    _seed_lines(db, [LINE_WEST_HORSESHOE], substations=(WEST_AR, WEST_CO))

    assert sp.find_nearest_transmission(WEST_AR[1], WEST_AR[2]) is None
    assert sp.find_nearest_transmission_measured(WEST_AR[1], WEST_AR[2]) == (None, True)


def test_l12_a_failed_placing_statement_serves_no_unverified_line(db, error_log, no_network,
                                                                   monkeypatch):
    """execute_query swallows a failed statement into None. When the statement that
    places the endpoint names fails, the line it could not check is not served."""
    import site_planner as sp
    real = sp.execute_query

    def placing_statement_fails(query, params=None, fetchone=False):
        if "FROM substations" in query and "ANY(" in query:
            query = query.replace("FROM substations", "FROM substations_that_do_not_exist")
        return real(query, params, fetchone)

    monkeypatch.setattr(sp, "execute_query", placing_statement_fails)
    _seed_lines(db, ASHBURN_LINES)

    assert sp.find_nearest_transmission(*SITE) is None
    assert any('"substations_that_do_not_exist" does not exist' in m for m in error_log.messages), \
        error_log.messages
    assert sp.find_nearest_transmission_measured(*SITE) == (None, False)


# Each statement the lookup sends, by what only that statement carries, and the table
# it reads, renamed below to one that does not exist so that statement alone fails.
_STATEMENTS = {
    "nearby-substations": (lambda q: "FROM substations" in q and "name IS NOT NULL" in q,
                           "substations"),
    "endpoint-lines": (lambda q: "FROM transmission_lines" in q, "transmission_lines"),
    "placing-names": (lambda q: "FROM substations" in q and "name = ANY(" in q, "substations"),
}


@pytest.mark.parametrize("statement", list(_STATEMENTS), ids=list(_STATEMENTS))
def test_l13_a_statement_that_cannot_run_is_not_measured(db, error_log, no_network, monkeypatch,
                                                         statement):
    """ASHBURN_LINES at SITE serve L1's line. With any one statement failing, no line is
    served, and the answer says nothing was measured rather than that no line is there."""
    import site_planner as sp
    real = sp.execute_query
    matches, table = _STATEMENTS[statement]
    failed = []

    def one_statement_fails(query, params=None, fetchone=False):
        if matches(query):
            failed.append(statement)
            query = query.replace(f"FROM {table}", f"FROM {table}_that_do_not_exist")
        return real(query, params, fetchone)

    monkeypatch.setattr(sp, "execute_query", one_statement_fails)
    _seed_lines(db, ASHBURN_LINES)

    assert sp.find_nearest_transmission_measured(*SITE) == (None, False)
    assert failed == [statement], f"expected {statement} to be sent, and fail, once: {failed}"
    assert len(error_log.messages) == 1, error_log.messages
    assert f'"{table}_that_do_not_exist" does not exist' in error_log.messages[0], error_log.messages


def test_c2_control_a_swallowed_sql_error_reaches_the_error_log(db, error_log):
    """Anti-vacuity for no_sql_errors: a statement that cannot run comes back as
    None AND lands in the log that fixture reads."""
    import site_planner as sp
    assert sp.execute_query("SELECT owner FROM transmission_lines") is None
    assert any('column "owner" does not exist' in m for m in error_log.messages), \
        error_log.messages


def _fetch_and_swallow():
    """The removed fallback's shape: requests.get inside an except-everything."""
    try:
        import requests
        requests.get("https://example.invalid/arcgis/rest/services/query", timeout=2)
    except Exception:
        pass


def _connect_and_swallow():
    """A client that skips name resolution and connects to an address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(("192.0.2.1", 443))  # TEST-NET-1: reserved, never routed
    except Exception:
        pass


@pytest.mark.parametrize("attempt", [_fetch_and_swallow, _connect_and_swallow],
                         ids=["requests-get", "raw-socket"])
def test_c3_control_a_swallowed_connection_attempt_is_recorded(monkeypatch, attempt):
    """Anti-vacuity for no_network: each patched entry point records an attempt
    even when the caller swallows the refusal."""
    attempts = _record_connections(monkeypatch)
    attempt()
    assert attempts, "nothing was recorded, so no_network could not catch a fallback"


# The step-2 statement site_planner ran before 2026-09-13's anchored lookup, verbatim.
_PREFIX_STEP2 = """
                SELECT from_sub AS line_name, voltage_kv, operator AS owner, status
                FROM transmission_lines
                WHERE (from_sub LIKE %s OR to_sub LIKE %s)
                  AND voltage_kv IS NOT NULL
                ORDER BY voltage_kv DESC
                LIMIT 1;
            """


def test_c4_control_the_prefix_statement_serves_the_alabama_line_on_l4s_rows(db):
    _seed_lines(db, [LINE_WEST_TAP150157, LINE_WEST_VERNON],
                substations=(WEST_AR, TAP150157, WEST_VERNON))

    db.execute(_PREFIX_STEP2, ("WEST%", "WEST%"))

    assert db.fetchone() == ("UNKNOWN109244", 500.0, "ALABAMA POWER CO", "IN SERVICE")


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
