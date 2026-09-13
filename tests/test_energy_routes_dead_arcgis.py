"""GET /api/v1/energy/substations and GET /api/v1/energy/compare-sites answer
410, and query_arcgis never hands a failed ArcGIS query to a route as data
(2026-09-13).

Both routes queried the Electric_Substations service on
services1.arcgis.com/Hp6G80Pky0om7QvQ, which no longer exists. ArcGIS answers
HTTP 200 with {"error":{"code":400,"message":"Invalid URL"}}. raise_for_status()
lets that through, and query_arcgis returned the body as data, as it did any
transport error, so every route that read `features` off it answered success
with a count of 0: the response could not tell a dead upstream from an empty
area. The notes above the two retired routes say why they were retired rather
than repointed.

  R   GET /api/v1/energy/substations and GET /api/v1/energy/compare-sites answer
      410, open no network connection, and name a route the backend serves
  Q   every route that reads query_arcgis answers a failure, never a success,
      when the query fails: the error body services1 returns on HTTP 200, an
      error beside an empty features list, an HTML page on HTTP 500 (as the DOT
      pipelines server answered on 2026-09-13), and a body with no features list
  Q4  the routes Q calls are every view in setup_energy_routes that calls
      query_arcgis, so a new caller cannot go untested
  C1  control: the same routes serve a real feature set as data
  C2  control: a connection attempt made and swallowed inside a request lands in
      the record R reads

query_arcgis runs for real. Only requests.get is replaced, and it returns real
requests.Response objects, so raise_for_status() and json() are the library's.
"""
import ast
import json
import os
import socket

import flask
import pytest
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(ROOT, "energy_infrastructure_routes.py")

# What services1.arcgis.com answered for the Electric_Substations service, its
# layer and its query on 2026-09-13.
INVALID_URL = {"error": {"code": 400, "message": "Invalid URL", "details": ["Invalid URL"]}}
# The shape of what geo.dot.gov answered for the natural-gas pipelines layer.
DOT_500 = (b'<!DOCTYPE html><html><head id="Head1"><title>Application Error</title>'
           b'</head><body></body></html>')

FAILED_QUERIES = {
    "error-body": (200, INVALID_URL),
    "error-beside-empty-features": (200, {**INVALID_URL, "features": []}),
    "http-500-html": (500, DOT_500),
    "no-features-list": (200, {"displayFieldName": "NAME", "fields": []}),
}

FEATURE = {"attributes": {"NAME": "LOUDOUN", "VOLTAGE": 500},
           "geometry": {"x": -77.52, "y": 38.96}}

BBOX = "minLat=38.9&maxLat=39.2&minLng=-77.7&maxLng=-77.3"
TX_BBOX = "minLat=30.2&maxLat=30.3&minLng=-97.8&maxLng=-97.7"

# Every route whose view calls query_arcgis, with arguments that reach the call.
# Q4 derives the set of routes from the module and compares.
ARCGIS_ROUTES = {
    "pipelines": f"/api/v1/energy/pipelines?{BBOX}",
    "transmission": f"/api/v1/energy/transmission?{BBOX}",
    "power-plants-bbox": f"/api/v1/energy/power-plants?{BBOX}",
    "wells-tx": "/api/v1/energy/wells?lat=30.25&lng=-97.75&state=TX",
    "wells-ca": "/api/v1/energy/wells?lat=36.5&lng=-119.5&state=CA",
    "texas-pipelines": f"/api/v1/energy/texas-pipelines?{TX_BBOX}",
}

# The retired routes, and the served route each one names instead.
RETIRED = {
    "substations": (f"/api/v1/energy/substations?{BBOX}",
                    "GET /api/v2/infrastructure/hifld/substations"),
    "compare-sites": ("/api/v1/energy/compare-sites?sites=39.044,-77.487;32.9,-96.8",
                      "POST /api/v1/site-planner/compare"),
}

INTERNAL_KEY = "test-internal-key"


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


def _arcgis_response(status, body):
    """A real requests.Response carrying `body`."""
    r = requests.Response()
    r.status_code = status
    r.reason = "OK" if status == 200 else "Internal Server Error"
    r._content = body if isinstance(body, bytes) else json.dumps(body).encode()
    r.encoding = "utf-8"
    r.url = "https://arcgis.example.invalid/arcgis/rest/services/layer/FeatureServer/0/query"
    return r


@pytest.fixture
def energy(monkeypatch):
    """The real module, the Flask app its routes are registered on, and a GET helper
    returning (status, JSON body)."""
    import energy_infrastructure_routes as eir
    assert os.path.samefile(eir.__file__, MODULE_PATH), eir.__file__
    assert getattr(requests, "__file__", None), "requests is a stub module in this run"
    # get_power_plants sits behind @require_plan('pro'), which admits a valid internal
    # key, and @protect_data, which admits a dchub.cloud Referer.
    monkeypatch.setattr(eir, "is_valid_internal_key", lambda key: key == INTERNAL_KEY)
    app = flask.Flask(__name__)
    eir.setup_energy_routes(app)
    client = app.test_client()

    def get(path):
        rv = client.get(path, headers={"X-Internal-Key": INTERNAL_KEY,
                                       "Referer": "https://dchub.cloud/land-power"})
        return rv.status_code, rv.get_json()

    return eir, app, get


@pytest.fixture
def arcgis(monkeypatch, energy):
    """Answer every ArcGIS request with one response. Returns the URLs requested."""
    eir, _app, _get = energy
    requested = []

    def answer(status, body):
        def fake_get(url, params=None, timeout=None, **kwargs):
            requested.append(url)
            return _arcgis_response(status, body)

        monkeypatch.setattr(eir.requests, "get", fake_get)
        return requested

    return answer


@pytest.mark.parametrize("path, served_instead", list(RETIRED.values()), ids=list(RETIRED))
def test_r_retired_route_answers_410_and_opens_no_connection(energy, monkeypatch, path, served_instead):
    _eir, _app, get = energy
    attempts = _record_connections(monkeypatch)
    status, body = get(path)
    assert status == 410, (status, body)
    assert body["success"] is False and body["retired"] is True, body
    assert not attempts, f"a retired route opened a network connection: {attempts}"
    _method, route = served_instead.split(" ", 1)
    assert route in body["instead"], body["instead"]
    with open(os.path.join(ROOT, "contracts", "route_serving_map.json"), encoding="utf-8") as fh:
        serving = json.load(fh)["serving"]
    assert served_instead in serving, f"`instead` names {served_instead}, which no module serves"


@pytest.mark.parametrize("status, body", list(FAILED_QUERIES.values()), ids=list(FAILED_QUERIES))
@pytest.mark.parametrize("path", list(ARCGIS_ROUTES.values()), ids=list(ARCGIS_ROUTES))
def test_q_a_failed_arcgis_query_is_answered_as_a_failure(energy, arcgis, path, status, body):
    _eir, _app, get = energy
    requested = arcgis(status, body)
    got_status, got = get(path)
    assert requested, f"{path} never queried ArcGIS, so this case tested nothing"
    assert got_status >= 500 and got["success"] is False, (got_status, got)
    assert "count" not in got and "data" not in got, got


def _views_calling_query_arcgis(source):
    """The route of every view in setup_energy_routes whose body calls query_arcgis."""
    tree = ast.parse(source)
    setup = next(n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "setup_energy_routes")
    routes = set()
    for fn in setup.body:
        if not isinstance(fn, ast.FunctionDef):
            continue
        if not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "query_arcgis" for n in ast.walk(fn)):
            continue
        paths = [d.args[0].value for d in fn.decorator_list
                 if isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "route"]
        assert paths, f"{fn.name} calls query_arcgis and carries no @app.route"
        routes.update(paths)
    return routes


def test_q4_q_calls_every_route_that_reads_query_arcgis():
    with open(MODULE_PATH, encoding="utf-8") as fh:
        readers = _views_calling_query_arcgis(fh.read())
    covered = {p.split("?", 1)[0] for p in ARCGIS_ROUTES.values()}
    assert readers == covered, {"reads query_arcgis, untested": sorted(readers - covered),
                                "tested, reads no query_arcgis": sorted(covered - readers)}


@pytest.mark.parametrize("path", list(ARCGIS_ROUTES.values()), ids=list(ARCGIS_ROUTES))
def test_c1_control_a_feature_set_is_served_as_data(energy, arcgis, path):
    _eir, _app, get = energy
    requested = arcgis(200, {"features": [FEATURE], "exceededTransferLimit": False})
    got_status, got = get(path)
    assert requested, f"{path} never queried ArcGIS"
    assert got_status == 200 and got["success"] is True, (got_status, got)
    assert got["count"] == 1 and got["data"] == [FEATURE], got


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
def test_c2_control_a_swallowed_attempt_inside_a_request_is_recorded(energy, monkeypatch, attempt):
    """Anti-vacuity for R, in its harness: when the view R requests makes an attempt
    and swallows it, the attempt is recorded, although the response is a plain 200.

    The view is swapped in app.view_functions rather than registered under a new
    URL rule: scripts/check_route_table_coherence.py reads the route literals in
    tests/ too, and reports a fixture path as a new route the edge does not serve."""
    _eir, app, get = energy

    def swallowing_view():
        attempt()
        return flask.jsonify(success=True)

    monkeypatch.setitem(app.view_functions, "get_substations", swallowing_view)
    attempts = _record_connections(monkeypatch)
    status, body = get(RETIRED["substations"][0])
    assert status == 200 and body == {"success": True}, body
    assert attempts, "nothing was recorded, so R could not catch a live query"
