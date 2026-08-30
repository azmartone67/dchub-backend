"""`healthy_working` must mean work came out, not merely that the loop ran.

★ THE STATE THIS EXISTS FOR, measured live 2026-08-30 on /api/v1/brain/status:

    actionable_findings_count: 39
    proposed_fixes_count:       0
    verdict:                    healthy_working
    verdict_detail:            "Running normally — last pass 21m ago,
                                0 proposal(s) in flight."

Thirty-nine open findings, nothing proposed against any of them, and the
verdict announced normal operation — in a detail line that says "0 proposal(s)"
out loud.

★ THE CAUSE WAS BRANCH ORDER, not intent. compute_brain_verdict read:

    if pf_count > 0 or (stale_min is not None and stale_min < 180):
        return ("healthy_working", ...)
    if actionable_count > 0:
        return ("healthy_backlog", ...)

A fresh learning log alone satisfied the first branch, so whenever the loop had
run recently the backlog branch was UNREACHABLE. The r36 change that added
`healthy_backlog` on 2026-05-31 — whose own docstring says "When a backlog
exists, say so honestly instead" — was dead code from the day it shipped, in
exactly the state it was written for.

★ A FRESH LOG MEANS THE LOOP RAN. It says nothing about whether anything came
out of it, and that distinction is the entire purpose of this verdict. Only
proposals in flight earn `healthy_working` on their own.

★ WHY THE BACKLOG IS NOT MERELY "ROUTED ELSEWHERE". The old detail line said
these findings "route to autopilot + Layer 5". Measured the same day, the
latest brain-autonomy run evaluated 22 proposals and opened ZERO — 21 rejected
`not_mechanical` against a 6-class SQL/datetime allowlist. A backlog that
routes into a filter which rejects everything is not routed, it is parked.
"""
from routes.brain_v2_layer4 import compute_brain_verdict

FRESH_LOG = 129          # minutes; the live value, comfortably under 180
RECENT_RUN = 21


def _verdict(actionable, proposals, stale=FRESH_LOG, run=RECENT_RUN):
    return compute_brain_verdict(True, run, stale, proposals, 7424, actionable)[0]


def test_the_live_regression_a_backlog_with_no_proposals_is_named():
    """39 open, 0 proposed, log fresh — the exact live state on 2026-08-30."""
    assert _verdict(39, 0) == "healthy_backlog"


def test_proposals_in_flight_still_earn_healthy_working():
    """Work coming out is what the verdict is supposed to assert."""
    assert _verdict(39, 3) == "healthy_working"


def test_no_backlog_and_a_fresh_log_is_still_healthy_working():
    """The fix must not turn a genuinely quiet, healthy brain red."""
    assert _verdict(0, 0) == "healthy_working"


def test_no_backlog_and_a_stale_log_is_quiet_not_backlog():
    assert _verdict(0, 0, stale=400) == "healthy_quiet"


def test_a_dropped_cron_still_outranks_everything():
    """`stalled` is the one state that needs a human; a backlog must not mask it."""
    assert _verdict(39, 0, run=400) == "stalled"


def test_the_detail_line_never_claims_normal_operation_while_nothing_is_proposed():
    """The failure was as much in the prose as the branch: a detail reading
    'Running normally ... 0 proposal(s) in flight' beside 39 open findings is
    what made the state easy to scroll past."""
    verdict, detail = compute_brain_verdict(True, RECENT_RUN, FRESH_LOG, 0, 7424, 39)
    assert verdict == "healthy_backlog"
    assert "Running normally" not in detail
    assert "39" in detail
