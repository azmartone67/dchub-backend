"""A backlog that stopped growing reads as a quiet week.

2026-09-08. Every stage and leak in the MCP funnel diagnostic was scoped to
24h or 7d. So this was invisible:

  * outreach_drafts: 4 rows at status='drafted', created 2026-06-20 — 80 days
    unapproved. Three of the four are the same paying customers the
    white-glove board lists as stranded.
  * 64 signals carry an email and have never received outreach, all time.
  * 127 of 33,682 signals carry an identity at all — 0.4%.

That last number reframes the funnel: 99.6% of captured demand is unreachable
by ANY outreach lane, however well armed. Building a bigger sender cannot move
a number gated on identity capture.

evaluate_stall_leaks is pure so the NOT-firing case is testable, which is the
half that matters — a sensor that fires on healthy input is not a sensor.
"""
import pytest

from routes.mcp_funnel_diag import (
    evaluate_stall_leaks, _DRAFT_STALE_DAYS, _UNCONTACTED_ALERT,
    _IDENTITY_FLOOR, _IDENTITY_MIN_VOLUME,
)


def _names(stages):
    return {l["name"] for l in evaluate_stall_leaks(stages)}


LIVE = {
    "4c_drafts_awaiting_approval": {"count": 4, "oldest_days": 80},
    "4b_identified_never_contacted": 64,
    "1b_identity_capture": {"signals_all_time": 33682, "with_identity": 127,
                            "capture_rate": 0.0038},
}

HEALTHY = {
    "4c_drafts_awaiting_approval": {"count": 0, "oldest_days": 0},
    "4b_identified_never_contacted": 0,
    "1b_identity_capture": {"signals_all_time": 33682, "with_identity": 30000,
                            "capture_rate": 0.89},
}


def test_the_live_shape_fires_all_three():
    assert _names(LIVE) == {"drafts_never_approved",
                            "identified_never_contacted",
                            "identity_capture_is_the_ceiling"}


def test_a_healthy_funnel_fires_nothing():
    """The load-bearing negative."""
    assert _names(HEALTHY) == set()


def test_missing_stages_fire_nothing():
    """An absent stage is an unknown, not a defect. Reporting an unknown as a
    failure is the mistake this repo keeps finding."""
    assert _names({}) == set()
    assert _names({"4c_drafts_awaiting_approval": {"_error": "OperationalError"},
                   "4b_identified_never_contacted": {"_error": "x"},
                   "1b_identity_capture": {"_error": "x"}}) == set()


def test_a_fresh_draft_is_not_a_stall():
    """Drafts written yesterday are a working queue, not an abandoned one."""
    s = dict(LIVE, **{"4c_drafts_awaiting_approval":
                      {"count": 4, "oldest_days": _DRAFT_STALE_DAYS - 1}})
    assert "drafts_never_approved" not in _names(s)


def test_small_install_does_not_report_an_identity_ceiling():
    """★ The volume floor. Without it a brand-new install with 3 signals and
    no emails reports a CRITICAL ceiling — firing on healthy input."""
    s = {"1b_identity_capture": {"signals_all_time": _IDENTITY_MIN_VOLUME - 1,
                                 "with_identity": 0, "capture_rate": 0.0}}
    assert _names(s) == set()


def test_the_ceiling_fires_once_volume_is_real():
    s = {"1b_identity_capture": {"signals_all_time": _IDENTITY_MIN_VOLUME,
                                 "with_identity": 0, "capture_rate": 0.0}}
    assert "identity_capture_is_the_ceiling" in _names(s)


@pytest.mark.parametrize("n,fires", [
    (_UNCONTACTED_ALERT - 1, False),
    (_UNCONTACTED_ALERT, True),
])
def test_uncontacted_threshold_boundary(n, fires):
    s = {"4b_identified_never_contacted": n}
    assert ("identified_never_contacted" in _names(s)) is fires


def test_leaks_are_all_time_not_windowed():
    """If these ever get a 7d filter they become invisible again — the exact
    blindness they were written to remove."""
    import inspect
    src = inspect.getsource(evaluate_stall_leaks)
    assert "7 days" not in src and "INTERVAL" not in src, (
        "evaluate_stall_leaks must stay window-free; it reads all-time stages")
