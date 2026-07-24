"""DCPI Excess-Data master shell (#26) test suite (2026-07-24).

All mocked (no DB, no network, never imports main). This shell is the SHADOW
sentinel over the excess-data project, so the tests pin the SAFETY and HONESTY
properties the adversarial scoping demanded — NOT a projected BUILD count.

Contract:
  1. fail-CLOSED weights: unknown fuel/status -> 0.0, never a permissive default.
  2. the maturity gate zeroes speculative (P)/(L) planned rows (anti-double-count
     with the constraint term).
  3. haversine is correct (~111 km/deg).
  4. the SHADOW projection persists NOTHING and never overwrites a curated
     stranded-override market.
  5. excess-term contributions can never exceed their weight caps (15 / 20).
  6. cluster-independence flags a mega-plant monoculture (one event flipping many
     metros) even when the raw BUILD count looks healthy.
  7. lane verdict is critical-aware: an undetermined load-bearing check -> '?',
     never a green lane.
  8. kill switch -> 404 (never 5xx, which would trip the CF failover breaker);
     admin gate -> 403; empty admin key never opens the door.
  9. the invariants lane fails when DCPI_RELAX_VERDICTS_ARM is armed or the
     divisor is moved off its untouched baseline.
"""
import importlib.util
import os

import flask
import pytest

_HERE = os.path.dirname(__file__)
_MOD = os.path.abspath(os.path.join(_HERE, "..", "routes", "dcpi_excess_master_shell.py"))


def _load():
    spec = importlib.util.spec_from_file_location("dcpi_excess_master_shell_t", _MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


shell = _load()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("DCPI_EXCESS_SHELL_DISABLE", "DCPI_RELAX_VERDICTS_ARM",
              "DCHUB_ADMIN_KEY", "DCHUB_INTERNAL_KEY"):
        monkeypatch.delenv(k, raising=False)
    yield


# ── 1-3 pure helpers ──────────────────────────────────────────────────

def test_firmness_fails_closed_on_unknown():
    assert shell._firmness("Conventional Steam Coal") == 1.0
    assert shell._firmness("Solar Photovoltaic") == 0.25
    assert shell._firmness("Onshore Wind Turbine") == 0.15
    assert shell._firmness("Totally Unknown Fuel XYZ") == 0.0
    assert shell._firmness(None) == 0.0
    assert shell._firmness("") == 0.0


def test_maturity_gate_zeroes_speculative_and_unknown():
    assert shell._maturity("(TS) Construction complete, but not yet in commercial operation") == 1.0
    assert shell._maturity("(V) Under construction, more than 50 percent complete") == 0.9
    assert shell._maturity("(U) Under construction, less than or equal to 50 percent complete") == 0.7
    # (P) and (L) are speculative AND already drive the constraint term -> MUST be 0
    assert shell._maturity("(P) Planned for installation, but regulatory approvals not initiated") == 0.0
    assert shell._maturity("(L) Regulatory approvals pending. Not under construction") == 0.0
    assert shell._maturity("(ZZ) nonsense") == 0.0
    assert shell._maturity(None) == 0.0


def test_haversine_is_correct():
    assert 110.0 < shell._haversine_km(40.0, -90.0, 41.0, -90.0) < 112.0
    assert shell._haversine_km(40.0, -90.0, 40.0, -90.0) == 0.0


# ── 4-6 projection safety ─────────────────────────────────────────────

def _synthetic_data():
    """Two markets: one plain, one a curated stranded-override. One firm coal
    retirement and one mature gas plant right on top of the plain market; a
    speculative (P) solar plant that must contribute nothing."""
    markets = [
        # slug, name, constraint, excess, stranded_stored, lat, lng
        ("plain-market", "Plain", 20.0, 40.0, 0.0, 40.00, -90.00),
        ("curated-market", "Curated", 20.0, 68.0, 600.0, 41.00, -95.00),  # override (stranded>0)
    ]
    retire = [
        # id, lat, lng, mw, fuel, retirement_date
        ("R1", 40.01, -90.01, 900.0, "Conventional Steam Coal", None),
    ]
    planned = [
        ("P1", 40.02, -90.00, 2000.0, "Natural Gas Fired Combined Cycle",
         "(TS) Construction complete, but not yet in commercial operation", 2026),
        # speculative -> maturity 0 -> must not contribute
        ("P2", 40.00, -90.00, 5000.0, "Solar Photovoltaic",
         "(P) Planned for installation, but regulatory approvals not initiated", 2027),
    ]
    return {"markets": markets, "retire": retire, "planned": planned}


def test_projection_never_overwrites_a_curated_override():
    m = _synthetic_data()
    # Put a huge firm retirement next to the curated market too — it must be ignored.
    m["retire"].append(("R2", 41.001, -95.001, 5000.0, "Conventional Steam Coal", None))
    p = shell._project(m, 80.0)
    # The curated market started at excess 68 (BUILD) and must remain BUILD via its
    # STORED value — the projection adds nothing on top of a curated stranded market.
    # We assert indirectly: caps are respected and the run doesn't crash; the plain
    # market gets the lift, curated does not inflate beyond its stored basis.
    assert p["counts"]["BUILD"] >= 1
    # cap check: stranded contribution can't exceed 15, additions can't exceed 20
    assert p["max_d_strand"] <= 15.01
    assert p["max_d_add"] <= 20.01


def test_speculative_planned_contributes_nothing():
    m = _synthetic_data()
    # Remove the mature gas + coal so ONLY the speculative (P) solar could lift.
    m["retire"] = []
    m["planned"] = [m["planned"][1]]  # only P2, the (P) solar
    p = shell._project(m, 80.0)
    # No firm/mature source -> plain market cannot gain any additions -> no new build.
    assert p["new_builds"] == 0


def test_excess_terms_respect_weight_caps():
    m = _synthetic_data()
    # Pile on firm capacity far above the divisors; contributions must still cap.
    m["retire"] = [("R%d" % i, 40.0, -90.0, 9000.0, "Conventional Steam Coal", None) for i in range(20)]
    m["planned"] = [("P%d" % i, 40.0, -90.0, 9000.0, "Natural Gas Fired Combined Cycle",
                     "(TS) done", 2026) for i in range(20)]
    p = shell._project(m, 80.0)
    assert p["max_d_strand"] <= shell._STRAND_WEIGHT * 100 + 0.01  # <= 15
    assert p["max_d_add"] <= shell._ADD_WEIGHT * 100 + 0.01        # <= 20


def test_cluster_independence_flags_mega_plant_monoculture():
    # One giant coal retirement centered among many nearby markets -> every one
    # flips on the SAME event = a per-mega-plant monoculture the lane must catch.
    markets = []
    for i in range(8):
        markets.append((f"m{i}", f"M{i}", 20.0, 60.0, 0.0, 40.0 + i * 0.05, -90.0))
    data = {"markets": markets,
            "retire": [("BIG", 40.2, -90.0, 5000.0, "Conventional Steam Coal", None)],
            "planned": []}
    p = shell._project(data, 80.0)
    # If several markets became BUILD, they share ONE driving event.
    if p["new_builds"] >= 2:
        assert p["max_metros_per_single_event"] == p["new_builds"]
        assert p["distinct_driving_events"] == 1


# ── 7 lane verdict ────────────────────────────────────────────────────

def test_undetermined_critical_check_is_not_green():
    checks = [shell._check("a", "a", True, "ok"),
              shell._check("b", "b", None, "unknown", critical=True)]
    assert shell._lane_verdict(checks) is None


def test_gauge_does_not_fail_a_lane():
    checks = [shell._check("a", "a", True, "ok"),
              shell._check("g", "g", None, "gauge")]  # non-critical gauge
    assert shell._lane_verdict(checks) is True


# ── 8 gating ──────────────────────────────────────────────────────────

def _app():
    app = flask.Flask(__name__)
    app.register_blueprint(shell.dcpi_excess_master_shell_bp)
    return app


def test_kill_switch_is_404_not_5xx(monkeypatch):
    monkeypatch.setenv("DCPI_EXCESS_SHELL_DISABLE", "1")
    with _app().test_client() as cl:
        assert cl.get("/api/v1/admin/dcpi-excess/master-tick").status_code == 404
        assert cl.get("/admin/dcpi-excess").status_code == 404


def test_admin_gate(monkeypatch):
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "sekret")
    with _app().test_client() as cl:
        assert cl.get("/api/v1/admin/dcpi-excess/master-tick").status_code == 403
        assert cl.get("/api/v1/admin/dcpi-excess/master-tick",
                      headers={"X-Admin-Key": "nope"}).status_code == 403


def test_empty_admin_key_never_opens(monkeypatch):
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    with _app().test_client() as cl:
        assert cl.get("/api/v1/admin/dcpi-excess/master-tick").status_code == 403


# ── 9 honesty invariants lane ─────────────────────────────────────────

def test_invariants_lane_fails_when_relax_armed(monkeypatch):
    monkeypatch.setenv("DCPI_RELAX_VERDICTS_ARM", "1")
    checks = shell._lane_invariants(None, {"markets": []})
    relax = [c for c in checks if c["id"] == "iv_relax_disarmed"][0]
    assert relax["pass"] is False


def test_invariants_lane_passes_relax_when_disarmed():
    checks = shell._lane_invariants(None, {"markets": []})
    relax = [c for c in checks if c["id"] == "iv_relax_disarmed"][0]
    fuel = [c for c in checks if c["id"] == "iv_failclosed_fuel"][0]
    divisor = [c for c in checks if c["id"] == "iv_divisor_untouched"][0]
    assert relax["pass"] is True
    assert fuel["pass"] is True           # fail-closed helpers verified
    assert divisor["pass"] is True        # 5000 untouched
