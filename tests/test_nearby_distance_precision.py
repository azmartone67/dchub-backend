"""Nearby-facility distances follow the caller's coordinate precision.

routes.lp_sites._match_new_facilities feeds new_facilities_nearby on
/api/v1/lp/saved and on the get_changes portfolio overlay. For a caller whose
facility coordinates are rounded (util.facility_tier_gate.coord_dp_for_tier),
the served distance, the radius test and the nearest-first order must be
measured from the facility's ROUNDED point, so the answer is a function of
that point alone. The central assertion below is exactly that: two facilities
in the same rounded cell give byte-identical output for every saved point,
including points on the radius edge.

No Flask app import, no DB, no network (conftest harness rules): a bare Flask
request context, stub tier modules via monkeypatch, and a fake connection.
"""
import math
import types

import pytest
from flask import Flask

from routes import lp_sites
from routes.lp_sites import (
    DISTANCE_APPROX_STATUS,
    _haversine_km,
    _match_new_facilities,
    _nearby_precision,
    _served_km,
)

# Synthetic, nowhere real. Both round to (12.35, -45.68) at 2 dp.
A = {"name": "Example Facility", "state": "EX", "capacity_mw": 40,
     "latitude": 12.3456, "longitude": -45.6789}
A_SAME_CELL = dict(A, latitude=12.3549, longitude=-45.6751)
C = {"name": "Other Facility", "state": "EX", "capacity_mw": 12,
     "latitude": 12.4012, "longitude": -45.7188}          # -> (12.40, -45.72)
C_SAME_CELL = dict(C, latitude=12.3951, longitude=-45.7249)
ROUNDED_A = (12.35, -45.68)


def _site(lat, lon, sid=1):
    return {"id": sid, "latitude": lat, "longitude": lon}


def _offset(lat, lon, km_north, km_east):
    return (lat + km_north / 111.2,
            lon + km_east / (111.2 * math.cos(math.radians(lat))))


def _probe_sites():
    """Saved points a caller could choose: inside the cell, around it, and a
    fine sweep across the 50 km radius edge on four bearings."""
    pts = [ROUNDED_A, (A["latitude"], A["longitude"])]
    for n in range(-6, 7):
        for e in range(-6, 7):
            pts.append(_offset(*ROUNDED_A, n * 0.35, e * 0.35))
    for step in range(81):
        r = 48.0 + step * 0.05
        for bn, be in ((1, 0), (0, 1), (-0.6, 0.8), (0.8, -0.6)):
            pts.append(_offset(*ROUNDED_A, r * bn, r * be))
    return pts


def test_rounded_rung_is_a_function_of_the_rounded_point_alone():
    differs_exact = 0
    for lat, lon in _probe_sites():
        s = [_site(lat, lon)]
        one = _match_new_facilities(s, [A, C], dp=2)
        two = _match_new_facilities(s, [A_SAME_CELL, C_SAME_CELL], dp=2)
        assert one == two, (lat, lon, one, two)
        capped = _match_new_facilities(s, [A, C], dp=2, cap_per_site=1)
        assert capped == _match_new_facilities(
            s, [A_SAME_CELL, C_SAME_CELL], dp=2, cap_per_site=1), (lat, lon)
        if (_match_new_facilities(s, [A]) != _match_new_facilities(s, [A_SAME_CELL])):
            differs_exact += 1
    # Non-vacuous: the exact path DOES tell these two facilities apart, so the
    # equality above is the rounding at work, not two indistinguishable inputs.
    assert differs_exact > 100


def test_radius_edge_is_decided_by_the_rounded_point():
    exact_only = rounded_only = 0
    for step in range(81):
        r = 48.0 + step * 0.05
        lat, lon = _offset(*ROUNDED_A, r * 0.8, r * -0.6)
        s = [_site(lat, lon)]
        rounded_km = _haversine_km(lat, lon, *ROUNDED_A)
        exact_km = _haversine_km(lat, lon, A["latitude"], A["longitude"])
        got = _match_new_facilities(s, [A], dp=2)
        assert bool(got) == (rounded_km <= 50.0), (r, rounded_km, got)
        exact_only += exact_km <= 50.0 < rounded_km
        rounded_only += rounded_km <= 50.0 < exact_km
    assert exact_only + rounded_only > 0      # the sweep crosses a disputed band


def test_rounded_rung_serves_whole_km_floor_one_and_says_so():
    lat, lon = _offset(*ROUNDED_A, -4.6, 3.3)
    row = _match_new_facilities([_site(lat, lon)], [A], dp=2)[1][0]
    assert row["km"] == _served_km(_haversine_km(lat, lon, *ROUNDED_A))
    assert isinstance(row["km"], int)
    assert row["distance_status"] == DISTANCE_APPROX_STATUS == "approximate_1km"
    assert set(row) == {"name", "state", "capacity_mw", "km", "distance_status"}
    exact = round(_haversine_km(lat, lon, A["latitude"], A["longitude"]), 1)
    assert row["km"] != exact

    inside = _match_new_facilities([_site(*ROUNDED_A)], [A], dp=2)[1][0]
    assert inside["km"] == 1                   # never 0: a cell, not a point
    assert _served_km(0.0) == 1 and _served_km(1.49) == 1
    assert _served_km(1.5) == 2 and _served_km(2.5) == 3   # half-up, not banker's


def test_mcp_twin_keeps_exact_km_adds_km_approx_and_matches_on_the_rounded_point():
    lat, lon = _offset(*ROUNDED_A, -4.6, 3.3)
    row = _match_new_facilities([_site(lat, lon)], [A], dp=2, mcp_twin=True)[1][0]
    assert row["km"] == round(_haversine_km(lat, lon, A["latitude"], A["longitude"]), 1)
    assert row["km_approx"] == _served_km(_haversine_km(lat, lon, *ROUNDED_A))
    assert "distance_status" not in row
    for step in range(81):                      # membership: the rounded point's
        r = 48.0 + step * 0.05
        plat, plon = _offset(*ROUNDED_A, r * 0.8, r * -0.6)
        s = [_site(plat, plon)]
        assert (bool(_match_new_facilities(s, [A], dp=2, mcp_twin=True))
                == bool(_match_new_facilities(s, [A], dp=2)))


def test_exact_rung_is_unchanged():
    lat, lon = _offset(*ROUNDED_A, -4.6, 3.3)
    row = _match_new_facilities([_site(lat, lon)], [A])[1][0]
    assert row == {"name": "Example Facility", "state": "EX", "capacity_mw": 40,
                   "km": round(_haversine_km(lat, lon, A["latitude"], A["longitude"]), 1)}


# ── who gets which rung ─────────────────────────────────────────────────────
@pytest.fixture
def tier_env(monkeypatch):
    """Stub the two resolvers _nearby_precision imports; the real
    coord_dp_for_tier decides the rung, at its default knobs."""
    for k in ("MAP_ANON_COORD_DP", "MAP_FREE_COORD_DP"):
        monkeypatch.delenv(k, raising=False)
    state = {"tier": "anon", "internal": False, "raise": False}
    ia = types.ModuleType("internal_auth")
    ia.is_valid_internal_key = lambda v: state["internal"] and v == "internal-test-key"
    atg = types.ModuleType("api_tier_gating")

    def get_request_tier():
        if state["raise"]:
            raise RuntimeError("tier store down")
        return state["tier"]
    atg.get_request_tier = get_request_tier
    monkeypatch.setitem(__import__("sys").modules, "internal_auth", ia)
    monkeypatch.setitem(__import__("sys").modules, "api_tier_gating", atg)
    return state


def _ctx(internal=False):
    headers = {"X-Internal-Key": "internal-test-key"} if internal else {}
    return Flask(__name__).test_request_context("/api/v1/lp/saved", headers=headers)


@pytest.mark.parametrize("tier,expected", [
    ("pro", (None, False)), ("developer", (None, False)), ("enterprise", (None, False)),
    # Free and identified sit on the anonymous rung since the 2026-09-21
    # exact-location policy (their exact view is the monthly allowance, which
    # is per facility, not per distance list); starter is metered the same way.
    ("free", (2, False)), ("identified", (2, False)), ("starter", (2, False)),
    ("anon", (2, False)),
    ("no-such-tier", (2, False)),
])
def test_rest_rung_is_the_facility_record_rung(tier_env, tier, expected):
    tier_env["tier"] = tier
    with _ctx():
        assert _nearby_precision() == expected


def test_internal_mcp_key_gets_the_twin(tier_env):
    tier_env.update(internal=True, tier="admin")
    with _ctx(internal=True):
        assert _nearby_precision() == (2, True)
    with _ctx(internal=False):               # the key, not the flag, decides
        assert _nearby_precision() == (None, False)


def test_a_resolver_error_answers_the_coarse_rung(tier_env):
    tier_env["raise"] = True
    with _ctx():
        assert _nearby_precision() == (2, False)


# ── wired through portfolio_snapshot (the function both endpoints call) ─────
class _Cur:
    def __init__(self, site, fac):
        self.site, self.fac, self.rows = site, fac, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "FROM saved_lp_sites" in sql:
            self.rows = [dict(self.site)]
        elif "FROM discovered_facilities" in sql:
            self.rows = [dict(self.fac)]
        else:
            raise RuntimeError("fake: signal not modelled")   # fail-soft lanes

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, site, fac):
        self.site, self.fac = site, fac

    def cursor(self, **kw):
        return _Cur(self.site, self.fac)

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.mark.parametrize("tier,internal,shape", [
    ("free", False, "rounded"), ("anon", False, "rounded"),
    ("pro", False, "exact"), ("admin", True, "twin"),
])
def test_portfolio_snapshot_serves_the_callers_rung(tier_env, monkeypatch, tier, internal, shape):
    pytest.importorskip("psycopg2.extras")
    lat, lon = _offset(*ROUNDED_A, -4.6, 3.3)
    site = {"id": 9, "name": "My parcel", "latitude": lat, "longitude": lon,
            "state": "EX", "market": None, "target_mw": None,
            "dcpi_score_at_save": None, "saved_at": None}
    monkeypatch.setattr(lp_sites, "_conn", lambda: _Conn(site, A))
    tier_env.update(tier=tier, internal=internal)
    with _ctx(internal=internal):
        pf = lp_sites.portfolio_snapshot("k_test", max_sites=5)
    row = pf["sites"][0]["new_facilities_nearby"][0]
    exact = round(_haversine_km(lat, lon, A["latitude"], A["longitude"]), 1)
    # Every non-exact rung is 2 dp at default knobs since the 2026-09-21
    # exact-location policy (free included); pinned per tier by
    # test_rest_rung_is_the_facility_record_rung above.
    dp = 2
    approx = _served_km(_haversine_km(lat, lon, round(A["latitude"], dp),
                                      round(A["longitude"], dp)))
    if shape == "rounded":
        assert row["km"] == approx and row["distance_status"] == "approximate_1km"
        assert "km_approx" not in row and exact not in row.values()
    elif shape == "exact":
        assert row["km"] == exact and "distance_status" not in row
    else:
        assert row["km"] == exact and row["km_approx"] == approx
    assert pf["moved"][0]["new_facilities_nearby"][0] == row
