"""The agent_requests poison-pill retry loop, turned into a red CI job (2026-08-17).

WHAT HAPPENED
=============
`agent_request_writer._flush_once` re-queued the whole batch on ANY exception.
psycopg2 raises

    ValueError: A string literal cannot contain NUL (0x00) characters.

while ADAPTING a row — client-side, inside execute_values, before a byte reaches
Postgres — so a request carrying a 0x00 (a scanner probing with %00 lands one in
`path` or `request_body`) produced a batch that could never be written and was
re-queued forever at the flush cadence. Observed in the Railway web deploy:

    07:50:50 agent_requests flush failed; 2704 rows re-queued: A string literal...
    07:51:54 agent_requests flush failed; 3039 rows re-queued: A string literal...

~1/sec, climbing toward the 5000-row cap, at which point every buffered audit
row is discarded. Both guards are pinned below: NULs are scrubbed before
adaptation, and any row that keeps failing is quarantined so the queue drains.

House rules honoured: nothing runs at module scope, and `main` is never
imported — `_flush_once` imports it lazily, so a stub in sys.modules is enough.
"""
import importlib
import sys


def _writer():
    """Fresh module state per test (the buffer and counters are globals)."""
    sys.modules.pop("agent_request_writer", None)
    m = importlib.import_module("agent_request_writer")
    m._started = True          # never start the real background flusher thread
    m._buf.clear()
    for k in ("enqueued", "inserted", "dropped", "poisoned"):
        m._stats[k] = 0
    return m


class _FakeConn:
    def __init__(self, fail=None):
        self.autocommit = None
        self.committed = False
        self._fail = fail

    def cursor(self):
        return self

    def execute(self, *a, **k):
        pass

    def close(self):
        pass

    def commit(self):
        if self._fail:
            raise self._fail
        self.committed = True

    def rollback(self):
        pass


def _install_main(monkeypatch, conn, boom=None):
    """Stub the lazy `from main import ...` inside _flush_once."""
    import types
    fake = types.ModuleType("main")

    def get_pg_connection(*a, **k):
        if boom is not None:
            raise boom
        return conn

    fake.get_pg_connection = get_pg_connection
    fake.return_pg_connection = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "main", fake)


def _capture_execute_values(monkeypatch, sink, raises=None):
    """Intercept the real execute_values and record the rows handed to psycopg2."""
    import psycopg2.extras

    def fake(cur, sql, argslist, page_size=100, **k):
        sink.extend(argslist)
        if raises is not None:
            raise raises

    monkeypatch.setattr(psycopg2.extras, "execute_values", fake)


def _adapt_all(rows):
    """The real psycopg2 adapter — the exact call that raised in production."""
    from psycopg2.extensions import adapt
    for row in rows:
        for v in row:
            if isinstance(v, str):
                adapt(v).getquoted()


def test_oracle_actually_bites_on_a_raw_nul_payload():
    """Guard the guard: the adapter below must really reject NUL, or the
    regression test that uses it as an oracle would pass vacuously."""
    import pytest
    with pytest.raises(ValueError) as ei:
        _adapt_all([("GET", "/v1/search\x00", 200)])
    assert "NUL" in str(ei.value)


def test_scrub_strips_nul_and_leaves_clean_rows_identical():
    m = _writer()
    row = ("plat", "curl/8", "1.2.3.4", "GET", "/v1/x\x00y", "q=\x001",
           "body\x00", 200, 12.5, "", "sess")
    out = m._scrub(row)
    assert out[4] == "/v1/xy"
    assert out[5] == "q=1"
    assert out[6] == "body"
    assert out[7] == 200 and out[8] == 12.5      # non-str fields untouched
    clean = ("plat", "curl/8", "1.2.3.4", "GET", "/v1/x", "", "", 200, 1.0, "", "s")
    assert m._scrub(clean) is clean              # no copy in the common case


def test_flush_hands_psycopg2_no_nul_bytes(monkeypatch):
    """THE REGRESSION. A row with 0x00 must flush, not poison the queue."""
    m = _writer()
    sink = []
    _install_main(monkeypatch, _FakeConn())
    _capture_execute_values(monkeypatch, sink)

    m.enqueue("plat", "ua\x00", "1.2.3.4", "GET", "/v1/search\x00", "q=x",
              "payload\x00here", 200, 5.0, "", "sess")
    m._flush_once()

    assert len(sink) == 1
    _adapt_all(sink)                             # would raise ValueError pre-fix
    assert "\x00" not in sink[0][6]
    assert sink[0][6] == "payloadhere"
    assert len(m._buf) == 0                      # drained, not re-queued
    assert m._stats["inserted"] == 1
    assert m._stats["poisoned"] == 0


def test_permanently_failing_rows_are_quarantined_so_the_queue_drains(monkeypatch):
    """Backstop for poison we have not thought of: bounded attempts, not forever."""
    m = _writer()
    m._MAX_TRIES = 3
    _install_main(monkeypatch, _FakeConn())
    _capture_execute_values(monkeypatch, [], raises=ValueError("permanently bad"))

    for _ in range(2):
        m.enqueue("plat", "ua", "1.2.3.4", "GET", "/v1/x", "", "", 200, 1.0, "", "s")

    for _ in range(m._MAX_TRIES):
        assert len(m._buf) > 0                   # still retrying
        m._flush_once()

    assert len(m._buf) == 0, "poison rows must not be re-queued forever"
    assert m._stats["poisoned"] == 2
    m._flush_once()                              # nothing left to loop on
    assert len(m._buf) == 0


def test_connection_failure_does_not_count_against_rows(monkeypatch):
    """A DB outage is not evidence a row is bad — the buffer must survive it."""
    m = _writer()
    m._MAX_TRIES = 3
    _install_main(monkeypatch, None, boom=Exception("Circuit breaker OPEN"))

    m.enqueue("plat", "ua", "1.2.3.4", "GET", "/v1/x", "", "", 200, 1.0, "", "s")
    for _ in range(m._MAX_TRIES * 3):
        m._flush_once()

    assert len(m._buf) == 1, "rows that never reached the server must be kept"
    assert m._stats["poisoned"] == 0
    assert m._buf[0][1] == 0                     # attempt counter never advanced
