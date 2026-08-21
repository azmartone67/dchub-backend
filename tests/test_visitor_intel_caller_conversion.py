"""The visitor-intelligence headline conversion rate must dedupe by caller.

mark_signals_converted() flips EVERY mcp_upgrade_signals row belonging to a
converting email, so `COUNT(*) FILTER (WHERE converted)` counts one buyer who
hit the paywall N times as N conversions. routes/mcp_funnel.py measured 69
"conversions" that were a single operator email flipped at one instant and
fixed it on 2026-06-15 with COUNT(DISTINCT caller_id); visitor-intelligence
kept publishing the raw count as its headline.

tests/test_funnel_consistency.py already asserts this — but it fetches LIVE
production and is skipif(not DCHUB_ADMIN_KEY), so CI skips it and it has never
gated anything. These tests drive the real _compute() against a scripted
cursor, so the rule is enforced offline, on every run.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TOTALS_ROW = (6010, 900, 40, 700, 12, 5, 25)   # matches the live totals SELECT


class _Cur:
    """Answers by SQL shape, so the caller query is served its own row."""

    def __init__(self, caller_row=(10, 3), caller_raises=False):
        self._caller_row = caller_row
        self._caller_raises = caller_raises
        self._last = ""
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._last = " ".join(str(sql).split())
        if "COUNT(DISTINCT caller_id)" in self._last and self._caller_raises:
            raise RuntimeError('column "caller_id" does not exist')

    def fetchone(self):
        if "COUNT(DISTINCT caller_id)" in self._last:
            return self._caller_row
        if "AS total_signals" in self._last:
            return _TOTALS_ROW
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self, *a, **k):
        return self._cur

    def rollback(self):
        self._cur.rolled_back = True


@pytest.fixture
def compute(monkeypatch):
    import routes.visitor_intelligence as V

    def _run(**kw):
        cur = _Cur(**kw)
        monkeypatch.setattr(V, "_get_db", lambda: _Conn(cur))
        return V._compute(days=30), cur

    return _run


def test_headline_exposes_a_distinct_caller_conversion_rate(compute):
    """★ The guard tests/test_funnel_consistency.py asserts against live prod."""
    out, _cur = compute()
    totals = out["totals"]

    assert "converted_callers" in totals and "caller_conv_rate_pct" in totals, (
        "visitor-intelligence headline still publishes only the raw "
        "signals/conversions ratio, which anon-signal inflation pins near 0%")
    assert totals["unique_callers"] == 10
    assert totals["converted_callers"] == 3
    assert totals["caller_conv_rate_pct"] == 30.0, (
        "the rate is not converted-callers over distinct callers")


def test_the_rate_is_not_computed_from_raw_row_counts(compute):
    """The inflated number must not be what the rate is built from: the totals
    row carries conversions=5 over 6,010 signals (0.08%); the honest answer is
    3 of 10 callers (30%)."""
    out, _cur = compute()
    totals = out["totals"]
    assert totals["conversions"] == 5, "raw count should still be reported"
    raw_ratio = round(totals["conversions"] * 100.0
                      / totals["total_paywall_signals"], 2)
    assert totals["caller_conv_rate_pct"] != raw_ratio, (
        f"the headline rate equals the raw signals ratio ({raw_ratio}%) — this "
        "is the structural 0% the guard exists to prevent")


def test_no_callers_reports_no_rate_rather_than_zero_percent(compute):
    """'0% converted' and 'no callers to measure' are different claims, and
    this repo has been burned by fallbacks that invent the confident one."""
    out, _cur = compute(caller_row=(0, 0))
    assert out["totals"]["unique_callers"] == 0
    assert out["totals"]["caller_conv_rate_pct"] is None


def test_a_missing_caller_id_column_does_not_zero_the_other_totals(compute):
    """★ Why this is a separate query. psycopg2 aborts the whole transaction on
    one failed statement, so folding caller_id into the totals SELECT would
    turn a missing column into every-later-read-returns-nothing — the all-zero
    /agent/index class. It must degrade to nulls and roll back."""
    out, cur = compute(caller_raises=True)
    totals = out["totals"]

    assert cur.rolled_back, "the failed query left the transaction aborted"
    assert totals["caller_conv_rate_pct"] is None
    assert totals["converted_callers"] is None
    assert totals["total_paywall_signals"] == 6010, (
        "a caller-query failure wiped the rest of the totals")
    assert totals["unique_sessions"] == 900
