"""The news-ingest guard must be able to FAIL, and must never report ok when
it could not measure.

The five-day 2026-09-03 intake collapse was invisible because every existing
check answered a question that stayed true while volume fell: "did the job
finish", "is the newest story recent". This guard answers "were rows added",
so these tests pin the two ways that answer can go wrong:

  · it reports ok on a window it could not measure (the silent-green class), and
  · its clock column is dead, so every window counts zero and a healthy day is
    indistinguishable from a stalled one.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from routes.news_ingest_health import (  # noqa: E402
    _BROAD_SWEEP_SOURCES,
    _DEGRADED_RATIO,
    _MIN_BASELINE_DAYS,
    _STALL_HOURS,
    _self_test,
    _verdict,
)


def _m(rows_24h=200, sources=25, sweeps=8, hours_since=2.0,
       baseline=180.0, baseline_days=20):
    return {
        "rows_24h": rows_24h,
        "distinct_sources_24h": sources,
        "rows_7d": rows_24h * 7,
        "broad_sweeps_24h": sweeps,
        "last_broad_sweep_at": "2026-09-08T18:39:00+00:00",
        "hours_since_broad_sweep": hours_since,
        "baseline_median_rows_per_day": baseline,
        "baseline_days_observed": baseline_days,
    }


def test_healthy_day_is_ok():
    v = _verdict(_m())
    assert v["verdict"] == "ok" and v["ok"] is True


def test_the_actual_september_collapse_is_caught():
    """The real numbers. 2026-09-06 wrote 5 rows from 4 sources against a
    trailing median near 180/day, and no broad sweep for days. If this passes
    as ok the guard is decorative."""
    v = _verdict(_m(rows_24h=5, sources=4, sweeps=0, hours_since=52.0))
    assert v["ok"] is False
    assert v["verdict"] == "stalled"


def test_degraded_fires_on_volume_alone_even_with_a_recent_sweep():
    """A sweep happened, so the stall arm stays quiet — volume must still fail
    on its own or a trickle passes forever."""
    v = _verdict(_m(rows_24h=10, sources=6, sweeps=1, hours_since=2.0,
                    baseline=180.0))
    assert v["verdict"] == "degraded" and v["ok"] is False


def test_unmeasurable_is_never_ok():
    """★ The whole point. 'Could not look' must not render as a clean bill of
    health — that is the failure mode this guard exists to end."""
    v = _verdict(_m(baseline=None, baseline_days=0))
    assert v["ok"] is False
    assert v["verdict"] == "unmeasurable"

    v2 = _verdict(_m(baseline=180.0, baseline_days=_MIN_BASELINE_DAYS - 1))
    assert v2["ok"] is False, "too few baseline days must not be judged ok"


def test_floor_is_derived_from_the_baseline_not_a_constant():
    """No invented targets: the same row count must be judged differently
    against different histories, or the floor is a hardcoded number wearing a
    baseline's clothes."""
    rows = 40
    strict = _verdict(_m(rows_24h=rows, baseline=400.0))
    lax = _verdict(_m(rows_24h=rows, baseline=60.0))
    assert strict["ok"] is False
    assert lax["ok"] is True
    assert strict["floor_rows_per_day"] != lax["floor_rows_per_day"]


def test_every_verdict_states_its_failure_condition():
    for m in (_m(), _m(rows_24h=5, sweeps=0, hours_since=99.0),
              _m(baseline=None, baseline_days=0)):
        assert _verdict(m).get("red_when"), "a check that cannot state red_when is not a check"


class _FakeCur:
    """Returns a scripted value per query shape, so the self-test legs can be
    driven into failure without a database."""

    def __init__(self, present=True, non_null=12271, future_rows=0):
        self._present, self._non_null, self._future = present, non_null, future_rows
        self._next = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "to_regclass" in s:
            self._next = (self._present,)
        elif "NOW() + INTERVAL" in s:
            self._next = (self._future,)
        elif "count(fetched_at)" in s:
            self._next = (self._non_null,)
        else:
            raise AssertionError(f"unexpected query in self-test: {s[:80]}")

    def fetchone(self):
        return self._next


def test_self_test_passes_on_a_healthy_table():
    assert _self_test(_FakeCur())["passed"] is True


def test_dead_clock_column_fails_the_guard():
    """★ THE COLUMN TRAP. news_articles.created_at is NULL on all 12,271 rows.
    A guard keyed on it counts zero for every window and reports a permanent
    collapse — or, keyed the other way, dismisses a real one. If this leg can
    pass on a fully-NULL column the guard cannot be trusted either direction."""
    st = _self_test(_FakeCur(non_null=0))
    assert st["passed"] is False
    assert st["legs"]["clock_column_live"]["passed"] is False


def test_absent_table_fails_rather_than_counting_as_zero_ingestion():
    st = _self_test(_FakeCur(present=False))
    assert st["passed"] is False
    assert st["legs"]["table_present"]["passed"] is False


def test_negative_canary_fails_when_the_time_filter_is_inert():
    """If a far-future window returns rows, the WHERE clause is not filtering
    and every count this guard publishes is meaningless."""
    st = _self_test(_FakeCur(future_rows=7))
    assert st["passed"] is False
    assert st["legs"]["negative_canary"]["passed"] is False


def test_thresholds_are_sane():
    assert _BROAD_SWEEP_SOURCES > 4, "degraded days reached 4 sources; the bar must clear them"
    assert _STALL_HOURS >= 12, "the driver fires every 3h; a short bar would flap"
    assert 0 < _DEGRADED_RATIO < 1
