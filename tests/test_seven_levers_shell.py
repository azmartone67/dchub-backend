"""Seven Levers Master Shell (#32, 2026-07-25) — pins the wave's contracts.

One wave, seven levers: zone-worker canon sync, recidivism→planner wiring,
slow-request capture, 7/7 usage-capture coverage, RAG recall anchors, loop
census, LinkedIn follower enum. Plus two REGRESSION pins from the build
itself: a repo-wide floor sweep nearly rewrote the retired-floor BAN LIST in
ai_surface_canon (the canon would have banned the canon) and falsified the
Surface Truth incident docstring — history and fences must survive future
sweeps byte-intact.

CI-SAFETY: unit-tests env has no DATABASE_URL/JWT_SECRET; modules import
directly (never via main); DB paths are exercised only via fail-soft
contracts; source-text checks carry the wiring assertions.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def shell():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import seven_levers_master_shell as m
    return m


# ── wiring ────────────────────────────────────────────────────────────

def test_shell_registered_in_main():
    src = _read("main.py")
    assert "register_blueprint(seven_levers_master_shell_bp)" in src
    assert "register_blueprint(perf_timing_bp)" in src


def test_shell_cron_ticked_and_killable():
    src = _read(os.path.join("routes", "cron_heartbeat.py"))
    assert "/api/v1/admin/seven-levers/master-tick" in src
    assert "SEVEN_LEVERS_SHELL_DISABLE" in src


def test_shell_no_store_and_beat():
    src = _read(os.path.join("routes", "seven_levers_master_shell.py"))
    assert "no-store" in src
    assert "seven-levers-shell-daily" in src
    assert "/api/v1/admin/ingest-runs/beat" in src


def test_shell_fetches_bust_the_zone_cache():
    # Admin/manifest GETs are zone-cached up to 3600s; a cached body is the
    # stale-green this shell exists to kill, so every edge fetch must carry
    # a cache-buster.
    src = _read(os.path.join("routes", "seven_levers_master_shell.py"))
    assert "cb=%d" in src


# ── honesty semantics ────────────────────────────────────────────────

def test_lane_verdict_never_green_by_silence(shell):
    assert shell._lane_verdict(
        [shell._check("x", "x", None, "unreachable", critical=True)]) == "?"
    assert shell._lane_verdict(
        [shell._check("x", "x", False, "bad")]) == "FAIL"
    assert shell._lane_verdict(
        [shell._check("x", "x", True, "ok", critical=True),
         shell._check("y", "y", None, "info", critical=False)]) == "PASS"


def test_crashed_lane_is_indeterminate(shell):
    def boom():
        raise RuntimeError("boom")
    assert shell._lane_verdict(shell._safe_lane(boom)) == "?"


def test_feed_family_normalizer(shell):
    assert shell._norm_feed("fiber-integration-daily") == "fiber-integration"
    assert shell._norm_feed("fiber-integration") == "fiber-integration"
    assert shell._norm_feed("EIA_SYNC") == "eia"


# ── lever 1 · zone sync sources ──────────────────────────────────────

def test_repo_worker_is_canon_clean_and_current():
    """The repo worker.js was a stale COPY (v4.9.24-era) while live ran
    v4.9.32 — numbers got edited in the repo without a deploy (the #30
    artifact-vs-reality failure). Now repo == deployed v4.9.33 canon-sync:
    no retired floors, no stale tool counts, version marker present."""
    src = _read("worker.js")
    assert "WORKER_VERSION = '4.9.34-execute-plan'" in src
    assert "21,000+" not in src
    assert "73 tools over" not in src
    assert "58 MCP tools" not in src


def test_server_mjs_serves_canon():
    src = _read("server.mjs")
    assert "21,000+" not in src
    assert "40 tools," not in src and "40 tools covering" not in src


# ── lever 2 · recidivism wiring ──────────────────────────────────────

def test_planner_consumes_recidivism():
    src = _read(os.path.join("routes", "brain_strategic_planner.py"))
    assert "def _read_recidivism" in src
    assert '"recidivism":   1200' in src
    assert "RECIDIVIST FINDINGS" in src
    assert "still_broken IS TRUE" in src


def test_recidivism_reader_failsoft(monkeypatch):
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import brain_strategic_planner as p
    monkeypatch.setattr(p, "_get_db", lambda: None)
    assert p._read_recidivism() == []


# ── lever 3 · perf capture ───────────────────────────────────────────

def test_perf_hooks_are_failsoft_and_killable(monkeypatch):
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import perf_timing as pt
    monkeypatch.setenv("PERF_TIMING_DISABLE", "1")
    assert pt._disabled() is True
    monkeypatch.delenv("PERF_TIMING_DISABLE", raising=False)
    assert pt._disabled() is False
    # normalizer bounds cardinality
    assert pt._norm_path("/api/v1/facility/123456789/detail") == \
        "/api/v1/facility/:id/detail"
    assert pt._norm_path("/api/v1/x?y=1") == "/api/v1/x"
    # no DATABASE_URL → silent no-op, never a raise
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    pt._LAST_WRITE[0] = 0.0
    assert pt._record("/x", "GET", 200, 2500) is None


def test_perf_write_is_rate_limited(monkeypatch):
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import perf_timing as pt
    calls = []
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid.invalid/x")
    import time as _t
    pt._LAST_WRITE[0] = _t.monotonic()   # a write just happened
    # inside the gap: returns before any connect attempt
    monkeypatch.setattr("psycopg2.connect",
                        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(RuntimeError()),
                        raising=False)
    pt._record("/x", "GET", 200, 2500)
    assert calls == []


# ── lever 4 · usage-capture coverage ─────────────────────────────────

def test_all_seven_call_sites_wired():
    sites = ("routes/brain_lane_driver.py",
             "routes/brain_strategic_planner.py",
             "routes/brain_investigator.py",
             "routes/brain_feature_proposer.py",
             "routes/brain_answer_cache.py",
             "routes/analyst_note.py")
    unwired = [s for s in sites if "record_llm_usage" not in _read(s)]
    assert not unwired, "missing usage capture: %s" % unwired


# ── lever 7 · media enum ─────────────────────────────────────────────

def test_linkedin_follower_enum_is_valid():
    """Live API test 2026-07-25: 'CompanyFollowedByMember' → 400 invalid
    enum; 'COMPANY_FOLLOWED_BY_MEMBER' → 200 firstDegreeSize=302. The
    collector was blind on a one-word spelling."""
    src = _read("linkedin_poster.py")
    assert "edgeType=COMPANY_FOLLOWED_BY_MEMBER" in src
    assert "edgeType=CompanyFollowedByMember" not in src


# ── sweep-regression pins ────────────────────────────────────────────

def test_retired_floor_ban_list_still_bans_the_retired_floor():
    """A repo-wide '21,000+ → 12,650+' sweep nearly rewrote the RETIRED
    list in ai_surface_canon — the canon would have banned itself. The
    ban list must keep the retired floors verbatim."""
    src = _read("ai_surface_canon.py")
    assert '"21,000+"' in src


def test_surface_truth_incident_history_intact():
    src = _read(os.path.join("routes", "surface_truth_master_shell.py"))
    assert "(20,000+, 21,000+, 22,000+)" in src
