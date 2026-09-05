"""The coverage sensor must fire on a US-only layer served as global — and must
NOT fire on a layer that is US-only by declaration.

Both halves matter. A sensor that never fires is the bug we are fixing; a sensor
that fires forever on HIFLD (correctly US-only) is one everybody learns to
ignore, which is the same bug wearing a different hat.

These drive the real _measure / _finding_for / _resolve against a fake cursor,
rather than restating them.
"""
import pytest

from routes import infra_coverage as ic


class _Cur:
    """Fake psycopg2 cursor. `cols` maps table -> column set; `grid` maps table
    -> list of (cell_lat, cell_lng, n) rows the GROUP BY would return."""

    def __init__(self, cols=None, grid=None):
        self.cols = cols or {}
        self.grid = grid or {}
        self._result = []
        self.queries = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        if "information_schema.columns" in sql:
            self._result = [(c,) for c in self.cols.get(params[0], set())]
            return
        table = sql.split("FROM ")[1].split()[0].strip()
        self._result = list(self.grid.get(table, []))

    def fetchall(self):
        return self._result

    def fetchone(self):
        return self._result[0] if self._result else None


# One dense cell = a single-region layer. HIFLD transmission looks like this.
US_ONLY = [(3, -8, 94000), (3, -7, 1200), (4, -8, 360)]
# Many cells, no single dominant one = a genuinely global layer.
GLOBAL = [(r, c, 900) for r, c in
          [(3, -8), (5, 0), (5, 1), (3, 12), (-2, -5), (4, 13), (-3, 2), (1, 10)]]


# ── the sensor fires on the thing that went unnoticed ─────────────────

def test_us_only_layer_served_as_global_fires():
    cur = _Cur(cols={"transmission_lines_eia": {"lat", "lng", "id"}},
               grid={"transmission_lines_eia": US_ONLY})
    m = ic._measure(cur, "transmission_lines")
    assert m["measured"] and m["declared_scope"] == "global"
    assert m["cells"] == 3
    assert m["concentration"] > 0.9
    f = ic._finding_for(m)
    assert f is not None, "a US-only layer served as global must fire"
    assert f["issue"] == "layer_scope_contradiction"
    assert "single-region" in f["detail"]


def test_a_growing_row_count_does_not_rescue_a_single_region_layer():
    """The whole point: volume says healthy, shape says one country."""
    cur = _Cur(cols={"transmission_lines_eia": {"lat", "lng"}},
               grid={"transmission_lines_eia": [(3, -8, 10_000_000)]})
    f = ic._finding_for(ic._measure(cur, "transmission_lines"))
    assert f is not None, "10M rows in ONE cell is still one region"


# ── and stays quiet where quiet is correct ────────────────────────────

def test_genuinely_global_layer_does_not_fire():
    cur = _Cur(cols={"substations": {"lat", "lng"}}, grid={"substations": GLOBAL})
    m = ic._measure(cur, "substations")
    assert m["cells"] == 8 and m["concentration"] < 0.9
    assert ic._finding_for(m) is None


def test_layer_declared_us_is_exempt():
    """HIFLD gas being US-only is CORRECT. A finding that fires forever trains
    everyone to ignore this sensor."""
    cur = _Cur(cols={"gas_pipelines": {"lat", "lng"}}, grid={"gas_pipelines": US_ONLY})
    m = ic._measure(cur, "gas_pipelines")
    assert m["declared_scope"] == "us"
    assert ic._finding_for(m) is None, "a declared-US layer must not fire"


# ── never 0 ───────────────────────────────────────────────────────────

def test_missing_columns_is_unmeasured_with_a_reason_not_zero():
    """Collapsing 'no such column' into a published 0 is the bug infra_growth
    was burned by on 2026-07-29. An unmeasured layer must say so."""
    cur = _Cur(cols={"transmission_lines_eia": {"id", "owner"}})  # no lat/lng
    m = ic._measure(cur, "transmission_lines")
    assert m["measured"] is False
    assert "cells" not in m and "concentration" not in m and "geocoded_rows" not in m
    assert m["reason"] and "transmission_lines_eia" in m["reason"]


def test_missing_table_is_unmeasured_not_zero():
    m = ic._measure(_Cur(cols={}), "substations")
    assert m["measured"] is False and m["reason"]


def test_unmeasured_layer_never_produces_a_finding():
    """An unmeasured layer must not be reported as a coverage failure — that
    would be asserting something we did not measure."""
    assert ic._finding_for({"label": "x", "measured": False,
                            "reason": "no columns", "declared_scope": "global"}) is None


def test_resolved_but_genuinely_empty_is_its_own_state():
    """Table exists, columns exist, no usable coordinates. That IS a real zero
    and is reported distinctly from 'could not measure'."""
    cur = _Cur(cols={"substations": {"lat", "lng"}}, grid={"substations": []})
    m = ic._measure(cur, "substations")
    assert m["measured"] is True and m["geocoded_rows"] == 0
    assert m["concentration"] is None and m["note"]
    f = ic._finding_for(m)
    assert f is not None and "no usable coordinates" in f["detail"]


# ── column discovery ──────────────────────────────────────────────────

def test_resolve_takes_the_first_candidate_that_actually_exists():
    """power_plants' coordinate columns could not be determined from source, so
    candidates are tried in order against information_schema. Assuming a name
    raises UndefinedColumn, which an except-block turns back into a silent 0."""
    cur = _Cur(cols={"power_plants": {"latitude", "longitude", "name"}})
    resolved, reason = ic._resolve(cur, "power_plants_eia")
    assert resolved == ("power_plants", "latitude", "longitude"), \
        "should fall through (lat,lng) to the (latitude,longitude) candidate"
    assert reason is None


def test_resolve_reports_what_it_tried():
    resolved, reason = ic._resolve(_Cur(cols={}), "power_plants_eia")
    assert resolved is None
    assert "power_plants(lat,lng)" in reason and "latitude,longitude" in reason


def test_every_geo_layer_declares_a_scope():
    missing = set(ic._GEO) - set(ic._SCOPE)
    assert not missing, f"layers with no declared scope cannot be judged: {missing}"


@pytest.mark.parametrize("scope", list(ic._SCOPE.values()))
def test_declared_scopes_are_known_values(scope):
    assert scope in ("us", "global")


# ── wiring ────────────────────────────────────────────────────────────

def test_job_is_registered_in_cron_dispatch():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "routes", "cron_heartbeat.py"), encoding="utf-8").read()
    assert '"infra_coverage"' in src, "not in _DISPATCH — it would never run"
    assert "/api/v1/jobs/infra-coverage" in src


def test_issue_type_is_registered_as_an_error_class():
    from routes import brain_error_classes as bec
    assert "layer_scope_contradiction" in {c.id for c in bec.REGISTRY}, \
        "unregistered issues are bucketed 'unknown' and dropped from actionable_now"


def test_kill_switch_does_not_report_success():
    """_classify records 'skipped' only when ok is not True; ok=True here would
    make a DISABLED sensor report success forever."""
    import inspect
    src = inspect.getsource(ic.infra_coverage)
    branch = src.split("INFRA_COVERAGE_DISABLE")[1].split("result =")[0]
    assert "ok=True" not in branch and '"ok": True' not in branch


def test_thresholds_are_not_trivially_satisfiable():
    """MIN_CELLS=1 or MAX_CONCENTRATION=1.0 would disarm the sensor while
    leaving every test above green."""
    assert ic.MIN_CELLS >= 2, "a 1-cell minimum can never fail"
    assert ic.MAX_CONCENTRATION < 1.0, "a 100% concentration cap can never fail"
