"""Guards for /api/v1/water/stress location filtering.

2026-08-07: the endpoint accepted lat/lon and discarded them. Two independent
faults stacked:

  1. NAME MISMATCH. The Flask route read `lng`; `server.mjs` (get_water_risk)
     declares and sends `lon`. Every MCP water call therefore missed the
     location branch entirely and fell into `ORDER BY state LIMIT 50`, which
     returns Arizona first for every point on earth. Ashburn VA came back as
     34 AZ + 16 CA wells, nearest 3,058 km, with no distance field.
  2. UNBOUNDED NEAREST. Even on the `lng` path, "distance" was Manhattan
     distance in raw degrees with no radius bound, so Frankfurt — which has no
     USGS coverage at all — got the arg-min of that: ten New Jersey wells
     ~6,400 km away, served as if they were an answer.

These tests pin the three properties that make the endpoint honest:
  - both spellings of longitude reach the location branch (guards fault 1),
  - the location SQL carries a real distance predicate AND a radius bound
    (guards fault 2),
  - the envelope always states that this is observed station data rather than
    a modelled index, and never claims a distance it does not have.

The route body is a Flask closure, so the testable logic lives in module-level
helpers (_water_point_sql, _water_float_arg, _water_envelope, ...) and those
are exercised directly. The closure itself is checked by source inspection.

The module is loaded from its path under a synthetic name so the `routes`
package __init__ — and through it main.py — is never imported.
"""
import ast
import importlib.util
import math
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(ROOT, "routes", "api_integration_wiring.py")
_SERVER_MJS = os.path.join(ROOT, "server.mjs")

pytestmark = pytest.mark.skipif(
    not os.path.exists(_PATH), reason="api_integration_wiring.py not in this tree")


def _mod():
    spec = importlib.util.spec_from_file_location("_api_int_wiring", _PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _source():
    with open(_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def _route_source():
    """Source of the _register_water_route function only."""
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_register_water_route":
            return ast.get_source_segment(_source(), node)
    raise AssertionError("_register_water_route not found")


class _Args(dict):
    """Minimal stand-in for request.args."""
    def get(self, k, default=None, type=None):  # noqa: A002
        v = dict.get(self, k, default)
        if type is not None and v is not None:
            try:
                return type(v)
            except (TypeError, ValueError):
                return default
        return v


# ── fault 1: the parameter name ────────────────────────────────────────────

def test_longitude_accepted_as_lon_not_only_lng():
    """The exact bug. server.mjs sends `lon`; the route read `lng`."""
    m = _mod()
    val, name, err = m._water_float_arg(_Args({"lon": "-77.49"}), m.WATER_LON_PARAMS)
    assert err is None
    assert val == pytest.approx(-77.49)
    assert name == "lon"


@pytest.mark.parametrize("name", ["lng", "lon", "long", "longitude"])
def test_every_documented_longitude_spelling_parses(name):
    m = _mod()
    val, used, err = m._water_float_arg(_Args({name: "8.68"}), m.WATER_LON_PARAMS)
    assert err is None and used == name and val == pytest.approx(8.68)


@pytest.mark.parametrize("name", ["lat", "latitude"])
def test_every_documented_latitude_spelling_parses(name):
    m = _mod()
    val, used, err = m._water_float_arg(_Args({name: "39.04"}), m.WATER_LAT_PARAMS)
    assert err is None and used == name and val == pytest.approx(39.04)


def test_lon_is_in_the_advertised_accepted_set():
    m = _mod()
    assert "lon" in m.WATER_LON_PARAMS and "lng" in m.WATER_LON_PARAMS


def test_unparseable_coordinate_is_an_error_not_a_silent_miss():
    """A garbage lat must not read as 'no lat supplied' — that is precisely
    how the original miss stayed invisible."""
    m = _mod()
    val, name, err = m._water_float_arg(_Args({"lat": "north"}), m.WATER_LAT_PARAMS)
    assert val is None and name == "lat" and err


def test_mcp_declared_param_names_are_all_accepted_by_the_route():
    """server.mjs's get_water_risk arg names must be a subset of what the
    route accepts. This is the cross-repo edge the bug lived on."""
    if not os.path.exists(_SERVER_MJS):
        pytest.skip("server.mjs not in this tree")
    with open(_SERVER_MJS, "r", encoding="utf-8") as fh:
        js = fh.read()
    idx = js.find("'get_water_risk'")
    assert idx != -1, "get_water_risk tool not found in server.mjs"
    decl = js[idx:idx + 400]
    m = _mod()
    accepted = set(m.WATER_LAT_PARAMS) | set(m.WATER_LON_PARAMS) | {
        "state", "radius_km", "radius", "limit"}
    # arg names appear as `{ lat: N, lon: N, state: S }`
    brace = decl[decl.find("{"):decl.find("}") + 1]
    declared = {p.split(":")[0].strip() for p in brace.strip("{}").split(",") if ":" in p}
    assert declared, "could not parse get_water_risk arg names from server.mjs"
    assert declared <= accepted, (
        "server.mjs sends %s which /api/v1/water/stress does not accept"
        % sorted(declared - accepted))


# ── fault 2: the distance predicate and the radius bound ───────────────────

def test_point_sql_has_a_real_distance_expression():
    m = _mod()
    sql, _ = m._water_point_sql(39.04, -77.49, 150.0, 25)
    low = sql.lower()
    assert "asin" in low and "radians" in low, "no haversine in the location SQL"
    assert "distance_km" in low
    # Manhattan-degrees was the old, wrong metric.
    assert "abs(latitude" not in low.replace(" ", "")


def test_point_sql_bounds_the_result_by_radius():
    """Without this predicate the query returns the nearest row on earth."""
    m = _mod()
    sql, params = m._water_point_sql(50.11, 8.68, 150.0, 25)
    assert "distance_km <= %(radius)s" in sql, "no radius bound in the location SQL"
    assert params["radius"] == pytest.approx(150.0)


def test_point_sql_binds_the_query_point_not_a_constant():
    m = _mod()
    _, params = m._water_point_sql(39.04, -77.49, 150.0, 25)
    assert params["qlat"] == pytest.approx(39.04)
    assert params["qlon"] == pytest.approx(-77.49)


def test_bounding_box_actually_contains_the_radius():
    """A box narrower than the radius would drop real stations before the
    exact distance test ever sees them.

    The strict case is the POLEWARD corner: at lat+dlat, a degree of longitude
    is shorter than at the centre latitude, so a box sized on cos(centre) is
    too narrow up there. Checked at Fairbanks/Prudhoe latitudes where the
    error is largest.
    """
    m = _mod()
    for lat in (0.0, 39.04, 61.2, 64.8, 71.3):
        dlat, dlon = m._water_bbox_deltas(lat, 150.0)
        # A point due north at exactly 150 km must be inside the lat band.
        assert dlat >= 150.0 / 111.32 - 1e-9
        # A point 150 km east of the box's poleward edge must still be inside.
        edge = min(abs(lat) + dlat, 90.0)
        east_deg = 150.0 / (111.320 * max(math.cos(math.radians(edge)), 1e-9))
        assert dlon >= min(east_deg, 180.0) - 1e-9, (
            "bbox too narrow at the poleward edge of lat=%s" % lat)


def test_bounding_box_widens_toward_the_poles():
    m = _mod()
    _, dlon_equator = m._water_bbox_deltas(0.0, 150.0)
    _, dlon_alaska = m._water_bbox_deltas(64.8, 150.0)
    assert dlon_alaska > dlon_equator


def test_antimeridian_drops_the_longitude_box_rather_than_wrapping_wrong():
    """Aleutian longitudes wrap past 180; a naive BETWEEN silently matches
    nothing. Dropping the box keeps the exact distance predicate in charge."""
    m = _mod()
    sql, params = m._water_point_sql(52.0, 179.6, 150.0, 25)
    assert "lon_min" not in params
    assert "distance_km <= %(radius)s" in sql


def test_radius_is_capped_in_the_route():
    src = _route_source()
    assert "WATER_RADIUS_MAX_KM" in src, "radius is not capped"


def test_route_has_no_unfiltered_fallback_branch():
    """The branch that produced the Arizona rows. Any query the route cannot
    answer for the caller's location must return zero rows, never a sample."""
    src = _route_source()
    flat = " ".join(src.split()).lower()
    assert "order by state limit 50" not in flat
    assert "from usgs_water_stress order by" not in flat, (
        "an unfiltered ORDER BY over the whole table is back")


def test_route_never_selects_without_a_location_predicate():
    """Every SELECT against usgs_water_stress in the route must be bounded by
    either the query point or an explicit state — except the single nearest-
    station lookup, whose only job is to report a distance in the limitation
    string when the answer is already empty."""
    src = _route_source()
    tree = ast.parse(src)
    selects = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and "usgs_water_stress" in node.value:
            selects.append(" ".join(node.value.split()).lower())
    assert selects, "no query against usgs_water_stress found in the route"
    for q in selects:
        bounded = ("upper(state) = %(st)s" in q) or ("order by distance_km asc limit 1" in q)
        assert bounded, "unbounded query against usgs_water_stress: %s" % q


# ── requirement 3: label what it is ────────────────────────────────────────

def test_envelope_states_this_is_observed_data_not_a_modelled_index():
    m = _mod()
    env = m._water_envelope([], {"lat": 50.11, "lon": 8.68}, "nothing nearby")
    assert env["is_modelled_index"] is False
    assert env["measurement"] == "observed_station_readings"
    assert "not a modelled water-stress index" in env["measurement_note"].lower()
    assert env["source"] == "USGS"


def test_empty_result_carries_a_reason():
    m = _mod()
    env = m._water_envelope([], {"lat": 50.11, "lon": 8.68}, "No USGS station within 150 km")
    assert env["count"] == 0
    assert env["data"] == []
    assert env["limitation"]


def test_rows_carry_distance_and_reading_age():
    m = _mod()
    row = m._water_row({
        "site_id": "312642110063701", "state": "AZ",
        "latitude": 31.445, "longitude": -110.111,
        "water_level_ft": 15.82, "water_level_date": "2025-07-02",
        "distance_km": 3058.4,
    })
    assert row["distance_km"] == pytest.approx(3058.4)
    assert row["reading_date"] == "2025-07-02"
    assert row["reading_age_days"] > 300
    assert row["measurement"] == "observed_station_readings"


def test_row_without_a_query_point_says_distance_is_unknown():
    """state= rows have no query point. distance_km must be present and null,
    not absent — an absent field reads as 'nearby' to a consumer."""
    m = _mod()
    row = m._water_row({"site_id": "x", "state": "AZ", "water_level_date": "2025-07-02"})
    assert "distance_km" in row and row["distance_km"] is None


def test_envelope_reports_the_age_span_of_what_it_returned():
    m = _mod()
    rows = [
        {"reading_date": "2025-05-06", "reading_age_days": 458},
        {"reading_date": "2025-11-17", "reading_age_days": 263},
    ]
    env = m._water_envelope(rows, {})
    assert env["reading_age"]["oldest_reading_date"] == "2025-05-06"
    assert env["reading_age"]["newest_reading_date"] == "2025-11-17"
    assert env["reading_age"]["max_age_days"] == 458


def test_undated_reading_reports_null_age_not_zero():
    m = _mod()
    assert m._water_age_days(None) is None
    assert m._water_age_days("not-a-date") is None
