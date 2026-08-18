"""tests/test_reach_trend_honesty.py — two ways /api/v1/ai/reach/trend published
an unreadable acquisition signal (2026-08-17).

★ new_external_ips EXCEEDED distinct_external_ips IN EVERY WEEK. The counter read
`SELECT COUNT(*) FROM reach_ip_seen WHERE first_seen_week = <week>` — every row
ever stamped to that week, which is NOT the set of IPs the week actually saw.
reach_ip_seen is cumulative and `ON CONFLICT DO NOTHING`, so stamps written by
the agent_requests-era writer and by the pre-07-27 looser predicate are permanent
and still counted. Measured live 2026-08-17:

    week 2026-07-06:  43 distinct IPs, 245 "new"
    week 2026-07-13:  81 distinct IPs, 179 "new"
    week 2026-05-25:   4 distinct IPs,  33 "new"

"IPs never seen before" cannot exceed "IPs seen".

★ THE IN-PROGRESS WEEK LOOKED LIKE A COLLAPSE. weeks[] ends with the current ISO
week holding only the hours elapsed at the last rollup. On Monday 2026-08-17 it
served 0 right after a complete week of 72. A prior session (08-05) already
mis-read this as a real 93% drop.

Run:  python3 -m pytest tests/test_reach_trend_honesty.py -v
"""
from __future__ import annotations

import datetime as dt

import pytest

from routes import ai_reach_rollup as rr


# ── a cursor that EMULATES reach_ip_seen, so the bound is pinned by behaviour ──
#
# A test that only greps the SQL for "ANY(" would pass against a query that
# builds the clause and never applies it. This fake resolves the COUNT the way
# Postgres would: full stamped set, intersected with the ANY() list when the
# statement carries one.

class _Cur:
    def __init__(self, week_rows, stamped_ips):
        self.week_rows = week_rows        # [(platform, ip, calls)] this week
        self.stamped = set(stamped_ips)   # reach_ip_seen rows for this week
        self.executed = []
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        s = " ".join(sql.split())
        if "FROM mcp_calls_identity" in s:
            self._last = list(self.week_rows)
        elif "COUNT(*) FROM reach_ip_seen" in s:
            hits = self.stamped
            if "ip_address = ANY(" in s:
                bound = params[1] if params and len(params) > 1 else []
                hits = self.stamped & set(bound)
            self._last = [(len(hits),)]
        else:
            self._last = [(0,)]

    def fetchall(self):
        return self._last or []

    def fetchone(self):
        return (self._last or [(0,)])[0]


@pytest.fixture(autouse=True)
def _no_execute_values(monkeypatch):
    """The seen-set INSERT is not under test; keep it from touching a driver."""
    monkeypatch.setattr(rr.psycopg2.extras, "execute_values",
                        lambda cur, sql, argslist, **k: None)


WEEK = dt.date(2026, 7, 6)
NEXT = dt.date(2026, 7, 13)


def _week_rows(n_ips, platform="claude"):
    return [(platform, f"203.0.113.{i}", 5) for i in range(n_ips)]


# ── THE PIN ────────────────────────────────────────────────────────────────

def test_new_ips_cannot_exceed_distinct_ips():
    """THE PIN — the exact live 2026-07-06 shape: 43 IPs seen this week, 245
    legacy stamps carrying that week's first_seen_week, only 12 of them real."""
    rows = _week_rows(43)
    this_weeks_ips = {ip for _p, ip, _n in rows}
    legacy = {f"104.16.0.{i}" for i in range(233)}      # CF POP era stamps
    stamped = set(list(this_weeks_ips)[:12]) | legacy   # 245 total
    assert len(stamped) == 245

    cur = _Cur(rows, stamped)
    out = rr._compute_week(cur, WEEK, NEXT)

    assert out["distinct_external_ips"] == 43
    assert out["new_external_ips"] == 12, (
        "new_external_ips must count only IPs THIS week actually saw; the "
        "unbounded count returns 245")
    assert out["new_external_ips"] <= out["distinct_external_ips"], \
        "IPs never seen before cannot exceed IPs seen"


def test_bound_holds_when_every_ip_is_new():
    """Upper edge: a week where all 43 are genuinely first-seen still reports
    43, so the bound is not silently clamping real acquisition to zero."""
    rows = _week_rows(43)
    stamped = {ip for _p, ip, _n in rows}
    out = rr._compute_week(_Cur(rows, stamped), WEEK, NEXT)
    assert (out["distinct_external_ips"], out["new_external_ips"]) == (43, 43)


def test_week_with_no_traffic_reports_zero_new():
    """A zero week must not inherit legacy stamps as acquisition — and must not
    issue an `= ANY('{}')` count at all."""
    cur = _Cur([], {f"104.16.0.{i}" for i in range(33)})
    out = rr._compute_week(cur, WEEK, NEXT)
    assert out["distinct_external_ips"] == 0
    assert out["new_external_ips"] == 0
    assert not any("reach_ip_seen WHERE first_seen_week" in " ".join(s.split())
                   and "COUNT(*)" in s for s, _p in cur.executed)


def test_returning_ips_are_not_counted_as_new():
    """The signal's whole purpose: IPs stamped to an EARLIER week are returning,
    not new, even though they are in this week's set."""
    rows = _week_rows(10)
    out = rr._compute_week(_Cur(rows, set()), WEEK, NEXT)   # nothing stamped here
    assert out["distinct_external_ips"] == 10
    assert out["new_external_ips"] == 0


# ── the in-progress week must declare itself ───────────────────────────────

def _rows():
    return [
        {"week_start": "2026-08-03", "distinct_external_ips": 38,
         "computed_at": "2026-08-17T02:53:57"},
        {"week_start": "2026-08-10", "distinct_external_ips": 72,
         "computed_at": "2026-08-17T02:53:57"},
        {"week_start": "2026-08-17", "distinct_external_ips": 0,
         "computed_at": "2026-08-17T02:53:57"},
    ]


def test_in_progress_week_is_flagged_partial():
    """THE PIN — the Monday-morning zero must not be chartable as a complete
    week. This is the row a prior session read as a 93% collapse."""
    rows = rr.mark_partial_weeks(_rows(), "2026-08-17")
    assert rows[-1]["partial"] is True
    assert rows[-1]["distinct_external_ips"] == 0


def test_complete_weeks_are_not_flagged_partial():
    """Control: without this, flagging everything partial would also pass."""
    rows = rr.mark_partial_weeks(_rows(), "2026-08-17")
    assert [r["partial"] for r in rows] == [False, False, True]


def test_coverage_hours_states_how_much_of_the_week_was_measured():
    """0 agents across 3 hours is a different claim from 0 across 7 days, and
    the number is what tells them apart."""
    rows = rr.mark_partial_weeks(_rows(), "2026-08-17")
    assert rows[-1]["coverage_hours"] == pytest.approx(2.9, abs=0.1)
    assert "coverage_hours" not in rows[0], \
        "a complete week has no partial coverage to report"


def test_coverage_hours_is_none_when_it_cannot_be_derived():
    """A missing computed_at must read as unknown, never as a measured 0."""
    rows = rr.mark_partial_weeks(
        [{"week_start": "2026-08-17", "computed_at": None}], "2026-08-17")
    assert rows[0]["partial"] is True
    assert rows[0]["coverage_hours"] is None


def test_no_current_week_row_means_nothing_is_partial():
    """The rollup can lag a week boundary; that must not mislabel the last
    complete week as in-progress."""
    rows = rr.mark_partial_weeks(_rows()[:2], "2026-08-17")
    assert [r["partial"] for r in rows] == [False, False]


def test_empty_input_does_not_raise():
    assert rr.mark_partial_weeks([], "2026-08-17") == []
    assert rr.mark_partial_weeks(None, "2026-08-17") is None
