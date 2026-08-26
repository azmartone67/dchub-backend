"""The scheduler's firing window, and the one place it must NOT widen.

WHY THIS EXISTS
---------------
`_should_run_now` fired only while `now_minute < 5`. A slot that missed those
five minutes was lost until the next day, and lost SILENTLY: both early returns
in `_run_with_guard` (in-process busy, cross-replica claim lost) return before
the try/finally that beats the dead-man, so a starved slot keeps reporting its
last SUCCESSFUL run. It reads healthy-but-stale, never failed.

Measured 2026-08-26: the five hour-08 slots behind `deals` (SCHEDULE order #3)
last ran 2026-08-24 08:04-08:06 UTC, in list order ~30s apart, all still
status=success. `crawler_run_claims` still held `deals` claimed
2026-08-25T08:00:28Z — 22.2h against a 1920s TTL — and `_release_crawler_run`
runs only in the finally, so the process died mid-run and the tick after the
restart landed past minute 5.

The window is now the whole target hour plus a short grace. The grace is
deliberately bounded, and hour 23 is deliberately excluded — see
test_no_grace_out_of_hour_23 for the bug that exclusion prevents.
"""
import importlib

import pytest

cs = importlib.import_module("crawler_scheduler")


@pytest.fixture(autouse=True)
def _two_slot_mode(monkeypatch):
    """Default these tests to the TWO-slot reading of a (h1, h2) pair.

    Production sets CRAWLER_SCHEDULE=once (verified on the live dchub-worker
    env 2026-08-26), which collapses target_hours to [hour1]. Tests that care
    about that path set it themselves; the rest must not inherit whatever the
    ambient environment happens to hold, or they pass for the wrong reason.
    """
    monkeypatch.delenv("CRAWLER_SCHEDULE", raising=False)


# ---------------------------------------------------------------------------
# The regression: a slot missed at :00 must still run later in its hour.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("minute", [0, 4, 5, 6, 30, 59])
def test_fires_anywhere_inside_the_target_hour(minute):
    """★ THE BUG. At minute >= 5 the old window returned False and the slot was
    lost for the day. `deals` blocking the loop past 08:05, or a redeploy
    landing at 08:30, is exactly how five hour-08 lanes went 46h without a run.
    """
    should, target = cs._should_run_now(8, 20, 8, minute, set())
    assert should is True, (
        f"08:{minute:02d} did not fire the hour-08 slot — a slot that misses "
        "the top of its hour is lost until the next day, silently")
    assert target == 8


def test_grace_reaches_into_the_next_hour():
    """A tick that lands just past the hour still catches the slot."""
    should, target = cs._should_run_now(8, 20, 9, 2, set())
    assert should is True
    assert target == 8, "the grace must credit the MISSED hour, not the current one"


def test_grace_is_bounded_and_does_not_run_all_day():
    """The grace is minutes, not hours. A day-wide catch-up would fire every
    already-past slot at once on every deploy, and this worker redeploys
    constantly."""
    assert cs._should_run_now(8, 20, 9, cs.CATCHUP_GRACE_MINUTES, set())[0] is False
    assert cs._should_run_now(8, 20, 9, 30, set())[0] is False
    assert cs._should_run_now(8, 20, 11, 2, set())[0] is False
    assert cs._should_run_now(8, 20, 7, 59, set())[0] is False, (
        "fired BEFORE its target hour")


# ---------------------------------------------------------------------------
# The exclusion that keeps the widening safe.
# ---------------------------------------------------------------------------

def test_no_grace_out_of_hour_23():
    """★ THE MIDNIGHT DOUBLE-RUN. _scheduler_loop resets last_run_hours on the
    day rollover:

        if last_reset_day != now.day:
            last_run_hours = {s[2]: set() for s in SCHEDULE}

    so a 23:00 target graced into 00:0x meets a FRESH EMPTY set — the
    already-ran guard cannot see yesterday's run, and the slot fires twice.
    model_relations and white_glove_propagate_catchup both declare (23, 23).
    """
    should, _ = cs._should_run_now(23, 23, 0, 2, set())
    assert should is False, (
        "hour-23 slot graced past midnight — last_run_hours has already been "
        "reset for the new day, so this is a SECOND run of the same slot")


def test_hour_23_still_fires_inside_its_own_hour():
    """The exclusion must cost hour 23 its grace, not its slot."""
    assert cs._should_run_now(23, 23, 23, 40, set())[0] is True


# ---------------------------------------------------------------------------
# Properties the widening must not break.
# ---------------------------------------------------------------------------

def test_already_run_target_does_not_repeat():
    """The widened window makes this load-bearing: without it a slot would
    re-fire on every 60s tick for the rest of its hour."""
    assert cs._should_run_now(8, 20, 8, 30, {8})[0] is False
    assert cs._should_run_now(8, 20, 9, 2, {8})[0] is False, "repeated via the grace"


def test_second_leg_is_independent_of_the_first():
    """A (8, 20) pair that already ran at 08 must still run at 20."""
    should, target = cs._should_run_now(8, 20, 20, 30, {8})
    assert should is True
    assert target == 20


def test_once_a_day_env_still_suppresses_hour2(monkeypatch):
    """★ PRODUCTION PATH. CRAWLER_SCHEDULE=once is set on dchub-worker, so
    target_hours collapses to [hour1] and hour2 never fires. The widened window
    must not resurrect the second leg — that would double every two-slot lane's
    real frequency while its declared dead-man cadence stayed put.
    """
    monkeypatch.setenv("CRAWLER_SCHEDULE", "once")
    assert cs._should_run_now(8, 20, 8, 30, set())[0] is True
    assert cs._should_run_now(8, 20, 20, 30, set())[0] is False, (
        "hour2 fired under CRAWLER_SCHEDULE=once")
    assert cs._should_run_now(8, 20, 21, 2, set())[0] is False, (
        "hour2 fired through the grace under CRAWLER_SCHEDULE=once")


def test_no_schedule_pair_is_an_adjacent_hour():
    """The grace spills one hour forward, so an (h, h+1) pair would let target h
    fire inside target h+1's own window. No such pair exists today; this fails
    the moment someone adds one, which is the point.
    """
    once = [(s[0], s[1], s[2]) for s in cs.SCHEDULE
            if s[0] != s[1] and abs(s[0] - s[1]) == 1]
    assert once == [], (
        f"adjacent-hour SCHEDULE pair(s) {once} — target h can now fire during "
        "h+1, colliding with that slot's own window; give the pair a wider gap")
