"""/api/v1/site-planner/analyze serves the substations its local lookup found and
opens no network connection to add more (2026-09-13).

When find_nearest_substations came back with fewer than five names (a sparse
site, or two of the nearest five sharing a name), analyze used to top the list up
from query_substations_live, a live ArcGIS query. The service it queried no longer
exists: ArcGIS answered HTTP 200 with error 400 "Invalid URL", the function
returned [] after the round trip, and nothing raised or logged. It was removed
rather than repointed; the comment where analyze called it says why.

That call caught every exception, and so does the handler. A test that refuses
connections and reads the response cannot catch a live top-up coming back: the
response is the same 200 either way. So N1 reads the RECORD of connection attempts.

  N1  after a lookup that found no substations, one, four, or five rows under four
      names, analyze answers 200 and opens no network connection
  N2  analyze serves exactly the substations the lookup found, and scores and
      estimates the queue from them
  C1  control: a connection attempt swallowed on this path lands in the record N1
      reads, while the response is still 200 with the same substations

The view is the real one register_site_planner_routes registers, called past
@require_pro (which imports main). N1, N2 and C1 replace find_nearest_substations
with the rows it returned, every other phase that reaches a database or a remote
service by a stub returning None, and so is the database connection. What runs
for real is analyze itself, the queue estimate and the suitability score.

★ 2026-09-13 — measured, or not measured. find_nearest_substations returned []
both when its query found no substation in the box and when the query did not
run (execute_query returns None after logging a query error or a missing
connection), so no caller could tell "no substation within 25 mi" from "nothing
was looked at". The composite score published power_grid as validated, because
compute_suitability_score returns a number for any input, with the substation
factors scored as absent; analyze and compare served [] and a null queue either
way; the site report printed "No mapped substation within 50 mi". The lookup now
returns None when its query did not run, and every caller says so.

The U tests run the real find_nearest_substations, execute_query and
get_neon_connection over a stand-in for the psycopg2 connection, or over no
database URL, through every caller of the lookup:

  U1  the lookup returns [] after a query that found no rows, and None after a
      query error or no connection, at the default 25 mi and the site report's 50 mi
  U2  rows the query returns come back as it returned them, under the caller's limit
  U3  analyze serves [] and a null queue either way, with substations_coverage
      'validated' after no rows, and 'unavailable' with the reason otherwise
  U4  compare marks each site the same way, and its recommendation says a lookup
      did not run only when one did not
  U5  when the lookup did not run, the composite score declares power_grid
      unavailable with the reason and a caveat, weighs the composite without it,
      and marks the answer no-store so its memo keeps none of it; after no rows,
      power_grid is validated, weighed and memoised
  U6  the composite score scores the substations a lookup found into a validated
      power_grid
  U7  compare with one site measured and one not names only the second
  U8  the site report prints "No mapped substation within 50 mi" only after a
      query that ran, and otherwise says the lookup did not run, a raising lookup
      included
  U9  a composite score whose power_grid gather raised declares it unavailable
      with a caveat, and is not memoised either
  UC1 control: each empty table reaches execute_query's own failure path, and logs
      what that path logs, the way its id says
  UC2 control: the stand-in connection offers nothing psycopg2's lacks

★ 2026-09-13 — the transmission lookup, measured or not. find_nearest_transmission
returned None both when no line is anchored near the site and when any of its three
statements did not run. Its success value is a dict, so no empty value could carry
"ran, found nothing". find_nearest_transmission_measured returns (line, measured),
and find_nearest_transmission still returns None for both. The fixtures above stub
the measured variant with a measured miss, so N1-U9 stay about substations. The T
tests run it for real, over a stand-in that answers each of its statements by which
one it is, through every caller:

  T1  the lookup returns (line, True) for a line, (None, True) for each way a lookup
      that ran matches no line, and (None, False) when step 1, 2 or 3 raised or there
      is no connection; each case sends and logs exactly what its id names, and
      find_nearest_transmission returns the line or None throughout
  T2  when a transmission statement did not run, the composite score declares
      power_grid unavailable with the reason and a caveat, weighs the composite
      without it, and marks the answer no-store so its memo keeps none of it; after
      a measured miss or a line, power_grid is validated and memoised
  T3  a line scores exactly the nearest transmission tier's 15 points over the same
      site's measured miss, so a validated power_grid in T2 scored the line
  T4  when neither lookup ran, power_grid's basis gives both reasons and every
      factor left out
  T5  analyze serves the line or null, as before, with transmission_coverage
      'validated' after a lookup that ran, and 'unavailable' with the reason otherwise
  T6  compare marks each row the same way, and its recommendation says the
      transmission lookup did not complete only when it did not
  T7  compare with one site missing each lookup names each lookup for its own site,
      and says the scores are not like-for-like
  T8  the site report fills the line from the substation only after a lookup that
      ran; after a statement that did not run, a raised lookup or its timeout, it
      prints the line as not measured
  TC1 control: the scripted stand-in offers nothing psycopg2's lacks
  TC2 control: a statement the script has no answer for fails, so an unexpected
      statement cannot pass as a miss

On this base an empty or None result still falls through to the lookup's Overpass
fallback. The recorder refuses that connection and the fallback swallows the
refusal, so the U tests read what the lookup and its callers return, not the
record of attempts.
"""
import logging
import os
import socket
import sys
import time
import types

import flask
import psycopg2
import psycopg2.extensions
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SITE = {"lat": 39.0440, "lng": -77.4870, "address": "Ashburn, VA 20147", "state": "VA"}


def _row(name, voltage_kv, lat, lng, distance_miles):
    """A row in the shape find_nearest_substations returns."""
    return {"name": name, "state": "VA", "voltage_kv": voltage_kv,
            "operator": "Dominion Energy", "lat": lat, "lng": lng,
            "distance_miles": distance_miles}


ASHBURN = _row("ASHBURN", 230.0, 39.0438, -77.4874, 0.03)
TAP_NEAR = _row("TAP", 115.0, 39.0610, -77.4610, 1.8)
BEAUMEADE = _row("BEAUMEADE", 230.0, 39.0290, -77.4390, 2.8)
TAP_FAR = _row("TAP", 115.0, 39.1010, -77.5430, 4.9)
LOUDOUN = _row("LOUDOUN", 500.0, 38.9580, -77.5250, 6.4)

# What find_nearest_substations returned, nearest first. Each leaves analyze with
# fewer than five names, which is when the live top-up used to run.
FOUND = {
    "none": [],
    "one": [ASHBURN],
    "four": [ASHBURN, TAP_NEAR, BEAUMEADE, LOUDOUN],
    "five-rows-four-names": [ASHBURN, TAP_NEAR, BEAUMEADE, TAP_FAR, LOUDOUN],
}

# Every phase of analyze, other than the substations and the transmission lookup,
# that reaches a database or a remote service. score_connectivity is imported inside
# the handler, from routes.connectivity_score, and is stubbed there. The transmission
# lookup returns a pair, so each fixture stubs it on its own (TX, below).
_IO_PHASES = ("geocode_address", "reverse_geocode",
              "estimate_congestion", "screen_environmental", "get_generation_mix",
              "find_nearby_facilities", "check_fiber_proximity",
              "find_nearby_gas_pipelines", "find_major_pipelines",
              "get_capacity_pipeline_nearby")


def _nothing(*args, **kwargs):
    return None


# The transmission lookup every caller reads since 2026-09-13. The fixtures stub it
# with a measured miss, which is what the None their old stub returned stood for; the
# T tests put the real one back.
TX = "find_nearest_transmission_measured"


def _transmission_missed(*args, **kwargs):
    return None, True


def _record_connections(monkeypatch):
    """Refuse and record every outbound connection attempt for one test.

    Patched at the socket layer, so any Python HTTP client counts, not only
    requests."""
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
def analyze(monkeypatch):
    """Call the registered analyze view for SITE after a lookup that returns `rows`,
    or after the real lookup when `rows` is None.

    Returns (connection attempts, HTTP status, JSON body)."""
    import routes.connectivity_score as connectivity_score
    import site_planner as sp

    assert os.path.samefile(sp.__file__, os.path.join(ROOT, "site_planner.py")), sp.__file__
    app = flask.Flask(__name__)
    sp.register_site_planner_routes(app)
    view = app.view_functions["site_planner_analyze"].__wrapped__

    real_transmission_lookup = getattr(sp, TX)
    for name in _IO_PHASES:
        monkeypatch.setattr(sp, name, _nothing)
    monkeypatch.setattr(sp, TX, _transmission_missed)
    monkeypatch.setattr(connectivity_score, "score_connectivity", _nothing)
    # With no connection execute_query returns None, so estimate_queue_depth falls
    # back to its regional estimate rather than reaching a database.
    monkeypatch.setattr(sp, "get_neon_connection", _nothing)

    def run(rows, during_lookup=_nothing):
        if rows is not None:
            def lookup(lat, lng, limit=5, max_distance_miles=25):
                during_lookup()
                return [dict(r) for r in rows]

            monkeypatch.setattr(sp, "find_nearest_substations", lookup)
        attempts = _record_connections(monkeypatch)
        with app.test_request_context(method="POST", json=SITE):
            rv = view()
        response, status = rv if isinstance(rv, tuple) else (rv, rv.status_code)
        return attempts, status, response.get_json()

    run.real_transmission_lookup = real_transmission_lookup
    return run


@pytest.mark.parametrize("rows", list(FOUND.values()), ids=list(FOUND))
def test_n1_analyze_opens_no_network_connection(analyze, rows):
    attempts, status, body = analyze(rows)

    # A handler that failed early would open no connection either.
    assert status == 200 and body["success"] is True, body
    assert not attempts, f"analyze opened a network connection: {attempts}"


# found, then the score breakdown's substation_proximity and substation_voltage.
SCORED = {
    "none": ([], None, None),
    "one": ([ASHBURN], "0.0 mi", "230.0 kV"),
    "four": (FOUND["four"], "0.0 mi", "500.0 kV"),
}


@pytest.mark.parametrize("rows, nearest, voltage", list(SCORED.values()), ids=list(SCORED))
def test_n2_analyze_serves_and_scores_the_substations_it_found(analyze, rows, nearest, voltage):
    _attempts, status, body = analyze(rows)

    assert status == 200 and body["success"] is True, body
    analysis = body["analysis"]
    assert analysis["substations"] == rows
    breakdown = analysis["suitability_score"]["breakdown"]
    assert breakdown.get("substation_proximity", {}).get("value") == nearest
    assert breakdown.get("substation_voltage", {}).get("value") == voltage
    assert (analysis["queue"] is not None) == bool(rows)


def _fetch_and_swallow():
    """The removed top-up's shape: requests.get inside an except-everything."""
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
def test_c1_control_a_swallowed_attempt_is_recorded_and_the_response_hides_it(analyze, attempt):
    """Anti-vacuity for N1, in its own harness: an attempt made and swallowed while
    analyze gathers substations is recorded, and the response could not show it."""
    rows = FOUND["four"]
    attempts, status, body = analyze(rows, during_lookup=attempt)

    assert attempts, "nothing was recorded, so N1 could not catch a live top-up"
    assert status == 200 and body["analysis"]["substations"] == rows


# ── Measured, or not measured: the real lookup over an empty table ──────────

LATLNG = (SITE["lat"], SITE["lng"])

# The columns the lookup's haversine query selects, in its order.
COLUMNS = ("name", "state", "voltage_kv", "operator", "lat", "lng", "distance_miles")


class _Cursor:
    """What execute_query uses of a psycopg2 cursor: `with`, execute, description
    and fetchall."""

    def __init__(self, conn):
        self._conn = conn
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params=None):
        self._conn.executed.append((query, params))
        if self._conn.error is not None:
            raise self._conn.error
        self.description = [(name,) for name in COLUMNS]

    def fetchall(self):
        return list(self._conn.rows)


class _Connection:
    """A stand-in for the psycopg2 connection get_neon_connection returns."""

    def __init__(self, rows=(), error=None):
        self.rows = list(rows)
        self.error = error
        self.executed = []

    def cursor(self):
        return _Cursor(self)

    def close(self):
        pass


def _no_rows(monkeypatch, sp):
    monkeypatch.setattr(sp, "get_neon_connection", lambda: _Connection())


def _query_error(monkeypatch, sp):
    error = psycopg2.OperationalError("SSL connection has been closed unexpectedly")
    monkeypatch.setattr(sp, "get_neon_connection", lambda: _Connection(error=error))


def _no_connection(monkeypatch, sp):
    """The real get_neon_connection, with no database URL to connect to. Under the
    analyze, compare and composite fixtures the connection is their stub instead,
    which also returns None."""
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


# Each way the lookup comes back empty: the set-up, what execute_query hands the
# lookup on that path (which is what the lookup now returns), and the start of
# what site_planner logs at ERROR there.
EMPTY = {
    "no-rows": (_no_rows, [], None),
    "query-error": (_query_error, None, "Query error: SSL connection has been closed unexpectedly"),
    "no-connection": (_no_connection, None, "No NEON_DATABASE_URL configured"),
}


class _Errors(logging.Handler):
    """What site_planner logs at ERROR during one test. Attached to the logger
    itself: caplog.records read after a fixture's yield holds only the teardown."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def errors():
    handler = _Errors()
    logger = logging.getLogger("site_planner")
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


@pytest.fixture
def sp():
    import site_planner

    assert os.path.samefile(site_planner.__file__, os.path.join(ROOT, "site_planner.py")), \
        site_planner.__file__
    return site_planner


def _count_opened(monkeypatch, sp):
    """Record every call to get_neon_connection from here on, passing each through
    to the connection set up before it."""
    connection = sp.get_neon_connection
    opened = []

    def open_connection():
        opened.append(True)
        return connection()

    monkeypatch.setattr(sp, "get_neon_connection", open_connection)
    return opened


def _empty(*args, **kwargs):
    return {}


# Every phase of compare, other than the substations, that reaches a database or a
# remote service. compare calls .get() on what most of them return, so they answer {}.
_COMPARE_IO_PHASES = ("geocode_address", "screen_environmental",
                      "estimate_congestion", "get_generation_mix", "find_nearby_facilities",
                      "find_nearby_gas_pipelines", "check_fiber_proximity")

# Two sites, each with an address the recommendation can name.
SITES = (SITE, {"lat": 39.0290, "lng": -77.4390, "address": "Beaumeade, VA 20147", "state": "VA"})


@pytest.fixture
def compare(monkeypatch):
    """Call the registered compare view for SITES, past @require_pro.

    Returns (HTTP status, JSON body)."""
    import site_planner as sp

    assert os.path.samefile(sp.__file__, os.path.join(ROOT, "site_planner.py")), sp.__file__
    app = flask.Flask(__name__)
    sp.register_site_planner_routes(app)
    view = app.view_functions["site_planner_compare"].__wrapped__

    real_transmission_lookup = getattr(sp, TX)
    for name in _COMPARE_IO_PHASES:
        monkeypatch.setattr(sp, name, _empty)
    monkeypatch.setattr(sp, TX, _transmission_missed)
    monkeypatch.setattr(sp, "get_neon_connection", _nothing)

    def run():
        _record_connections(monkeypatch)
        with app.test_request_context(method="POST", json={"sites": [dict(s) for s in SITES]}):
            rv = view()
        response, status = rv if isinstance(rv, tuple) else (rv, rv.status_code)
        return status, response.get_json()

    run.real_transmission_lookup = real_transmission_lookup
    return run


# Every phase of the composite score, other than the substations, that reaches a
# database or a remote service. Its FEMA NRI and WRI Aqueduct queries are inline
# urllib calls: the recorder refuses them, and those factors come back unavailable.
_COMPOSITE_IO_PHASES = ("geocode_address", "reverse_geocode",
                        "estimate_congestion", "screen_environmental",
                        "find_nearby_gas_pipelines", "find_nearby_facilities",
                        "check_fiber_proximity")


@pytest.fixture
def composite(monkeypatch):
    """Call the registered composite-score view for SITE past @require_pro but
    through its memo, over a stand-in redis_cache.

    Returns (HTTP status, JSON body, response headers, what the memo stored)."""
    import routes.connectivity_score as connectivity_score
    import site_planner as sp

    assert os.path.samefile(sp.__file__, os.path.join(ROOT, "site_planner.py")), sp.__file__
    app = flask.Flask(__name__)
    sp.register_site_planner_routes(app)
    # @require_pro's wrapper holds the memo's, which holds the handler.
    view = app.view_functions["site_planner_composite_score"].__wrapped__

    real_transmission_lookup = getattr(sp, TX)
    for name in _COMPOSITE_IO_PHASES:
        monkeypatch.setattr(sp, name, _nothing)
    monkeypatch.setattr(sp, TX, _transmission_missed)
    monkeypatch.setattr(connectivity_score, "score_connectivity", _nothing)
    monkeypatch.setattr(sp, "get_neon_connection", _nothing)

    stored = {}
    memo = types.ModuleType("redis_cache")
    memo.cache_get = lambda key: None
    memo.cache_set = lambda key, data, ttl=300: stored.__setitem__(key, data)
    monkeypatch.setitem(sys.modules, "redis_cache", memo)
    monkeypatch.delenv("DCHUB_SLOW_TOOL_CACHE", raising=False)

    def run():
        _record_connections(monkeypatch)
        with app.test_request_context(method="POST", json=dict(SITE)):
            rv = view()
        response, status = (rv[0], rv[1]) if isinstance(rv, tuple) else (rv, rv.status_code)
        return status, response.get_json(), response.headers, stored

    run.real_transmission_lookup = real_transmission_lookup
    return run


@pytest.mark.parametrize("miles", [25, 50], ids=["25mi", "50mi"])
@pytest.mark.parametrize("case", list(EMPTY), ids=list(EMPTY))
def test_u1_the_lookup_returns_none_only_when_its_query_did_not_run(monkeypatch, sp, case, miles):
    set_up, returned, _logged = EMPTY[case]
    set_up(monkeypatch, sp)
    _record_connections(monkeypatch)

    found = sp.find_nearest_substations(*LATLNG, limit=5, max_distance_miles=miles)

    # [] == None is False, so equality keeps "found none" and "did not run" apart.
    assert found == returned, (
        f"{case} at {miles} mi: expected {returned!r} "
        f"({'the query did not run' if returned is None else 'the query ran and found none'}), "
        f"got {found!r}")


def test_u2_rows_from_the_table_come_back_as_the_query_returned_them(monkeypatch, sp):
    conn = _Connection(rows=[tuple(row[c] for c in COLUMNS) for row in (ASHBURN, BEAUMEADE)])
    monkeypatch.setattr(sp, "get_neon_connection", lambda: conn)
    _record_connections(monkeypatch)

    found = sp.find_nearest_substations(*LATLNG, limit=3)

    assert found == [ASHBURN, BEAUMEADE]
    assert len(conn.executed) == 1
    query, params = conn.executed[0]
    assert "FROM substations" in query and params[-1] == 3


@pytest.mark.parametrize("case", list(EMPTY), ids=list(EMPTY))
def test_u3_analyze_says_whether_its_substations_were_measured(analyze, monkeypatch, sp, case):
    set_up, returned, _logged = EMPTY[case]
    set_up(monkeypatch, sp)
    opened = _count_opened(monkeypatch, sp)

    _attempts, status, body = analyze(None)

    # With no substations, the lookup is the only phase here that reaches the
    # connection, so exactly one call means the real lookup ran.
    assert opened == [True], "analyze did not run the real lookup"
    assert status == 200 and body["success"] is True, body
    analysis = body["analysis"]
    assert analysis["substations"] == [] and analysis["queue"] is None
    if returned is None:
        assert analysis["substations_coverage"] == "unavailable", analysis
        assert analysis["substations_basis"].startswith("substation lookup did not run"), analysis
    else:
        assert analysis["substations_coverage"] == "validated", analysis
        assert "did not run" not in analysis["substations_basis"], analysis


@pytest.mark.parametrize("case", list(EMPTY), ids=list(EMPTY))
def test_u4_compare_says_whether_each_sites_substations_were_measured(compare, monkeypatch, sp, case):
    set_up, returned, _logged = EMPTY[case]
    set_up(monkeypatch, sp)
    opened = _count_opened(monkeypatch, sp)

    status, body = compare()

    # One lookup per site, and nothing else here reaches the connection.
    assert opened == [True, True], "compare did not run the real lookup for both sites"
    assert status == 200 and body["success"] is True, body
    expected = "unavailable" if returned is None else "validated"
    for site in body["comparison"]:
        assert site["nearest_sub_miles"] is None and site["queue_mw"] is None, site
        assert site["substations_coverage"] == expected, site
    reason = body["recommendation"]["reason"]
    assert ("substation lookup did not run" in reason) == (returned is None), reason


@pytest.mark.parametrize("case", list(EMPTY), ids=list(EMPTY))
def test_u5_the_composite_score_declares_power_grid_unavailable_when_the_lookup_did_not_run(
        composite, monkeypatch, sp, case):
    set_up, returned, _logged = EMPTY[case]
    set_up(monkeypatch, sp)
    opened = _count_opened(monkeypatch, sp)

    status, body, headers, stored = composite()

    assert opened == [True], "the composite score did not run the real lookup"
    assert status == 200 and body["success"] is True, body
    power_grid = body["sub_scores"]["power_grid"]
    caveats = body["caveats"]
    if returned is None:
        assert power_grid["coverage"] == "unavailable" and power_grid["score"] is None, power_grid
        assert power_grid["basis"].startswith("substation lookup did not run"), power_grid
        assert body["coverage"]["power_grid"] == "unavailable"
        assert "power_grid" not in body["weights_over_validated"], body["weights_over_validated"]
        assert any(c.startswith("power_grid: substation lookup did not run") for c in caveats), caveats
        assert "no-store" in headers.get("Cache-Control", ""), dict(headers)
        assert stored == {}, "the memo kept an answer that says a lookup did not run"
    else:
        assert power_grid["coverage"] == "validated", power_grid
        assert isinstance(power_grid["score"], (int, float)), power_grid
        assert body["coverage"]["power_grid"] == "validated"
        assert "power_grid" in body["weights_over_validated"], body["weights_over_validated"]
        assert not any(c.startswith("power_grid:") for c in caveats), caveats
        assert "no-store" not in headers.get("Cache-Control", ""), dict(headers)
        assert len(stored) == 1, "control: the memo stores an answer whose lookups ran"
    # A miss is marked whether or not the memo kept the answer: the view returns
    # (resp, 200), and the header goes on the Response that tuple carries.
    assert headers.get("X-Tool-Cache") == "MISS", dict(headers)


def test_u6_the_composite_score_scores_the_substations_a_lookup_found(composite, monkeypatch, sp):
    def lookup(lat, lng, limit=5, max_distance_miles=25):
        return [dict(ASHBURN), dict(LOUDOUN)]

    monkeypatch.setattr(sp, "find_nearest_substations", lookup)

    status, body, headers, stored = composite()

    assert status == 200 and body["success"] is True, body
    power_grid = body["sub_scores"]["power_grid"]
    assert power_grid["coverage"] == "validated", power_grid
    # 0.03 mi is the nearest proximity tier (25 points) and 500 kV the highest
    # voltage tier (15): both substation factors were scored.
    assert power_grid["score"] >= 40, power_grid
    assert len(stored) == 1 and "no-store" not in headers.get("Cache-Control", "")


def test_u7_compare_names_only_the_site_whose_lookup_did_not_run(compare, monkeypatch, sp):
    measured, unmeasured = SITES

    def lookup(lat, lng, limit=5, max_distance_miles=25):
        return [dict(ASHBURN)] if lat == measured["lat"] else None

    monkeypatch.setattr(sp, "find_nearest_substations", lookup)

    status, body = compare()

    assert status == 200 and body["success"] is True, body
    by_address = {site["address"]: site for site in body["comparison"]}
    assert by_address[measured["address"]]["substations_coverage"] == "validated"
    assert by_address[measured["address"]]["nearest_sub_name"] == "ASHBURN"
    assert by_address[unmeasured["address"]]["substations_coverage"] == "unavailable"
    reason = body["recommendation"]["reason"]
    assert f"substation lookup did not run for {unmeasured['address']}," in reason, reason
    assert measured["address"] not in reason, reason
    assert "not like-for-like" in reason, reason


@pytest.mark.parametrize("case", list(EMPTY) + ["raises"], ids=list(EMPTY) + ["raises"])
def test_u8_the_site_report_prints_no_mapped_substation_only_after_a_query_that_ran(
        monkeypatch, sp, case):
    from routes import site_report

    assert os.path.samefile(site_report.__file__, os.path.join(ROOT, "routes", "site_report.py")), \
        site_report.__file__
    if case == "raises":
        def lookup(*args, **kwargs):
            raise RuntimeError("the lookup raised")

        monkeypatch.setattr(sp, "find_nearest_substations", lookup)
        measured = False
    else:
        set_up, returned, _logged = EMPTY[case]
        set_up(monkeypatch, sp)
        measured = returned is not None
    opened = _count_opened(monkeypatch, sp)
    _record_connections(monkeypatch)

    power = site_report._gather_power(*LATLNG, "VA")

    assert opened == ([] if case == "raises" else [True]), "the report did not run the lookup"
    assert power["substation"] == "—" and power["_dist"] is None and power["_score"] is None
    if measured:
        assert power["substation_coverage"] == "validated"
        assert power["substation_note"] == "No mapped substation within 50 mi in DC Hub's grid layer."
        assert "did not run" not in power["assessment"], power["assessment"]
    else:
        assert power["substation_coverage"] == "unavailable"
        assert power["substation_note"].startswith("Not measured"), power["substation_note"]
        assert "No mapped substation" not in power["substation_note"], power["substation_note"]
        assert "substation lookup did not run" in power["assessment"], power["assessment"]


def test_u9_the_composite_score_does_not_memoise_a_power_grid_gather_that_raised(
        composite, monkeypatch, sp):
    def lookup(lat, lng, limit=5, max_distance_miles=25):
        return [dict(ASHBURN)]

    def transmission(*args, **kwargs):
        raise RuntimeError("the transmission lookup raised")

    monkeypatch.setattr(sp, "find_nearest_substations", lookup)
    monkeypatch.setattr(sp, TX, transmission)

    status, body, headers, stored = composite()

    assert status == 200 and body["success"] is True, body
    power_grid = body["sub_scores"]["power_grid"]
    assert power_grid == {"score": None, "coverage": "unavailable",
                          "basis": "gather failed: RuntimeError"}, power_grid
    assert "power_grid: gather failed: RuntimeError." in body["caveats"], body["caveats"]
    assert "no-store" in headers.get("Cache-Control", ""), dict(headers)
    assert stored == {}, "the memo kept an answer whose power_grid gather raised"


@pytest.mark.parametrize("case", list(EMPTY), ids=list(EMPTY))
def test_uc1_control_each_empty_table_takes_the_path_its_id_names(monkeypatch, sp, errors, case):
    """Anti-vacuity for the U tests' cases: each must reach execute_query's own
    failure path and log what that path logs. A stand-in that failed some other
    way (lacking a method psycopg2's cursor has, say) would still hand the lookup
    None, and the U tests would pass over a path production never takes."""
    set_up, returned, logged = EMPTY[case]
    set_up(monkeypatch, sp)
    _record_connections(monkeypatch)
    seen = []
    real = sp.execute_query

    def spy(*args, **kwargs):
        seen.append(real(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(sp, "execute_query", spy)

    sp.find_nearest_substations(*LATLNG)

    assert seen == [returned]
    if logged is None:
        assert not errors.messages, errors.messages
    else:
        assert any(m.startswith(logged) for m in errors.messages), errors.messages


def test_uc2_control_the_stand_in_offers_nothing_psycopg2_lacks():
    """execute_query swallows an AttributeError like any other error, so a stand-in
    offering what psycopg2 does not could keep U2 and UC1 green over a query path
    that cannot run."""
    cursor_api = ({n for n in vars(_Cursor) if not n.startswith("_")}
                  | {"__enter__", "__exit__", "description"})
    connection_api = {n for n in vars(_Connection) if not n.startswith("_")}

    assert cursor_api == {"execute", "fetchall", "__enter__", "__exit__", "description"}
    assert connection_api == {"cursor", "close"}
    assert all(hasattr(psycopg2.extensions.cursor, n) for n in cursor_api), cursor_api
    assert all(hasattr(psycopg2.extensions.connection, n) for n in connection_api), connection_api


# ── Measured, or not measured: the transmission lookup ──────────────────────

# The columns each statement of the transmission lookup selects, in its order, and
# estimate_queue_depth's, which the route tests below also reach.
TX_COLUMNS = {
    "nearby": ("name", "lat", "lng", "distance_miles"),
    "lines": ("id", "from_sub", "to_sub", "voltage_kv", "owner", "status"),
    "places": ("name", "lat", "lng"),
    "queue": ("total_queue_mw", "project_count", "avg_age_years"),
}


def _tx_step(query):
    """Which statement `query` is, by a phrase only that statement carries."""
    if "FROM transmission_lines" in query:
        return "lines"
    if "FROM substations" in query and "name = ANY(" in query:
        return "places"
    if "FROM substations" in query and "name IS NOT NULL" in query:
        return "nearby"
    if "FROM queue_entries" in query:
        return "queue"
    return None


class _TxDatabase:
    """The tables behind the lookup. `answers` maps a step to the rows its statement
    returns, in TX_COLUMNS order, or to the exception it raises. A step with no answer
    raises, so an unexpected statement cannot pass as a miss."""

    def __init__(self, **answers):
        self.answers = answers
        self.steps = []

    def connect(self):
        return _TxConnection(self)


class _TxCursor:
    """What execute_query uses of a psycopg2 cursor: `with`, execute, description,
    fetchall and fetchone."""

    def __init__(self, db):
        self._db = db
        self._rows = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, query, params=None):
        step = _tx_step(query)
        self._db.steps.append(step)
        answer = self._db.answers.get(step, LookupError(f"no answer scripted for {step!r}"))
        if isinstance(answer, Exception):
            raise answer
        self.description = [(name,) for name in TX_COLUMNS[step]]
        self._rows = [tuple(row) for row in answer]

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _TxConnection:
    """A stand-in for the psycopg2 connection get_neon_connection returns."""

    def __init__(self, db):
        self._db = db

    def cursor(self):
        return _TxCursor(self._db)

    def close(self):
        pass


# ASHBURN and the 230 kV line from PLEASANT VIEW that ends there, the line
# tests/test_transmission_readers_sql.py serves in L1, with its far end 3 mi away.
TX_ANSWERS = {
    "nearby": [("ASHBURN", 39.0438, -77.4874, 0.03)],
    "lines": [(1, "PLEASANT VIEW", "ASHBURN", 230.0, "VIRGINIA ELECTRIC & POWER CO", "IN SERVICE")],
    "places": [("ASHBURN", 39.0438, -77.4874), ("PLEASANT VIEW", 39.07591, -77.528824)],
    "queue": [],
}
LINE = {"line_name": "PLEASANT VIEW", "voltage_kv": 230.0, "owner": "VIRGINIA ELECTRIC & POWER CO",
        "status": "IN SERVICE", "volt_class": None, "distance_miles": 0.0,
        "matched_substation": "ASHBURN"}

# Each way the lookup ends: the answers that differ from TX_ANSWERS, what it returns,
# the statements it sends, and the start of what site_planner logs at ERROR (None:
# nothing). BARCOLA's coordinates put PLEASANT VIEW's only copy in Florida.
TX_CASES = {
    "line": ({}, (LINE, True), ["nearby", "lines", "places"], None),
    "no-substation-nearby": ({"nearby": []}, (None, True), ["nearby"], None),
    "no-line-named": ({"lines": []}, (None, True), ["nearby", "lines"], None),
    "line-not-placed": (
        {"places": [("ASHBURN", 39.0438, -77.4874), ("PLEASANT VIEW", 27.823994, -81.887856)]},
        (None, True), ["nearby", "lines", "places"], None),
    "nearby-raises": (
        {"nearby": psycopg2.OperationalError("canceling statement due to statement timeout")},
        (None, False), ["nearby"], "Query error: canceling statement due to statement timeout"),
    "lines-raises": (
        {"lines": psycopg2.OperationalError("SSL connection has been closed unexpectedly")},
        (None, False), ["nearby", "lines"], "Query error: SSL connection has been closed unexpectedly"),
    "places-raises": (
        {"places": psycopg2.OperationalError("terminating connection due to administrator command")},
        (None, False), ["nearby", "lines", "places"],
        "Query error: terminating connection due to administrator command"),
}


def _tx_tables(monkeypatch, sp, overrides):
    """Answer the lookup's statements from TX_ANSWERS, with `overrides` applied."""
    db = _TxDatabase(**{**TX_ANSWERS, **overrides})
    monkeypatch.setattr(sp, "get_neon_connection", db.connect)
    return db


def _substations_found(lat, lng, limit=5, max_distance_miles=25):
    return [dict(ASHBURN), dict(LOUDOUN)]


@pytest.mark.parametrize("case", list(TX_CASES) + ["no-connection"],
                         ids=list(TX_CASES) + ["no-connection"])
def test_t1_the_transmission_lookup_says_whether_it_measured(monkeypatch, sp, errors, case):
    if case == "no-connection":
        _no_connection(monkeypatch, sp)
        db, returned, steps, logged = _TxDatabase(), (None, False), [], "No NEON_DATABASE_URL configured"
    else:
        overrides, returned, steps, logged = TX_CASES[case]
        db = _tx_tables(monkeypatch, sp, overrides)
    _record_connections(monkeypatch)

    assert sp.find_nearest_transmission_measured(*LATLNG) == returned
    assert db.steps == steps, f"{case} sent {db.steps}"
    if logged is None:
        assert not errors.messages, errors.messages
    else:
        assert len(errors.messages) == 1 and errors.messages[0].startswith(logged), errors.messages
    # The wrapper keeps the contract tests/test_transmission_readers_sql.py pins.
    assert sp.find_nearest_transmission(*LATLNG) == returned[0]


@pytest.mark.parametrize("case", list(TX_CASES), ids=list(TX_CASES))
def test_t2_the_composite_score_declares_power_grid_unavailable_when_a_transmission_statement_did_not_run(
        composite, monkeypatch, sp, case):
    overrides, (_line, measured), _steps, _logged = TX_CASES[case]
    monkeypatch.setattr(sp, "find_nearest_substations", _substations_found)
    monkeypatch.setattr(sp, TX, composite.real_transmission_lookup)
    db = _tx_tables(monkeypatch, sp, overrides)

    status, body, headers, stored = composite()

    assert db.steps.count("nearby") == 1, f"the composite score did not run the real lookup: {db.steps}"
    assert status == 200 and body["success"] is True, body
    power_grid = body["sub_scores"]["power_grid"]
    caveats = body["caveats"]
    if measured:
        assert power_grid["coverage"] == "validated", power_grid
        assert isinstance(power_grid["score"], (int, float)), power_grid
        assert "power_grid" in body["weights_over_validated"], body["weights_over_validated"]
        assert not any(c.startswith("power_grid:") for c in caveats), caveats
        assert "no-store" not in headers.get("Cache-Control", ""), dict(headers)
        assert len(stored) == 1, "control: the memo stores an answer whose lookups ran"
    else:
        assert power_grid["coverage"] == "unavailable" and power_grid["score"] is None, power_grid
        assert power_grid["basis"].startswith("transmission lookup did not complete"), power_grid
        assert power_grid["basis"].endswith(
            "rather than scored without transmission proximity"), power_grid
        assert body["coverage"]["power_grid"] == "unavailable"
        assert "power_grid" not in body["weights_over_validated"], body["weights_over_validated"]
        assert any(c.startswith("power_grid: transmission lookup did not complete") for c in caveats), \
            caveats
        assert "no-store" in headers.get("Cache-Control", ""), dict(headers)
        assert stored == {}, "the memo kept an answer that says a lookup did not complete"


def test_t3_a_line_scores_the_nearest_transmission_tier_over_a_measured_miss(composite, monkeypatch, sp):
    monkeypatch.setattr(sp, "find_nearest_substations", _substations_found)
    monkeypatch.setattr(sp, TX, composite.real_transmission_lookup)
    scores = {}
    for case in ("line", "no-line-named"):
        _tx_tables(monkeypatch, sp, TX_CASES[case][0])
        _status, body, _headers, _stored = composite()
        scores[case] = body["sub_scores"]["power_grid"]["score"]

    # LINE is anchored 0.0 mi from the site: the 'excellent' tier, 15 points.
    assert scores["line"] - scores["no-line-named"] == 15, scores


def test_t4_when_neither_lookup_ran_power_grid_gives_both_reasons(composite, monkeypatch, sp):
    monkeypatch.setattr(sp, "find_nearest_substations", lambda lat, lng, limit=5, max_distance_miles=25: None)
    monkeypatch.setattr(sp, TX, composite.real_transmission_lookup)
    _tx_tables(monkeypatch, sp, TX_CASES["lines-raises"][0])

    status, body, headers, stored = composite()

    assert status == 200 and body["success"] is True, body
    basis = body["sub_scores"]["power_grid"]["basis"]
    assert basis.startswith("substation lookup did not run"), basis
    assert "; transmission lookup did not complete: " in basis, basis
    assert basis.endswith("rather than scored without substation proximity, voltage, queue depth "
                          "and transmission proximity"), basis
    assert "no-store" in headers.get("Cache-Control", "") and stored == {}, dict(headers)


@pytest.mark.parametrize("case", list(TX_CASES), ids=list(TX_CASES))
def test_t5_analyze_says_whether_its_transmission_line_was_measured(analyze, monkeypatch, sp, case):
    overrides, (line, measured), _steps, _logged = TX_CASES[case]
    monkeypatch.setattr(sp, TX, analyze.real_transmission_lookup)
    db = _tx_tables(monkeypatch, sp, overrides)

    _attempts, status, body = analyze([ASHBURN])

    assert db.steps.count("nearby") == 1, f"analyze did not run the real lookup: {db.steps}"
    assert status == 200 and body["success"] is True, body
    analysis = body["analysis"]
    # The same values as before, the line or null: js/site-planner-panel.js reads
    # `a.transmission || {}`.
    assert analysis["transmission"] == line
    if measured:
        assert analysis["transmission_coverage"] == "validated", analysis
        assert "did not complete" not in analysis["transmission_basis"], analysis
    else:
        assert analysis["transmission_coverage"] == "unavailable", analysis
        assert analysis["transmission_basis"].startswith("transmission lookup did not complete"), analysis


@pytest.mark.parametrize("case", list(TX_CASES), ids=list(TX_CASES))
def test_t6_compare_says_whether_each_sites_transmission_line_was_measured(compare, monkeypatch, sp, case):
    overrides, (line, measured), _steps, _logged = TX_CASES[case]
    monkeypatch.setattr(sp, "find_nearest_substations", _substations_found)
    monkeypatch.setattr(sp, TX, compare.real_transmission_lookup)
    db = _tx_tables(monkeypatch, sp, overrides)

    status, body = compare()

    assert db.steps.count("nearby") == 2, f"compare did not run the real lookup for both sites: {db.steps}"
    assert status == 200 and body["success"] is True, body
    for site in body["comparison"]:
        assert site["nearest_tx_miles"] == (line or {}).get("distance_miles"), site
        assert site["transmission_coverage"] == ("validated" if measured else "unavailable"), site
    reason = body["recommendation"]["reason"]
    assert ("transmission lookup did not complete" in reason) == (not measured), reason
    # Both sites miss the same lookup, so their scores stay comparable.
    assert "not like-for-like" not in reason, reason


def test_t7_compare_names_each_lookup_that_did_not_run_for_its_own_site(compare, monkeypatch, sp):
    first, second = SITES

    def substations(lat, lng, limit=5, max_distance_miles=25):
        return None if lat == first["lat"] else [dict(ASHBURN)]

    def transmission(lat, lng, max_distance_miles=15):
        return None, lat == first["lat"]

    monkeypatch.setattr(sp, "find_nearest_substations", substations)
    monkeypatch.setattr(sp, TX, transmission)

    status, body = compare()

    assert status == 200 and body["success"] is True, body
    by_address = {site["address"]: site for site in body["comparison"]}
    assert by_address[first["address"]]["transmission_coverage"] == "validated"
    assert by_address[second["address"]]["transmission_coverage"] == "unavailable"
    reason = body["recommendation"]["reason"]
    assert (f" The substation lookup did not run for {first['address']}, so its score leaves out "
            "substation proximity, voltage and queue depth. The transmission lookup did not "
            f"complete for {second['address']}, so its score leaves out transmission proximity; "
            "the scores are not like-for-like.") in reason, reason


@pytest.mark.parametrize("case", list(TX_CASES) + ["raises", "times-out"],
                         ids=list(TX_CASES) + ["raises", "times-out"])
def test_t8_the_site_report_fills_the_line_from_the_substation_only_after_a_lookup_that_ran(
        monkeypatch, sp, case):
    from routes import site_report

    assert os.path.samefile(site_report.__file__, os.path.join(ROOT, "routes", "site_report.py")), \
        site_report.__file__
    # A real name, so the report probes for its line: 115 kV, Dominion Energy.
    monkeypatch.setattr(sp, "find_nearest_substations",
                        lambda lat, lng, limit=5, max_distance_miles=25: [dict(TAP_NEAR)])
    if case == "raises":
        def lookup(*args, **kwargs):
            raise RuntimeError("the lookup raised")

        monkeypatch.setattr(sp, TX, lookup)
        line, measured = None, False
    elif case == "times-out":
        def lookup(*args, **kwargs):
            time.sleep(0.5)
            return dict(LINE), True

        cap = site_report._call_with_timeout
        monkeypatch.setattr(sp, TX, lookup)
        # The real cap, shortened from 6 s so the lookup outlives it.
        monkeypatch.setattr(site_report, "_call_with_timeout",
                            lambda fn, timeout, *args, **kwargs: cap(fn, 0.05, *args, **kwargs))
        line, measured = None, False
    else:
        overrides, (line, measured), _steps, _logged = TX_CASES[case]
        _tx_tables(monkeypatch, sp, overrides)
    _record_connections(monkeypatch)

    power = site_report._gather_power(*LATLNG, "VA")

    assert (power["substation"], power["voltage"], power["operator"]) == ("TAP", "115 kV", "Dominion Energy"), \
        power
    assert power["transmission_coverage"] == ("validated" if measured else "unavailable"), power
    if line:
        assert (power["line_voltage"], power["line_owner"]) == ("230 kV", "VIRGINIA ELECTRIC & POWER CO"), power
    elif measured:
        # A lookup that ran and matched no line leaves the line to the substation, as before.
        assert (power["line_voltage"], power["line_owner"]) == ("115 kV", "Dominion Energy"), power
    else:
        assert (power["line_voltage"], power["line_owner"]) == ("—", "—"), power
        assert power["line_note"].startswith("Not measured"), power
        assert "transmission lookup did not complete" in power["assessment"], power["assessment"]
    if measured:
        assert "line_note" not in power and "did not complete" not in power["assessment"], power


def test_tc1_control_the_scripted_stand_in_offers_nothing_psycopg2_lacks():
    """Anti-vacuity for the T tests: execute_query swallows an AttributeError like any
    other error, so a stand-in offering what psycopg2 does not could keep a measured
    case green over a path that cannot run."""
    cursor_api = ({n for n in vars(_TxCursor) if not n.startswith("_")}
                  | {"__enter__", "__exit__", "description"})
    connection_api = {n for n in vars(_TxConnection) if not n.startswith("_")}

    assert cursor_api == {"execute", "fetchall", "fetchone", "__enter__", "__exit__", "description"}
    assert connection_api == {"cursor", "close"}
    assert all(hasattr(psycopg2.extensions.cursor, n) for n in cursor_api), cursor_api
    assert all(hasattr(psycopg2.extensions.connection, n) for n in connection_api), connection_api


def test_tc2_control_a_statement_with_no_scripted_answer_fails(monkeypatch, sp, errors):
    """Anti-vacuity for the stand-in: a statement it has no answer for raises, and the
    lookup reports it unmeasured, so no route test can pass over an unexpected
    statement answered with an empty list."""
    db = _TxDatabase(nearby=TX_ANSWERS["nearby"])
    monkeypatch.setattr(sp, "get_neon_connection", db.connect)
    _record_connections(monkeypatch)

    assert sp.find_nearest_transmission_measured(*LATLNG) == (None, False)
    assert db.steps == ["nearby", "lines"], db.steps
    assert errors.messages and errors.messages[0].startswith("Query error: no answer scripted for 'lines'"), \
        errors.messages
