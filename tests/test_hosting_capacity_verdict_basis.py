"""tests/test_hosting_capacity_verdict_basis.py — WS1 honest-numbers guard (2026-07-28).

Guards `_substation_headroom()` in routes/depth_master_shell.py, which powers the
public GET /api/v1/grid/hosting-capacity and the per-market snapshots persisted by
_act_hosting_capacity().

The bug this locks shut: the endpoint shipped verdict="abundant" alongside
total_capacity_mva=0 and total_available_mva=null. The adjective came from
substation DENSITY + EHV count + distance (no MVA term at all), while the two MVA
fields came from `sum(x or 0 ...)` over columns nothing populates — so a confident
siting adjective rode out on a numeric basis that did not exist. Live, every point
probed scored 94-100 -> "abundant".

Ways it can regress, each one asserted below:
  (1) VERDICT-WITHOUT-BASIS — the adjective returns while capacity_mva is empty.
  (2) TEST-ROW BASIS — `cap_sum > 0` looks like a basis, but live Phoenix's
      200 MVA is two rows literally named test_sub_xyz / test_sub_diag written
      straight into prod. A count-and-name-filtered basis is required.
  (3) ZERO-ERASED — `(avail_sum or None)` collapsed a legitimate reported 0.0 into
      "no data"; reported-0 and no-data must stay distinguishable.
  (4) SCORE NULLED — _act_hosting_capacity:577 skips any market whose
      headroom_score is None, so nulling the score silently stops the
      grid_ext_metrics writes for every market. The score must stay a number.
  (5) UNLABELLED ESTIMATE — the voltage-class MVA estimate must stay banded and
      must never be published as total_capacity_mva.

House rules (reference_dchub_green_main_0709): pre-merge pytest has NO DB and must
NEVER import main — so the function is pulled out with `ast` and executed against
stubs. Nothing runs at module scope.

Run:  python3 -m pytest tests/test_hosting_capacity_verdict_basis.py -v
"""
from __future__ import annotations

import ast
import math
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SHELL = _ROOT / "routes" / "depth_master_shell.py"


def _num(v):
    """Mirror of depth_master_shell._num (a free variable of the extracted func)."""
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _func_src(name: str) -> str:
    """Slice `def name(` out of the shell by ast (parsed, not regex-guessed).

    ★ A silently-empty extraction passes every downstream assertion, so this
    asserts the parse really found a FunctionDef with a real body.
    """
    src = _SHELL.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            assert f"def {name}(" in body, f"{name} sliced but body is wrong"
            assert len(body.splitlines()) > 40, f"{name} extraction too short to be real"
            return body
    raise AssertionError(f"{name}() not found in {_SHELL.name}")


def _load(rows):
    """exec the real _substation_headroom against stubs returning `rows`.

    Its free variables are _num, _conn and math (plus `re`, imported inside the
    function). A NameError here means a free var stopped resolving — which would
    otherwise leave the function silently untested.
    """
    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            pass

        def fetchall(self):
            return rows

    class _C:
        def cursor(self):
            return _Cur()

        def close(self):
            pass

    ns = {"_num": _num, "_conn": lambda: _C(), "math": math}
    exec(compile(_func_src("_substation_headroom"), "<shell>", "exec"), ns)
    for free_var in ("_num", "_conn", "math"):
        assert free_var in ns, f"free var {free_var} unresolved"
    return ns["_substation_headroom"]


def _rows(n, cap=None, avail=None, kv=230.0, name="Sub"):
    """(name, operator, voltage_kv, max_voltage_kv, capacity_mva, available_mva, lat, lng)
    spaced ~0.1 km apart so every row lands inside the default 40 km radius."""
    return [(f"{name}{i}", "Op", kv, 0.0, cap, avail, 40.0 + 0.001 * i, -75.0)
            for i in range(n)]


# ── (1) verdict is withheld when no substation reports capacity ───────────

def test_verdict_withheld_when_capacity_column_is_empty():
    """Live Providence/Syracuse/NYC: hundreds of substations, capacity_mva all NULL."""
    hr = _load(_rows(309))(40.0, -75.0, 40.0)
    assert hr["verdict"] is None, "an adjective shipped with no numeric basis"
    assert hr["total_capacity_mva"] is None, "0 must not stand in for 'no data'"
    assert hr["total_available_mva"] is None
    assert hr["verdict_withheld_reason"], "withholding must state its reason"
    assert "capacity" in hr["verdict_withheld_reason"].lower()
    # the density signal is still published, under a name that says what it is
    assert hr["density_class"] == "abundant"
    assert hr["score_basis"] == "substation_density_proximity"


# ── (2) prod test rows are not a capacity basis ──────────────────────────

def test_test_named_rows_do_not_unlock_a_verdict():
    """Phoenix r=5 returns 200 MVA entirely from test_sub_xyz + test_sub_diag."""
    rows = _rows(40) + [
        ("test_sub_xyz", "Op", 230.0, 0.0, 100.0, None, 40.0, -75.0),
        ("test_sub_diag", "Op", 230.0, 0.0, 100.0, None, 40.0, -75.0),
    ]
    hr = _load(rows)(40.0, -75.0, 40.0)
    assert hr["verdict"] is None, "cap_sum > 0 is not a valid basis — these are test rows"
    assert hr["total_capacity_mva"] is None, "fabricated MVA must not be totalled"
    assert hr["capacity_basis"]["excluded_test_named"] == 2


def test_thin_real_coverage_reports_the_number_but_withholds_the_adjective():
    """2 real rows out of 100: publish the sum WITH its coverage, not a verdict."""
    rows = _rows(98) + [
        ("Real A", "Op", 230.0, 0.0, 400.0, None, 40.0, -75.0),
        ("Real B", "Op", 230.0, 0.0, 400.0, None, 40.0, -75.0),
    ]
    hr = _load(rows)(40.0, -75.0, 40.0)
    assert hr["total_capacity_mva"] == 800.0
    assert hr["capacity_basis"]["capacity_coverage_pct"] == 2.0
    assert hr["verdict"] is None


# ── (3) a reported 0 is not the same as no data ──────────────────────────

def test_reported_zero_is_distinguishable_from_no_data():
    """Live '800/900 Network Substation' carries capacity_mva = 0.0 explicitly."""
    hr = _load(_rows(5, cap=0.0))(40.0, -75.0, 40.0)
    assert hr["total_capacity_mva"] == 0.0, "reported 0.0 was erased into None"
    assert hr["capacity_basis"]["with_capacity_mva_reported"] == 5
    assert hr["capacity_basis"]["with_capacity_mva_gt0"] == 0
    assert hr["verdict"] is None, "reported-but-zero capacity is not a basis for 'abundant'"


def test_available_mva_zero_survives_when_really_reported():
    hr = _load(_rows(10, cap=400.0, avail=0.0))(40.0, -75.0, 40.0)
    assert hr["total_available_mva"] == 0.0, "the old `or None` erased a real 0"
    assert hr["total_capacity_mva"] == 4000.0
    assert hr["verdict"] == hr["density_class"] and hr["verdict"] is not None


def test_source_does_not_reintroduce_the_or_none_erasure():
    src = _func_src("_substation_headroom")
    assert '"total_available_mva": (avail_sum or None)' not in src
    assert '"total_capacity_mva": cap_sum,' in src


# ── (4) headroom_score must stay a number (persistence depends on it) ────

@pytest.mark.parametrize("rows", [_rows(309), _rows(5, cap=0.0), []])
def test_headroom_score_is_never_none(rows):
    """_act_hosting_capacity skips any market whose headroom_score is None — nulling
    it silently stops the grid_ext_metrics snapshot for every market."""
    hr = _load(rows)(40.0, -75.0, 40.0)
    assert isinstance(hr["headroom_score"], int)


def test_act_hosting_capacity_still_gates_on_the_score():
    """Locks the coupling this test exists to protect."""
    src = _SHELL.read_text(encoding="utf-8")
    assert 'hr.get("headroom_score") is None' in src


# ── (5) the voltage-class estimate stays labelled as an estimate ─────────

def test_voltage_class_estimate_is_banded_and_separately_named():
    hr = _load(_rows(6, kv=500.0))(40.0, -75.0, 40.0)
    band = hr["est_capacity_mva_band"]
    assert band["banded"] is True
    assert band["low_mva"] < band["high_mva"], "a band needs two ends"
    assert band["substations_classified"] == 6
    assert "ESTIMATE" in band["note"]
    # never laundered into the measured field
    assert hr["total_capacity_mva"] is None


def test_implausible_voltages_are_excluded_from_the_estimate():
    """HIFLD NODATA and live 1000/1115 kV junk must not inflate the band."""
    rows = _rows(3, kv=1115.0, name="Junk") + _rows(3, kv=500.0, name="Real")
    hr = _load(rows)(40.0, -75.0, 40.0)
    band = hr["est_capacity_mva_band"]
    assert band["substations_implausible_voltage"] == 3
    assert band["substations_classified"] == 3
    assert band["low_mva"] == 2700   # 3 x 900, the >765 kV rows dropped


# ── shape stability + fail-soft ─────────────────────────────────────────

def test_zero_substation_branch_keeps_the_same_shape():
    hr = _load([])(40.0, -75.0, 40.0)
    for key in ("total_capacity_mva", "total_available_mva", "capacity_basis",
                "est_capacity_mva_band", "score_basis", "headroom_score", "verdict"):
        assert key in hr, f"early return dropped {key}"
    assert hr["verdict"] == "no_transmission_nearby"
    assert hr["total_capacity_mva"] is None


def test_returns_none_without_coordinates():
    """The route turns None into available=false — it must never raise."""
    fn = _load(_rows(3))
    assert fn(None, None) is None
    assert fn("", "") is None
