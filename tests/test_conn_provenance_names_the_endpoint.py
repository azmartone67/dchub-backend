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
