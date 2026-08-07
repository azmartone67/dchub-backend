"""Guard: the data-liveness board must convict a treadmill, must NEVER convict
a layer whose history it cannot read, and must never publish an instrument
repoint as growth.

FENCES routes/data_liveness_master_shell.py. Every test drives the real shipped
functions (_classify, _lane_treadmill, _lane_never_ran, _lane_health_signal,
_population) against a fake cursor.

──────────────────────────────────────────────────────────────────────────
★★ TWO REGRESSIONS CAUGHT ON LIVE DATA BEFORE MERGE, BOTH FENCED HERE.

1. SHARE-OF-NET vs SHARE-OF-TABLE (test_small_one_day_change_is_real_growth).
   The first classifier called a window a STEP when the largest single day held
   >=90% of the window's NET CHANGE. That is 100% for ANY layer whose change
   landed on one day — so metro_fiber_routes going 55,046 -> 55,064 (+18 rows,
   entirely real) was flagged as a discontinuity and erased from growth. The
   magnitude that matters is share of the TABLE.

2. STEP-REMOVED ZERO PUBLISHED AS A MEASUREMENT
   (test_discontinuity_is_unmeasurable_not_a_treadmill). The first version set
   sustained = net - step and handed that on as a number. For transmission_lines
   (0 -> 95,560 on the 2026-08-07 repoint) that produced sustained = 0, and the
   treadmill lane then CONVICTED a layer that had existed for one day of
   accusing it of adding nothing for thirty. Unknown must render None.

──────────────────────────────────────────────────────────────────────────
Every "unreadable" assertion is `is None`, never `not passed` — `assert not x`
passes on False and would let the exact defect through.

Live figures, measured 2026-08-07 on the Neon read replica:
    gas_pipelines       30,918 rows, 30,000 rewritten in 30d, net   0  TREADMILL
    transmission_lines  95,560 rows, 95,558 rewritten,  0 -> 95,560  DISCONTINUITY
    power_plants        14,480 rows, 14,414 rewritten, 13,446 -> 14,480  STEP
    data_centers        24,472 rows, net +2,543 / 30d, spread     GROWING
    metro_fiber_routes  55,064 rows, net +18 / 30d                GROWING
    substations        126,858 rows, net +65 / 30d                GROWING
    6 of 23 stampable jobs never in cron_last_run (incl. subsea_sync)
    11 of 21 jobs carry a last_status from a 2nd caller (worst 9,120 min)

No DB and no network. Nothing runs at module scope.

Run locally:
    python3 -m pytest tests/test_data_liveness_shell.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

GAS_ROWS, GAS_REWRITTEN = 30918, 30000
TRANS_ROWS = 95560
PLANTS_ROWS, PLANTS_FROM = 14480, 13446
FIBER_FROM, FIBER_TO = 55046, 55064


def _mod():
    import routes.data_liveness_master_shell as m
    return m


class _Cur:
    """Fake cursor. `rows_for(sql)` picks a canned result by SQL shape."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def execute(self, sql):
        c = self.cfg
        if c.get("raise_all"):
            raise RuntimeError("UndefinedTable: boom")
        # The series query is the only one built as a CTE chain — and it ALSO
        # contains COUNT(DISTINCT snapshot_date) in its `n` CTE, so it must be
        # matched BEFORE the instrument-coverage query.
        if "WITH w AS" in sql:
            self._rows = c.get("series", [])
        elif "infra_growth_snapshot" in sql:
            self._rows = c.get("instrument", [(29, 14, "2026-08-07", 0)])
        elif "cron_last_run" in sql and "last_completed_at" in sql:
            self._rows = c.get("health", [])
        elif "cron_last_run" in sql:
            self._rows = c.get("seen", [])
        elif "COUNT(*) FILTER" in sql:
            self._rows = c.get("counts", {}).get(_tbl(sql), [(0, 0)])
        else:
            self._rows = []

    def fetchall(self):
        return self._rows


def _tbl(sql):
    return sql.rsplit("FROM ", 1)[-1].strip()


class _Conn:
    def __init__(self, cfg):
        self.cfg = cfg

    def cursor(self):
        return _Cur(self.cfg)

    def rollback(self):
        pass

    def close(self):
        pass


def _patch(mp, cfg):
    m = _mod()
    mp.setattr(m, "_conn", lambda: _Conn(cfg))
    return m


def _by_id(checks):
    return {c["id"]: c for c in checks}


# ── _classify: the shape rules ──────────────────────────────────────────────
def test_discontinuity_when_window_starts_at_zero():
    """transmission_lines 0 -> 95,560. A layer does not populate from empty;
    the instrument was repointed. Growth is UNKNOWN, not +95,560 and not 0."""
    shape, sust = _mod()._classify(0, TRANS_ROWS, TRANS_ROWS, TRANS_ROWS)
    assert shape == "DISCONTINUITY"
    assert sust is None, "a repoint must not yield a measured sustained value"


def test_step_when_one_day_moves_a_large_share_of_the_table():
    """power_plants 13,446 -> 14,480 in one day = 7.1% of the table. A repoint
    and a bulk load are indistinguishable here, so the board refuses to guess."""
    shape, sust = _mod()._classify(PLANTS_FROM, PLANTS_ROWS, 1034, 1034)
    assert shape == "STEP"
    assert sust is None


def test_small_one_day_change_is_real_growth():
    """★ REGRESSION (share-of-net vs share-of-table). metro_fiber_routes moved
    55,046 -> 55,064: +18 rows, all on one day. Share-of-NET is 100%, which the
    first classifier called a discontinuity and erased. Share-of-TABLE is
    0.03% — real, small, sustained growth."""
    shape, sust = _mod()._classify(FIBER_FROM, FIBER_TO, 18, 18)
    assert shape == "SPREAD", "a +18 row day is not a discontinuity"
    assert sust == 18


def test_spread_growth_is_measured():
    shape, sust = _mod()._classify(21929, 24472, 2543, 400)
    assert shape == "SPREAD" and sust == 2543


def test_flat_is_a_measured_zero_not_unknown():
    """gas_pipelines really did net zero — that is a MEASUREMENT, and the
    treadmill lane depends on being able to tell it from unknown."""
    shape, sust = _mod()._classify(GAS_ROWS, GAS_ROWS, 0, 0)
    assert shape == "FLAT" and sust == 0


# ── treadmill: convicts, and its must-fail control ──────────────────────────
def _treadmill(mp, series, counts):
    m = _patch(mp, {"series": series, "counts": counts})
    return _by_id(m._lane_treadmill())


def test_treadmill_convicts_a_full_rewrite_with_zero_growth():
    """gas_pipelines: 97% of rows rewritten in 30d, net change zero."""
    mp = pytest.MonkeyPatch()
    try:
        out = _treadmill(
            mp,
            [("gas_pipelines", GAS_ROWS, GAS_ROWS, 0, 0, "2026-08-03", 29)],
            {"gas_pipelines": [(GAS_ROWS, GAS_REWRITTEN)]})
    finally:
        mp.undo()
    c = out["treadmill_gas_pipelines"]
    assert c["pass"] is False
    assert "TREADMILL" in c["detail"]


def test_full_rewrite_WITH_growth_is_not_a_treadmill():
    """MUST-FAIL CONTROL. Same 97% rewrite, but the table actually grew. If the
    lane convicted on rewrite volume alone, this fails."""
    mp = pytest.MonkeyPatch()
    try:
        out = _treadmill(
            mp,
            [("gas_pipelines", 20000, GAS_ROWS, 10918, 300, "2026-08-03", 29)],
            {"gas_pipelines": [(GAS_ROWS, GAS_REWRITTEN)]})
    finally:
        mp.undo()
    assert out["treadmill_gas_pipelines"]["pass"] is True
    assert "TREADMILL" not in out["treadmill_gas_pipelines"]["detail"]


def test_flat_layer_without_rewrite_is_not_a_treadmill():
    """MUST-FAIL CONTROL #2. Zero growth alone is a legitimately static
    reference layer — only rewrite AND zero growth is the pathology."""
    mp = pytest.MonkeyPatch()
    try:
        out = _treadmill(
            mp, [("substations", 126793, 126858, 65, 20, "2026-08-01", 29)],
            {"substations": [(126858, 68)]})
    finally:
        mp.undo()
    assert out["treadmill_substations"]["pass"] is True
    assert "N/A" in out["treadmill_substations"]["detail"]


def test_discontinuity_is_unmeasurable_not_a_treadmill():
    """★ REGRESSION. transmission_lines was repointed 2026-08-07 and holds ONE
    day of valid history. The first version derived sustained = 0 from the
    step and convicted it of adding nothing for 30 days."""
    mp = pytest.MonkeyPatch()
    try:
        out = _treadmill(
            mp,
            [("transmission_lines", 0, TRANS_ROWS, TRANS_ROWS, TRANS_ROWS,
              "2026-08-07", 29)],
            {"transmission_lines": [(TRANS_ROWS, 95558)]})
    finally:
        mp.undo()
    c = out["treadmill_transmission_lines"]
    assert c["pass"] is None, "one day of history cannot convict"
    assert "UNMEASURABLE" in c["detail"]
    assert "TREADMILL" not in c["detail"]


def test_missing_series_is_unmeasurable_not_zero():
    mp = pytest.MonkeyPatch()
    try:
        out = _treadmill(mp, [], {"gas_pipelines": [(GAS_ROWS, GAS_REWRITTEN)]})
    finally:
        mp.undo()
    c = out["treadmill_gas_pipelines"]
    assert c["pass"] is None
    assert "NOT zero" in c["detail"]


def test_treadmill_db_unavailable_is_none():
    m = _mod()
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(m, "_conn", lambda: None)
        checks = m._lane_treadmill()
    finally:
        mp.undo()
    assert all(c["pass"] is None for c in checks)
    assert m._lane_verdict(checks) == "?"


# ── never_ran: scope is the whole point ─────────────────────────────────────
def test_never_ran_only_judges_stampable_endpoints():
    """cron_last_run is written by an after_request bound to the /api/jobs/
    blueprint. A job pointing at /api/v1/admin/* can NEVER appear there, so its
    absence is not evidence — accusing it would invent a finding out of an
    instrument's blind spot."""
    m = _mod()
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(m, "_declared_jobs", lambda: ({
            "subsea_sync": "/api/jobs/subsea-sync",
            "discovery": "/api/jobs/discovery",
            "d1_facilities_sync": "/api/v1/admin/d1-sync/run",
            "kmz_discovery": "/api/kmz-discovery/run",
        }, None))
        mp.setattr(m, "_conn", lambda: _Conn({"seen": [("discovery",)]}))
        c = _by_id(m._lane_never_ran())["declared_jobs_have_run"]
    finally:
        mp.undo()
    assert c["pass"] is False
    assert "subsea_sync" in c["detail"]
    # the two non-/api/jobs entries must be named as OUT OF SCOPE, not accused
    assert "OUT OF SCOPE" in c["detail"]
    for j in ("d1_facilities_sync", "kmz_discovery"):
        assert j in c["detail"].split("OUT OF SCOPE")[1]


def test_never_ran_passes_when_every_stampable_job_has_run():
    """MUST-FAIL CONTROL."""
    m = _mod()
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(m, "_declared_jobs", lambda: (
            {"discovery": "/api/jobs/discovery"}, None))
        mp.setattr(m, "_conn", lambda: _Conn({"seen": [("discovery",)]}))
        c = _by_id(m._lane_never_ran())["declared_jobs_have_run"]
    finally:
        mp.undo()
    assert c["pass"] is True


def test_unparseable_scheduler_is_unmeasurable_not_a_finding():
    m = _mod()
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(m, "_declared_jobs", lambda: ({}, "SyntaxError: nope"))
        checks = m._lane_never_ran()
    finally:
        mp.undo()
    assert checks[0]["pass"] is None
    assert "UNMEASURABLE" in checks[0]["detail"]


def test_declared_jobs_reads_the_real_scheduler():
    """Not a stub: the real dchub-scheduler.py must parse and yield jobs with
    /api/jobs/ endpoints, including subsea_sync."""
    jobs, err = _mod()._declared_jobs()
    assert err is None, err
    assert len(jobs) > 20
    assert jobs.get("subsea_sync") == "/api/jobs/subsea-sync"


# ── health signal ───────────────────────────────────────────────────────────
def test_health_signal_flags_a_second_caller():
    """A completion stamped 9,120 minutes after the last successful start
    cannot belong to that run — no request lasts 6.3 days."""
    mp = pytest.MonkeyPatch()
    try:
        m = _patch(mp, {"health": [("ai-outreach", "http_401", 9120.0),
                                   ("discovery", "ok", 0.0)]})
        c = _by_id(m._lane_health_signal())["status_belongs_to_the_run"]
    finally:
        mp.undo()
    assert c["pass"] is False
    assert "ai-outreach" in c["detail"]
    assert "NOT a health signal" in c["detail"]


def test_health_signal_passes_when_completions_belong_to_their_runs():
    """MUST-FAIL CONTROL. Every healthy job completes <1 min after it starts."""
    mp = pytest.MonkeyPatch()
    try:
        m = _patch(mp, {"health": [("discovery", "ok", 0.0),
                                   ("news-refresh", "ok", 0.8)]})
        c = _by_id(m._lane_health_signal())["status_belongs_to_the_run"]
    finally:
        mp.undo()
    assert c["pass"] is True


def test_health_signal_does_not_flag_on_status_alone():
    """A genuine in-run failure (500 stamped immediately) is NOT a second
    caller. The lane keys off the TIMING, not the status text."""
    mp = pytest.MonkeyPatch()
    try:
        m = _patch(mp, {"health": [("backup", "http_500", 0.2)]})
        c = _by_id(m._lane_health_signal())["status_belongs_to_the_run"]
    finally:
        mp.undo()
    assert c["pass"] is True


# ── growth lane + population ────────────────────────────────────────────────
def test_growth_fails_only_when_nothing_is_accumulating():
    mp = pytest.MonkeyPatch()
    try:
        m = _patch(mp, {"series": [
            ("gas_pipelines", GAS_ROWS, GAS_ROWS, 0, 0, "2026-08-03", 29),
            ("fcc_fiber_hexes", 2640850, 2640850, 0, 0, "2026-07-20", 29)]})
        c = _by_id(m._lane_net_growth())["something_is_growing"]
    finally:
        mp.undo()
    assert c["pass"] is False
    assert "NOTHING IS ACCUMULATING" in c["detail"]


def test_growth_passes_when_one_layer_accumulates():
    """MUST-FAIL CONTROL. A pile of flat quarterly layers is fine so long as
    something is alive — convicting on flatness is how a guard gets deleted."""
    mp = pytest.MonkeyPatch()
    try:
        m = _patch(mp, {"series": [
            ("data_centers", 21929, 24472, 2543, 400, "2026-08-01", 29),
            ("gas_pipelines", GAS_ROWS, GAS_ROWS, 0, 0, "2026-08-03", 29)]})
        c = _by_id(m._lane_net_growth())["something_is_growing"]
    finally:
        mp.undo()
    assert c["pass"] is True
    assert "data_centers" in c["detail"]


def test_repoint_is_never_counted_as_accumulation():
    """A DISCONTINUITY layer must not appear in the growing list, however big
    its apparent delta."""
    mp = pytest.MonkeyPatch()
    try:
        m = _patch(mp, {"series": [
            ("transmission_lines", 0, TRANS_ROWS, TRANS_ROWS, TRANS_ROWS,
             "2026-08-07", 29)]})
        c = _by_id(m._lane_net_growth())["something_is_growing"]
    finally:
        mp.undo()
    assert c["pass"] is False, "a repoint is not growth"
    assert "transmission_lines" not in c["detail"].split("SUSTAINED growth")[1].split(".")[0]


def test_unreadable_snapshot_table_is_unmeasurable_not_dead():
    mp = pytest.MonkeyPatch()
    try:
        m = _patch(mp, {"raise_all": True})
        checks = m._lane_net_growth()
    finally:
        mp.undo()
    assert checks[0]["pass"] is None
    assert "not a finding of zero growth" in checks[0]["detail"]


def test_unknown_never_renders_zero():
    m = _mod()
    assert m._fmt(None) == "UNKNOWN"
    assert m._fmt(0) == "0"


def test_population_is_built_from_the_executed_lists():
    m = _mod()
    pop = m._population()
    assert pop["lanes"] == [lid for lid, _, _ in m._LANES]
    assert pop["treadmill_layers"] == [l[0] for l in m._TREADMILL_LAYERS]
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(m, "_TREADMILL_LAYERS",
                   m._TREADMILL_LAYERS + (("zz", "zz_t", "created_at"),))
        assert "zz" in m._population()["treadmill_layers"]
    finally:
        mp.undo()
    assert m._population()["treadmill_layers"] == pop["treadmill_layers"]


def test_no_literal_percent_in_generated_sql():
    """Every statement runs with no bound params; a literal % is the psycopg2
    trap that has 500'd this codebase."""
    m = _mod()
    for w in (7, 30):
        assert "%" not in m._series_sql(w)
