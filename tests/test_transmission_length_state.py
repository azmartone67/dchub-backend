"""Guard: transmission_lines rows carry a TRUE length_miles and a state. 2026-09-23.

WHY
───
Measured read-only in prod 2026-09-23: all 94,619 transmission_lines rows had
state NULL and length_miles 0. The only reader of those columns,
land_power_crawler's market profile (`COUNT(*), SUM(length_miles) ... WHERE
state = %s`), so published transmission_line_count = 0 and
total_transmission_miles = 0 for every state. The weekly EIA ingest asked for
returnGeometry=false and never set either column; the one-shot backfill that
once filled them was wiped by every Monday replace.

The EIA layer's Shape__Length is Web Mercator metres — measured against the
true length on a live page of 2,000 lines, median 1.255x, max 1.532x — so it
cannot be used raw. The runner now fetches EPSG:4326 geometry and measures it
(util/polyline_geometry.py). Full live layer, 2026-09-23: 94,619/94,619
lengths, 94,617/94,619 states, 591,711 miles; longest line 844.146 mi
(Celilo-Sylmar, the Pacific DC Intertie, ~846 mi published).

WHAT THIS PINS
──────────────
  G1. Segment length agrees with Karney's geodesic (pyproj, WGS84) — reference
      values recorded below, so the suite needs no pyproj.
  G2. State is the state of the point HALFWAY ALONG the line, with a
      nearest-vertex-along-the-line fallback when that point is in water, ''
      for a line in no US area, and a raise (never NULL) when the boundary
      dataset is missing.
  R1. The route carries fields 9 and 10 into the length_miles and state
      columns of the INSERT — the columns, not just the tuple.
  R2. Bad values coerce to NULL, never 0; an old 8-field row coerces to NULL.
  R3. A full-replace where under MEASURED_FLOOR of rows carry both is refused
      BEFORE a connection opens (the old rows survive).
  C1. The runner's row is exactly the route's row shape, field for field, and
      it asks the service for geometry in EPSG:4326.
"""
import gzip
import importlib.util
import json
import math
import os
import sys
import urllib.parse

import pytest

flask = pytest.importorskip("flask")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.transmission_ingest as tx  # noqa: E402
from util import polyline_geometry as pg  # noqa: E402
from util.state_polygons import state_containing  # noqa: E402

ADMIN_KEY = "test-admin-key"

# Columns of transmission_lines as measured in prod 2026-09-23
# (information_schema.columns, ordinal order).
LIVE_COLUMNS = ["id", "hifld_id", "name", "operator", "voltage_kv", "from_sub",
                "to_sub", "length_miles", "state", "status", "line_type",
                "source", "last_updated", "created_at"]


def _load_infra_fetch():
    spec = importlib.util.spec_from_file_location(
        "infra_fetch_under_test", os.path.join(ROOT, "tools", "infra_fetch.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── G1: length ───────────────────────────────────────────────────────────────

# (lng1, lat1, lng2, lat2) -> metres, pyproj.Geod(ellps="WGS84").line_length,
# computed 2026-09-23.
KARNEY = [
    ((-97.7431, 30.2672, -96.7970, 32.7767), 292395.395),   # Austin -> Dallas
    ((-150.0, 70.0, -149.0, 70.2), 44068.743),               # North Slope, AK
    ((-77.49, 39.04, -77.0, 38.9), 45219.614),               # Ashburn -> DC
    ((0.0, 0.0, 0.0, 1.0), 110574.389),                      # 1 deg of meridian
    ((0.0, 0.0, 1.0, 0.0), 111319.491),                      # 1 deg of equator
]


@pytest.mark.parametrize("seg,metres", KARNEY)
def test_segment_length_matches_the_geodesic(seg, metres):
    got = pg._segment_m(*seg)
    assert abs(got - metres) / metres < 5e-5, (seg, got, metres)


def test_length_is_not_the_web_mercator_length():
    # 69.5 N, the North Slope: Mercator inflates by sec(lat) ~ 2.86x here.
    miles, _ = pg.measure_line([[[-150.0, 69.5], [-149.0, 69.5]]])
    true_miles = 2 * math.pi * 6378137.0 / 360 * math.cos(math.radians(69.5)) / 1609.344
    assert abs(miles - true_miles) / true_miles < 5e-3, (miles, true_miles)


def test_a_multipart_line_does_not_count_the_gap_between_parts():
    # One short piece in Austin, one in Dallas: ~6.4 mi of line, ~180 mi apart.
    miles, _ = pg.measure_line([[[-97.74, 30.27], [-97.70, 30.30]],
                                [[-96.80, 32.78], [-96.75, 32.80]]])
    assert 5 < miles < 8, miles


def test_no_usable_vertex_is_unmeasured_not_zero():
    assert pg.measure_line(None) == (None, None)
    assert pg.measure_line([[["x", "y"]], []]) == (None, None)


# ── G2: state ────────────────────────────────────────────────────────────────

def test_state_is_the_halfway_point_not_the_first_vertex():
    line = [[[-77.20, 39.10], [-77.49, 39.04], [-78.48, 38.03]]]
    assert state_containing(39.10, -77.20) == "MD"          # first vertex
    assert pg.measure_line(line)[1] == "VA"


def test_a_water_midpoint_takes_the_nearest_vertex_along_the_line():
    # Norwalk CT -> across Long Island Sound -> Northport NY. The CT vertex is
    # first; the NY vertex is nearer the halfway point along the line.
    line = [[[-73.41, 41.10], [-73.37, 40.90], [-73.36, 40.86]]]
    walked, total = pg._walk(pg._clean_paths(line))
    mid_lng, mid_lat = pg._point_at(walked, total / 2)
    assert state_containing(mid_lat, mid_lng) == "", "fixture: midpoint must be in water"
    assert state_containing(41.10, -73.41) == "CT", "fixture: first vertex must be CT"
    assert pg.measure_line(line)[1] == "NY"


def test_a_line_in_no_us_area_is_an_empty_answer():
    assert pg.measure_line([[[-79.38, 43.65], [-79.30, 43.70]]])[1] == ""   # Toronto


def test_missing_boundaries_raise_instead_of_writing_null(monkeypatch):
    import util.state_polygons as sp
    monkeypatch.setattr(sp, "state_containing", lambda lat, lng: None)
    with pytest.raises(pg.StateBoundariesUnavailable):
        pg.measure_line([[[-97.74, 30.27], [-97.70, 30.30]]])


# ── R2: coercion ─────────────────────────────────────────────────────────────

_BASE = ["100001", "ONCOR", "ONCOR", 345, "A", "B", "IN SERVICE", "AC; OVERHEAD"]


def test_coerce_carries_length_and_state():
    r = tx._coerce_body_row(_BASE + [12.3456, "TX"])
    assert r[8] == 12.346 and r[9] == "TX"
    assert len(r) == len(tx._ROW_FIELDS)


def test_an_old_eight_field_row_is_null_not_zero():
    r = tx._coerce_body_row(list(_BASE))
    assert r[8] is None and r[9] is None


@pytest.mark.parametrize("miles", [-1, float("nan"), "inf", True, "long", None])
def test_bad_length_is_null(miles):
    assert tx._coerce_body_row(_BASE + [miles, "TX"])[8] is None


@pytest.mark.parametrize("state,want", [("tx", "TX"), (" va ", "VA"), ("Texas", None),
                                        ("T1", None), ("", None), (None, None)],
                         ids=["lower", "padded", "full-name", "digit", "empty", "none"])
def test_state_coercion(state, want):
    assert tx._coerce_body_row(_BASE + [1.0, state])[9] == want


# ── R1 / R3: the handler ─────────────────────────────────────────────────────

class _ConnectWasCalled(AssertionError):
    pass


class _FakeCursor:
    def __init__(self, log):
        self.log = log
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        self.log.append((sql, params))

    def fetchall(self):
        assert "information_schema.columns" in self._last
        return [(c,) for c in LIVE_COLUMNS]

    def mogrify(self, ph, values):
        # As strict as psycopg2 on arity: a placeholder/value mismatch raises.
        if ph.count("%s") != len(values):
            raise TypeError("placeholders %d != values %d" % (ph.count("%s"), len(values)))
        self.log.append(("MOGRIFY", tuple(values)))
        return repr(tuple(values)).encode()


class _FakeConn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self.log)

    def commit(self):
        self.log.append(("COMMIT", None))


def _post(rows, connect):
    app = flask.Flask(__name__)
    real = tx.psycopg2.connect
    tx.psycopg2.connect = connect
    try:
        body = gzip.compress(json.dumps({"rows": rows}).encode())
        with app.test_request_context(
                "/api/v1/admin/ingest/transmission-lines", method="POST", data=body,
                headers={"X-Admin-Key": ADMIN_KEY, "Content-Type": "application/json",
                         "Content-Encoding": "gzip"}):
            res = tx.ingest_transmission_lines()
    finally:
        tx.psycopg2.connect = real
    resp, status = (res if isinstance(res, tuple) else (res, 200))
    return status, resp.get_json()


def _spy(*_a, **_kw):
    raise _ConnectWasCalled("connected on a payload that must be refused")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql://stub/stub")


def test_length_and_state_land_in_their_columns():
    states = ["TX", "VA", "CA", "NY"]
    rows = [[str(900000 + i), "OWNER %d" % i, "OWNER %d" % i, 138, "S%d" % i,
             "T%d" % i, "IN SERVICE", "AC; OVERHEAD", round(1.5 + i, 3), states[i % 4]]
            for i in range(1203)]           # > 2 batches of 500
    log = []
    status, body = _post(rows, lambda *a, **k: _FakeConn(log))
    assert status == 200 and body["inserted"] == 1203, body

    inserts = [sql for sql, _ in log if isinstance(sql, str) and sql.startswith("INSERT")]
    assert len(inserts) == 3
    collist = inserts[0].split("(", 1)[1].split(")", 1)[0].split(",")
    assert "length_miles" in collist and "state" in collist, collist
    values = [v for tag, v in log if tag == "MOGRIFY"]
    assert len(values) == 1203
    by_id = {v[collist.index("hifld_id")]: v for v in values}
    for r in rows:
        v = by_id[r[0]]
        assert v[collist.index("length_miles")] == r[8]
        assert v[collist.index("state")] == r[9]
        assert v[collist.index("voltage_kv")] == 138.0
    assert ("COMMIT", None) in log


def test_an_unmeasured_layer_is_refused_before_connecting():
    rows = [[str(i)] + _BASE[1:] for i in range(100)]   # old 8-field rows
    status, body = _post(rows, _spy)
    assert status == 400 and "refused full-replace" in body["error"], body


def test_the_floor_boundary():
    n = 100
    below = int(n * tx.MEASURED_FLOOR) - 1
    rows = [[str(i)] + _BASE[1:] + ([2.0, "TX"] if i < below else [2.0, None])
            for i in range(n)]
    status, _ = _post(rows, _spy)
    assert status == 400, "a layer below the floor was not refused"

    at = int(math.ceil(n * tx.MEASURED_FLOOR))
    rows = [[str(i)] + _BASE[1:] + ([2.0, "TX"] if i < at else [2.0, None])
            for i in range(n)]
    log = []
    status, body = _post(rows, lambda *a, **k: _FakeConn(log))
    assert status == 200 and body["inserted"] == n, body


# ── C1: runner <-> route ─────────────────────────────────────────────────────

def test_the_runner_row_is_the_route_row_and_asks_for_4326(monkeypatch):
    infra = _load_infra_fetch()
    seen = []
    page = {"features": [{
        "attributes": {"ID": "300042", "OWNER": "ONCOR", "VOLTAGE": 345,
                       "SUB_1": "A", "SUB_2": "B", "STATUS": "IN SERVICE",
                       "TYPE": "AC; OVERHEAD"},
        "geometry": {"paths": [[[-97.7431, 30.2672], [-96.7970, 32.7767]]]},
    }]}

    def fake_get(url, *a, **k):
        seen.append(url)
        return json.dumps(page).encode()

    monkeypatch.setattr(infra, "_get", fake_get)
    rows = infra.fetch_transmission_lines(10)

    q = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
    assert q["returnGeometry"] == ["true"] and q["outSR"] == ["4326"], q
    assert len(rows) == 1 and len(rows[0]) == len(tx._ROW_FIELDS)
    row = rows[0]
    assert abs(row[8] - 292395.395 / 1609.344) < 0.01 and row[9] == "TX", row
    coerced = tx._coerce_body_row(json.loads(json.dumps(row)))
    assert list(coerced) == [row[0], row[1], row[2], 345.0, row[4], row[5],
                             row[6], row[7], row[8], row[9]], coerced


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
