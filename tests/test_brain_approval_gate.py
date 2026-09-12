"""The two gates that make the brain's adversarial refuter COUNT.

Measured on the live board 2026-09-12, both were open:
  * /api/v1/brain/self-assessment reported human_rejection_rate 0.0 over
    human_review_count_60d 293 and scored rejection 4/4 — a review gate that
    has never once disagreed, scored as a perfect record.
  * 8 of 15 self-directed agenda items were marked BOTH "refuted" and
    "approved", the lowest at confidence 0.10, and the approve endpoint handed
    them to the code drafter without reading either field.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.brain_learning import rejection_component, REJECTION_DEAD_SIGNAL_N
from routes.brain_innovation_dashboard import _pr_block_reason, PR_MIN_CONFIDENCE


# ── the dead-signal floor ────────────────────────────────────────────
def test_zero_rejection_over_a_real_sample_is_dead_not_perfect():
    """The shipped bug: 0.0% over 293 reviews scored 4/4."""
    score, signal = rejection_component(0.0, 293)
    assert signal == "dead", "a gate that never disagrees is not a live signal"
    assert score < 4, f"0.0% over 293 reviews must not score full marks, got {score}"


def test_zero_rejection_on_a_small_sample_is_not_punished():
    """Below the floor a zero is just a small sample, not a dead gate."""
    score, signal = rejection_component(0.0, REJECTION_DEAD_SIGNAL_N - 1)
    assert signal == "live"
    assert score == 4


def test_a_healthy_low_rejection_rate_still_scores_full_marks():
    """The floor must catch EXACTLY zero, not 'low' — 8% is a working gate."""
    score, signal = rejection_component(0.08, 293)
    assert (score, signal) == (4, "live")


def test_the_band_still_degrades_with_rising_rejection():
    assert rejection_component(0.15, 100)[0] == 3
    assert rejection_component(0.30, 100)[0] == 2
    assert rejection_component(0.50, 100)[0] == 1
    assert rejection_component(0.90, 100)[0] == 0


def test_dead_scores_worse_than_every_live_rate_it_outranked():
    """0.0% used to tie the best possible score. It must now rank below the
    rates that represent a gate doing its job."""
    dead = rejection_component(0.0, 293)[0]
    assert dead < rejection_component(0.08, 293)[0]
    assert dead < rejection_component(0.15, 293)[0]
    assert dead < rejection_component(0.30, 293)[0]


# ── the approve→PR verdict gate ──────────────────────────────────────
def test_a_refuted_item_does_not_auto_draft_a_pr():
    """The shipped bug: agenda #100258, refuted at confidence 0.10, approved."""
    why = _pr_block_reason({"refutation_survived": False, "confidence": 0.10})
    assert why, "a refuted item must not silently become a draft PR"
    assert "refut" in why.lower()


def test_a_low_confidence_item_does_not_auto_draft_a_pr():
    why = _pr_block_reason(
        {"refutation_survived": True, "confidence": PR_MIN_CONFIDENCE - 0.01})
    assert why
    assert "confidence" in why.lower()


def test_a_survived_high_confidence_item_is_allowed_through():
    assert _pr_block_reason(
        {"refutation_survived": True, "confidence": 0.66}) == ""


def test_an_unknown_verdict_fails_open():
    """A DB hiccup must not block the operator: None never blocks."""
    assert _pr_block_reason(
        {"refutation_survived": None, "confidence": None}) == ""
    assert _pr_block_reason({}) == ""


def test_the_floor_is_a_real_threshold_not_a_rubber_stamp():
    """Exactly AT the floor passes; a hair under it blocks. Pins the
    comparison direction so an inverted '>' cannot pass this file."""
    assert _pr_block_reason(
        {"refutation_survived": True, "confidence": PR_MIN_CONFIDENCE}) == ""
    assert _pr_block_reason(
        {"refutation_survived": True, "confidence": PR_MIN_CONFIDENCE - 0.001})


# ── the detector that would have caught this ─────────────────────────
# routes/brain_consistency_radar.check_review_gate_never_disagrees is the
# can't-fail signature (check A of the 2026-09-07 null-signal spec) for the
# exact bug above: 293 review decisions, zero rejections, scored 4/4.
import pytest


class _Cur:
    def __init__(self, rows): self._rows = rows
    def execute(self, *a, **k): pass
    def fetchall(self): 
        if isinstance(self._rows, Exception):
            raise self._rows
        return self._rows
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Conn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _Cur(self._rows)


def _run(monkeypatch, rows):
    import routes.brain_consistency_radar as R
    monkeypatch.setattr(R, "_db",
                        lambda: None if rows is None else _Conn(rows))
    return R.check_review_gate_never_disagrees()


def test_the_detector_fires_on_the_shipped_state():
    """293 decisions, not one rejection."""
    import routes.brain_consistency_radar as R
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, [("approve", 293)])
    finally:
        mp.undo()
    assert len(out) == 1, out
    assert out[0]["issue"] == "review_gate_never_disagrees"
    assert "293" in out[0]["detail"]


def test_the_detector_is_silent_when_the_gate_actually_fires():
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, [("approve", 280), ("reject", 13)])
    finally:
        mp.undo()
    assert out == []


def test_one_rejection_is_enough_to_prove_the_signal_is_live():
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, [("approve", 292), ("reject", 1)])
    finally:
        mp.undo()
    assert out == []


def test_a_small_sample_of_zero_rejections_is_not_a_dead_gate():
    import routes.brain_consistency_radar as R
    mp = pytest.MonkeyPatch()
    try:
        out = _run(mp, [("approve", R._REVIEW_GATE_MIN_SAMPLE - 1)])
    finally:
        mp.undo()
    assert out == []


def test_the_sample_floor_is_a_real_threshold():
    import routes.brain_consistency_radar as R
    mp = pytest.MonkeyPatch()
    try:
        at = _run(mp, [("approve", R._REVIEW_GATE_MIN_SAMPLE)])
        under = _run(mp, [("approve", R._REVIEW_GATE_MIN_SAMPLE - 1)])
    finally:
        mp.undo()
    assert len(at) == 1, "exactly at the floor must fire"
    assert under == [], "a hair under must not"


def test_an_unreadable_signal_is_unmeasured_never_a_clean_pass():
    """No DB, or a broken query, must return [] — the ABSENCE of a finding,
    not a claim that the gate is healthy."""
    mp = pytest.MonkeyPatch()
    try:
        assert _run(mp, None) == []
        assert _run(mp, Exception("relation does not exist")) == []
    finally:
        mp.undo()


def test_the_detector_is_registered_in_the_sweep():
    """A check defined but absent from scan_all's container NEVER RUNS.
    Asserted with ast against executable text, so a name in a comment
    cannot satisfy it."""
    import ast, os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "routes", "brain_consistency_radar.py")
    tree = ast.parse(open(path).read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "scan_all":
            for sub in ast.walk(node):
                if isinstance(sub, ast.For) and isinstance(sub.iter, (ast.Tuple, ast.List)):
                    names |= {e.id for e in sub.iter.elts if isinstance(e, ast.Name)}
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                   and sub.func.attr == "append":
                    names |= {a.id for a in sub.args if isinstance(a, ast.Name)}
    assert "check_review_gate_never_disagrees" in names, sorted(names)[:5]
