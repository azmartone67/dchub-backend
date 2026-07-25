"""Growthfix wave (2026-07-24) — pins fixes 2-5 of the loop-audit fix-wave.

  2. competitor_gap_crawler: rotating sitemap window (the page cap used to
     re-read the SAME first 500 <loc>s every run → 15 zero-row runs while
     "green") + affirmative no_new_data beat.
  3. ingest_runs: no_new_data is an OK status, RESETS the consecutive_zero
     counter, and exempts the >=3-zero-row alarm; eia/osm workflows report it.
  5. growthfix master shell (#26): registered in main.py + cron_heartbeat,
     and its lane verdict never reads PASS when a critical check couldn't run.

CI-SAFETY: the unit-tests job installs ONLY pytest, so most tests here are
PURE (ast/string on source). competitor_gap_crawler is stdlib-only and is
imported directly; the shell needs flask and is importorskip-guarded.
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IR = os.path.join(ROOT, "routes", "ingest_runs.py")
CG = os.path.join(ROOT, "routes", "competitor_gap_crawler.py")
GF = os.path.join(ROOT, "routes", "growthfix_master_shell.py")
CH = os.path.join(ROOT, "routes", "cron_heartbeat.py")
MAIN = os.path.join(ROOT, "main.py")
EIA = os.path.join(ROOT, ".github", "workflows", "eia-pricing-ingest.yml")
OSM = os.path.join(ROOT, ".github", "workflows", "osm-crawl.yml")


def _read(path):
    return open(path, encoding="utf-8").read()


def _set_literal(path, var_name):
    tree = ast.parse(_read(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == var_name
                for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{var_name} not found in {path}")


# ── 3 · ingest_runs no_new_data ──────────────────────────────────────

def test_ok_status_includes_no_new_data():
    ok = _set_literal(IR, "_OK_STATUS")
    assert "no_new_data" in ok and "no-new-data" in ok


def test_no_new_data_set_defined():
    nnd = _set_literal(IR, "_NO_NEW_DATA")
    assert nnd == {"no_new_data", "no-new-data"}


def test_beat_resets_counter_on_no_new_data():
    src = _read(IR)
    assert "if status.lower() in _NO_NEW_DATA and rows_sig == 0:" in src
    assert "rows_sig = 1" in src


def test_deadman_zero_row_alarm_exempts_no_new_data():
    src = _read(IR)
    assert 'if cz and cz >= 3 and (st or "").lower() not in _NO_NEW_DATA:' in src


def test_workflows_report_no_new_data():
    for path in (EIA, OSM):
        src = _read(path)
        assert "no_new_data" in src, f"{path} never reports no_new_data"
        assert '\\"status\\":\\"$STATUS\\"' in src, \
            f"{path} beat does not send the computed STATUS"


def test_no_new_data_is_earned_not_a_bare_zero_map():
    """2026-07-25 — no_new_data is an ASSERTION that zero was EXPECTED: it
    resets consecutive_zero and exempts the >=3-zero-row alarm. Mapping a bare
    rows==0 to it silences broken counters instead of fixing them. Both feeds
    had one: eia read total_records off an async 202 that has never carried it
    (0 forever, task never polled), and osm exited 0 on crawler errors. The
    status must come from a run that demonstrably completed and found nothing.
    """
    for path, zero_var in ((EIA, "COUNT"), (OSM, "RI")):
        src = _read(path)
        assert '[ "$%s" = "0" ] && STATUS="no_new_data"' % zero_var not in src, \
            f"{path} maps a bare rows==0 to no_new_data — it must be earned"
        assert "${BEAT_STATUS:-error}" in src, \
            f"{path} does not default to status=error when the run step " \
            "produced no outputs (silence must not read as healthy)"
        assert "if: always()" in src, \
            f"{path} beat step is skippable by a failed job — a skipped beat " \
            "is silence, and silence reads as healthy until the cadence expires"


# ── 2 · competitor gap rotating window ───────────────────────────────

def _sitemap_xml(n):
    locs = "".join(
        f"<url><loc>https://example.com/providers/op-{i}</loc></url>"
        for i in range(n))
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + locs + "</urlset>")


def test_parser_offset_rotates_window():
    import importlib
    cg = importlib.import_module("routes.competitor_gap_crawler")
    xml = _sitemap_xml(10)
    out = cg.parse_competitor_sitemap(
        "testsrc", "https://example.com/sitemap.xml",
        limit=3, _prefetched_text=xml, offset=4)
    assert out["locs_seen"] == 10
    assert out["window_offset"] == 4
    urls = [c["source_url"] for c in out["parsed"]]
    assert urls == [f"https://example.com/providers/op-{i}" for i in (4, 5, 6)]
    # wrap: offset past the end comes back around, never an empty window
    out2 = cg.parse_competitor_sitemap(
        "testsrc", "https://example.com/sitemap.xml",
        limit=3, _prefetched_text=xml, offset=14)
    assert out2["window_offset"] == 4
    # offset=0 keeps the historical first-page behavior
    out3 = cg.parse_competitor_sitemap(
        "testsrc", "https://example.com/sitemap.xml",
        limit=3, _prefetched_text=xml, offset=0)
    assert [c["source_url"] for c in out3["parsed"]] == \
        [f"https://example.com/providers/op-{i}" for i in (0, 1, 2)]


def test_orchestrator_walks_window_daily():
    src = _read(CG)
    assert "tm_yday" in src, "no deterministic daily offset"
    assert "offset=_off" in src, "orchestrator never passes the offset"


def test_crawler_beats_no_new_data():
    src = _read(CG)
    assert '_status = "no_new_data"' in src


# ── 5 · growthfix master shell wiring + honesty ──────────────────────

def test_shell_registered_in_main():
    src = _read(MAIN)
    assert "growthfix_master_shell_bp" in src
    assert "register_blueprint(growthfix_master_shell_bp)" in src


def test_shell_cron_dispatch_registered():
    src = _read(CH)
    assert "growthfix_shell_daily" in src
    assert "/api/v1/admin/growthfix/master-tick" in src
    assert 'GROWTHFIX_SHELL_DISABLE") != "1"' in src


def test_shell_lane_verdict_honesty():
    pytest.importorskip("flask")
    from routes.growthfix_master_shell import _check, _lane_verdict
    # an indeterminate CRITICAL check must never render green
    assert _lane_verdict([_check("a", "a", None, "", critical=True)]) == "?"
    # any hard failure is FAIL
    assert _lane_verdict([_check("a", "a", True, "", critical=True),
                          _check("b", "b", False, "")]) == "FAIL"
    # all criticals affirmatively green (non-critical gauges may be "?")
    assert _lane_verdict([_check("a", "a", True, "", critical=True),
                          _check("b", "b", None, "")]) == "PASS"


def test_shell_sql_avoids_percent_trap():
    """★ psycopg2 trap: ANY literal percent in a statement executed without a
    params tuple attempts percent-substitution and 500s. The shell's SQL is
    literal-only, so its source must contain no percent-formatted SQL."""
    src = _read(GF)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "SELECT" in node.value.upper():
                assert "%" not in node.value, \
                    f"percent inside literal SQL: {node.value[:60]!r}"


def test_shell_age_days_accepts_text_timestamps():
    """★ house trap: coverage_gaps.created_at is TEXT — the shell's first
    live tick 500'd on '.tzinfo' of a str. _age_days must parse strings."""
    pytest.importorskip("flask")
    from routes.growthfix_master_shell import _age_days, _as_dt
    assert _age_days("2026-07-11 06:04:18.230977+00") is not None
    assert _age_days("2026-07-11T06:04:18Z") is not None
    assert _age_days("not a timestamp") is None
    assert _as_dt(None) is None


def test_shell_lane_crash_renders_indeterminate():
    pytest.importorskip("flask")
    from routes.growthfix_master_shell import _lane_verdict, _safe_lane

    def _boom(_c):
        raise AttributeError("'str' object has no attribute 'tzinfo'")
    checks = _safe_lane(_boom, None)
    assert _lane_verdict(checks) == "?"
    assert "lane crashed" in checks[0]["detail"]
