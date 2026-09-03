"""A lane's triage REASON goes stale silently. Two of five did, inside a day.

routes/lane_triage.LANE_TRIAGE tells an engineer WHY a red lane is red, and
/api/v1/ops/deadman publishes those strings verbatim in `red_triage`. They are
hand-written prose with nothing bonding them to the check that actually fails,
so when a lane's failing check CHANGES, the reason keeps describing the old one
and sends the next reader at work that is already done.

★ MEASURED 2026-09-03, against a table written 2026-09-02:

  loop_flywheel/cron      said "asserts 'dead-man board clear' ... circular by
                          construction". Live 01:45Z that check reads
                          "202 feeds, 0 overdue" and PASSES — D2 had fixed the
                          circularity the same day the table was written. The
                          lane fails on its OTHER check, cron_dupes.

  context_integrity/      said "counts bare-{} internal fetchers still to
  envelope                migrate in routes/". Live 01:57Z that check reads
                          "15 migrated, none remaining" and PASSES. The lane
                          fails on "every L14 context probe answered" — one
                          probe ReadTimeout against 127.0.0.1:8080.

Both were classed code_actionable and published as such, so the board was
telling an engineer to go fix two checks that already work.

★ WHAT THIS FILE CAN AND CANNOT DO. Prose cannot be diffed against a live
dashboard from a unit test. What CAN be bonded is the one claim that is a fact
about the source: cron_dupes takes no reading — it passes a literal False — so
it cannot go green when the work is done, and cannot go red for any new reason
either. The AST assertion below fails the moment someone makes it a real
measurement, which forces the triage reason to be rewritten in the same commit.
That is the drift that actually recurs.

★ AST, not substring. `"False" in source` would also match a comment, a
docstring or an unrelated default — the vacuous-assertion shape. This resolves
the call and reads the argument node.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from routes.lane_triage import CODE_ACTIONABLE, LANE_TRIAGE, classify


def _check_call_args(func, check_id):
    """The ast.Call node for `_check("<check_id>", ...)` inside `func`."""
    tree = ast.parse(inspect.getsource(func).lstrip())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "_check" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and first.value == check_id:
            return node
    return None


# ── the bond: cron_dupes is a literal, and the reason must say so ──────

def test_cron_dupes_still_takes_no_reading():
    """★ If this fails, cron_dupes became a real measurement — good news, and
    the loop_flywheel/cron triage reason must be rewritten in the same commit
    because it currently tells the reader the check is a hardcoded False."""
    from routes.loop_flywheel_master_shell import _lane_cron
    call = _check_call_args(_lane_cron, "cron_dupes")
    assert call is not None, "the cron_dupes check has moved or been renamed"
    verdict = call.args[2]
    assert isinstance(verdict, ast.Constant) and verdict.value is False, (
        "cron_dupes now computes its verdict — rewrite the "
        "('loop_flywheel','cron') reason in routes/lane_triage.py")


def test_the_reason_names_the_check_that_actually_fails():
    klass, why = classify("loop_flywheel", "cron")
    assert "cron_dupes" in why
    assert "hardcoded False" in why


def test_the_reason_no_longer_asserts_the_circularity_that_was_fixed():
    """D2 fixed it on 2026-09-02; the board kept publishing it as the cause."""
    _klass, why = classify("loop_flywheel", "cron")
    circular = why.split("replaces", 1)[-1]
    assert "PASSES" in circular, (
        "the superseded reason must be marked as superseded, not just deleted "
        "— a reader who saw the old text needs to know it was retired")


def test_cron_is_classed_by_who_closes_it_not_by_the_old_reason():
    """A ~314-job dedup wave is an engineer's work order, not a broken
    measurement. instrument would send them to fix the check instead."""
    assert classify("loop_flywheel", "cron")[0] == "build"


def test_envelope_no_longer_blames_the_completed_migration():
    _klass, why = classify("context_integrity", "envelope")
    assert "L14 context probe" in why
    assert "ReadTimeout" in why


# ── the general property: a corrected reason must carry its date ───────

_RECHECKED = [("loop_flywheel", "cron"), ("context_integrity", "envelope")]


@pytest.mark.parametrize("key", _RECHECKED)
def test_every_re_measured_reason_is_dated(key):
    """A reason with no as-of is unfalsifiable: a reader cannot tell whether
    it was verified today or a month ago. Both of the stale ones were
    undated."""
    _klass, why = classify(*key)
    assert "RE-MEASURED 2026-09-03" in why, key


@pytest.mark.parametrize("key", _RECHECKED)
def test_a_re_measured_reason_says_what_it_replaced(key):
    _klass, why = classify(*key)
    assert "replaces" in why, key


# ── narrowness: the rest of the table is untouched ────────────────────

def test_no_other_reason_was_edited():
    """★ This change corrects exactly two entries. Any other entry gaining a
    RE-MEASURED stamp without someone actually re-measuring it would be the
    same unfalsifiable prose this file exists to stop."""
    stamped = {k for k, (_c, w) in LANE_TRIAGE.items() if "RE-MEASURED" in w}
    assert stamped == set(_RECHECKED)


def test_the_table_is_still_wellformed():
    for key, val in LANE_TRIAGE.items():
        assert isinstance(key, tuple) and len(key) == 2, key
        klass, why = val
        assert isinstance(why, str) and why.strip(), key
        assert klass in ("build", "commercial", "judgment", "owner-flag",
                         "diagnose", "instrument"), key


def test_code_actionable_membership_is_unchanged_by_the_reclass():
    """cron moved instrument -> build. Both are code_actionable, so the
    board's published counts do not move — only the reason a reader acts on."""
    assert {"build", "instrument"} <= CODE_ACTIONABLE
