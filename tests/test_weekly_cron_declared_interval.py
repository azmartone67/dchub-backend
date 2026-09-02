"""D11 (QA sweep 2026-09-02) — weekly Railway arms declare their interval, and
both liveness detectors read the declaration when the row has none.

MEASURED 2026-09-02: loop-control lane 1 `cron_liveness=FAIL "gas-refresh
silent 236612s (65.7h)"`; heal `cron_silently_dead x2 "no declared interval,
34.8h past the stale threshold"`. dchub-jobs.yml schedules gas-refresh and
site-baseline `30 6 * * 0` (weekly, Sunday). brain_consistency_radar's
check_cron_freshness fell back to the 30h default because cron_last_run.
expected_interval_s was NULL — only _record_cron_run stamps it, from
jobs_routes._JOB_INTERVALS, and neither job was in that map. False red six
days out of seven, on two detectors.

The fix: declare 7d in _JOB_INTERVALS (stamped on the next run), and have
BOTH detectors consult the map when the column is NULL so the threshold is
right immediately.
"""
from __future__ import annotations

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEEK = 7 * 86400


def test_weekly_arms_declare_a_week():
    import routes.jobs_routes as jr
    assert jr._JOB_INTERVALS.get("gas-refresh") == WEEK
    assert jr._JOB_INTERVALS.get("site-baseline") == WEEK


def test_the_declaration_matches_the_workflow_schedule():
    """The map must not drift from dchub-jobs.yml: the arm that runs on the
    weekly cron is the one that lists these two jobs."""
    with open(os.path.join(ROOT, ".github", "workflows", "dchub-jobs.yml"),
              encoding="utf-8") as fh:
        yml = fh.read()
    assert "'30 6 * * 0'" in yml, "weekly Sunday cron gone from dchub-jobs.yml"
    m = re.search(r'"30 6 \* \* 0"\)\s*JOBS="([^"]+)"', yml)
    assert m, "the `30 6 * * 0` case arm no longer names its jobs"
    jobs = set(m.group(1).split(","))
    assert {"gas-refresh", "site-baseline"} <= jobs, jobs


# ── the radar: NULL column -> declared interval -> no false red ───────────

class _Cur:
    def __init__(self, rows):
        self._rows = rows
        self._last = None

    def execute(self, sql, params=None):
        self._last = " ".join(str(sql).split())

    def fetchone(self):
        return ("cron_last_run",)          # to_regclass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)

    def close(self):
        pass


def _radar_findings(monkeypatch, rows):
    import routes.brain_consistency_radar as bcr
    monkeypatch.setattr(bcr, "_db", lambda: _Conn(rows))
    return {f["url"]: f for f in bcr.check_cron_freshness()
            if f["issue"] == "cron_silently_dead"}


def test_radar_reads_the_declared_interval_when_the_column_is_null(monkeypatch):
    """Kills: falling to the 30h default for an unstamped weekly arm. Rows are
    (job_name, last_started_at, expected_interval_s, run_count, seconds_since)."""
    rows = [
        ("gas-refresh",   None, None, 12, 236612),          # 65.7h — the live value
        ("site-baseline", None, None, 9,  5 * 86400),       # 5d: fine for a weekly job
        ("news-refresh",  None, None, 400, 40 * 3600),      # 40h, undeclared: 30h default still bites
    ]
    found = _radar_findings(monkeypatch, rows)
    assert "/api/jobs/gas-refresh" not in found, found.get("/api/jobs/gas-refresh")
    assert "/api/jobs/site-baseline" not in found
    assert "/api/jobs/news-refresh" in found, "the default must still catch an undeclared daily job"


def test_radar_still_flags_a_weekly_arm_two_weeks_silent(monkeypatch):
    """The declaration is a threshold, not an allowlist."""
    rows = [("gas-refresh", None, None, 12, 15 * 86400)]
    found = _radar_findings(monkeypatch, rows)
    f = found.get("/api/jobs/gas-refresh")
    assert f, "a weekly arm silent for 15 days must be flagged"
    assert f["count_kind"] == "seconds_since" and f["count"] == 15 * 86400
    assert "expected every 604800s" in f["detail"], f["detail"]
    assert "declared" in f["detail"]


def test_a_stamped_column_still_wins_over_the_declaration(monkeypatch):
    rows = [("gas-refresh", None, 3600, 12, 236612)]      # row says hourly
    found = _radar_findings(monkeypatch, rows)
    assert "/api/jobs/gas-refresh" in found


# ── loop-control lane 1 agrees ────────────────────────────────────────────

@pytest.fixture
def lc(monkeypatch):
    pytest.importorskip("flask")
    import routes.loop_control_master_shell as m
    monkeypatch.setattr(m, "_has_table", lambda c, name: True)
    return m


def _lane(lc, monkeypatch, rows):
    """rows: (job_name, expected_interval_s, seconds_since)."""
    worst = max(rows, key=lambda r: r[2]) if rows else None
    monkeypatch.setattr(lc, "_rows", lambda c, sql, params=None: rows)
    monkeypatch.setattr(lc, "_row", lambda c, sql, params=None: worst)
    return {c["id"]: c for c in lc._lane_cron_liveness(object())}


def test_loop_control_lane_does_not_flag_an_unstamped_weekly_arm(lc, monkeypatch):
    checks = _lane(lc, monkeypatch, [("gas-refresh", None, 236612),
                                     ("alert-emails", 4 * 3600, 900)])
    assert checks["no_dead_crons"]["pass"] is True, checks["no_dead_crons"]["detail"]
    # the worst offender is the weekly arm at 65.7h — inside ITS limit, not 48h
    assert checks["worst_offender"]["pass"] is True, checks["worst_offender"]["detail"]
    assert "gas-refresh" in checks["worst_offender"]["detail"]


def test_loop_control_lane_still_flags_dead_crons(lc, monkeypatch):
    checks = _lane(lc, monkeypatch, [("gas-refresh", None, 15 * 86400),
                                     ("news-refresh", None, 40 * 3600),
                                     ("alert-emails", 4 * 3600, 9 * 3600)])
    assert checks["no_dead_crons"]["pass"] is False
    d = checks["no_dead_crons"]["detail"]
    assert d.startswith("3 job(s) past threshold"), d
    assert "gas-refresh" in d and "news-refresh" in d and "alert-emails" in d
    assert checks["worst_offender"]["pass"] is False


def test_loop_control_lane_fails_closed_without_the_map(lc, monkeypatch):
    """An import failure must over-report, never certify (the retirement
    allowlist has the same rule)."""
    monkeypatch.setattr(lc, "_declared_intervals", lambda: {})
    checks = _lane(lc, monkeypatch, [("gas-refresh", None, 236612)])
    assert checks["no_dead_crons"]["pass"] is False


def test_both_queries_still_exclude_retired_jobs(lc):
    """The retirement test pins the literal twice; keep it honest here too."""
    import inspect
    src = inspect.getsource(lc._lane_cron_liveness)
    assert src.count("NOT (job_name = ANY(%(retired)s))") == 2
