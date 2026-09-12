"""A failed write must name the ENDPOINT it was attempted on.

WHY. dchub-backend holds pools against TWO Neon computes — a writable primary
and a read-only replica. On 2026-09-09 four writers were failing SQLSTATE 25006
continuously while `ai_requests` took 2,256 rows in 30 minutes on the same
database, so the failures were a SUBSET of attempts and no log said which
endpoint the failing ones were on. Every theory fit the evidence equally.

★ THE TRAP THIS FILE EXISTS FOR. psycopg2 raises
  InterfaceError("connection already closed") from get_dsn_parameters(), and
  every writer instrumented here closes in a `finally` that runs BEFORE its
  except block. Reading the host off the closed connection returns 'unknown'
  100% of the time — a probe that always answers the same thing, whatever is
  true, which is worse than no probe because it looks like an answer.
"""
import logging
import os

import pytest

from routes._conn_provenance import classify, conn_host, note_failed_write

PRIMARY = "ep-polished-breeze-af22mhng-pooler.c-2.us-west-2.aws.neon.tech"
REPLICA = "ep-dark-glade-af2837o8-pooler.c-2.us-west-2.aws.neon.tech"


class FakeConn:
    """psycopg2's observed behaviour: the host is unreadable once closed."""

    def __init__(self, host):
        self._host = host
        self.closed = False

    def get_dsn_parameters(self):
        if self.closed:
            raise RuntimeError("connection already closed")
        return {"host": self._host, "dbname": "neondb"}

    def close(self):
        self.closed = True


class OuterWrapper:
    """db_utils.PGConnectionWrapper — holds another WRAPPER, not a connection."""

    def __init__(self, inner):
        self._conn = inner


class InnerWrapper:
    """main._PoolConnWrapper — the raw connection hides behind ._raw."""

    __slots__ = ("_raw",)

    def __init__(self, raw):
        self._raw = raw


def _load_real_surface_brain():
    """Load routes/surface_brain.py FROM DISK, bypassing sys.modules.

    ★ `import routes.surface_brain` is NOT safe in a full-suite run.
      tests/test_market_brief_guard.py and tests/test_market_rotation_
      reachability.py each do

          sys.modules.setdefault("routes.surface_brain",
                                 types.ModuleType("routes.surface_brain"))

      to keep their own imports cheap. Whichever is collected first installs a
      STUB carrying only `auto_log` for the rest of the process. Running this
      file alone gets the real module and passes; `pytest tests/` gets the stub
      and this test would either error on the missing attribute or — far worse
      — silently exercise a stub and report green. Load by path so the answer
      does not depend on collection order.
    """
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "routes", "surface_brain.py")
    spec = importlib.util.spec_from_file_location("_real_surface_brain_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Non-vacuity: if this ever hands back something stub-shaped, fail loudly
    # rather than skip — a skipped guard reads the same as a passing one.
    assert hasattr(mod, "_conn"), "loaded a stub, not routes/surface_brain.py"
    return mod

# ── the host must survive two layers of wrapper ──────────────────────────────
def test_walks_nested_wrappers_to_the_real_connection():
    nested = OuterWrapper(InnerWrapper(FakeConn(REPLICA)))
    assert conn_host(nested) == REPLICA, \
        "stopped at a wrapper — a single-level peek reports 'unknown'"


def test_bare_connection_still_works():
    assert conn_host(FakeConn(PRIMARY)) == PRIMARY


# ── THE regression: a closed connection cannot be asked ──────────────────────
def test_closed_connection_is_unknown_not_a_wrong_answer():
    c = FakeConn(PRIMARY)
    c.close()
    assert conn_host(c) == "unknown", \
        "a closed conn must not be reported as some host it might not be"


def test_a_captured_host_string_passes_through():
    """The call sites capture the host WHILE OPEN and hand in the string.
    This is the only path that survives a close-in-finally."""
    assert conn_host(PRIMARY) == PRIMARY


# ── the verdict has to distinguish the two computes ─────────────────────────
def test_classify_separates_primary_from_replica(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"postgresql://{PRIMARY}/neondb")
    monkeypatch.setenv("NEON_REPLICA_URL", f"postgresql://{REPLICA}/neondb")
    assert classify(PRIMARY) == "primary"
    assert classify(REPLICA) == "replica"
    assert classify("somewhere.else") == "other:somewhere.else"
    assert classify("unknown") == "unknown"


def test_classify_does_not_invent_a_verdict_without_env(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_REPLICA_URL", raising=False)
    monkeypatch.delenv("DATABASE_READ_URL", raising=False)
    assert classify(PRIMARY) == "other:" + PRIMARY


# ── it is instrumentation: it may never break its caller ────────────────────
def test_note_failed_write_never_raises():
    class Exploding:
        def get_dsn_parameters(self):
            raise ValueError("boom")

    note_failed_write(Exploding(), "t", "where", ValueError("x"))
    note_failed_write(None, "t", "where", None)


def test_the_log_line_carries_host_and_verdict(monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", f"postgresql://{PRIMARY}/neondb")
    monkeypatch.setenv("NEON_REPLICA_URL", f"postgresql://{REPLICA}/neondb")
    with caplog.at_level(logging.WARNING, logger="conn_provenance"):
        note_failed_write(REPLICA, "surface_telemetry", "probe", RuntimeError("ro"))
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "endpoint=replica" in line
    assert REPLICA in line
    # control: the assertion above must be able to fail
    assert "endpoint=primary" not in line


# ── the CALL SITE, end to end ────────────────────────────────────────────────
# Everything above tests the helper. This tests the thing that actually broke:
# auto_log closes its connection in a `finally` that runs before the except, so
# passing the CONNECTION instead of the captured host logs 'unknown' forever.
def test_auto_log_reports_the_real_endpoint_not_unknown(monkeypatch, caplog):
    flask = pytest.importorskip("flask")
    surface_brain = _load_real_surface_brain()

    conn = FakeConn(REPLICA)

    class Cur:
        description = None  # a real cursor always has it (None before a statement)
        rowcount = -1  # psycopg2: -1 = no statement / not determinable
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *a, **kw):
            raise RuntimeError("cannot execute INSERT in a read-only transaction")

    conn.cursor = lambda *a, **kw: Cur()

    monkeypatch.setattr(surface_brain, "_conn", lambda: conn)
    monkeypatch.setattr(surface_brain, "_rate_limited", lambda _h: False)
    monkeypatch.setenv("DATABASE_URL", f"postgresql://{PRIMARY}/neondb")
    monkeypatch.setenv("NEON_REPLICA_URL", f"postgresql://{REPLICA}/neondb")

    app = flask.Flask(__name__)
    with app.test_request_context("/", headers={"User-Agent": "probe"}):
        with caplog.at_level(logging.WARNING, logger="conn_provenance"):
            surface_brain.auto_log("some-surface", "view")

    line = "\n".join(r.getMessage() for r in caplog.records
                     if "FAILED WRITE" in r.getMessage())
    assert line, "auto_log swallowed the failure without reporting provenance"
    assert conn.closed, "fixture is unrealistic — the real auto_log closes in finally"
    assert "host=unknown" not in line, \
        "reported 'unknown': the host was read AFTER close instead of captured while open"
    assert REPLICA in line and "endpoint=replica" in line


# ── round 2: what the SERVER says about the failing connection ──────────────
# Round 1 returned endpoint=primary for every failure, which exonerated the read
# replica and left "a session on a writable primary refusing writes". These
# facts only exist server-side, so they have to be asked for, not inferred.
from routes._conn_provenance import session_state  # noqa: E402


class _SessionCur:
    """★ MIRRORS db_utils.PGCursorWrapper — execute/fetchone/close and NO
    __enter__/__exit__.

    The first version of this fixture implemented the context-manager protocol.
    That made it MORE capable than the real object, so `with conn.cursor()`
    passed here and raised AttributeError in production — swallowed by the broad
    except, producing zero SESSION lines against a live connection while every
    test stayed green. A fixture may never be able to do something the real
    thing cannot.
    """
    description = None  # a real cursor always has it (None before a statement)
    rowcount = -1  # psycopg2: -1 = no statement / not determinable

    def __init__(self, row, log):
        self._row = row
        self._log = log
        self.closed = False

    def execute(self, sql, *a):
        self._log.append(sql)

    def fetchone(self):
        return self._row

    def close(self):
        self.closed = True


class SessionConn:
    """A connection whose transaction is ABORTED, as it is after a failed write."""

    def __init__(self, row):
        self._row = row
        self.rolled_back = False
        self.sql = []
        self.last_cursor = None

    def rollback(self):
        self.rolled_back = True

    def cursor(self):
        self.last_cursor = _SessionCur(self._row, self.sql)
        return self.last_cursor

    def get_dsn_parameters(self):
        return {"host": REPLICA, "user": "neondb_owner", "dbname": "neondb",
                "options": "-c default_transaction_read_only=on"}


_ROW = ("on", "on", "neondb_owner", "neondb", 4242, False)


def test_session_state_reports_what_the_server_says():
    st = session_state(SessionConn(_ROW))
    assert st is not None
    assert st["tx_read_only"] == "on"
    assert st["default_read_only"] == "on"
    assert st["user"] == "neondb_owner" and st["pid"] == 4242
    assert st["dsn_options"] == "-c default_transaction_read_only=on"


def test_session_state_rolls_back_first():
    """Without this every query returns 25P02 and the probe learns nothing."""
    c = SessionConn(_ROW)
    session_state(c)
    assert c.rolled_back, "did not roll back — an aborted tx answers 25P02, not facts"


def test_session_state_finds_the_connection_through_wrappers():
    assert session_state(OuterWrapper(InnerWrapper(SessionConn(_ROW)))) is not None


def test_session_state_is_none_for_a_captured_host_string():
    """surface_brain hands in a string; there is no live session to ask."""
    assert session_state(PRIMARY) is None
    assert session_state(None) is None


def test_session_state_never_raises():
    class Exploding:
        def cursor(self):
            raise ValueError("boom")

        def rollback(self):
            raise ValueError("boom")

    assert session_state(Exploding()) is None


def test_note_failed_write_emits_the_session_line(monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", f"postgresql://{PRIMARY}/neondb")
    with caplog.at_level(logging.WARNING, logger="conn_provenance"):
        note_failed_write(SessionConn(_ROW), "agent_requests", "probe", RuntimeError("ro"))
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "conn-provenance: SESSION" in line
    assert "tx_read_only=on" in line and "pid=4242" in line
    assert "tx_read_only=off" not in line   # control: the assertion can fail


def test_session_state_does_not_need_a_context_manager_cursor():
    """THE #4287 regression, stated directly: PGCursorWrapper has no __enter__.

    Asserted structurally as well as behaviourally — if a future fixture grows
    __enter__ this test would start passing for the wrong reason.
    """
    assert not hasattr(_SessionCur(_ROW, []), "__enter__"), \
        "fixture drifted from PGCursorWrapper — it must NOT be a context manager"
    from db_utils import PGCursorWrapper
    assert not hasattr(PGCursorWrapper, "__enter__"), \
        "PGCursorWrapper gained __enter__; re-check what session_state relies on"
    st = session_state(SessionConn(_ROW))
    assert st is not None, "session_state needed `with cursor()` — it must not"
    assert st["pid"] == 4242


def test_session_state_closes_the_cursor_it_opened():
    c = SessionConn(_ROW)
    session_state(c)
    assert c.last_cursor is not None and c.last_cursor.closed, \
        "leaked a cursor on a pooled connection"
