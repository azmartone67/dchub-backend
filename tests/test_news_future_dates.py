#!/usr/bin/env python3
"""The news read paths must never serve a future-dated article.

★ WHY THIS IS A READ-SIDE GUARD, tested by source inspection.

One row of 10,966 in `news_articles` carried an EVENT date in `published_at` —
Data Center Knowledge's `/events/data-center-world-power`, dated 48 days ahead.
Under `ORDER BY published_at DESC` it sat at `articles[0]` for weeks, and because
the anonymous MCP trim returns only `articles[0]`, a conference landing page was
the ENTIRE news result an arriving agent received. It also became
`MAX(published_at)`, so the freshness SLA read -1153h and reported "within_sla"
on a record that does not exist yet.

A producer guard cannot fix it: every writer is `ON CONFLICT (id) DO NOTHING`, so
the offending row is frozen in the table and no ingest change will ever touch it.
One was tried on 2026-05-21 for this exact symptom and the row is still live.

These tests read the SOURCE rather than executing the queries, because the suite
has no database and must never import main.py. That is a real limit: they prove
the clause is present and correctly shaped, not that Postgres accepts it. The
outside-in QA probe is what proves the behaviour, by re-reading /api/news from an
anonymous seat.
"""
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _source(rel: str) -> str:
    return (ROOT / rel).read_text()


def _function_source(rel: str, name: str) -> str:
    """Pull one function out by AST so a match cannot come from another handler."""
    tree = ast.parse(_source(rel))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(_source(rel), node) or ""
    raise AssertionError(f"{name} not found in {rel} — did it get renamed?")


def _code_only(src: str) -> str:
    """Strip `#` comment lines.

    ★ Comments satisfy grep. The to_char assertion below first FAILED against a
    correct implementation because the only `to_char(` in the function was in the
    comment explaining why not to use one. This codebase has the same lesson
    recorded from the other direction — a test asserting `"X-Admin-Key" in src`
    stayed green with the admin branch deleted, because the header name also
    appeared in a comment.
    """
    return "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))


class TestApiNewsAlias:
    def test_the_agent_facing_endpoint_excludes_future_rows(self):
        src = _code_only(_function_source("main.py", "api_news_alias"))
        assert "published_at <= %s" in src, \
            "/api/news is what MCP get_news calls; without this a future-dated " \
            "row is the only article an anonymous agent receives"

    def test_null_published_at_is_still_served(self):
        src = _code_only(_function_source("main.py", "api_news_alias"))
        assert "published_at IS NULL OR published_at <= %s" in src, \
            "rows with no date must not be silently dropped by the guard"

    def test_the_bound_is_a_parameter_not_a_sql_literal(self):
        # ★ Load-bearing. published_at is TEXT on prod but a real TIMESTAMP in
        # environments the codebase still supports, and a to_char() text literal
        # raises there. In THIS function the raise is invisible: the table
        # fallback loop catches it and silently serves a different table, so a
        # broken clause reroutes get_news instead of failing loudly.
        src = _code_only(_function_source("main.py", "api_news_alias"))
        assert "to_char(" not in src, \
            "a to_char literal breaks on a real TIMESTAMP column, and the " \
            "failure is swallowed by the table-fallback loop"

    def test_the_guard_precedes_the_optional_filters(self):
        # The clause and its param are appended together; anything appended
        # after must keep that pairing or the positional args desynchronise.
        src = _code_only(_function_source("main.py", "api_news_alias"))
        guard = src.index("published_at <= %s")
        query_filter = src.index("title ILIKE %s")
        assert guard < query_filter, \
            "clause/param pairs must be appended in order or params desync"


class TestLiveNewsEndpoint:
    def test_the_human_page_endpoint_excludes_future_rows(self):
        # ★ BOTH endpoints or neither. dchub.cloud/news reads /api/news/live
        # FIRST, so fixing only the agent channel would clear the MCP surface
        # while a September conference stayed the top story on the human page —
        # the defect reported closed while still on screen.
        src = _code_only(_function_source("routes/deals_routes.py", "get_live_news"))
        assert "published_at <= %s" in src

    def test_timedelta_is_imported(self):
        # The guard computes now+6h; without the import this raises NameError
        # at request time, not at import time, so CI would stay green.
        assert re.search(r"^from datetime import .*\btimedelta\b",
                         _source("routes/deals_routes.py"), re.M), \
            "timedelta is used by the guard and must be imported"


class TestGraceWindow:
    def test_both_endpoints_use_the_same_six_hour_grace(self):
        # 6h matches the grace the news domain already declares for itself in
        # freshness_public — not a number invented here — and absorbs rows
        # written timezone-naive as UTC.
        for rel, fn in (("main.py", "api_news_alias"),
                        ("routes/deals_routes.py", "get_live_news")):
            src = _code_only(_function_source(rel, fn))
            assert "timedelta(hours=6)" in src, f"{rel}:{fn} lost the grace window"
