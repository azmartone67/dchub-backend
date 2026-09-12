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
import re
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
    description = None  # a real cursor always has it (None before a statement)
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
        description = None  # a real cursor always has it (None before a statement)
        rowcount = -1  # psycopg2: -1 = no statement / not determinable
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


# ── the single-row writer: the count must name rows, not offers ──────────────
# 2026-09-12 02:24Z, same worker, the run immediately after #4438 restored
# ingestion: `✅ Sync done in 49.9s — 322 new, 322 to announcements, 12081
# total`, while /api/v1/admin/news/ingest-health moved rows_24h from 10 to 182.
# 322 was exactly the number of articles OFFERED; about 172 rows landed.
#
# `save_articles` counted with `if c.rowcount > 0`, and its cursor is a
# db_utils.PGCursorWrapper whose execute() fires `SELECT lastval()` of its own
# after any INSERT carrying no RETURNING. The rowcount read back described that
# SELECT — one row, always — so an ON CONFLICT DO NOTHING that inserted nothing
# still counted as saved.
#
# Every semantic the fake below adds was measured against a real Postgres 18
# through the real PGCursorWrapper before it was written here (a fixture that
# models a database more capable than the real one turns a can't-work into a
# green): a conflicting ON CONFLICT DO NOTHING reports rowcount 0; a fresh
# insert reports 1; `SELECT lastval()` reports 1 when a sequence has been used
# in the session and raises 55000 when it has not; that raise aborts the
# transaction until a ROLLBACK TO SAVEPOINT; and `RETURNING id` hands back one
# row per row inserted and nothing for a skipped conflict.
class Aborted(Exception):
    """psycopg2 raises for both `lastval is not yet defined in this session`
    (55000) and the `current transaction is aborted` that follows it."""


class RowStoreCursor(FakeCursor):
    """FakeCursor plus the parts a row-at-a-time INSERT needs to be observable.

    Deliberately shaped like a psycopg2 cursor, because the REAL
    PGCursorWrapper is layered over it — `.description`, `.connection` and a
    per-statement `.rowcount` are read by the wrapper's own code, not by tests.
    """
    rowcount = -1  # psycopg2: -1 = no statement / not determinable

    def __init__(self, conn):
        super().__init__(conn)
        self.description = None
        self.connection = conn

    def execute(self, sql, params=None):
        head = " ".join(sql.strip().split()[:3]).upper()
        upper = sql.upper()
        if self.conn.aborted and not head.startswith("ROLLBACK TO"):
            raise Aborted("current transaction is aborted, commands ignored")
        if "LASTVAL()" in upper:
            self.conn.lastval_probes += 1
            if not self.conn.lastval_defined:
                self.conn.aborted = True
                self.description = None
                self.rowcount = -1
                raise Aborted("lastval is not yet defined in this session")
            self.conn.serial += 1
            self.description = [("lastval",)]
            self.rowcount = 1
            self._result = (self.conn.serial,)
            return self
        if head.startswith("INSERT INTO") and "NEWS_ARTICLES" in upper:
            # One `(%s` group after VALUES is the row-at-a-time form and its
            # first parameter is the id; several groups is a multi-row INSERT
            # and every parameter is an id.
            tail = sql[upper.index("VALUES"):] if "VALUES" in upper else ""
            groups = len(re.findall(r"\(\s*%s", tail))
            ids = list(params or ()) if groups > 1 else [(params or (None,))[0]]
            self.conn.offered += len(ids)
            fresh = [i for i in ids if i not in self.conn.ids()]
            self.conn.pending.extend(fresh)
            self.rowcount = len(fresh)
            if "RETURNING" in upper:
                self.description = [("id",)]
                self._returned = list(fresh)
                self._result = self._returned.pop(0) if self._returned else None
                if self._result is not None:
                    self._result = (self._result,)
            else:
                self.description = None
                self._result = None
            return self
        result = super().execute(sql, params)
        if head.startswith("ROLLBACK TO"):
            # Postgres: this is how an aborted transaction becomes usable
            # again. Without it modelled, a correct SAVEPOINT-guarded probe
            # would look like it had failed.
            self.conn.aborted = False
        self.description = [("col",)] if self._result is not None else None
        self.rowcount = 1 if self._result is not None else 0
        return result


class RowStoreConn(FakeConn):
    def __init__(self, *, lastval_defined=True, **kw):
        super().__init__(**kw)
        # A long-lived worker process has touched some serial table by the time
        # news runs, so lastval() is defined. That is the production path, and
        # the path that overcounted.
        self.lastval_defined = lastval_defined
        self.aborted = False
        self.lastval_probes = 0
        self.serial = 500
        self.offered = 0

    def ids(self):
        """Uncommitted rows conflict too — they are in the same transaction."""
        return set(self.committed) | set(self.pending)

    def cursor(self):
        return RowStoreCursor(self)

    def commit(self):
        if self.aborted:      # psycopg2: committing an aborted tx discards it
            self.pending = []
            self.aborted = False
            self.savepoints.clear()
            return
        super().commit()

    def rollback(self):
        self.aborted = False
        super().rollback()


def wrapped(conn):
    """The connection save_articles gets: cursors are REAL PGCursorWrappers."""
    import db_utils

    class Pooled:
        def cursor(self):
            return db_utils.PGCursorWrapper(conn.cursor())

        def commit(self):
            conn.commit()

        def rollback(self):
            conn.rollback()

        def close(self):
            pass

    return Pooled()


def store(monkeypatch, engine, conn, *, has_column=True):
    """Point save_articles at `conn` through the real wrapper."""
    monkeypatch.setattr(engine, "get_db", lambda *a, **k: wrapped(conn))
    monkeypatch.setattr(engine, "publisher_url_column_available",
                        lambda: has_column)
    # The PG batch writer and the announcements writer are separate code paths
    # with their own connections; silencing them keeps `committed` a reading of
    # the single-row writer alone.
    monkeypatch.setattr(engine, "_sync_articles_to_pg", lambda a: None)


def test_the_count_never_exceeds_the_rows_the_database_stored(engine, monkeypatch):
    """★ THE BUG. 322 offered, 172 new, 150 already present -> "322 new".

    The reported count is compared against rows the fake actually stored, not
    against a number computed the same way the writer computes it."""
    conn = RowStoreConn(columns={"publisher_url"})
    store(monkeypatch, engine, conn)
    already = articles(150)
    engine.save_articles(already)
    assert len(conn.committed) == 150
    conn.offered = 0

    reported = engine.save_articles(already + articles(322)[150:])

    landed = len(conn.committed) - 150
    assert conn.offered == 322, "the run must offer 322 articles to be the real case"
    assert landed == 172, f"expected 172 new rows to land, {landed} did"
    assert reported <= landed, (
        f"save_articles reported {reported} new for {landed} rows actually "
        f"stored — a count of articles OFFERED, not inserted")
    assert reported == landed, (
        f"save_articles reported {reported}, the database stored {landed}")


def test_a_conflicting_insert_is_not_counted(engine, monkeypatch):
    """The same article twice is one row, and must be reported as one."""
    conn = RowStoreConn(columns={"publisher_url"})
    store(monkeypatch, engine, conn)

    first = engine.save_articles(articles(4))
    second = engine.save_articles(articles(4))

    assert first == 4
    assert second == 0, f"re-offering the same 4 articles reported {second} new"
    assert len(conn.committed) == 4


def test_the_count_holds_without_the_publisher_url_column(engine, monkeypatch):
    """The narrow statement is a separate literal and needs the same RETURNING."""
    conn = RowStoreConn(columns=set())
    store(monkeypatch, engine, conn, has_column=False)

    engine.save_articles(articles(5))
    reported = engine.save_articles(articles(8))

    assert len(conn.committed) == 8
    assert reported == 3, f"narrow-statement path reported {reported} for 3 new rows"


# ── the wrapper underneath, exercised as itself ──────────────────────────────
def test_the_real_wrapper_reports_the_inserts_rowcount_not_its_own_probe(engine):
    """db_utils regression pin: 102 reads of rowcount in this repo sit right
    after an INSERT with no RETURNING (13 provably on a pooled wrapper cursor,
    67 depending on the caller). All of them read this number.

    Two shapes, because they fail for different reasons. A CONFLICTING insert
    catches a probe that runs when nothing was inserted. A MULTI-ROW insert
    catches a rowcount that describes the probe rather than the statement —
    the only shape that can, since a single new row and a lastval() both
    report exactly 1."""
    import db_utils

    conn = RowStoreConn(columns={"publisher_url"})
    cur = db_utils.PGCursorWrapper(conn.cursor())
    plain = ("INSERT INTO news_articles (id) VALUES (%s) "
             "ON CONFLICT (id) DO NOTHING")

    cur.execute(plain, ("a1",))
    assert cur.rowcount == 1, "a row that inserted must report 1"
    assert conn.lastval_probes == 1, (
        "the wrapper did not probe lastval, so this test is no longer "
        "exercising the statement shape that broke — re-aim it")
    conn.commit()

    cur.execute(plain, ("a1",))
    assert cur.rowcount == 0, (
        f"a conflicting ON CONFLICT DO NOTHING reported rowcount "
        f"{cur.rowcount}; the wrapper is describing its own SELECT lastval()")
    conn.commit()
    assert conn.committed == ["a1"]

    before = conn.lastval_probes
    cur.execute("INSERT INTO news_articles (id) VALUES (%s),(%s),(%s) "
                "ON CONFLICT (id) DO NOTHING", ("b1", "b2", "b3"))
    assert conn.lastval_probes == before + 1, (
        "no probe ran, so this case cannot show a probe clobbering rowcount")
    assert cur.rowcount == 3, (
        f"a 3-row INSERT reported rowcount {cur.rowcount} — that is the "
        f"SELECT lastval() the wrapper ran after it, not the INSERT")
    conn.commit()
    assert len(conn.committed) == 4


def test_the_fixture_can_still_show_the_false_count(engine):
    """★ CONTROL. The assertion above is worthless if this fake cannot produce
    the 1 that the old wrapper read. Runs the old shape by hand: INSERT, then
    the probe, then read the RAW cursor's rowcount — as db_utils used to."""
    conn = RowStoreConn(columns={"publisher_url"})
    raw = conn.cursor()
    raw.execute("INSERT INTO news_articles (id) VALUES (%s) "
                "ON CONFLICT (id) DO NOTHING", ("a1",))
    conn.commit()
    raw.execute("INSERT INTO news_articles (id) VALUES (%s) "
                "ON CONFLICT (id) DO NOTHING", ("a1",))
    assert raw.rowcount == 0, "the conflicting insert itself must report 0"
    raw.execute("SELECT lastval()")
    assert raw.rowcount == 1, (
        "the fake cannot show a probe overwriting the insert's rowcount, so it "
        "cannot prove the fix")


def test_a_cold_session_still_keeps_the_row_it_inserted(engine, monkeypatch):
    """The probe used to cost the write, not just the count.

    With no sequence used yet, `SELECT lastval()` raises 55000 and aborts the
    transaction, so the INSERT that had just succeeded was discarded at commit.
    Measured against real Postgres: one new row in, zero rows committed."""
    conn = RowStoreConn(columns={"publisher_url"}, lastval_defined=False)
    store(monkeypatch, engine, conn)

    reported = engine.save_articles(articles(3))

    assert len(conn.committed) == 3, (
        f"{3 - len(conn.committed)} of 3 rows were lost to the lastval probe")
    assert reported == 3


def test_the_lastval_probe_cannot_abort_the_callers_transaction(engine):
    """Directly: a failing probe must leave the connection usable."""
    import db_utils

    conn = RowStoreConn(columns={"publisher_url"}, lastval_defined=False)
    cur = db_utils.PGCursorWrapper(conn.cursor())
    cur.execute("INSERT INTO news_articles (id) VALUES (%s) "
                "ON CONFLICT (id) DO NOTHING", ("a1",))
    assert conn.lastval_probes == 1, "the probe must have been attempted"
    assert not conn.aborted, (
        "a failed lastval() left the transaction aborted — it needs a SAVEPOINT")
    assert cur.lastrowid is None
    cur.execute("INSERT INTO news_articles (id) VALUES (%s) "
                "ON CONFLICT (id) DO NOTHING", ("a2",))
    conn.commit()
    assert conn.committed == ["a1", "a2"]


def test_the_writer_does_not_count_with_a_rowcount(engine):
    """AST floor: the count may not come from rowcount, and both statement
    literals must carry RETURNING."""
    saver = _source("save_articles")
    for node in ast.walk(saver):
        if isinstance(node, ast.Attribute) and node.attr == "rowcount":
            raise AssertionError(
                "save_articles reads rowcount again — through the pooled "
                "wrapper that number describes a SELECT lastval(), not the "
                "INSERT")
    inserts = [n.value for n in ast.walk(saver)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "INSERT INTO news_articles" in n.value]
    assert len(inserts) == 2, f"expected 2 INSERT literals, found {len(inserts)}"
    for sql in inserts:
        assert "RETURNING" in sql.upper(), (
            f"an INSERT with no RETURNING makes the wrapper probe lastval and "
            f"clobber rowcount: {' '.join(sql.split())[:80]}")
        assert "ON CONFLICT" in sql.upper(), "dedup must stay on the statement"


# ── the same two facts, against a real database ──────────────────────────────
# NOT coverage: this skips unless a DSN is handed to it, so CI proves nothing
# here and the guards above are what hold the line. It exists so the next person
# to touch the fake can re-measure the semantics the fake encodes, in one
# command, instead of trusting this file:
#
#   NEWS_TEST_PG_DSN="dbname=postgres" python3 -m pytest \
#       tests/test_news_ingest_writes_survive.py -k real_postgres -v
import os

real_pg = pytest.mark.skipif(
    not os.environ.get("NEWS_TEST_PG_DSN"),
    reason="set NEWS_TEST_PG_DSN to re-measure the wrapper against real Postgres")


@pytest.fixture()
def pg_table():
    """★ Hands out a `probe()` that OWNS the connection and always closes it.

    An earlier version of this fixture let each test open its own connection and
    close it on the last line. Running it against a deliberately broken wrapper
    hung for two minutes: the failing assertion skipped the close, the aborted
    transaction sat `idle in transaction` holding a lock on the table, and the
    teardown DROP waited on it forever. A test that cannot fail cleanly cannot
    be used to measure a fix — and a lock_timeout makes the teardown say so
    instead of hanging."""
    dsn = os.environ["NEWS_TEST_PG_DSN"]
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute("SET lock_timeout = '5s'")
    cur.execute("DROP TABLE IF EXISTS wrapper_probe; DROP TABLE IF EXISTS wrapper_probe_seq")
    cur.execute("CREATE TABLE wrapper_probe (id TEXT PRIMARY KEY)")
    cur.execute("CREATE TABLE wrapper_probe_seq (n SERIAL PRIMARY KEY)")
    opened = []

    def stored():
        c = admin.cursor()
        c.execute("SELECT COUNT(*) FROM wrapper_probe")
        n = c.fetchone()[0]
        c.close()
        return n

    def probe():
        import db_utils
        conn = psycopg2.connect(dsn)
        opened.append(conn)
        return conn, db_utils.PGCursorWrapper(conn.cursor())

    try:
        yield probe, stored
    finally:
        for conn in opened:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        try:
            cur.execute("DROP TABLE IF EXISTS wrapper_probe; "
                        "DROP TABLE IF EXISTS wrapper_probe_seq")
        finally:
            admin.close()


@real_pg
def test_real_postgres_conflicting_insert_reports_zero_rows(pg_table):
    """The measurement this whole fix rests on."""
    probe, stored = pg_table
    conn, cur = probe()
    # a sequence used earlier in the session is what makes lastval() succeed,
    # and a long-lived worker process always has one
    cur.execute("INSERT INTO wrapper_probe_seq DEFAULT VALUES")
    conn.commit()
    sql = "INSERT INTO wrapper_probe (id) VALUES (%s) ON CONFLICT (id) DO NOTHING"

    cur.execute(sql, ("x",))
    assert cur.rowcount == 1
    conn.commit()
    assert stored() == 1

    cur.execute(sql, ("x",))
    assert cur.rowcount == 0, (
        "a conflicting insert reports the wrapper's SELECT lastval(), not the "
        "INSERT — this is the 322-for-172 bug")
    conn.commit()
    assert stored() == 1


@real_pg
def test_real_postgres_cold_session_keeps_the_row(pg_table):
    """No sequence used yet: lastval() raises 55000 and used to abort the
    transaction, discarding the INSERT that had just succeeded."""
    probe, stored = pg_table
    conn, cur = probe()

    cur.execute("INSERT INTO wrapper_probe (id) VALUES (%s) "
                "ON CONFLICT (id) DO NOTHING", ("cold",))
    conn.commit()

    assert stored() == 1, "the lastval probe cost the write it was reporting on"
