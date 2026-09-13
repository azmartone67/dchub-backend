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
@require_pro (which imports main). find_nearest_substations is replaced by the
rows it returned, every other phase that reaches a database or a remote service
by a stub returning None, and so is the database connection. What runs for real
is analyze itself, the queue estimate and the suitability score.
find_nearest_substations' own fallback, taken only when the database returns
nothing, is not covered here.
"""
import os
import socket

import flask
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

# Every phase of analyze, other than the substations, that reaches a database or a
# remote service. score_connectivity is imported inside the handler, from
# routes.connectivity_score, and is stubbed there.
_IO_PHASES = ("geocode_address", "reverse_geocode", "find_nearest_transmission",
              "estimate_congestion", "screen_environmental", "get_generation_mix",
              "find_nearby_facilities", "check_fiber_proximity",
              "find_nearby_gas_pipelines", "find_major_pipelines",
              "get_capacity_pipeline_nearby")


def _nothing(*args, **kwargs):
    return None


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
    """Call the registered analyze view for SITE after a lookup that returns `rows`.

    Returns (connection attempts, HTTP status, JSON body)."""
    import routes.connectivity_score as connectivity_score
    import site_planner as sp

    assert os.path.samefile(sp.__file__, os.path.join(ROOT, "site_planner.py")), sp.__file__
    app = flask.Flask(__name__)
    sp.register_site_planner_routes(app)
    view = app.view_functions["site_planner_analyze"].__wrapped__

    for name in _IO_PHASES:
        monkeypatch.setattr(sp, name, _nothing)
    monkeypatch.setattr(connectivity_score, "score_connectivity", _nothing)
    # With no connection execute_query returns None, so estimate_queue_depth falls
    # back to its regional estimate rather than reaching a database.
    monkeypatch.setattr(sp, "get_neon_connection", _nothing)

    def run(rows, during_lookup=_nothing):
        def lookup(lat, lng, limit=5, max_distance_miles=25):
            during_lookup()
            return [dict(r) for r in rows]

        monkeypatch.setattr(sp, "find_nearest_substations", lookup)
        attempts = _record_connections(monkeypatch)
        with app.test_request_context(method="POST", json=SITE):
            rv = view()
        response, status = rv if isinstance(rv, tuple) else (rv, rv.status_code)
        return attempts, status, response.get_json()

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
