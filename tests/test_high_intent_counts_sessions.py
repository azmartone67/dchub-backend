"""r-rows-are-not-sessions + r-threshold-drift (2026-09-03).

Two defects, one root: a PUBLISHED number that restates something instead of
reading it.

1. /api/v1/mcp/high-intent/stats counted ROWS for two fields named after
   SESSIONS. mcp_high_intent_sessions is keyed per (session_id, tool), so one
   session crossing the threshold on N tools contributed N. Live 2026-09-03:
   high_intent_sessions_30d=1725 against 393 DISTINCT sessions on the same
   table and window (handoff-funnel has always counted DISTINCT) — 4.4x, on a
   public route, with claim_minted_rate_30d_pct dividing one inflated count by
   another and publishing it as a session rate.

2. The published high_intent basis ASSERTED "a session that makes exactly one
   gated call never enters this table". That is true only at threshold >= 2.
   HIGH_INTENT_THRESHOLD is read from env at module import and prod ran on 1,
   so the sentence was false and no code change could have told you.

These assertions are anchored to the FUNCTION that owns each behaviour, never
to a substring of the whole file — a module-wide `in src` check passes on a
comment that merely mentions the string.
"""
import ast
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@127.0.0.1:5432/none")


def _func_src(path, name):
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError("function %s not found in %s" % (name, path))


# ── 1. rows are not sessions ────────────────────────────────────────────────
def test_high_intent_stats_counts_distinct_sessions_not_rows():
    body = _func_src(ROOT / "routes" / "mcp_high_intent_claim.py", "high_intent_stats")
    assert "COUNT(*) FROM mcp_high_intent_sessions" not in body, (
        "high_intent_stats counts ROWS from a per-(session,tool) table into a "
        "field named *_sessions_30d. One session on N tools counts N times."
    )
    # and the two headline fields are actually the DISTINCT form
    assert body.count("COUNT(DISTINCT mcp_session_id) FROM mcp_high_intent_sessions") >= 2


# ── 2. the basis READS the threshold, never restates it ─────────────────────
def _basis():
    import flask_mcp_endpoints as f
    return f._high_intent_basis


def test_basis_states_the_actual_threshold():
    for t in (1, 2, 5):
        assert str(t) in _basis()(t), t


def test_basis_does_not_claim_repeat_use_at_threshold_one():
    """THE REGRESSION. At threshold 1 a single gated call DOES enter."""
    s = _basis()(1)
    assert "never enters this table" not in s, (
        "published basis denies entry that the live threshold allows: %s" % s)
    assert "DOES enter" in s


def test_basis_still_states_repeat_use_when_threshold_is_two():
    s = _basis()(2)
    assert "REPEAT paid-tool use" in s
    assert "never enters this table" in s


def test_basis_survives_an_unreadable_threshold():
    """An unreadable threshold must assert NEITHER shape."""
    s = _basis()(None)
    assert "the configured threshold" in s
    assert "DOES enter" not in s
    assert "REPEAT paid-tool use" not in s


# ── 3. the filter boundary is declared, not left to be differenced ──────────
def test_handoff_funnel_declares_the_paywall_filter_boundary():
    body = _func_src(ROOT / "flask_mcp_endpoints.py", "handoff_funnel")
    assert "population_vs_paywall_hit" in body
    assert "_is_non_human_client" in body, (
        "the declaration must name the predicate that refuses the write, or a "
        "reader cannot check it")
