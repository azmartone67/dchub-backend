"""Guard for the robust WoW baseline — routes/weekly_series.py (2026-08-11).

★ THE DEFECT ONE LAYER UP FROM THE ONE THIS MODULE ALREADY FIXED

Fixing the window to complete ISO weeks removed a baseline that MOVED under the
reader. It did not remove a baseline that is a SINGLE OBSERVATION. A
week-over-week percentage inherits all of its baseline week's volatility, so
when that one week is an outlier the honest series still produces a dishonest
headline.

Live, the day this was written:

    2026-07-06   43 agents   3,514 calls
    2026-07-13   81 agents   2,701 calls
    2026-07-20   62 agents   1,971 calls
    2026-07-27   85 agents   8,334 calls   <- baseline, 3.57x its neighbours
    2026-08-03   38 agents   2,381 calls   <- current

Published delta: calls -71.4%. But 2,381 sits inside the established
1,971-3,514 band. Calls did not fall 71%; they returned to trend from a spike.

★ THE TEST THAT MATTERS IS test_the_agent_decline_survives_the_correction.

A robust baseline is not a way to make bad weeks look better, and if it were,
it would be worse than the bug. The calls panic dissolves (-71% -> -23%) and
the agent decline STANDS (-55% -> -47%). That asymmetry is the only reason to
trust the instrument.

Pure functions: no DB, no network, and never imports main.
"""
import datetime as _dt

import pytest

# The module imports a DB connection at import time, so exec the pure block.
# ★ The slice starts at _DEFINITION_CHANGES, not _ROBUST_BASELINE_WEEKS:
# _robust_wow calls _comparability, which is defined in that earlier block.
# Slicing below it produced a NameError at call time, not at exec time — i.e.
# the tests would have failed loudly, but only after appearing to import fine.
_SRC = open("routes/weekly_series.py", encoding="utf-8").read()
_NS = {"_dt": _dt}
exec(_SRC[_SRC.index("_DEFINITION_CHANGES = ["):_SRC.index("def _partial_week")], _NS)

_robust_wow = _NS["_robust_wow"]
_baseline_outlier_flag = _NS["_baseline_outlier_flag"]
_median = _NS["_median"]


def _w(start, agents, calls, status="measured"):
    return {"week_start": start, "agents": agents, "calls": calls, "status": status}


# The real series, verbatim from the live endpoint on 2026-08-11.
LIVE = [
    _w("2026-06-15", 0, 38), _w("2026-06-22", 0, 84), _w("2026-06-29", 1, 281),
    _w("2026-07-06", 43, 3514), _w("2026-07-13", 81, 2701),
    _w("2026-07-20", 62, 1971), _w("2026-07-27", 85, 8334),
    _w("2026-08-03", 38, 2381),
]


def test_the_calls_panic_was_an_artifact_of_the_baseline():
    r = _robust_wow(LIVE)
    assert r["calls_pct"] == -23.4, "the published single-week delta said -71.4%"
    assert r["baseline_calls"] == 3107.5
    assert r["baseline_weeks_used"] == [
        "2026-07-06", "2026-07-13", "2026-07-20", "2026-07-27"]


def test_the_agent_decline_survives_the_correction():
    """THE test. If a robust baseline flattered every metric it would be a
    worse instrument than the one it replaces. The agent decline must stand.
    """
    r = _robust_wow(LIVE)
    assert r["agents_pct"] == -46.9
    assert r["agents_pct"] < -40, (
        "the agent fall is real and must survive — a baseline that erases it "
        "is a vanity metric, not a correction"
    )


def test_the_outlier_baseline_is_flagged():
    f = _baseline_outlier_flag(LIVE)
    assert f["checked"] is True
    assert f["is_outlier"] is True
    assert f["baseline_week_start"] == "2026-07-27"
    assert f["ratio_to_median"] == 3.57
    assert "read `robust_wow` instead" in f["means"]


def test_an_in_line_baseline_is_not_flagged():
    """The flag must discriminate. If it fired on every week it would carry no
    information and readers would learn to ignore it.
    """
    steady = [_w(f"2026-0{i}-01", 50, 2000 + i * 10) for i in range(1, 8)]
    f = _baseline_outlier_flag(steady)
    assert f["checked"] is True
    assert f["is_outlier"] is False
    assert "broadly agree" in f["means"]


def test_the_rule_is_published_and_declared_non_inferential():
    """With n this small, dressing a ratio threshold as a significance test
    would be theatre. The payload must say so.
    """
    f = _baseline_outlier_flag(LIVE)
    assert "NOT a significance test" in f["rule"]
    assert "2.0x" in f["rule"] or "2.0" in f["rule"]
    r = _robust_wow(LIVE)
    assert r["baseline_kind"] == "trailing_median"
    assert r["baseline_n"] == 4
    assert r["baseline_window_weeks"] == 4


def test_refuses_rather_than_shrinking_the_window():
    """Same discipline as _wow: too little history refuses outright. Silently
    computing a 2-week median and calling it a 4-week baseline is the exact
    class of quiet dishonesty this module exists to retire.
    """
    r = _robust_wow(LIVE[:3])
    assert r["agents_pct"] is None and r["calls_pct"] is None
    assert "need 5 measured complete weeks" in r["reason"]


def test_unmeasured_weeks_are_never_arithmeticd():
    """A null week is a missing observation, not a zero. It must not enter a
    median — that would silently drag the baseline toward zero.
    """
    with_gap = LIVE[:-1] + [_w("2026-08-03", None, None, status="no_observation")]
    r = _robust_wow(with_gap)
    # The unmeasured week is dropped, so the window slides back to real weeks.
    assert r["current_week_start"] != "2026-08-03"


def test_zero_baseline_withholds_rather_than_fabricates():
    zeros = [_w(f"2026-0{i}-01", 0, 0) for i in range(1, 6)] + [_w("2026-06-01", 5, 50)]
    r = _robust_wow(zeros)
    assert r["agents_pct"] is None and r["calls_pct"] is None
    assert "undefined" in (r["reason"] or "")


def test_median_is_a_median():
    assert _median([1, 2, 3]) == 2.0
    assert _median([1, 2, 3, 4]) == 2.5
    assert _median([]) is None
    assert _median([None, None]) is None
    assert _median([5, None, 1]) == 3.0     # nulls dropped, not zeroed


def test_both_deltas_are_published():
    """`wow` keeps its key and meaning for existing consumers. Replacing it
    silently would break every reader that already quotes it.
    """
    assert 'out["wow"] = _wow(out["weeks"])' in _SRC
    assert 'out["robust_wow"] = _robust_wow(out["weeks"])' in _SRC
    assert 'out["wow_baseline_check"] = _baseline_outlier_flag(out["weeks"])' in _SRC
