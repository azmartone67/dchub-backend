"""tests/test_surface_integrity_seo_lane.py — the SEO lane's own mutation test
(2026-09-02).

★ WHY THIS FILE EXISTS. /api/v1/admin/surface-integrity's `seo_measurement`
lane was a hardcoded string: "Last measured 2026-08-01 ... rank/impression
truth lives in Google Search Console ... behind interactive auth unavailable
to this process ... the honest state is UNMEASURED". It rendered FAIL on
every tick since 2026-08-08 — while routes/gsc_performance.py had a
service-account daily ingest (cron green 2026-09-01T06:43Z) and
/api/v1/seo/performance?dimension=site held 247 site-day rows, newest
2026-08-29, measured 2026-09-02 00:24Z. A false red on a health board is
exactly as misleading as a false green.

Every test here feeds the real lane function a scripted series, flips ONE
input, and asserts the verdict moves. A lane that stays the same colour under
a mutation is a lane that cannot fail.

★ NEVER set DCHUB_ADMIN_KEY at module scope here (see
test_adoption_master_shell.py for the leak that rule comes from).
"""
from __future__ import annotations

import datetime as _dt
import inspect
import os

from routes import surface_integrity_master_shell as sims  # noqa: E402
from routes.brain_ascension_master_shell import _lane_verdict  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = _dt.date(2026, 9, 2)        # the UTC date of the live reading
LIVE_NEWEST = _dt.date(2026, 8, 29)  # 4 days old on that date — at the allowance


def _series(newest, n_rows, oldest="2025-12-26", stored=247):
    rows = [{"date": (newest - _dt.timedelta(days=i)).isoformat(),
             "clicks": 44, "impressions": 7209, "ctr": 0.0062, "position": 13.8}
            for i in range(n_rows)]
    return {"rows": rows, "window_days": 14,
            "coverage": {"oldest": oldest, "newest": newest.isoformat(),
                         "rows_stored": stored}}


def _gsc(checks):
    return next(c for c in checks if c["id"] == "seo_gsc_series_current")


def _bing(checks):
    return next(c for c in checks if c["id"] == "seo_bing_standing_measured")


def test_the_live_series_passes(monkeypatch):
    """★ THE REGRESSION: the series that existed while the lane said UNMEASURED."""
    monkeypatch.setattr(sims, "_read_seo_series", lambda: _series(LIVE_NEWEST, 10))
    checks = sims._lane_seo_measurement(today=TODAY)
    assert _gsc(checks)["pass"] is True
    assert _lane_verdict(checks) == "PASS"
    assert "2026-08-29" in _gsc(checks)["detail"]
    assert "in-process" in _gsc(checks)["detail"]


def test_a_stale_series_fails(monkeypatch):
    """★ MUTATION: newest 8 days old -> FAIL. A lane that passes here cannot
    tell a running ingest from a dead one."""
    monkeypatch.setattr(sims, "_read_seo_series",
                        lambda: _series(TODAY - _dt.timedelta(days=8), 10))
    checks = sims._lane_seo_measurement(today=TODAY)
    assert _gsc(checks)["pass"] is False
    assert _lane_verdict(checks) == "FAIL"


def test_one_day_past_the_allowance_fails():
    """The boundary is <= 4 days: 4 passes, 5 fails."""
    for age, expect in ((4, True), (5, False)):
        checks = sims._lane_seo_measurement.__wrapped__(today=TODAY) if hasattr(
            sims._lane_seo_measurement, "__wrapped__") else None
        # (no wrapper) — drive through the module hook instead
        sims_read = sims._read_seo_series
        try:
            sims._read_seo_series = lambda a=age: _series(TODAY - _dt.timedelta(days=a), 10)
            checks = sims._lane_seo_measurement(today=TODAY)
        finally:
            sims._read_seo_series = sims_read
        assert _gsc(checks)["pass"] is expect, (age, _gsc(checks)["detail"])


def test_too_few_rows_fails_even_when_fresh(monkeypatch):
    """A single fresh row is not a series."""
    monkeypatch.setattr(sims, "_read_seo_series", lambda: _series(LIVE_NEWEST, 3))
    checks = sims._lane_seo_measurement(today=TODAY)
    assert _gsc(checks)["pass"] is False
    assert _lane_verdict(checks) == "FAIL"


def test_an_unreadable_series_is_indeterminate_never_pass(monkeypatch):
    """Unreadable is '?', not FAIL and not PASS — the rule in the module
    docstring: a reading whose provenance is unknown is UNMEASURED."""
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(sims, "_read_seo_series", boom)
    checks = sims._lane_seo_measurement(today=TODAY)
    assert _gsc(checks)["pass"] is None
    assert "unreadable" in _gsc(checks)["detail"]
    assert _lane_verdict(checks) == "?"


def test_a_missing_newest_is_a_fail_not_a_crash(monkeypatch):
    empty = {"rows": [], "window_days": 14,
             "coverage": {"oldest": None, "newest": None, "rows_stored": 0}}
    monkeypatch.setattr(sims, "_read_seo_series", lambda: empty)
    checks = sims._lane_seo_measurement(today=TODAY)
    assert _gsc(checks)["pass"] is False


def test_bing_stays_unmeasured_and_cannot_decide_the_lane(monkeypatch):
    monkeypatch.setattr(sims, "_read_seo_series", lambda: _series(LIVE_NEWEST, 10))
    checks = sims._lane_seo_measurement(today=TODAY)
    b = _bing(checks)
    assert b["pass"] is None
    assert b["critical"] is False, "critical would make the lane '?' forever"
    assert "UNMEASURED" in b["detail"] and "Bing Webmaster Tools" in b["detail"]
    assert _lane_verdict(checks) == "PASS", "Bing's None must not block a PASS"
    # and it must not be able to fail it either — same series, GSC failing
    monkeypatch.setattr(sims, "_read_seo_series", lambda: _series(LIVE_NEWEST, 1))
    assert _lane_verdict(sims._lane_seo_measurement(today=TODAY)) == "FAIL"


def test_the_lane_is_wired_and_the_constant_is_gone():
    assert ("seo_measurement", "SEO measurement currency", sims._lane_seo_measurement) in sims._LANES
    src = open(os.path.join(REPO_ROOT, "routes", "surface_integrity_master_shell.py"),
               encoding="utf-8").read()
    assert "Last measured 2026-08-01" not in src, "the hardcoded verdict is back"
    assert "no in-app proxy may stand in for it" not in src


def test_the_reader_is_in_process_not_http():
    src = inspect.getsource(sims._read_seo_series)
    assert "site_series" in src
    for tok in ("requests", "http", "_head("):
        assert tok not in src, f"the lane must read the table, not the edge: {tok}"


def test_site_series_shares_the_read_routes_queries():
    """One implementation for the board and the API, or they drift."""
    src = open(os.path.join(REPO_ROOT, "routes", "gsc_performance.py"),
               encoding="utf-8").read()
    assert "def site_series(" in src
    body = src[src.index("def site_series("):src.index("def read_performance(")]
    assert "_coverage_of(raw, \"site\")" in body and "_site_rows(raw, days)" in body
    route = src[src.index("def read_performance("):]
    assert "_coverage_of(raw, dim)" in route and "_site_rows(raw, days)" in route


def test_thresholds_are_the_documented_ones():
    assert sims._SEO_MAX_AGE_DAYS == 4
    assert sims._SEO_MIN_ROWS == 5
    assert sims._SEO_WINDOW_DAYS >= 7
