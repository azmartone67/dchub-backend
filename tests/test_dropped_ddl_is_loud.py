"""A statement the pooled cursor throws away must SAY SO.

The bug class
-------------
`db_utils.PGCursorWrapper.execute` returns early for CREATE TABLE / CREATE
INDEX / ALTER TABLE whenever `SKIP_DDL` is set — and it defaults to '1' and is
absent from prod config. It used to do that in complete silence: no raise, no
log, no table.

That silence, not the skip, is what cost nine days. `news_engine.init_news_db`
issued `ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS publisher_url TEXT`
on a pooled cursor; the statement never reached Postgres; every INSERT naming
the column then failed inside a fallback; and news intake ran at ~10 rows/day
instead of ~157 from 2026-09-08 to 09-12 while the sync log reported "172 new
articles inserted" (#4438). Nothing in the logs said the ALTER had not run.

What is pinned here
-------------------
The skip STAYS. `SKIP_DDL` has defaulted on for a long time and 57 functions
have been running against that default, so flipping it would execute 212 frozen
statements at once — see
tests/test_news_ingest_writes_survive.py::test_ddl_through_the_pooled_cursor_is_still_a_silent_no_op,
which deliberately pins the no-op. This file pins only that the drop is
OBSERVABLE: a greppable warning, naming the caller, once per call site.

The two halves are complementary, not contradictory — that test asserts the
statement is not forwarded and does not raise; this one asserts it is not
silent. A change that made the drop loud AND forwarded would fail that test; a
change that restored silence fails this one.
"""
from __future__ import annotations

import logging

import pytest

import db_utils


@pytest.fixture(autouse=True)
def _fresh_dedupe():
    """The suppression cache is module-level and outlives a test.

    Without this, whichever test ran first would be the only one to see a
    warning and the rest would pass vacuously — green for the wrong reason,
    and dependent on collection order.
    """
    db_utils._DDL_DROPPED_SEEN.clear()
    yield
    db_utils._DDL_DROPPED_SEEN.clear()


class _Underlying:
    """A raw cursor double. Records what actually reached the driver."""
    description = None
    # psycopg2 cursors ALWAYS expose rowcount (-1 when undefined), and since
    # #4453 the wrapper reads it immediately after execute() so a later lastval
    # probe cannot overwrite the count. Without it here that read raised
    # AttributeError *inside the wrapper's own try*, which logs "PG query
    # failed" and rolls back — so a statement that was forwarded fine looked
    # dropped, and the three "not dropped are not reported" cases below went
    # red on origin/main. A double less capable than the real cursor on the
    # success path turns success into failure.
    rowcount = -1

    def __init__(self):
        self.seen = []

    def execute(self, sql, params=None):
        self.seen.append(sql)


DDL = "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS publisher_url TEXT"


def test_the_double_models_every_cursor_attribute_the_wrapper_reads():
    """★ THE META-GUARD, added 2026-09-12 after #4453 broke this file.

    Derived, not hand-listed: it reads which `self._cur.<attr>` the wrapper
    touches on its SUCCESS path and requires the double to model each.

    ★ The walk is PRUNED AT `except` HANDLERS, which is the whole subtlety.
      `self._cur.connection.rollback()` sits in a try nested INSIDE execute()'s
      own handler, so a naive "every ast.Try body" scan reports `connection` as
      success-path and demands the double grow an attribute that only the error
      path touches — that path is itself wrapped in try/except, so a missing
      attribute there cannot turn success into failure. The first version of
      this test did exactly that and contradicted this docstring.
    """
    import ast
    import pathlib

    def success_reads(node):
        """self._cur.<attr> loads reachable without entering an except handler."""
        out = set()
        if (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "_cur"):
            out.add(node.attr)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ExceptHandler):
                continue                         # error path — guarded there
            out |= success_reads(child)
        return out

    src = pathlib.Path(db_utils.__file__).read_text(encoding="utf-8")
    cls = next(n for n in ast.walk(ast.parse(src))
               if isinstance(n, ast.ClassDef) and n.name == "PGCursorWrapper")
    fn = next(n for n in cls.body
              if isinstance(n, ast.FunctionDef) and n.name == "execute")
    reads = success_reads(fn)
    assert {"execute", "rowcount"} <= reads, (
        f"the scan found {sorted(reads)} — it must at least see the execute() "
        f"call and the rowcount read #4453 added, or it is blind and would pass "
        f"on anything")
    missing = sorted(a for a in reads if not hasattr(_Underlying(), a))
    assert not missing, (
        f"PGCursorWrapper.execute reads self._cur.{missing} on its success "
        f"path, but this double does not model {missing}. The read raises "
        f"inside the wrapper's own try, which logs 'PG query failed' and makes "
        f"a forwarded statement look dropped — exactly how #4453 turned this "
        f"file red. Add the attribute to _Underlying, modelling psycopg2.")


def test_a_dropped_statement_is_reported(caplog):
    """The exact statement from #4438, on the exact cursor that dropped it."""
    with caplog.at_level(logging.WARNING, logger="db_utils"):
        db_utils.PGCursorWrapper(_Underlying()).execute(DDL)
    assert db_utils._DDL_DROPPED_TOKEN in caplog.text, (
        "the pooled cursor dropped a DDL statement and said nothing — this is "
        "the silence that hid the news_articles.publisher_url ALTER for nine "
        "days (#4438)")


def test_the_report_names_the_statement_and_the_caller(caplog):
    """A warning that names only db_utils tells you the trap exists, not who
    walked into it. All 212 frozen statements would produce the same line."""
    with caplog.at_level(logging.WARNING, logger="db_utils"):
        db_utils.PGCursorWrapper(_Underlying()).execute(DDL)
    assert "publisher_url" in caplog.text, "the statement itself is not in the log"
    assert "test_dropped_ddl_is_loud.py" in caplog.text, (
        "the warning does not name the CALLER — it points at db_utils, which "
        "is the one file that is never the thing to fix")


def test_the_fix_is_named_in_the_message(caplog):
    """Someone reading this line at 3am should not have to find the fix."""
    with caplog.at_level(logging.WARNING, logger="db_utils"):
        db_utils.PGCursorWrapper(_Underlying()).execute(DDL)
    assert "ddl_cursor" in caplog.text, (
        "the warning does not name db_utils.ddl_cursor(), the blessed path")


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "INSERT INTO news_articles (title) VALUES ('x')",
    "UPDATE news_articles SET title = 'x'",
])
def test_statements_that_are_not_dropped_are_not_reported(caplog, sql):
    """A warning on every query is a warning on nothing."""
    u = _Underlying()
    with caplog.at_level(logging.WARNING, logger="db_utils"):
        db_utils.PGCursorWrapper(u).execute(sql)
    assert db_utils._DDL_DROPPED_TOKEN not in caplog.text, (
        f"{sql!r} was reported as dropped, but it is forwarded")
    assert u.seen, f"{sql!r} did not reach the driver"


def test_one_call_site_reports_once(caplog):
    """These fire from boot-time init functions, several of which run per
    worker. A line per execution is noise, and noise is silence with extra
    steps."""
    cur = db_utils.PGCursorWrapper(_Underlying())

    def boot():                     # ONE call site, called repeatedly
        cur.execute("CREATE TABLE IF NOT EXISTS learned_entities (id TEXT)")

    with caplog.at_level(logging.WARNING, logger="db_utils"):
        for _ in range(25):
            boot()
    assert caplog.text.count(db_utils._DDL_DROPPED_TOKEN) == 1, (
        "25 executions of one call site produced "
        f"{caplog.text.count(db_utils._DDL_DROPPED_TOKEN)} warnings")


def test_distinct_call_sites_each_report(caplog):
    """Dedupe must not collapse two different offences into one report."""
    cur = db_utils.PGCursorWrapper(_Underlying())
    with caplog.at_level(logging.WARNING, logger="db_utils"):
        cur.execute("CREATE TABLE IF NOT EXISTS a (id TEXT)")
        cur.execute("CREATE TABLE IF NOT EXISTS b (id TEXT)")
    assert caplog.text.count(db_utils._DDL_DROPPED_TOKEN) == 2, (
        "two distinct dropped statements did not produce two reports")


def test_reporting_never_breaks_the_caller(caplog):
    """This runs on a path that is already degrading. A logging bug must not
    become a database bug: still returns the cursor, still does not raise,
    still does not forward."""
    u = _Underlying()
    cur = db_utils.PGCursorWrapper(u)
    with caplog.at_level(logging.WARNING, logger="db_utils"):
        assert cur.execute(DDL) is cur, "execute no longer returns the cursor"
    assert u.seen == [], (
        "the pooled cursor now FORWARDS DDL — that is a different change from "
        "making the drop loud; see test_news_ingest_writes_survive.py")
    assert db_utils.SKIP_DDL, (
        "SKIP_DDL's default was flipped. That executes 212 frozen statements "
        "from ~57 functions at once and is explicitly not the fix here")


def test_the_logger_is_reachable_from_a_bare_root_handler():
    """Railway captures stdout via the root logger. A warning emitted on a
    logger with propagate=False would pass every caplog assertion above and
    still never appear in production logs."""
    assert db_utils.logger.propagate, (
        "db_utils' logger does not propagate — these warnings would be "
        "invisible in Railway logs, which is where they have to be read")
