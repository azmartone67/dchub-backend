"""A prioritiser that cannot fail is the problem it was built to solve.

The whole premise of this module is that DC Hub does not lack detection — it
lacks anything that decides what to take next. That makes two failure modes
fatal rather than cosmetic:

  · it silently reads nothing and recommends nothing, forever (the exact shape
    of the loops it exists to unstick), and
  · one loud recurring item pins the top of the list permanently, which is
    already what happened to `human_decisions` at seen_count 1220/328/1790.

Both are pinned here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routes.brain_product_lead import (  # noqa: E402
    _AGE_CAP_DAYS,
    _MIN_TOTAL_ROWS,
    _RECURRENCE_CAP,
    score,
    verdict_for,
)


def test_recurrence_is_capped_so_one_loud_item_cannot_own_the_list():
    """★ seen_count 1790 must not outrank everything forever. That is not a
    hypothetical: `addressable_demand_unconverted` has been surfaced 1,790
    times and consumed zero times, and an uncapped rank would keep it pinned
    at #1 while nothing else was ever looked at."""
    loud = score(age_days=10, recurrence=1790, ready=False)
    merely_high = score(age_days=10, recurrence=_RECURRENCE_CAP, ready=False)
    assert loud == merely_high, "recurrence past the cap must stop adding weight"


def test_age_is_capped_too():
    assert score(400, 1, False) == score(_AGE_CAP_DAYS, 1, False)


def test_older_still_outranks_newer_below_the_cap():
    """Capping must not flatten the signal entirely — inside the cap, age has
    to still order things or the rank carries no information."""
    assert score(90, 1, False) > score(10, 1, False)


def test_more_recurrence_still_outranks_less_below_the_cap():
    assert score(10, 50, False) > score(10, 5, False)


def test_ready_work_outranks_identical_blocked_work():
    """A list where every top row is structurally stuck is a list nobody can
    start. Ready work must win ties."""
    assert score(30, 10, ready=True) > score(30, 10, ready=False)


def test_score_is_monotonic_and_never_negative():
    prev = -1.0
    for age in (0, 1, 5, 20, 60, 119, 120, 500):
        s = score(age, 1, False)
        assert s >= 0
        assert s >= prev
        prev = s


def test_floor_constant_is_meaningful():
    """The floor exists so four empty queues read as a broken reader rather
    than a cleared backlog. A floor of 0 would restore the silent green."""
    assert _MIN_TOTAL_ROWS > 0


def _verdict_for(n_items, failed=None):
    """Thin adapter over the ROUTE'S OWN function — deliberately not a
    re-implementation. A copy of the branch here would stay green while the
    route regressed, which is the exact failure this module exists to catch."""
    v = verdict_for(n_items, failed)
    return v["verdict"], v["ok"]


def test_empty_read_is_unmeasurable_not_all_clear():
    assert _verdict_for(0) == ("unmeasurable", False)
    assert _verdict_for(_MIN_TOTAL_ROWS - 1) == ("unmeasurable", False)


def test_a_failed_queue_is_never_reported_as_ok():
    """★ A queue that ERRORED is not a queue that is empty. If a broken read
    counted as zero parked items, the ranker would announce a cleared backlog
    on the day its own SQL broke."""
    assert _verdict_for(500, failed={"brain_findings": "ProgrammingError: x"}) == \
        ("unmeasurable", False)


def test_healthy_backlog_reports_ok():
    assert _verdict_for(419) == ("ok", True)
