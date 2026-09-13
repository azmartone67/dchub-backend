#!/usr/bin/env python3
"""The news facility scan took its write connection from the read pool.

crawler_scheduler._run_facility_discovery calls
news_facility_extractor.scan_news_sources() with no connection, and the scan
took one from main.get_read_db(). That function hands out the read-replica
pool whenever DATABASE_READ_URL / NEON_REPLICA_URL is set, and dchub-worker,
where the scheduler runs, logs `Read replica pool initialized` at boot. The
scan's INSERTs went through that connection. insert_discovered_facility
swallows every exception and returns None, the same None it returns for a
rejected or already-known candidate, so a refused write read as "not
inserted": facilities_inserted 0 and nothing else. The connection was never
given back either.

NOT MEASURED LIVE: that the replica refuses the write. The 2026-09-13 07:00 UTC
run never reached an INSERT (24 candidates: 18 rejected by the name gate, 6
already known by source_url), so its logs cannot say either way.

Fenced here, against the real modules:
  1. a read-only connection makes the scan REPORT the failure (a count, an
     error, success False), not 0 rows as success;
  2. a skipped candidate is not a failure (None means both);
  3. with no connection given, the scan writes through main.get_db, never
     main.get_read_db, and gives the connection back; a caller's stays open;
  4. the real main.get_db is the primary pool and the real main.get_read_db
     hands out the replica pool, so the fake main in (3) has the real shape;
  5. the scheduler logs the keys the scan returns.

Run:  python3 -m pytest tests/test_news_scan_writes_through_primary.py -v
"""
import ast
import functools
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import news_facility_extractor as nfe  # noqa: E402
from util.facility_name_sanity import facility_reject_reason  # noqa: E402

READ_ONLY = "cannot execute INSERT in a read-only transaction"


# ── a database that behaves like psycopg2, and no better ────────────────────

class _Cursor:
    def __init__(self, conn):
        self._conn, self._rows = conn, []

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        if self._conn.aborted:
            raise RuntimeError("current transaction is aborted")
        self._conn.statements.append(text)
        self._rows = []
        if text.startswith("INSERT INTO discovered_facilities"):
            if self._conn.read_only:
                self._conn.aborted = True
                raise RuntimeError(READ_ONLY)
            self._rows = [(9000 + len(self._conn.statements),)]
        elif "WHERE source_url" in text and self._conn.known_url:
            self._rows = [(1,)]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _Conn:
    """An error aborts the transaction until rollback; `read_only` refuses
    INSERT the way a standby does; `known_url` makes the source_url probe hit.
    No __enter__/__exit__, like the pooled wrappers."""

    def __init__(self, read_only=False, known_url=False):
        self.read_only, self.known_url = read_only, known_url
        self.statements, self.aborted = [], False
        self.commits = self.rollbacks = self.closes = 0

    def cursor(self, *_a, **_kw):
        return _Cursor(self)

    def commit(self):
        if self.aborted:  # PostgreSQL answers COMMIT with ROLLBACK here
            self.aborted = False
            return
        self.commits += 1

    def rollback(self):
        self.aborted = False
        self.rollbacks += 1

    def close(self):
        self.closes += 1

    def insert_attempts(self):
        return sum(s.startswith("INSERT INTO discovered_facilities")
                   for s in self.statements)


FEED = ("<rss><channel><title>Feed</title>"
        "<item><title>South Reach Networks opens a Fort Pierce data center</title></item>"
        "<item><title>South Reach Networks adds 40MW at its Fort Pierce campus</title></item>"
        "</channel></rss>")
JUNK = "Home | Data Center Frontier"  # rejected live, 2026-09-13 07:00:49 UTC


def _candidate(title, **over):
    fac = {"name": "South Reach Networks Fort Pierce",
           "provider": "South Reach Networks",
           "city": "Fort Pierce", "state": "FL", "country": "US",
           "latitude": None, "longitude": None, "power_mw": None, "sqft": None,
           "status": "Announced", "source": "news_extraction",
           "source_url": "https://feed.invalid/a/" + title[:40].replace(" ", "-"),
           "confidence_score": 0.6, "discovered_at": "2026-09-13", "notes": "",
           "investment_usd": None, "acreage": None}
    fac.update(over)
    return fac


@pytest.fixture
def one_feed(monkeypatch):
    """One source serving two articles. `make["fn"]` is what each article
    extracts to."""
    import requests

    class _Resp:
        status_code = 200
        text = FEED

    make = {"fn": _candidate}
    monkeypatch.setattr(nfe, "NEWS_SOURCES", [
        {"name": "T", "rss": "https://feed.invalid/rss", "category": "construction"}])
    monkeypatch.setattr(requests, "get", lambda *_a, **_kw: _Resp())
    monkeypatch.setattr(nfe, "extract_facility_from_article",
                        lambda title, _body, _url, _name: make["fn"](title))
    return make


def test_the_fixture_candidates_sit_on_both_sides_of_the_name_gate():
    """Floor. A candidate the gate rejects never reaches the connection, so if
    the good one were rejected every write assertion below would be vacuous."""
    assert not facility_reject_reason(_candidate("x"))
    assert facility_reject_reason(_candidate("x", name=JUNK, provider=None))


# ── 1 + 2. what the scan reports ────────────────────────────────────────────

def test_a_read_only_connection_reports_the_failed_inserts(one_feed):
    conn = _Conn(read_only=True)
    res = nfe.scan_news_sources(conn)

    assert res["facilities_found"] == 2
    assert conn.insert_attempts() == 2, conn.statements
    assert res["facilities_inserted"] == 0
    assert res["facilities_insert_failed"] == 2
    assert res["success"] is False
    assert any(READ_ONLY in e for e in res["errors"]), res["errors"]
    assert conn.rollbacks >= 2


def test_a_writable_connection_succeeds_and_the_callers_conn_stays_open(one_feed):
    """The control for the test above: same feed, same candidates, a
    connection that accepts the write."""
    conn = _Conn()
    res = nfe.scan_news_sources(conn)

    assert res["facilities_inserted"] == 2 and conn.commits == 2
    assert res["facilities_insert_failed"] == 0
    assert res["success"] is True
    assert conn.closes == 0


@pytest.mark.parametrize("skip", ["name-gate", "known-source-url"])
def test_a_skipped_candidate_is_not_a_failure(one_feed, skip):
    """Counting every None as a failure would turn each day's rejected
    headlines into a failed run. Both connections here refuse writes, so a
    skip path that fell through to the INSERT would show up as a failure."""
    if skip == "name-gate":
        one_feed["fn"] = lambda title: _candidate(title, name=JUNK, provider=None)
        conn = _Conn(read_only=True)
    else:
        conn = _Conn(read_only=True, known_url=True)

    res = nfe.scan_news_sources(conn)

    assert res["facilities_found"] == 2
    assert conn.insert_attempts() == 0, conn.statements
    assert res["facilities_inserted"] == 0
    assert res["facilities_insert_failed"] == 0
    assert res["success"] is True


# ── 3. the connection the scan takes for itself ─────────────────────────────

@pytest.fixture
def fake_main(monkeypatch):
    """main as the scan imports it: get_db the primary, get_read_db the
    replica. The two tests after this one bind that shape to main.py."""
    primary, replica, calls = _Conn(), _Conn(read_only=True), []
    mod = types.ModuleType("main")

    def get_db(*_a, **_kw):
        calls.append("get_db")
        return primary

    def get_read_db(*_a, **_kw):
        calls.append("get_read_db")
        return replica

    mod.get_db, mod.get_read_db = get_db, get_read_db
    monkeypatch.setitem(sys.modules, "main", mod)
    return primary, replica, calls


def test_with_no_connection_the_scan_writes_through_the_primary(one_feed, fake_main):
    primary, replica, calls = fake_main
    res = nfe.scan_news_sources()

    assert calls == ["get_db"]
    assert replica.statements == []
    assert primary.insert_attempts() == 2
    assert res["facilities_inserted"] == 2 and res["success"] is True
    assert primary.closes == 1


# ── 4. the fake main above is the real one's shape ──────────────────────────

@functools.lru_cache(maxsize=1)
def _main_defs():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    out = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in ("get_db", "get_read_db"):
            out.setdefault(node.name, []).append(node)
    return out


def _compile_real(name, namespace):
    defs = _main_defs().get(name, [])
    assert len(defs) == 1, f"main.py defines {name} {len(defs)} times at module level"
    exec(compile(ast.Module(body=defs, type_ignores=[]), "main.py", "exec"), namespace)
    return namespace[name]


def test_the_real_main_get_db_is_the_primary_pool():
    primary = object()
    get_db = _compile_real("get_db", {"get_pg_connection": lambda *a, **k: primary})
    assert get_db() is primary


def test_the_real_main_get_read_db_hands_out_the_replica_pool():
    replica = _Conn(read_only=True)

    class _Pool:
        handed = 0

        def getconn(self):
            self.handed += 1
            return replica

        def putconn(self, *_a, **_kw):
            pass

    pool = _Pool()
    get_read_db = _compile_real("get_read_db", {
        "_pg_pool_read": pool,
        "_track_checkout": lambda *a, **k: None,
        "_track_return": lambda *a, **k: None,
        "_original_get_read_db": lambda: pytest.fail("fell back to the primary"),
        "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
    })

    conn = get_read_db()
    assert pool.handed == 1
    assert conn.cursor()._conn is replica


# ── 5. the scheduler's step-1 line ──────────────────────────────────────────

class _Log:
    """Records calls. Not caplog: a module that runs logging.disable() at
    import anywhere in the suite empties caplog for every later test."""

    def __init__(self):
        self.records = []

    def __getattr__(self, level):
        def _rec(msg, *args, **_kw):
            self.records.append((level, str(msg) % args if args else str(msg)))
        return _rec


@pytest.mark.parametrize("result, level, text", [
    ({"articles_scanned": 9, "facilities_found": 3, "facilities_inserted": 1,
      "facilities_insert_failed": 2, "errors": ["x"], "success": False},
     "warning", "3 found, 1 inserted, 2 insert failures"),
    ({"articles_scanned": 9, "facilities_found": 4, "facilities_inserted": 4,
      "facilities_insert_failed": 0, "errors": [], "success": True},
     "info", "4 found, 4 inserted, 0 insert failures"),
])
def test_the_scheduler_logs_the_keys_the_scan_returns(monkeypatch, result, level, text):
    import crawler_scheduler as cs

    log = _Log()
    monkeypatch.setattr(cs, "logger", log)
    monkeypatch.setattr(nfe, "scan_news_sources", lambda *_a, **_kw: dict(result))
    was_set = cs._stop_event.is_set()
    cs._stop_event.set()  # return right after step 1
    try:
        cs._run_facility_discovery()
    finally:
        if not was_set:
            cs._stop_event.clear()

    step1 = [r for r in log.records if "[1/4] News extraction" in r[1]]
    assert len(step1) == 1, log.records
    assert step1[0][0] == level
    assert step1[0][1].endswith(text), step1[0][1]
