"""The /api/v1/energy/* routes that proxied live ArcGIS layers answer 410 to every
caller, open no network connection, and name something that is served (2026-09-13).

setup_energy_routes registers eight GET routes. Seven passed their arguments to a
live ArcGIS layer and are retired; site-analysis reads the database and stays.

  substations      services1 Electric_Substations no longer exists ("Invalid URL")
  compare-sites    three of its four live layers no longer answered
  pipelines        the DOT natural-gas pipelines layer answered HTTP 500
  transmission     read the superseded services1 copy of the transmission layer
  power-plants     services1 Power_Plants no longer exists; it was Pro-gated
  wells            two of its four state regulator layers no longer answered
  texas-pipelines  read the Texas RRC "Well Number" layer and served it as pipelines

The note above each retired route in energy_infrastructure_routes.py gives the
measurements.

  R   every retired route answers 410 with the retirement body, to an anonymous
      caller and to an internal one, for each argument shape that used to reach its
      network call and for none, and opens no network connection. The anonymous 410
      from power-plants matters: behind the Pro gate, a retirement would ask a caller
      to pay for a route that serves nothing.
  I   `instead` names, with its method, a route the backend serves that is not itself
      retired (wells: the two regulator layers that still answered, because DC Hub
      keeps no well data)
  M   no retired path is in main.py's LOCKED_GATE_MANIFEST, whose boot canary counts
      anything but 401/403/404 on a gated path as UNGATED
  N   the routes setup_energy_routes registers are exactly the retired ones plus
      site-analysis, read from the app's url_map, so a restored or new live route
      cannot go untested
  C   control: a connection attempt made and swallowed inside a request lands in the
      record R reads
"""
import ast
import json
import os
import re
import socket

import flask
import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(ROOT, "energy_infrastructure_routes.py")
MAIN_PATH = os.path.join(ROOT, "main.py")

BBOX = "minLat=38.9&maxLat=39.2&minLng=-77.7&maxLng=-77.3"
TX_BBOX = "minLat=31.8&maxLat=32.0&minLng=-102.2&maxLng=-102.0"

# Each retired route: the argument shapes that reached its network call before it was
# retired, and the route its `instead` names. R also sends no arguments at all.
RETIRED = {
    "/api/v1/energy/substations": (
        [BBOX], "GET /api/v2/infrastructure/hifld/substations"),
    "/api/v1/energy/compare-sites": (
        ["sites=39.044,-77.487;32.9,-96.8"], "POST /api/v1/site-planner/compare"),
    "/api/v1/energy/pipelines": (
        [BBOX, f"{BBOX}&type=interstate&operator=williams"],
        "GET /api/v2/infrastructure/hifld/gas-pipelines"),
    "/api/v1/energy/transmission": (
        [BBOX, f"{BBOX}&minVoltage=345"], "GET /api/v1/grid/transmission-proximity"),
    "/api/v1/energy/power-plants": (
        ["lat=39.04&lng=-77.49&radius=50000", f"{BBOX}&minMW=100&fuel=gas"],
        "GET /api/v1/energy/power-plants/nearby"),
    "/api/v1/energy/wells": (
        ["lat=30.25&lng=-97.75&state=TX", "lat=35.4&lng=-119.0&state=CA",
         "lat=32.7&lng=-103.9&state=NM", "lat=40.3&lng=-104.7&state=CO",
         "lat=31.42&lng=-103.49"],
        None),
    "/api/v1/energy/texas-pipelines": (
        [TX_BBOX, f"{TX_BBOX}&operator=energy"],
        "GET /api/v2/infrastructure/hifld/gas-pipelines"),
}
# DC Hub keeps no well data. The regulator layers that answered on 2026-09-13.
WELLS_INSTEAD = {
    "https://gis.rrc.texas.gov/server/rest/services/rrc_public/RRC_Public_Viewer_Srvs/MapServer/1",
    "https://gis.conservation.ca.gov/server/rest/services/WellSTAR/Wells/MapServer/0",
}
STILL_SERVED = {"/api/v1/energy/site-analysis"}

CALLERS = {
    "anonymous": {},
    "internal": {"X-Internal-Key": "test-internal-key",
                 "Referer": "https://dchub.cloud/land-power"},
}


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
def energy(monkeypatch):
    """The Flask app the real module's routes are registered on, a GET helper
    returning (status, JSON body), and the record of connection attempts. The
    recorder is installed before any route is registered or called."""
    import energy_infrastructure_routes as eir
    assert os.path.samefile(eir.__file__, MODULE_PATH), eir.__file__
    attempts = _record_connections(monkeypatch)
    app = flask.Flask(__name__)
    eir.setup_energy_routes(app)
    client = app.test_client()

    def get(path, headers=None):
        rv = client.get(path, headers=headers or {})
        return rv.status_code, rv.get_json()

    return app, get, attempts


def _urls():
    for path, (shapes, _named) in RETIRED.items():
        for query in ["", *shapes]:
            yield pytest.param(f"{path}?{query}" if query else path,
                               id=f"{path.rsplit('/', 1)[1]}:{query or 'no-args'}")


@pytest.mark.parametrize("caller", list(CALLERS))
@pytest.mark.parametrize("url", list(_urls()))
def test_r_retired_route_answers_410_and_opens_no_connection(energy, url, caller):
    _app, get, attempts = energy
    status, body = get(url, CALLERS[caller])
    assert status == 410, (status, body)
    assert body["success"] is False and body["retired"] is True, body
    assert body["error"] == "route_retired" and body["retired_at"] == "2026-09-13", body
    assert body["reason"] and body["instead"], body
    assert not attempts, f"a retired route opened a network connection: {attempts}"


def _named_route(instead):
    """The (method, path) an `instead` string names."""
    words = instead.split()
    method, target = ("GET", words[0]) if words[0].startswith("/") else (words[0], words[1])
    return method, target.split("?", 1)[0]


@pytest.mark.parametrize("path", list(RETIRED))
def test_i_instead_names_a_served_route_that_is_not_retired(energy, path):
    _app, get, attempts = energy
    _status, body = get(path)
    named = RETIRED[path][1]
    if named is None:
        assert set(re.findall(r"https://\S+", body["instead"])) == WELLS_INSTEAD, body["instead"]
    else:
        method, route = _named_route(body["instead"])
        assert f"{method} {route}" == named, body["instead"]
        assert route not in RETIRED, f"{path} points at {route}, which is retired too"
        with open(os.path.join(ROOT, "contracts", "route_serving_map.json"), encoding="utf-8") as fh:
            serving = json.load(fh)["serving"]
        assert named in serving, f"`instead` names {named}, which no module serves"
    assert not attempts, attempts


def _locked_gate_manifest():
    """main.py's LOCKED_GATE_MANIFEST read out of its source: {tier: [path, ...]}."""
    with open(MAIN_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(getattr(t, "id", None) == "LOCKED_GATE_MANIFEST" for t in node.targets)):
            return {k.value: [c.value for c in ast.walk(v)
                              if isinstance(c, ast.Constant) and isinstance(c.value, str)]
                    for k, v in zip(node.value.keys, node.value.values)}
    raise AssertionError("LOCKED_GATE_MANIFEST not found at main.py's top level")


def test_m_no_retired_path_is_in_the_locked_gate_manifest():
    listed = {path: tier for tier, paths in _locked_gate_manifest().items() for path in paths}
    # Anchors: the reader found the list the canary probes, and the route power-plants
    # names instead is still listed there as Pro.
    assert len(listed) >= 40, sorted(listed)
    assert listed.get("/api/v1/energy/power-plants/nearby") == "pro", listed.get(
        "/api/v1/energy/power-plants/nearby")
    assert not {p: listed[p] for p in RETIRED if p in listed}


def test_n_setup_registers_only_the_retired_routes_and_site_analysis(energy):
    app, _get, attempts = energy
    rules = {r.rule for r in app.url_map.iter_rules() if r.endpoint != "static"}
    expected = set(RETIRED) | STILL_SERVED
    assert rules == expected, {"registered, not covered here": sorted(rules - expected),
                               "covered here, not registered": sorted(expected - rules)}
    assert not attempts, attempts


def _fetch_and_swallow():
    """A live query the way the retired routes made one: requests.get inside an
    except that swallows everything."""
    try:
        requests.get("https://arcgis.example.invalid/arcgis/rest/services/query", timeout=2)
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
def test_c_control_a_swallowed_attempt_inside_a_request_is_recorded(energy, monkeypatch, attempt):
    """Anti-vacuity for R, in its harness: when the view R requests makes an attempt
    and swallows it, the attempt is recorded, although the response is a plain 200.

    The view is swapped in app.view_functions rather than registered under a new
    URL rule: scripts/check_route_table_coherence.py reads the route literals in
    tests/ too, and reports a fixture path as a new route the edge does not serve."""
    app, get, attempts = energy
    assert getattr(requests, "__file__", None), "requests is a stub module in this run"

    def swallowing_view():
        attempt()
        return flask.jsonify(success=True)

    monkeypatch.setitem(app.view_functions, "get_substations", swallowing_view)
    status, body = get("/api/v1/energy/substations")
    assert status == 200 and body == {"success": True}, body
    assert attempts, "nothing was recorded, so R could not catch a live query"
