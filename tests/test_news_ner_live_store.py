"""The NER scan must read the LIVE news store, and read it FIRST.

The defect this pins (2026-08-17): the scan's fallback chain tried two
`news_items` shapes that have never matched (relation absent in prod) and then
`news` — a table whose only writer is a manual admin endpoint nothing
schedules — while the automated pipeline writes `news_articles`. The feed
starved for a week and the deadman board flapped error/no_new_data.

Source-level assertions on purpose: the shapes are inline SQL inside the
route function, and what broke was WHICH TABLES the chain names and in what
order. AST-extracting the whole function would drag in flask; reading the
source of `_scan` binds exactly the contract that failed.
"""
from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "routes", "news_entity_extraction.py")


def _scan_source():
    src = open(PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_scan":
            return ast.get_source_segment(src, node)
    raise AssertionError("_scan not found in routes/news_entity_extraction.py")


def test_scan_reads_news_articles_before_the_starved_fallbacks():
    src = _scan_source()
    i_live = src.find("FROM news_articles")
    i_dead = src.find("FROM news_items")
    i_manual = src.find("FROM news ")
    assert i_live != -1, "the scan no longer reads news_articles — the live store"
    assert i_dead == -1 or i_live < i_dead, "news_articles must be tried FIRST"
    assert i_manual == -1 or i_live < i_manual, "news_articles must be tried FIRST"


def test_news_articles_shape_never_applies_interval_to_the_text_column():
    """published_at is TEXT in prod. `NOW() - INTERVAL` against it errors,
    the error is swallowed, and the chain falls back to the starved table —
    i.e. this exact regression would silently reintroduce the outage."""
    src = _scan_source()
    start = src.find("FROM news_articles")
    assert start != -1
    # the news_articles SELECT runs from its SQL string to the next shape label
    stop = src.find("news_items", start)
    stop = stop if stop != -1 else len(src)
    segment = src[start:stop]
    assert "INTERVAL" not in segment, (
        "news_articles shape must compare published_at against an ISO "
        "date-string cutoff, never NOW() - INTERVAL (TEXT column)")


def test_swallowed_shape_errors_are_recorded_not_lost():
    src = _scan_source()
    assert "shape_errors" in src, (
        "a skipped shape must be recorded — two dead SQLs survived "
        "indefinitely because the except swallowed them silently")
