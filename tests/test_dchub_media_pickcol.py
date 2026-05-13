"""dchub_media._pick_col schema-aware column picker.

Phase 239+ feed-v3 builds queries by introspecting actual table columns
and picking the best of several candidates. If this function ever
returns the wrong column name (or breaks on the empty-set edge case),
feed-v3 silently returns 0 items — exactly the regression we saw in
PR #46 (cur-scope bug) and the UNION wrapper in PR #46 (both later
fixed in #47/#48).

This test is the safety net for the schema-aware logic.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DCM = os.path.join(ROOT, "dchub_media.py")


def _pick_col():
    src = open(DCM, encoding="utf-8").read()
    m = re.search(
        r"^def _pick_col\(.*?(?=^def |^class |\Z)",
        src, re.DOTALL | re.MULTILINE,
    )
    assert m, "_pick_col not found in dchub_media.py"
    ns = {}
    exec(m.group(0), ns)
    return ns["_pick_col"]


def test_pick_col_returns_first_match():
    pick = _pick_col()
    cols = {"title", "summary", "url", "published_at"}
    assert pick(cols, "url", "source_url", "link") == "url"


def test_pick_col_falls_through_to_second_candidate():
    pick = _pick_col()
    cols = {"title", "source_url", "summary"}
    assert pick(cols, "url", "source_url", "link") == "source_url"


def test_pick_col_returns_none_when_no_candidates_match():
    pick = _pick_col()
    cols = {"unrelated_col"}
    assert pick(cols, "url", "source_url", "link") is None


def test_pick_col_handles_empty_cols_set():
    """Critical: a missing-table introspection returns set() —
    _pick_col must not crash."""
    pick = _pick_col()
    assert pick(set(), "url", "source_url") is None


def test_pick_col_preserves_candidate_order():
    """First candidate wins even if a later one is 'better' — order
    matters because callers list candidates by preference."""
    pick = _pick_col()
    cols = {"url", "source_url"}  # BOTH present
    assert pick(cols, "url", "source_url") == "url"
    # Reverse the candidate list — now source_url should win
    assert pick(cols, "source_url", "url") == "source_url"
