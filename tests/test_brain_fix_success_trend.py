"""Fix-success trend (2026-09-18).

/api/v1/brain/effectiveness states its own purpose as "look at
fix_success_rate trending up" — and until this change it computed only a
single CURRENT value, so the one question the endpoint exists to answer
("is the brain learning?") had no series behind it.

compute_fix_success_trend() is pure so the two rules that keep the verdict
honest are testable without a database:

  · the CURRENT month is PARTIAL and must never be compared against a
    complete one — otherwise elapsed time reads as a swing;
  · a month under the graded floor cannot carry a direction.

Run: python3 -m pytest tests/test_brain_fix_success_trend.py -v
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes.brain_learning import compute_fix_success_trend as trend  # noqa: E402


def _m(month, graded, pct):
    return {"month": month, "graded": graded, "success_rate_pct": pct,
            "checks": graded, "fix_succeeded": 0, "fix_failed": 0}


# ── the partial-month rule ────────────────────────────────────────────

def test_partial_current_month_is_never_compared():
    """★ A half-elapsed month at 90% must not be reported as improvement over
    a complete month at 40%. The comparison is Aug vs Jul, not Sep vs Aug."""
    # ★ September carries 90 graded outcomes — WELL ABOVE the floor — so the
    # only rule that can exclude it is the partial-month rule. With a small
    # sample here the floor would exclude it instead and this test would pass
    # even with the partial-month rule deleted.
    out = trend([_m("2026-09", 90, 90.0), _m("2026-08", 60, 40.0),
                 _m("2026-07", 55, 55.0)], this_month="2026-09")
    assert out["latest_complete_month"] == "2026-08"
    assert out["prior_complete_month"] == "2026-07"
    assert out["direction"] == "declining"
    assert out["delta_pp"] == -15.0
    assert out["partial_month"] == "2026-09"


def test_partial_month_with_a_huge_sample_is_still_excluded():
    """Excluded because it is INCOMPLETE, not because it is small — a big
    partial month is exactly the one most likely to be mistaken for a trend."""
    out = trend([_m("2026-09", 5000, 99.0), _m("2026-08", 60, 40.0),
                 _m("2026-07", 55, 41.0)], this_month="2026-09")
    assert out["latest_complete_month"] == "2026-08"
    assert out["direction"] == "flat"


# ── the sample floor ──────────────────────────────────────────────────

def test_month_below_the_graded_floor_carries_no_direction():
    out = trend([_m("2026-08", 3, 100.0), _m("2026-07", 2, 0.0)],
                this_month="2026-09")
    assert out["direction"] is None
    assert "fewer than 2 complete months" in out["note"]


def test_direction_is_none_not_flat_when_undeterminable():
    """★ `flat` is a MEASURED verdict; absence of data must not borrow it."""
    assert trend([], this_month="2026-09")["direction"] is None
    assert trend(None, this_month="2026-09")["direction"] is None
    assert trend([_m("2026-08", 99, 50.0)],
                 this_month="2026-09")["direction"] is None


def test_a_month_with_nothing_gradeable_is_skipped_not_zeroed():
    """success_rate_pct None means nothing was gradeable — it is not 0%."""
    out = trend([_m("2026-08", 40, None), _m("2026-07", 40, 50.0),
                 _m("2026-06", 40, 45.0)], this_month="2026-09")
    assert out["latest_complete_month"] == "2026-07"
    assert out["prior_complete_month"] == "2026-06"


# ── direction + band ──────────────────────────────────────────────────

def test_improving_declining_and_flat_band():
    up = trend([_m("2026-08", 40, 60.0), _m("2026-07", 40, 40.0)],
               this_month="2026-09")
    assert up["direction"] == "improving" and up["delta_pp"] == 20.0
    down = trend([_m("2026-08", 40, 40.0), _m("2026-07", 40, 60.0)],
                 this_month="2026-09")
    assert down["direction"] == "declining"
    # inside the band ⇒ noise, not a trend
    flat = trend([_m("2026-08", 40, 51.0), _m("2026-07", 40, 50.0)],
                 this_month="2026-09")
    assert flat["direction"] == "flat"


def test_out_of_order_rows_still_compare_the_two_latest():
    """The SQL orders DESC, but the verdict must not depend on that."""
    out = trend([_m("2026-06", 40, 10.0), _m("2026-08", 40, 60.0),
                 _m("2026-07", 40, 40.0)], this_month="2026-09")
    assert out["latest_complete_month"] == "2026-08"
    assert out["prior_complete_month"] == "2026-07"


def test_floor_and_band_are_tunable():
    out = trend([_m("2026-08", 3, 60.0), _m("2026-07", 3, 40.0)],
                this_month="2026-09", min_graded=2, flat_band=0.5)
    assert out["direction"] == "improving"
    assert out["min_graded_per_month"] == 2 and out["flat_band_pp"] == 0.5
