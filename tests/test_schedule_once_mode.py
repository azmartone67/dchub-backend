"""CRAWLER_SCHEDULE=once kills hour2 — a job that needs two runs needs two ENTRIES.

`_should_run_now` computes `target_hours = [hour1] if once_a_day else [hour1,
hour2]`, and `CRAWLER_SCHEDULE=once` is the DEPLOYED value on dchub-worker and
dchub-backend. So the second half of every `(a, b)` pair is dead in production.

This cost a real fix. #3263 moved the founding cohort-welcome sweep from `(9)`
to `(9, 21)` to halve a customer's wait for the founder-call invite, and the
21:00 leg never ran. The tell was in the data all along: crm_outbound_flush is
`(7, 19)` and EVERY crm_pushed_at in the table is 07:0x, never 19:0x.

Fenced here:
  1. once-mode really does ignore hour2 (and normal mode really does use it);
  2. a job that must run N times a day has N SCHEDULE entries with distinct
     hour1 — the only construction that survives once-mode;
  3. every SCHEDULE name resolves to a runner, so a typo'd second entry fails
     loudly instead of silently never running;
  4. the dead-man cadence reports the gap the job ACTUALLY keeps, so 33 feeds
     with a distinct hour2 stop reading overdue every night.

Behavioural, against the real module — it imports with no DATABASE_URL.
"""

import collections
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import crawler_scheduler as cs  # noqa: E402


@pytest.fixture
def once(monkeypatch):
    monkeypatch.setenv("CRAWLER_SCHEDULE", "once")


@pytest.fixture
def twice(monkeypatch):
    monkeypatch.delenv("CRAWLER_SCHEDULE", raising=False)


# ── 1. what once-mode actually does ──────────────────────────────────────
def test_once_mode_ignores_hour2(once):
    """The whole trap in one assertion."""
    assert cs._should_run_now(7, 19, 7, 0, set()) == (True, 7)
    assert cs._should_run_now(7, 19, 19, 0, set())[0] is False, (
        "hour2 fired under CRAWLER_SCHEDULE=once — if this ever passes, the "
        "second-entry workaround below is no longer needed and every (a,b) "
        "pair in SCHEDULE became live at once")


def test_normal_mode_uses_both_slots(twice):
    assert cs._should_run_now(7, 19, 7, 0, set()) == (True, 7)
    assert cs._should_run_now(7, 19, 19, 0, set()) == (True, 19)


def test_a_slot_already_run_today_does_not_refire(once):
    assert cs._should_run_now(7, 19, 7, 0, {7})[0] is False


def test_a_doubled_slot_fires_once_in_normal_mode(twice):
    """(21, 21) must not double-fire if CRAWLER_SCHEDULE=once is ever removed."""
    fired, hour = cs._should_run_now(21, 21, 21, 0, set())
    assert (fired, hour) == (True, 21)
    assert cs._should_run_now(21, 21, 21, 5, {21})[0] is False


# ── 2. jobs that must run more than once a day ───────────────────────────
# runner function -> runs required per day, under the DEPLOYED schedule mode.
TWICE_DAILY_REQUIRED = {
    "_run_founding_customer_welcome": 2,
}


def _distinct_hour1_by_runner():
    out = collections.defaultdict(set)
    for hour1, _hour2, name, _ in cs.SCHEDULE:
        fn = cs._RUNNERS.get(name)
        if fn is not None:
            out[fn.__name__].add(int(hour1) % 24)
    return out


def test_the_registry_is_not_empty():
    """Anchor: an empty SCHEDULE satisfies every 'at least N' below."""
    assert len(cs.SCHEDULE) > 50, (
        f"SCHEDULE has {len(cs.SCHEDULE)} entries — the module did not load "
        "properly and this file is not passing, it is blind")


@pytest.mark.parametrize("runner,required", sorted(TWICE_DAILY_REQUIRED.items()))
def test_multi_run_jobs_have_one_entry_per_run(runner, required):
    hours = _distinct_hour1_by_runner().get(runner, set())
    assert len(hours) >= required, (
        f"{runner} must run {required}x/day but has {len(hours)} SCHEDULE "
        f"entr(y/ies) with a distinct hour1 {sorted(hours)}. Under "
        "CRAWLER_SCHEDULE=once only hour1 fires, so a (a, b) PAIR gives ONE "
        "run a day, not two. Add a second entry whose hour1 is the second "
        "time — see ai_platform_onboarder / ai_platform_onboarder_b.")


def test_the_founding_sweep_covers_morning_and_evening():
    """Not just 'two entries' — two entries far enough apart to halve the wait."""
    hours = sorted(_distinct_hour1_by_runner()["_run_founding_customer_welcome"])
    assert len(hours) == 2, hours
    gap = min((hours[1] - hours[0]) % 24, (hours[0] - hours[1]) % 24)
    assert gap >= 8, (
        f"the two founding-welcome slots are {hours} — {gap}h apart. Two slots "
        "bunched together leave the same long overnight wait they exist to cut.")


# ── 3. a second entry that never runs is worse than none ─────────────────
def test_every_scheduled_name_resolves_to_a_runner():
    missing = [s[2] for s in cs.SCHEDULE if s[2] not in cs._RUNNERS]
    assert not missing, (
        f"SCHEDULE names with no _RUNNERS entry: {missing}. The loop silently "
        "skips a name it cannot resolve (`if should_run and name in _RUNNERS`), "
        "so a typo here is a job that never runs and never complains.")


def test_scheduled_names_are_unique():
    names = [s[2] for s in cs.SCHEDULE]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, (
        f"duplicate SCHEDULE names {dupes} — last_run_hours is keyed by name, "
        "so two entries sharing one share a fired-slot set and can suppress "
        "each other. Give the second entry its own name.")


# ── 4. the dead-man cadence must describe the real gap ───────────────────
def test_cadence_reports_the_real_gap_in_once_mode(once):
    assert cs._schedule_cadence_hours(7, 19) == 36.0, (
        "a (7, 19) job runs ONCE a day under once-mode, so its dead-man "
        "threshold must be 24h*1.5, not 12h*1.5 — an 18h threshold reads "
        "OVERDUE from 01:00 to 07:00 every single night on a healthy job")


def test_cadence_still_honours_a_real_pair_in_normal_mode(twice):
    assert cs._schedule_cadence_hours(7, 19) == 18.0
    assert cs._schedule_cadence_hours(9, 9) == 36.0


def test_cadence_is_never_below_the_floor(once):
    assert cs._schedule_cadence_hours(3, 4, floor=3.0) >= 3.0

# ── 5. the two writers of the shared feed must agree ─────────────────────
def test_shared_feed_cadence_matches_the_runners_own_stamp():
    """founding_customer_welcome is beaten by _run_with_guard (entry name) AND
    by the runner's explicit _stamp_cron_run. If those disagree the feed's
    cadence flip-flops depending on which landed last. Pinned to each other by
    parsing the stamp out of the source, so neither can drift alone."""
    import ast
    src = open(os.path.join(ROOT, "crawler_scheduler.py"), encoding="utf-8").read()
    stamps = [n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "_stamp_cron_run"
              and n.args and isinstance(n.args[0], ast.Constant)
              and n.args[0].value == "founding_customer_welcome"]
    assert len(stamps) == 1, (
        f"expected exactly one _stamp_cron_run for this feed, found {len(stamps)}")
    seconds = stamps[0].args[1].value
    expected = round((seconds / 3600.0) * 1.5, 1)
    assert cs._SCHEDULE_CADENCE_H["founding_customer_welcome"] == expected, (
        f"the guard-beat cadence "
        f"{cs._SCHEDULE_CADENCE_H['founding_customer_welcome']} disagrees with "
        f"the runner's own {seconds}s stamp (= {expected}h with grace) — the "
        "feed will report whichever writer landed last")


def test_the_pm_feed_keeps_its_own_daily_cadence():
    """_run_with_guard beats the _pm name ONCE a day; claiming 12h there would
    make a healthy feed read overdue every afternoon."""
    assert cs._SCHEDULE_CADENCE_H["founding_customer_welcome_pm"] > 18.0
