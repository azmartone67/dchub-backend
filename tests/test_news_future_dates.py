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


class TestApiV1NewsRestAlias:
    """★2026-08-22: the THIRD news read path. #2216 guarded /api/news (MCP
    get_news) and /api/news/live (the human page) and left /api/v1/news —
    the REST/OpenAPI alias served by routes/deals_routes.get_agent_news (also
    /api/news-feed) — unguarded. Measured live 2026-08-22 00:40Z: articles[0]
    on /api/v1/news was the 2026-09-21 conference page while MCP get_news read
    clean. Same guard, same shape, same grace."""

    def test_the_rest_alias_excludes_future_rows(self):
        src = _code_only(_function_source("routes/deals_routes.py", "get_agent_news"))
        assert "published_at <= %s" in src, \
            "/api/v1/news is the OpenAPI news surface; without this a future-dated " \
            "row is articles[0] for every REST/Actions caller"

    def test_the_bound_is_a_parameter_not_a_sql_literal(self):
        src = _code_only(_function_source("routes/deals_routes.py", "get_agent_news"))
        assert "to_char(" not in src

    def test_same_six_hour_grace_as_the_other_two_paths(self):
        src = _code_only(_function_source("routes/deals_routes.py", "get_agent_news"))
        assert "timedelta(hours=6)" in src

    def test_the_guard_is_appended_with_its_parameter_before_the_optional_filters(self):
        # The clause and its param must be appended together, ahead of the
        # category/source filters, or the positional args desynchronise.
        src = _code_only(_function_source("routes/deals_routes.py", "get_agent_news"))
        guard = src.index("published_at <= %s")
        bound = src.index("timedelta(hours=6)")
        cat = src.index("_pg_news_cat_filter()} = %s")
        assert guard < bound < cat


def _helper_namespace():
    """exec ONLY the clamp helper out of news_engine.py — the module imports
    feedparser/requests/db_utils at import time and this suite has no network
    and no DB. AST-extracted so the test runs the real function body."""
    src = _source("news_engine.py")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_clamp_future_published_at")
    from datetime import datetime, timezone
    ns = {"datetime": datetime, "timezone": timezone}
    exec(ast.get_source_segment(src, fn), ns)
    return ns["_clamp_future_published_at"]


def _calls_in(rel, fn_name):
    src = _source(rel)
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == fn_name)
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", None))
    return names


class TestIngestClampReachesBothWriters:
    """The producer-side half. ★Comments satisfy grep, so both writers are
    checked by walking the AST for the CALL, not by searching the text."""

    def test_the_pg_write_through_clamps(self):
        # THE regression: _sync_articles_to_pg is what fills the table every
        # agent read serves, and it had no clamp at all.
        assert "_clamp_future_published_at" in _calls_in("news_engine.py", "_sync_articles_to_pg")

    def test_the_sqlite_writer_uses_the_same_helper(self):
        assert "_clamp_future_published_at" in _calls_in("news_engine.py", "save_articles")

    def test_a_naive_future_iso_string_is_clamped(self):
        # ★ The load-bearing case: the retired inline clamp raised TypeError on
        # naive-vs-aware comparison and swallowed it, so naive RSS dates — the
        # shape of the 2026-09-21 row — were never clamped.
        from datetime import datetime
        clamp = _helper_namespace()
        now = datetime(2026, 8, 22, 0, 0, 0)
        assert clamp("2026-09-21T11:00:00", now=now) == now.isoformat()

    def test_an_aware_future_iso_string_is_clamped(self):
        from datetime import datetime, timezone
        clamp = _helper_namespace()
        now = datetime(2026, 8, 22, 0, 0, 0, tzinfo=timezone.utc)
        assert clamp("2026-09-21T11:00:00+00:00", now=now) == now.isoformat()
        assert clamp("2026-09-21T11:00:00Z", now=now) == now.isoformat()

    def test_past_dates_and_garbage_are_left_alone(self):
        from datetime import datetime
        clamp = _helper_namespace()
        now = datetime(2026, 8, 22, 0, 0, 0)
        assert clamp("2026-08-21T23:35:55", now=now) == "2026-08-21T23:35:55"
        assert clamp("not a date", now=now) == "not a date"
        assert clamp(None, now=now) is None
        assert clamp("", now=now) == ""

    def test_a_naive_future_date_is_clamped_against_the_real_clock(self):
        # ★ The branch the explicit-`now` cases above cannot reach. The retired
        # inline clamp failed EXACTLY here: real clock + naive input → TypeError
        # (naive vs aware) → swallowed → no clamp. A mutation that rebuilds that
        # pairing (`datetime.now(timezone.utc)` for a naive input) must fail
        # this, and did not fail anything before this test existed.
        from datetime import datetime
        clamp = _helper_namespace()
        before = datetime.utcnow()
        out = clamp("2999-01-01T00:00:00")
        after = datetime.utcnow()
        assert out != "2999-01-01T00:00:00", "a naive far-future date must be clamped with no `now` given"
        got = datetime.fromisoformat(out)
        assert got.tzinfo is None, "a naive input must clamp to a NAIVE now (same column shape)"
        assert before <= got <= after

    def test_an_aware_future_date_is_clamped_against_the_real_clock(self):
        from datetime import datetime, timezone
        clamp = _helper_namespace()
        before = datetime.now(timezone.utc)
        out = clamp("2999-01-01T00:00:00+00:00")
        after = datetime.now(timezone.utc)
        got = datetime.fromisoformat(out)
        assert got.tzinfo is not None
        assert before <= got <= after

    def test_a_datetime_object_is_serialised_and_clamped(self):
        from datetime import datetime
        clamp = _helper_namespace()
        now = datetime(2026, 8, 22, 0, 0, 0)
        assert clamp(datetime(2026, 9, 21, 11, 0, 0), now=now) == now.isoformat()
        assert clamp(datetime(2026, 8, 1, 0, 0, 0), now=now) == "2026-08-01T00:00:00"


class TestAdminClampCoversEveryReadSurface:
    """One admin call must reach every table a reader is pointed at:
    news_articles (REST + MCP reads), news (legacy radar) and announcements
    (/api/health/data-freshness + get_backup_status). On 2026-08-22 the first
    two were clamped and the health board still said `stale / future_rejected`
    because the third carried the same row."""

    def test_announcements_is_clamped_too(self):
        src = _code_only(_function_source("routes/brain_autoaction_helpers.py",
                                          "admin_news_clamp_future_dates"))
        assert "UPDATE announcements" in src
        assert "published_date::text::timestamptz > NOW()" in src, \
            "published_date is TEXT on prod (#1683): compare through a guarded cast"

    def test_announcements_clamp_is_savepointed_so_a_missing_table_cannot_abort_the_commit(self):
        src = _code_only(_function_source("routes/brain_autoaction_helpers.py",
                                          "admin_news_clamp_future_dates"))
        assert "SAVEPOINT sp_ann" in src and "ROLLBACK TO SAVEPOINT sp_ann" in src

    def test_the_response_reports_the_third_table(self):
        src = _code_only(_function_source("routes/brain_autoaction_helpers.py",
                                          "admin_news_clamp_future_dates"))
        assert "touched_announcements=" in src
