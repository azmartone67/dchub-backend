"""shell#41 WS5 (2026-07-29) — cross-layer site discovery + the floodplain
false-negative it had to kill first.

These are BEHAVIOUR tests, not grep tests. The functions under test are pulled
out of the shipped source with `ast` and executed against stubs, so a comment
mentioning the right words cannot satisfy them (a comment produces no AST node),
and no test here imports main.py, Flask app state, or the database.

★ Every extraction asserts it actually found and compiled a real function body.
An empty parse passing all assertions is the filed AST-guard trap: a guard that
silently tests nothing is worse than no guard.

EXPECTED, unpatched (origin/main as of 1f0c8be6):
  test_fema_base_is_the_live_endpoint                          FAIL
  test_empty_feature_set_is_unmapped_not_zone_x                FAIL
  test_missing_sfha_flag_is_not_read_as_outside                FAIL
  test_cross_layer_module_exists                               FAIL (no module)
  test_flood_helper_fails_closed_on_no_coverage                FAIL (no module)
  test_scope_is_mandatory                                      FAIL (no module)
  test_voltage_basis_never_launders_a_name_regex               FAIL (no module)
  test_no_headroom_filter_is_offered                           FAIL (no module)
  test_synthetic_fiber_routes_layer_is_not_used                FAIL (no module)
  test_geo_indexes_target_live_column_names                    FAIL
  test_grid_headroom_does_not_default_state_to_colorado        FAIL
  test_extraction_canary_actually_compiled_something           PASS  (control)
  → 11 failed, 1 passed

EXPECTED, patched: 12 passed, 0 failed.
The canary is the MUST-PASS control: if it is the only green line the suite ran;
if it is red the harness itself is broken and the other results mean nothing.
"""

import ast
import pathlib


def _root():
    return pathlib.Path(__file__).resolve().parent.parent


def _parse(relpath):
    """→ (tree, source). Asserts the file exists AND produced a non-empty
    module body — never let an empty/failed parse silently pass everything."""
    p = _root() / relpath
    assert p.exists(), "missing source file: %s" % relpath
    src = p.read_text()
    tree = ast.parse(src)
    assert isinstance(tree, ast.Module) and len(tree.body) > 0, \
        "%s parsed to an EMPTY module — the guard would pass vacuously" % relpath
    return tree, src


def _extract(relpath, names, namespace=None):
    """Compile the named top-level functions out of `relpath` into a namespace.
    Asserts every requested name was found and is callable afterwards."""
    tree, _ = _parse(relpath)
    wanted = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in names]
    found = {n.name for n in wanted}
    missing = set(names) - found
    assert not missing, "not found in %s: %s" % (relpath, sorted(missing))
    for fn in wanted:
        assert fn.body, "%s.%s has an EMPTY body" % (relpath, fn.name)
    ns = dict(namespace or {})
    mod = ast.Module(body=wanted, type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod), relpath, "exec"), ns)
    for n in names:
        assert callable(ns.get(n)), "%s did not compile to a callable" % n
    return ns


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeRequests:
    """Stands in for `requests`. Records the URL so a test can assert which
    FEMA host the shipped code actually calls."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload if payload is not None else {"features": []}
        self.calls = []

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append({"url": url, "params": params})
        return _FakeResponse(self.status_code, self.payload)


# ── the control ────────────────────────────────────────────────────────────
def test_extraction_canary_actually_compiled_something():
    """MUST-PASS control. Proves the harness runs and the AST extractor really
    produces executable code — so a green suite is evidence, not silence."""
    ns = _extract("risk_assessment_api.py", ["get_flood_zone_description"])
    assert ns["get_flood_zone_description"]("AE").startswith("High risk")
    assert ns["get_flood_zone_description"]("D").startswith("Undetermined")


# ── FEMA: the false-negative machine ───────────────────────────────────────
def test_fema_base_is_the_live_endpoint():
    """The old base hazards.fema.gov/gis/nfhl/... returns HTTP 404 (WebSEAL),
    which is why /api/risk/flood-zone answered 'FEMA API unavailable' for every
    point. Assert the CALLED url, not the constant — a stale constant with a
    correct-looking comment would still be dead."""
    fake = _FakeRequests(200, {"features": []})
    tree, _ = _parse("risk_assessment_api.py")
    base = None
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "FEMA_BASE" for t in node.targets)
                and isinstance(node.value, ast.Constant)):
            base = node.value.value
    assert base, "FEMA_BASE not found as a module-level string"
    ns = _extract("risk_assessment_api.py",
                  ["get_fema_flood_zone", "get_flood_zone_description"],
                  {"requests": fake, "FEMA_BASE": base})
    ns["get_fema_flood_zone"](39.0437, -77.4875)
    url = fake.calls[0]["url"]
    assert "/gis/nfhl/rest/services" not in url, \
        "still pointed at the dead WebSEAL base: %s" % url
    assert url.startswith("https://hazards.fema.gov/arcgis/rest/services"), url
    assert "/public/NFHL/MapServer/28/query" in url, url


def test_empty_feature_set_is_unmapped_not_zone_x():
    """★★ THE WS5 HEADLINE. FEMA answers HTTP 200 with features == [] for any
    point outside NFHL coverage — verified live for Alaska interior
    (63.5, -149.5) and for IRELAND (53.0, -8.0). The shipped code turned that
    into flood_zone 'X' / special_flood_hazard_area False / flood_risk 'low',
    i.e. it certified an Irish site as outside the US 100-year floodplain.
    'No coverage' must never render as 'no hazard'."""
    fake = _FakeRequests(200, {"features": []})
    ns = _extract("risk_assessment_api.py",
                  ["get_fema_flood_zone", "get_flood_zone_description"],
                  {"requests": fake, "FEMA_BASE": "https://x/"})
    out = ns["get_fema_flood_zone"](53.0, -8.0)          # Ireland
    assert out.get("flood_zone") != "X", \
        "no-coverage was rendered as zone X — the false negative is back"
    assert out.get("flood_risk") != "low", \
        "no-coverage was rendered as low flood risk"
    assert out.get("special_flood_hazard_area") is not False, \
        "no-coverage answered a hard False for SFHA; it must be None/unknown"
    assert out.get("coverage") == "unmapped", out


def test_missing_sfha_flag_is_not_read_as_outside():
    """The old code did attrs.get('SFHA_TF', 'F') == 'T' — a MISSING flag read
    as 'not in the floodplain'. Absent evidence is not negative evidence."""
    fake = _FakeRequests(200, {"features": [{"attributes": {"FLD_ZONE": "AE"}}]})
    ns = _extract("risk_assessment_api.py",
                  ["get_fema_flood_zone", "get_flood_zone_description"],
                  {"requests": fake, "FEMA_BASE": "https://x/"})
    out = ns["get_fema_flood_zone"](39.0, -77.0)
    assert out.get("special_flood_hazard_area") is not False, out
    assert out.get("coverage") == "undetermined", out

    # Zone D = "no analysis performed" — undetermined, not safe.
    fake_d = _FakeRequests(200, {"features": [
        {"attributes": {"FLD_ZONE": "D", "SFHA_TF": "F"}}]})
    ns_d = _extract("risk_assessment_api.py",
                    ["get_fema_flood_zone", "get_flood_zone_description"],
                    {"requests": fake_d, "FEMA_BASE": "https://x/"})
    out_d = ns_d["get_fema_flood_zone"](47.9, -100.0)
    assert out_d.get("coverage") == "undetermined", out_d

    # A genuine outside-SFHA answer must still come through cleanly.
    fake_x = _FakeRequests(200, {"features": [
        {"attributes": {"FLD_ZONE": "X", "SFHA_TF": "F",
                        "ZONE_SUBTY": "AREA OF MINIMAL FLOOD HAZARD"}}]})
    ns_x = _extract("risk_assessment_api.py",
                    ["get_fema_flood_zone", "get_flood_zone_description"],
                    {"requests": fake_x, "FEMA_BASE": "https://x/"})
    out_x = ns_x["get_fema_flood_zone"](39.0437, -77.4875)
    assert out_x.get("coverage") == "outside_sfha", out_x
    assert out_x.get("special_flood_hazard_area") is False, out_x


# ── the new surface ────────────────────────────────────────────────────────
def test_cross_layer_module_exists():
    tree, src = _parse("routes/cross_layer_sites.py")
    routes = [s for s in ast.walk(tree)
              if isinstance(s, ast.Constant)
              and s.value == "/api/v1/sites/cross-layer"]
    assert routes, "the route path is not declared in the module"


def test_flood_helper_fails_closed_on_no_coverage():
    """The discovery surface's own FEMA read must be three-state and must never
    let 'unmapped' or a transient failure satisfy an exclude-floodplain filter."""
    fake = _FakeRequests(200, {"features": []})
    ns = _extract("routes/cross_layer_sites.py", ["_flood_status"],
                  {"time": __import__("time"), "_requests": fake,
                   "_FLOOD_CACHE": {}, "_FLOOD_CACHE_TTL": 0,
                   "_FLOOD_CACHE_MAX": 0})
    assert ns["_flood_status"](53.0, -8.0)["status"] == "unmapped"

    fake_sfha = _FakeRequests(200, {"features": [
        {"attributes": {"FLD_ZONE": "AE", "SFHA_TF": "T"}}]})
    ns2 = _extract("routes/cross_layer_sites.py", ["_flood_status"],
                   {"time": __import__("time"), "_requests": fake_sfha,
                    "_FLOOD_CACHE": {}, "_FLOOD_CACHE_TTL": 0,
                    "_FLOOD_CACHE_MAX": 0})
    assert ns2["_flood_status"](29.0, -95.0)["status"] == "in_sfha"

    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("network down")

    ns3 = _extract("routes/cross_layer_sites.py", ["_flood_status"],
                   {"time": __import__("time"), "_requests": _Boom(),
                    "_FLOOD_CACHE": {}, "_FLOOD_CACHE_TTL": 0,
                    "_FLOOD_CACHE_MAX": 0})
    st = ns3["_flood_status"](39.0, -77.0)["status"]
    assert st == "unknown", st
    assert st != "outside_sfha", "a transient failure must never read as safe"


def test_scope_is_mandatory():
    """An unscoped variant over a 126,840-row and a 2,640,850-row table is the
    sitemap-stampede incident again. No scope → an error, never a scan."""
    ns = _extract("routes/cross_layer_sites.py",
                  ["_parse_scope"], {"_MAX_RADIUS_KM": 250.0})
    scope, err = ns["_parse_scope"]({})
    assert scope is None and err, "an unscoped request was accepted"

    scope, err = ns["_parse_scope"]({"lat": "39.0437", "lon": "-77.4875",
                                     "radius_km": "50"})
    assert err is None and scope["kind"] == "point", (scope, err)

    # a runaway radius or a continent-sized bbox is refused, not clamped silently
    _, err = ns["_parse_scope"]({"lat": "39", "lon": "-77", "radius_km": "5000"})
    assert err, "an unbounded radius was accepted"
    _, err = ns["_parse_scope"]({"bbox": "-80,-170,80,170"})
    assert err, "a continent-sized bbox was accepted"


def test_voltage_basis_never_launders_a_name_regex():
    """HIFLD stores voltage 0 even for a substation named 'Ashburn 500kV', so a
    name-parsed kV is legitimate — but it must never travel as a column value."""
    ns = _extract("routes/cross_layer_sites.py", ["_voltage", "_num"],
                  {"_KV_IN_NAME": __import__("re").compile(r"(\d{2,3})\s*kv",
                                                           __import__("re").I)})
    assert ns["_voltage"](500.0, 0, "Ashburn") == (500.0, "column")
    assert ns["_voltage"](0, 0, "ASHBURN 500KV") == (500.0, "name_regex")
    assert ns["_voltage"](0, 0, "Nameless Sub") == (None, "unknown")


def test_no_headroom_filter_is_offered():
    """WS6 (aa3b4b92) already ruled: no measured MW headroom exists. Assert the
    route accepts no headroom parameter — an AST check, so a comment saying the
    right thing cannot satisfy it."""
    tree, _ = _parse("routes/cross_layer_sites.py")
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for banned in ("min_headroom_mw", "max_headroom_mw", "headroom_mva"):
        assert banned not in literals, \
            "%s is offered as a parameter but nothing measures it" % banned
    # ...and the hole is DECLARED, not silently omitted.
    assert "headroom_mw" in literals, \
        "constraint_coverage must name headroom_mw as unavailable"


def test_synthetic_fiber_routes_layer_is_not_used():
    """320 of a live 500-row fiber_routes sample are route_type='metro_inferred'
    with source_id 'synth-…' — a drawn line between two PeeringDB facilities.
    It must never back a distance claim; fcc_fiber_hex must."""
    tree, _ = _parse("routes/cross_layer_sites.py")
    sql = " ".join(n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str))
    assert "FROM fiber_routes" not in sql, "the synthetic fiber layer is queried"
    assert "FROM fcc_fiber_hex" in sql, "the real fiber coverage layer is not queried"
    assert "FROM transmission_lines" not in sql, \
        "transmission_lines has no coordinate columns — it cannot back a distance"


# ── the silently-failing DDL ───────────────────────────────────────────────
def test_geo_indexes_target_live_column_names():
    """The geo CREATE INDEX statements named latitude/longitude on tables whose
    live columns are lat/lng — inside a swallowing try/except, so they failed
    silently for months. Read the SQL out of the AST, not the comments."""
    tree, _ = _parse("add_performance_indexes.py")
    stmts = []
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "indexes" for t in node.targets)):
            for s in ast.walk(node.value):
                if isinstance(s, ast.Constant) and isinstance(s.value, str):
                    stmts.append(s.value)
    assert stmts, "could not read the `indexes` list out of the AST"
    joined = " ; ".join(stmts)
    for bad in ("substations (latitude", "gas_pipelines (latitude",
                "discovered_power_plants (latitude"):
        assert bad not in joined, \
            "%s names columns that do not exist live (lat/lng)" % bad
    assert "ON substations (lat, lng)" in joined
    assert "ON gas_pipelines (lat, lng)" in joined
    assert "ON power_plants (lat, lon)" in joined, \
        "power_plants uses `lon`, not `lng` or `longitude`"
    assert "ON fcc_fiber_hex (lat, lng)" in joined
    assert "ON carrier_facility_presence (facility_lat, facility_lng)" in joined


# ── the fabricated published field ─────────────────────────────────────────
def test_grid_headroom_does_not_default_state_to_colorado():
    """nlr_intelligence.grid_headroom published location.state = 'CO' for every
    point that omitted ?state= — live-probed wrong for Ashburn VA, Austin TX and
    Portland OR. AST walk of the FUNCTION, so a comment quoting the old line
    (there is one, deliberately) cannot satisfy or break this."""
    tree, _ = _parse("nlr_intelligence.py")
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "grid_headroom"), None)
    assert fn is not None and fn.body, "grid_headroom not found in nlr_intelligence.py"
    for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
        f = call.func
        if (isinstance(f, ast.Attribute) and f.attr == "get"
                and isinstance(f.value, ast.Attribute) and f.value.attr == "args"):
            consts = [a.value for a in call.args
                      if isinstance(a, ast.Constant)]
            if "state" in consts:
                assert "CO" not in consts, \
                    "location.state still defaults to Colorado"
