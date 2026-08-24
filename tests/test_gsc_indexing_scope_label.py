"""/api/gsc/indexing returns a TOP-100 subtotal. It must not be named a total.

2026-08-24: the endpoint hardcodes rowLimit=100 and GSC orders by clicks DESC,
so every number it returns describes the top 100 pages. It published them as
`total_clicks` / `total_impressions` / `indexed_pages`. Measured that day:

    endpoint summary.total_clicks  ->   316   (top 100 pages, 28d)
    property 28d clicks            -> ~1,300  (searchAnalytics, dimensions=[])

A 4x under-read named "total". This is the same failure the GSC Overview
"3,018 total web search clicks" caused -- that number is a cumulative 3-month
running sum, and was read as a daily record because nothing in its name said
otherwise. A field name is part of the measurement.
"""
import ast
import os
import re

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "google_search_console.py")
_TEXT = open(_SRC, encoding="utf-8").read()


def _indexing_source() -> str:
    tree = ast.parse(_TEXT)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "indexing_status":
            return ast.get_source_segment(_TEXT, node) or ""
    raise AssertionError("indexing_status not found")


def test_row_limit_is_still_100_or_this_guard_is_stale():
    """If the cap is lifted, the whole premise changes -- fail loudly."""
    src = _indexing_source()
    assert re.search(r"'rowLimit':\s*100", src), (
        "rowLimit is no longer 100 in indexing_status. If the endpoint now "
        "paginates the whole property, the scope warning below is wrong and "
        "this guard needs rewriting rather than deleting."
    )


def test_scope_is_declared_in_the_payload():
    src = _indexing_source()
    assert "'scope'" in src, (
        "the response does not declare its scope. A caller cannot tell a "
        "top-100 subtotal from a property total by looking at the JSON."
    )
    assert re.search(r"TOP-100|top.100", src, re.I), (
        "the scope string does not say the numbers cover only the top 100 pages"
    )


def test_honest_field_names_are_present():
    src = _indexing_source()
    for k in ("top_pages_clicks", "top_pages_impressions", "pages_returned"):
        assert k in src, f"{k} missing -- the subtotal has no honestly-named field"
    assert "'is_property_total': False" in src, (
        "is_property_total is not stated as False; a reader has to infer it"
    )


def test_legacy_names_carry_a_deprecation_note():
    """They stay for compatibility, but must not read as totals silently."""
    src = _indexing_source()
    if "'total_clicks'" in src:
        assert "_deprecated" in src, (
            "total_clicks is still published with no _deprecated note saying "
            "it is a top-100 subtotal. Either name it honestly or mark it."
        )
