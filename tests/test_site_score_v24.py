"""/api/site-score composite-v2.4: a score that separates two metro sites, and
says what each factor rests on (live screen 2026-09-25, NoVA + north NJ).

Every anchor scored power 100, gas 95, market 60, risk 65: counts saturate in
a dense metro, risk was STATE_RISK's default 65 because find_sites' handoff
sent no state, and each failed lookup was `except: pass`. The handler is run
from main.py (lifted by ast) against a fake cursor that answers each statement
by shape; an unrecognised statement fails the test.
"""
import ast
import copy
import logging
import pathlib
import re

import flask
import pytest

from util import site_scoring as ss

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")


# ── the rules ───────────────────────────────────────────────────────────

def test_saturated_density_no_longer_decides_power():
    near, b1 = ss.power_score(2.0, 500, 40, 30)
    far, b2 = ss.power_score(15.0, 230, 40, 30)
    assert b1 == b2 == "measured_point:nearest_hv_substation"
    assert near > far
    assert ss.density_power(40, 30) == (100.0, True)       # what v2.3 served both


def test_power_without_a_nearest_hv_substation_says_it_is_a_count():
    assert ss.power_score(None, None, 40, 30) == (100.0, "density_count_saturated")
    assert ss.power_score(None, None, 2, 2) == (47.0, "density_count")


def test_gas_by_distance_else_band():
    assert ss.gas_score(1.0, 25) == (97.0, "measured_point:nearest_pipeline")
    assert ss.gas_score(30.0, 25)[0] == 20.0
    assert ss.gas_score(None, 25) == (95.0, "count_band")


def test_market_is_labelled_a_density_band():
    assert ss.market_score(60) == (60.0, "facility_density_band")
    assert ss.market_score(10) == (85.0, "facility_density_band")


def test_composite_renormalises_over_what_was_scored():
    full = {"power_infrastructure": 80, "gas_pipeline_access": 60, "fiber_connectivity": 90,
            "market_conditions": 60, "risk_resilience": 70}
    assert ss.composite(full) == (73.0, "all_factors")   # 20+6+13.5+9+24.5
    no_risk = dict(full, risk_resilience=None)
    o, basis = ss.composite(no_risk)
    assert basis == "renormalised_without:risk_resilience"
    assert o == round((80 * .25 + 60 * .10 + 90 * .15 + 60 * .15) / .65, 1)


# ── the handler ─────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, world):
        self.w, self._rows = world, []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        w = self.w
        if "FROM substations WHERE lat BETWEEN" in s:
            if w.get("hv_boom"):
                raise RuntimeError("substations down")
            rows = [w["hv"]] if w.get("hv") else []
        elif "FROM gas_pipelines WHERE lat BETWEEN" in s:
            rows = [w["gas"]] if w.get("gas") else []
        elif "SELECT MAX(updated_at)" in s:
            rows = [("2026-09-01 00:00:00",)]
        elif "FROM discovered_facilities" in s:
            rows = [(60, 5000.0)]
        elif "FROM substations" in s and "COUNT(*)" in s:
            if w.get("count_boom"):
                raise RuntimeError("substations down")
            rows = [(40,)]
        elif "FROM gas_pipelines" in s and "COUNT(*)" in s:
            rows = [(25,)]
        elif "FROM discovered_power_plants" in s:
            rows = [(30, 9000.0)]
        elif "information_schema.columns" in s:
            rows = [(0,)]
        elif "COUNT(DISTINCT provider)" in s:
            rows = [(3,)]
        elif "SELECT MAX(period) FROM eia_retail_rates" in s:
            rows = [(None,)]
        else:
            raise AssertionError(f"_Cur has no answer for: {s[:160]}")
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, world):
        self.cur = _Cur(world)

    def cursor(self):
        return self.cur

    def close(self):
        pass


def _run(monkeypatch, query, **world):
    import routes.connectivity_score as cs
    monkeypatch.setattr(cs, "score_connectivity", lambda *a, **k: {"error": "no_db"})
    ss._AS_OF.update(at=0.0, value={})
    fn = copy.deepcopy(next(n for n in ast.parse(MAIN_SRC).body
                            if isinstance(n, ast.FunctionDef) and n.name == "api_site_score"))
    fn.decorator_list = []
    ns = {"request": flask.request, "jsonify": flask.jsonify,
          "get_read_db": lambda: _Conn(world), "logger": logging.getLogger("t")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)  # noqa: S102
    app = flask.Flask("t")
    with app.test_request_context("/api/site-score?" + query):
        resp = ns["api_site_score"]()
    body = resp.get_json() if hasattr(resp, "get_json") else resp[0].get_json()
    assert body.get("success"), body
    return body


ASHBURN = "lat=39.0438&lon=-77.4874"


def test_two_saturated_metro_sites_now_differ_on_power(monkeypatch):
    near = _run(monkeypatch, ASHBURN + "&state=VA", hv=("Loudoun 500", 500.0, 39.05, -77.49),
                gas=(39.05, -77.49))
    far = _run(monkeypatch, ASHBURN + "&state=VA", hv=("Remote 230", 230.0, 39.17, -77.49),
               gas=(39.30, -77.49))
    assert near["scores"]["power_infrastructure"] > far["scores"]["power_infrastructure"]
    assert near["scores"]["gas_pipeline_access"] > far["scores"]["gas_pipeline_access"]
    assert near["coverage"]["power_infrastructure"]["basis"] == "measured_point:nearest_hv_substation"
    assert near["nearest"]["hv_substation"]["voltage_kv"] == 500.0
    assert near["methodology_version"] == "composite-v2.4"
    assert near["coverage"]["power_infrastructure"]["as_of"] == "2026-09-01"


def test_a_missing_state_is_resolved_from_the_point(monkeypatch):
    body = _run(monkeypatch, ASHBURN, hv=("Loudoun 500", 500.0, 39.05, -77.49))
    assert body["location"]["state"] == "VA"
    assert body["coverage"]["state"]["basis"] == "census_point_in_polygon"
    assert body["scores"]["risk_resilience"] == 72
    assert body["coverage"]["risk_resilience"] == {
        "scored": True, "basis": "state_table",
        "source": "DC Hub state risk table (not county-level)", "as_of": None}
    assert body["overall_basis"] == "all_factors"


def test_no_state_means_risk_not_scored_never_65(monkeypatch):
    body = _run(monkeypatch, "lat=51.5074&lon=-0.1278")            # London: no US state
    assert body["scores"]["risk_resilience"] is None
    assert body["coverage"]["risk_resilience"]["scored"] is False
    assert body["coverage"]["risk_resilience"]["basis"].startswith("risk_not_scored")
    assert body["overall_basis"] == "renormalised_without:risk_resilience"
    assert body["overall_score"] is not None


def test_a_failed_lookup_is_named_not_read_as_zero(monkeypatch):
    body = _run(monkeypatch, ASHBURN + "&state=VA", count_boom=True, hv_boom=True)
    errs = body["coverage"]["power_infrastructure"]["lookup_errors"]
    assert errs == {"substations": "RuntimeError", "nearest_hv_substation": "RuntimeError"}
    assert body["coverage"]["power_infrastructure"]["basis"].startswith("density_count")


def test_find_sites_hands_the_state_to_analyze_site():
    src = (ROOT / "routes" / "find_sites.py").read_text(encoding="utf-8")
    assert '"analyze_site lat=%s lon=%s state=%s"' in src


def test_the_preview_below_pro_leaks_no_new_distance():
    fn = next(n for n in ast.parse(MAIN_SRC).body
              if isinstance(n, ast.FunctionDef) and n.name == "_site_score_preview")
    body = ast.get_source_segment(MAIN_SRC, fn)
    assert "'nearest'" not in body and "'coverage'" not in body and "hv_substation" not in body
