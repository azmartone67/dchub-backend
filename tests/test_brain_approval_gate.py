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
