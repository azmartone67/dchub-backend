"""tests/test_main_branch_verdict.py — the main-CI monitor must judge HEAD
(2026-08-31).

THE INCIDENT. main-branch-health beat the public board `main_red` while main was
already fixed, because it answered with the newest COMPLETED run rather than the
run for main's HEAD:

    f5b191c8b  pre-merge completed 22:20:56 -> FAILURE   (stale architecture map)
    2e00f9aee  #3473 lands — THE FIX — run starts 22:23:28
    22:33:01   monitor runs; HEAD still in flight, so the newest COMPLETED run is
               the superseded commit -> "main_red ... Every open PR is blocked"
    22:46:02   HEAD's run completes -> SUCCESS

A red nobody can clear is how a board stops being read, and this is the same feed
a REAL red main would appear on.

These tests pin both directions: the false red is gone AND a genuine red on HEAD
is still reported immediately.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_main_branch_verdict.py -v
"""
from __future__ import annotations

import importlib.util
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "tools" / "deadman" / "main_branch_verdict.py"

HEAD = "2e00f9aee62bd519b1d6bcb46e5e4f6acecaa2e2"
OLD = "f5b191c8b0000000000000000000000000000000"


def _mod():
    spec = importlib.util.spec_from_file_location("main_branch_verdict", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _done(sha, conclusion):
    return {"headSha": sha, "status": "completed", "conclusion": conclusion}


def _running(sha):
    return {"headSha": sha, "status": "in_progress", "conclusion": None}


def _all(runs):
    """Same run list for each gating workflow."""
    return {wf: list(runs) for wf in _mod().GATING}


# --------------------------------------------------------------------------
# ★THE REGRESSION
# --------------------------------------------------------------------------

def test_a_superseded_commits_failure_is_not_the_branchs_verdict():
    """★REGRESSION — the 22:33:01 reading, replayed exactly.

    HEAD's run is in flight; the newest COMPLETED run belongs to the commit HEAD
    replaced. That is 'not measured yet', never 'main is red'."""
    m = _mod()
    runs = _all([_running(HEAD), _done(OLD, "failure")])
    status, note = m.verdict(HEAD, runs)
    assert status == "pending", (
        "a failure on a commit that HEAD already replaced must not be reported "
        "as the branch being red — got %r (%s)" % (status, note))
    assert "2e00f9aee" in note


def test_pending_is_not_reported_as_success_either():
    """Not-yet-known must not be laundered into a green."""
    m = _mod()
    status, _ = m.verdict(HEAD, _all([_running(HEAD), _done(OLD, "success")]))
    assert status == "pending"


# --------------------------------------------------------------------------
# The monitor must still BITE. Without these the fix is just a mute button.
# --------------------------------------------------------------------------

def test_a_real_failure_on_head_is_reported_red():
    """★The whole point of the monitor."""
    m = _mod()
    status, note = m.verdict(HEAD, _all([_done(HEAD, "failure")]))
    assert status == "main_red" and "pre-merge.yml(failure)" in note


def test_an_older_green_does_not_excuse_a_red_head():
    """The mirror of the bug: HEAD is broken, the commit before it was fine."""
    m = _mod()
    status, _ = m.verdict(HEAD, _all([_done(HEAD, "failure"), _done(OLD, "success")]))
    assert status == "main_red"


def test_a_red_head_wins_over_a_pending_sibling():
    """One workflow has already failed on HEAD; another is still running. That
    failure is actionable now — do not wait it out."""
    m = _mod()
    g = _mod().GATING
    runs = {g[0]: [_done(HEAD, "failure")],
            g[1]: [_running(HEAD)],
            g[2]: [_done(HEAD, "success")]}
    status, _ = m.verdict(HEAD, runs)
    assert status == "main_red"


def test_a_cancelled_or_timed_out_run_is_not_a_pass():
    """conclusion != success is red, whatever flavour of not-success it is."""
    m = _mod()
    for bad in ("cancelled", "timed_out", "startup_failure", None):
        status, _ = m.verdict(HEAD, _all([_done(HEAD, bad)]))
        assert status == "main_red", "conclusion=%r must not read as green" % bad


def test_all_green_on_head_is_success():
    m = _mod()
    status, note = m.verdict(HEAD, _all([_done(HEAD, "success"), _done(OLD, "failure")]))
    assert status == "success" and "3 gating" in note


# --------------------------------------------------------------------------
# Unreadable is its own answer — an unmeasured main is not a green one.
# --------------------------------------------------------------------------

def test_nothing_readable_is_unmeasured_not_success():
    m = _mod()
    status, _ = m.verdict(HEAD, {wf: [] for wf in m.GATING})
    assert status == "unmeasured"


def test_a_workflow_with_no_run_for_head_yet_is_pending():
    """Just-merged: the push has not created the run yet."""
    m = _mod()
    status, note = m.verdict(HEAD, _all([_done(OLD, "success")]))
    assert status == "pending" and "no run for HEAD yet" in note


def test_one_unreadable_workflow_does_not_mask_a_measured_red():
    m = _mod()
    g = m.GATING
    runs = {g[0]: [_done(HEAD, "failure")], g[1]: [], g[2]: [_done(HEAD, "success")]}
    status, _ = m.verdict(HEAD, runs)
    assert status == "main_red"
