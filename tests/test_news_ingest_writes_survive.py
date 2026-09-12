"""news_engine — the rows a sync reports must be the rows that survive it.

2026-09-12. News intake had been dead for nine days while every log line said
otherwise. Two defects, both of which this file pins:

  1. `init_news_db` carried the only
     `ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS publisher_url TEXT`,
     and it ran through `db_utils.PGCursorWrapper`, whose `execute()` RETURNS
     WITHOUT SENDING anything whose SQL starts with CREATE TABLE / CREATE INDEX
     / ALTER TABLE (`SKIP_DDL` defaults to `'1'`). The statement reached
     Postgres zero times in nine days, raised nothing and logged nothing, so
     every INSERT naming the column failed against a column that could never
     appear.

  2. The batch writer retried a failed chunk by calling `pg_conn.rollback()`
     first — which ends the whole transaction and discards every chunk already
     inserted. Of 320 articles only the last chunk survived the closing commit,
     while the counter kept adding the rowcounts the rollback had thrown away.
     Measured on the Railway worker: `📰 PG sync: 172 new articles inserted`
     on a run that moved the table from 11,909 rows to 11,910.

So the assertions here are about what is COMMITTED, never about what a function
returned or logged. The fake database models savepoints and rollbacks precisely
so that "the rows survived" is a fact the test can read, and
`test_the_fixture_can_observe_the_loss_it_is_looking_for` proves that fake can
still show the old behaviour losing them.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture()
def engine():
    """A freshly imported news_engine with its measured column state reset."""
    module = importlib.import_module("news_engine")
    module._PUBLISHER_URL_COLUMN = None
    yield module
    module._PUBLISHER_URL_COLUMN = None


# ── a Postgres faithful enough to lose rows ──────────────────────────────────
class Missing(Exception):
    """Stands in for psycopg2.errors.UndefinedColumn."""


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self._result = None

    def execute(self, sql, params=None):
        head = " ".join(sql.strip().split()[:3]).upper()
        self.conn.statements.append(head)
        if head.startswith("SAVEPOINT"):
            self.conn.savepoints[sql.strip().split()[1]] = len(self.conn.pending)
        elif head.startswith("RELEASE SAVEPOINT"):
            self.conn.savepoints.pop(sql.strip().split()[2], None)
        elif head.startswith("ROLLBACK TO"):
            name = sql.strip().split()[-1]
            if name not in self.conn.savepoints:
                raise Missing(f'savepoint "{name}" does not exist')
            del self.conn.pending[self.conn.savepoints[name]:]
        elif "INFORMATION_SCHEMA.COLUMNS" in sql.upper():
            self._result = (1,) if "publisher_url" in self.conn.columns else None
        elif head.startswith("ALTER TABLE"):
            if self.conn.alter_works:
                self.conn.columns.add("publisher_url")
        elif head.startswith("DELETE FROM"):
            pass
        return self

    def fetchone(self):
        return self._result

    def close(self):
        pass


class FakeConn:
    """Commit semantics are the whole point: `pending` survives only a commit,
    a savepoint rollback trims it, and a connection rollback empties it."""

    def __init__(self, *, columns=(), alter_works=True, fail_chunks=(),
                 fail_wide_chunks=()):
        self.columns = set(columns)
        self.alter_works = alter_works
        self.fail_chunks = set(fail_chunks)        # chunk index -> always fails
        self.fail_wide_chunks = set(fail_wide_chunks)  # fails only the 13-col form
        self.pending: list = []
        self.committed: list = []
        self.savepoints: dict[str, int] = {}
        self.statements: list[str] = []
        self.rollbacks = 0
        self.chunk_calls = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed.extend(self.pending)
        self.pending = []
        self.savepoints.clear()

    def rollback(self):
        self.rollbacks += 1
        self.pending = []
        self.savepoints.clear()

    def close(self):
        pass


def install(monkeypatch, conn, *, legacy_ok=True):
    """Point psycopg2 at `conn` and give it an execute_values that can fail."""
    monkeypatch.setenv("NEON_DATABASE_URL", "postgres://fake/db")
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: conn)

    def execute_values(cur, sql, rows, template=None):
        index = conn.chunk_calls
        wide = "publisher_url" in sql
        if wide:
            conn.chunk_calls += 1
        if index in conn.fail_chunks or (wide and index in conn.fail_wide_chunks):
            raise Missing('column "publisher_url" of relation "news_articles" '
                          'does not exist')
        if not wide and not legacy_ok:
            raise Missing("insert rejected")
        conn.pending.extend(rows)
        cur.rowcount = len(rows)

    monkeypatch.setattr(psycopg2.extras, "execute_values", execute_values)


def articles(n):
    return [{"id": f"id{i}", "title": f"t{i}", "url": f"http://x/{i}",
             "source": "S", "published_at": None, "publisher_url": "http://p"}
            for i in range(n)]


# ── the batch writer ─────────────────────────────────────────────────────────
def test_one_failing_chunk_does_not_take_the_others(engine, monkeypatch):
    """320 articles is four chunks. One bad chunk used to cost all four.

    Chunk 3 fails BOTH ways here — wide and narrow — so it is genuinely lost;
    the question is whether it takes its siblings with it."""
    conn = FakeConn(columns={"publisher_url"}, fail_chunks={2})
    install(monkeypatch, conn, legacy_ok=False)

    engine._sync_articles_to_pg(articles(320))

    assert len(conn.committed) == 220, (
        "chunks 1, 2 and 4 must survive a failure in chunk 3")
    assert conn.rollbacks == 0, (
        "a chunk failure must roll back to its SAVEPOINT, never the connection")


def test_the_fixture_can_observe_the_loss_it_is_looking_for(engine, monkeypatch):
    """★ CONTROL. The assertion above is worthless if this fake cannot show a
    connection-wide rollback destroying committed-in-transaction work."""
    conn = FakeConn(columns={"publisher_url"})
    install(monkeypatch, conn)
    cur = conn.cursor()
    cur.execute("SAVEPOINT news_chunk")
    conn.pending.extend(["row-a", "row-b"])
    conn.rollback()
    conn.commit()
    assert conn.committed == [], "the fake cannot lose rows, so it cannot prove they were kept"


def test_a_column_the_database_lacks_costs_the_field_not_the_articles(engine, monkeypatch):
    """The wide INSERT fails, the narrow one lands, and every row still arrives."""
    conn = FakeConn(columns=set(), alter_works=False, fail_wide_chunks={0, 1, 2, 3})
    install(monkeypatch, conn)

    engine._sync_articles_to_pg(articles(320))

    assert len(conn.committed) == 320
    assert conn.rollbacks == 0


def test_the_column_is_added_when_it_is_missing(engine, monkeypatch):
    conn = FakeConn(columns=set(), alter_works=True)
    install(monkeypatch, conn)

    engine._sync_articles_to_pg(articles(10))

    assert "ALTER TABLE NEWS_ARTICLES" in conn.statements
    assert "publisher_url" in conn.columns
    assert len(conn.committed) == 10


def test_the_count_reported_is_the_count_that_survived(engine, monkeypatch, caplog):
    conn = FakeConn(columns={"publisher_url"}, fail_chunks={2})
    install(monkeypatch, conn, legacy_ok=False)

    with caplog.at_level("INFO"):
        engine._sync_articles_to_pg(articles(320))

    line = next((r.message for r in caplog.records if "PG sync:" in r.message), "")
    assert line, "the writer printed no summary line"
    assert f"{len(conn.committed)} new articles committed" in line, (
        f"summary {line!r} does not name the {len(conn.committed)} rows that "
        f"survived the commit")
    assert "100 dropped" in line, "a dropped chunk must be reported, not absorbed"


# ── the schema check ─────────────────────────────────────────────────────────
def test_ensure_reads_the_column_back_instead_of_trusting_the_alter(engine):
    """`ADD COLUMN IF NOT EXISTS` reports success whether or not it did
    anything, and this whole outage was a statement that appeared to run."""
    lying = FakeConn(columns=set(), alter_works=False)
    assert engine.ensure_publisher_url_column(lying) is False

    honest = FakeConn(columns=set(), alter_works=True)
    assert engine.ensure_publisher_url_column(honest) is True


def test_a_column_already_there_is_not_altered(engine):
    conn = FakeConn(columns={"publisher_url"})
    assert engine.ensure_publisher_url_column(conn) is True
    assert not any(s.startswith("ALTER") for s in conn.statements)


def test_a_failed_check_does_not_narrow_what_gets_written(engine, monkeypatch):
    """A check that cannot look must not answer 'the column is absent'."""
    monkeypatch.setenv("NEON_DATABASE_URL", "postgres://x/y")
    monkeypatch.setattr(psycopg2, "connect",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert engine.publisher_url_column_available() is True


# ── the trap this fix exists because of ──────────────────────────────────────
def _source(name):
    tree = ast.parse((REPO / "news_engine.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"news_engine has no {name}()")


def test_ddl_through_the_pooled_cursor_is_still_a_silent_no_op():
    """If this ever fails, db_utils changed and news_engine's direct-connection
    ALTER can be reconsidered. Until then, DDL written against a get_db()
    cursor is dead code that raises nothing."""
    import db_utils

    class Underlying:
        def __init__(self):
            self.seen = []

        def execute(self, sql, params=None):
            self.seen.append(sql)

    underlying = Underlying()
    db_utils.PGCursorWrapper(underlying).execute(
        "ALTER TABLE news_articles ADD COLUMN IF NOT EXISTS publisher_url TEXT")
    assert underlying.seen == [], (
        "the pooled cursor now forwards DDL — revisit ensure_publisher_url_column")
    assert db_utils.SKIP_DDL, "SKIP_DDL defaults on; that default is the trap"


def test_the_alter_does_not_live_where_it_cannot_run():
    init = _source("init_news_db")
    for node in ast.walk(init):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "ALTER TABLE" not in node.value.upper(), (
                "init_news_db runs on a pooled connection, which drops DDL — "
                "the ALTER belongs in ensure_publisher_url_column")


def test_the_ensure_never_borrows_a_pooled_connection():
    ensure = _source("ensure_publisher_url_column")
    calls = [n.func.id for n in ast.walk(ensure)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "get_db" not in calls and "get_bg_db" not in calls, (
        "a pooled connection cannot run this ALTER")


def test_both_writers_ask_before_they_write():
    saver = _source("save_articles")
    calls = [n.func.id for n in ast.walk(saver)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "publisher_url_column_available" in calls
    for node in ast.walk(saver):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "SAVEPOINT sp_article" not in node.value, (
                "the savepoint recovery this writer relied on never existed")
