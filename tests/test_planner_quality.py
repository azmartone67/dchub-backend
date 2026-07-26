"""Planner-quality board (2026-07-26) — pins the metric definitions.

The board exists because completion-rate cannot see a correct-looking run
that violated an invariant (the Dallas-becomes-CAISO class). The properties
worth pinning are therefore the DEFINITIONS, not the rendering:

  1. a gated tier preview is a WORKING step, never a failure;
  2. integrity requires BOTH every step good AND zero constraint rejects;
  3. planning and execution stay separate — an unresolved step is a PLANNING
     miss and must not be charged against execution;
  4. no DB is an honest error, never an empty-but-green board.

CI-SAFETY: no DATABASE_URL in the unit env — the module imports directly and
every DB path is exercised through its fail-soft contract.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def pq():
    pytest.importorskip("flask")
    import sys
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from routes import planner_quality as m
    return m


# ── wiring ────────────────────────────────────────────────────────────

def test_board_registered_and_no_store():
    assert "register_blueprint(planner_quality_bp)" in _read("main.py")
    src = _read(os.path.join("routes", "planner_quality.py"))
    assert "no-store" in src
    assert "/admin/planner-quality" in src


# ── metric definitions ───────────────────────────────────────────────

def test_gated_preview_counts_as_a_working_step(pq):
    # A tier-gated preview is the paywall working, not a failed step — if it
    # ever counts as a failure the board will condemn healthy anonymous runs.
    assert "gated_preview" in pq._GOOD
    assert "executed" in pq._GOOD
    assert "failed" not in pq._GOOD
    assert "skipped_unresolved" not in pq._GOOD


def test_pct_is_none_not_zero_on_an_empty_denominator(pq):
    # 0/0 must render '—', never 0% — a board that shows 0% for "no data"
    # reads as a catastrophic regression.
    assert pq._pct(0, 0) is None
    assert pq._pct(1, 4) == 25.0


def test_no_db_is_an_honest_error_never_an_empty_green_board(pq, monkeypatch):
    monkeypatch.setattr(pq, "_db", lambda: None)
    out = pq._compute(7)
    assert out["ok"] is False and out["error"]


def test_days_window_is_bounded(pq):
    src = _read(os.path.join("routes", "planner_quality.py"))
    assert "min(90" in src and "max(1" in src


def test_integrity_requires_clean_steps_and_zero_rejects():
    # Pinned as source text: a run is only 'clean' when every step is good
    # AND no constraint mint was rejected. Dropping either half is exactly
    # how "completed" starts lying again.
    src = _read(os.path.join("routes", "planner_quality.py"))
    assert "if tot and good == tot and not rej:" in src


def test_planning_and_execution_stay_separate():
    # An unresolved step is a PLANNING miss (the graph asked for an artifact
    # nothing produces) and must be excluded from the execution denominator,
    # or a planner bug reads as an executor bug.
    src = _read(os.path.join("routes", "planner_quality.py"))
    assert 'b["steps"] - b["unresolved"]' in src
    assert '"planning_resolved_pct"' in src and '"execution_ok_pct"' in src


def test_platform_view_reads_callers_not_crawlers():
    src = _read(os.path.join("routes", "planner_quality.py"))
    # planner-family tools only — crawl/reach traffic never touches these.
    assert "tool = 'execute_plan' OR tool LIKE 'recipe:%%'" in src
