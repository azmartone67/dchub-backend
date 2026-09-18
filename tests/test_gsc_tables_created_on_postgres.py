"""The GSC tables exist AFTER init_gsc_tables() runs — asked of the database.

★ WHY THIS IS NOT tests/test_ddl_through_pool.py's JOB. That file reads the
SOURCE and decides whether the DDL is pointed at a raw cursor. It cannot know
whether the statement then reached Postgres, and every previous version of this
bug looked correct in exactly that way: a `CREATE TABLE IF NOT EXISTS` with a
boot log line saying "tables initialized", against a database that had no such
table. `init_gsc_tables()` printed that line on every boot from the day it was
written and created nothing.

So this asks the database, on its own connection, after the function has run.
A green boot log is not proof; `to_regclass` is.

★ AND WHY THE CONTROL BELOW IS HALF THE FILE. If SKIP_DDL were off in this
environment, the first test would pass no matter which cursor the DDL rode,
and would go on passing through a full regression. test_the_trap_is_armed
pins that the pooled path really does drop the statement HERE, so the other
tests mean what they say.

Needs a throwaway Postgres: DCHUB_PG_TEST_DSN, stamped by
scripts/neon_ci_branch.py (this file DROPs). Set GSC_TABLES_REQUIRE=1 to fail
rather than skip when it is absent — the ephemeral-db lane sets both, because
a lane that skips silently builds a fake track record.
"""
from __future__ import annotations

import os

import pytest

from util.ephemeral_db_guard import assert_ephemeral

DSN = os.environ.get("DCHUB_PG_TEST_DSN", "")
REQUIRED = os.environ.get("GSC_TABLES_REQUIRE") == "1"
TABLES = ("gsc_index_requests", "gsc_crawl_errors", "gsc_sitemap_submissions")

if REQUIRED and not DSN:
    raise RuntimeError(
        "GSC_TABLES_REQUIRE=1 but DCHUB_PG_TEST_DSN is empty — this lane was "
        "asked to prove the GSC tables get created and has no database to ask.")

needs_branch = pytest.mark.skipif(
    not DSN, reason="set DCHUB_PG_TEST_DSN to a stamped ephemeral branch")

if DSN:
    assert_ephemeral(DSN)   # refuses anything without the CI sentinel


def _raw():
    """A connection of OUR own — never the one the code under test used."""
    psycopg2 = pytest.importorskip("psycopg2")
    c = psycopg2.connect(DSN, connect_timeout=15)
    c.autocommit = True
    return c


def _present():
    c = _raw()
    try:
        with c.cursor() as cur:
            out = {}
            for t in TABLES:
                cur.execute("SELECT to_regclass(%s)", (f"public.{t}",))
                out[t] = cur.fetchone()[0] is not None
            return out
    finally:
        c.close()


def _drop():
    c = _raw()
    try:
        with c.cursor() as cur:
            for t in TABLES:
                cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    finally:
        c.close()


@pytest.fixture
def clean_slate(monkeypatch):
    # SKIP_DDL='1' is the PRODUCTION default (db_utils.py:13) and the condition
    # the bug needed. ★ This setenv only BITES when db_utils has not been
    # imported yet — db_utils.SKIP_DDL is a module-level constant read once at
    # import, so in a full-suite run where something imported db_utils first
    # this line does nothing at all. It is not the guarantee; the control test
    # below is, and it is written to fail loudly in exactly that case.
    monkeypatch.setenv("SKIP_DDL", "1")
    monkeypatch.setenv("DATABASE_URL", DSN)
    _drop()
    assert _present() == {t: False for t in TABLES}, "drop did not take"
    yield
    _drop()


@needs_branch
def test_the_trap_is_armed_in_this_environment(clean_slate):
    """The control. DDL on a pooled cursor must still vanish here — otherwise
    the test below proves nothing about the cursor it rode."""
    import db_utils
    assert db_utils.SKIP_DDL is True, (
        "SKIP_DDL is off in this run, so a pooled cursor would execute DDL "
        "normally and every other test in this file would pass vacuously")
    psycopg2 = pytest.importorskip("psycopg2")
    raw = psycopg2.connect(DSN, connect_timeout=15)
    try:
        wrapped = db_utils.PGConnectionWrapper(raw, return_func=lambda c: None)
        cur = wrapped.cursor()
        for t in TABLES:
            cur.execute(f"CREATE TABLE IF NOT EXISTS {t} (id SERIAL PRIMARY KEY)")
        try:
            wrapped.commit()
        except Exception:
            pass
    finally:
        raw.close()
    assert _present() == {t: False for t in TABLES}, (
        "a pooled cursor CREATED a table — the wrapper's DDL skip is not "
        "active, so this file cannot tell a fixed function from a broken one")


@needs_branch
def test_init_gsc_tables_really_creates_all_three(clean_slate):
    """★ The whole point. Three CREATEs, all of them dropped before this fix."""
    import google_search_console

    google_search_console.init_gsc_tables()

    missing = [t for t, ok in _present().items() if not ok]
    assert not missing, (
        f"init_gsc_tables() returned and printed its success line, but "
        f"{missing} do not exist. The DDL is back on a pooled cursor.")


@needs_branch
def test_the_insert_that_was_500ing_now_works(clean_slate):
    """POST /api/gsc/sitemap/submit PUTs the sitemap to Google FIRST and only
    then runs this INSERT, so before the fix the submission succeeded and the
    caller still got a 500 with nothing recorded."""
    import google_search_console
    google_search_console.init_gsc_tables()

    c = _raw()
    try:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO gsc_sitemap_submissions (sitemap_url, status) "
                "VALUES (%s, %s)", ("https://dchub.cloud/sitemap.xml", "submitted"))
            cur.execute("SELECT sitemap_url, status FROM gsc_sitemap_submissions")
            assert cur.fetchone() == ("https://dchub.cloud/sitemap.xml", "submitted")
    finally:
        c.close()
