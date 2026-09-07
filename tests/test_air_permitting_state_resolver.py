"""_ap_resolve_state_detail — the state a US coordinate is actually in.

The air-permitting score uses `state` to pick the regulatory context (which
agency, which NNSR/PSD thresholds) and to weight 5% of the composite, so
resolving it wrongly does not look like an error — it looks like a confident
answer about the wrong jurisdiction.

Bounding boxes cannot get this right for an irregular state. Ashburn VA sits
inside BOTH Virginia's and Maryland's box; Maryland's is smaller, so
smallest-bbox-wins returned MD for the densest data-center market on earth and
the answer cited MDE instead of Virginia DEQ.

Pulled out of main.py with ast (no import — main.py opens pools and registers
~200 blueprints) and executed against a stubbed connection.
"""
import ast
import math
import pathlib

import pytest

_MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"


def _load(names):
    """Execute the named top-level functions from main.py in a bare namespace."""
    tree = ast.parse(_MAIN.read_text(encoding="utf-8"))
    want = dict.fromkeys(names)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef,)) and node.name in want:
            want[node.name] = node
    missing = [n for n, v in want.items() if v is None]
    assert not missing, f"not found in main.py: {missing} — the resolver moved or was renamed"
    ns = {"_ap_math": math}
    for n in names:
        exec(compile(ast.Module(body=[want[n]], type_ignores=[]), "<main>", "exec"), ns)
    return ns


class _Cur:
    def __init__(self, rows): self._rows = rows
    def execute(self, *a, **k): pass
    def fetchall(self): return self._rows


class _Conn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _Cur(self._rows)


def _ns_with(rows, boxes=None, raise_on_connect=False):
    ns = _load(["_ap_resolve_state_bbox", "_ap_resolve_state_detail", "_ap_resolve_state"])
    ns["_AP_STATE_BOXES"] = boxes if boxes is not None else {
        # real-ish boxes: Ashburn (39.04, -77.48) is inside BOTH, MD's is smaller
        "VA": ((36.5, -83.7), (39.5, -75.2)),
        "MD": ((37.9, -79.5), (39.7, -75.0)),
    }
    ns["_ap_in_bounds"] = lambda la, lo, box: (
        box[0][0] <= la <= box[1][0] and box[0][1] <= lo <= box[1][1])
    ns["_AP_SUBSTATION_STATE_MAX_KM"] = 80.0

    def _get():
        if raise_on_connect:
            raise RuntimeError("pool down")
        return _Conn(rows)
    ns["get_pg_connection"] = _get
    ns["return_pg_connection"] = lambda c: None
    return ns


# ── the bug this exists to prevent ──────────────────────────────────────────
def test_bbox_alone_returns_the_wrong_state_for_ashburn():
    # Locking in WHY the fallback is only a fallback. If this ever passes as VA,
    # the boxes changed and the rest of this file needs re-reading.
    ns = _ns_with([])
    assert ns["_ap_resolve_state_bbox"](39.04, -77.48) == "MD"


def test_nearest_substation_overrides_the_bbox_answer():
    # Two real VA substations within ~3 km of Ashburn.
    ns = _ns_with([("VA", 39.02473, -77.48925), ("VA", 39.0563, -77.4497)])
    state, basis = ns["_ap_resolve_state_detail"](39.04, -77.48)
    assert state == "VA", "bbox says MD; the substations say VA and they are right"
    assert basis.startswith("nearest_substation")


def test_the_nearest_substation_wins_not_the_first_row():
    # MD row first and closer in list order, VA row genuinely nearer.
    ns = _ns_with([("MD", 39.40, -77.48), ("VA", 39.041, -77.481)])
    state, _ = ns["_ap_resolve_state_detail"](39.04, -77.48)
    assert state == "VA"


def test_far_substation_is_not_trusted_and_falls_back():
    # ~140 km away — beyond the 80 km trust radius.
    ns = _ns_with([("WV", 40.30, -77.48)])
    state, basis = ns["_ap_resolve_state_detail"](39.04, -77.48)
    assert basis == "state_bbox_fallback"
    assert state == "MD"          # the fallback's answer, correctly labelled


def test_no_substations_falls_back_and_says_so():
    ns = _ns_with([])
    state, basis = ns["_ap_resolve_state_detail"](39.04, -77.48)
    assert (state, basis) == ("MD", "state_bbox_fallback")


def test_db_failure_fails_soft_rather_than_taking_the_score_down():
    ns = _ns_with([], raise_on_connect=True)
    state, basis = ns["_ap_resolve_state_detail"](39.04, -77.48)
    assert basis == "state_bbox_fallback" and state == "MD"


def test_unusable_rows_are_skipped_not_fatal():
    ns = _ns_with([("VA", None, None), ("VA", "x", "y"), ("VA", 39.041, -77.481)])
    state, basis = ns["_ap_resolve_state_detail"](39.04, -77.48)
    assert state == "VA" and basis.startswith("nearest_substation")


def test_state_is_normalised():
    ns = _ns_with([(" va ", 39.041, -77.481)])
    assert ns["_ap_resolve_state_detail"](39.04, -77.48)[0] == "VA"


def test_outside_coverage_returns_none_without_raising():
    ns = _ns_with([], boxes={"VA": ((36.5, -83.7), (39.5, -75.2))})
    state, basis = ns["_ap_resolve_state_detail"](0.0, 0.0)   # Gulf of Guinea
    assert state is None and basis == "state_bbox_fallback"


def test_thin_wrapper_still_returns_a_bare_state():
    # _ap_state_supported() calls this and expects a string-or-None, not a tuple.
    ns = _ns_with([("VA", 39.041, -77.481)])
    assert ns["_ap_resolve_state"](39.04, -77.48) == "VA"
