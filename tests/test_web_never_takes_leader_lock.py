"""A DCHUB_ROLE=web replica must NEVER take the fleet leader lock (2026-08-22, step 7b).

WHAT WAS MEASURED (live prod)
=============================
GET https://dchub.cloud/api/v1/ops/leader returned holder application_name
'dchub-leader:web:5b320878:9' with this_replica_role 'web'. pid 9 is the web
service's gunicorn worker (worker containers boot pid 5), and the stamp helper
main._leader_lock_app_name() only exists as of #3047 — so a DCHUB_ROLE=web
process running CURRENT code opened a stamped direct-Neon connection and won the
singleton advisory lock 911714323. While a web replica holds the crown the whole
in-process worker fleet (71 crawler slots, publishers, brain, self-heal) idles.

WHY the existing rule did not stop it
=====================================
The "web must never lead" rule lived ONLY at the two call sites — the IS_LEADER
expression (`... and _ROLE_RUNS_BG and _acquire_leader_lock()`) and the keepalive
thread-start (`if ... and _ROLE_RUNS_BG:`). The three functions that actually
OPEN the lock connection — initial acquire, keepalive re-acquire, and the steal
— had no role guard of their own, so any path reaching them from a web process
(a new caller, a re-import, a follower-promotion re-acquire) bypassed the rule.

THE CONTRACT being guarded (this file)
======================================
- _acquire_leader_lock() in a DCHUB_ROLE=web process opens NO connection and
  issues NO pg_try_advisory_lock (fail-CLOSED: returns False — web never leads)
- the worker role STILL acquires (the guard does not weaken the worker path)
- _steal_stale_leader_lock() in a web process terminates NOTHING and never
  reaches util.leader_election.terminate_stale_holder
- _leader_keepalive_loop() refuses (returns) before its while-loop in a web
  process — web never re-acquires
- the refusal logs exactly one INFO line: leader-lock: skipped (DCHUB_ROLE=web)
- the lock id (911714323) and the application_name stamp are unchanged

NO NETWORK, NO DB; main.py is never imported (module scope opens pools). The
functions are AST-extracted from the source text and exec'd against fakes.
"""
import ast
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _exec_fn(rel, name, ns):
    """AST-extract a single function from a source file and exec it with ns."""
    src = (ROOT / rel).read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(ast.get_source_segment(src, fn), ns)
    return ns[name]


def _fn_src(rel, name):
    src = (ROOT / rel).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not in {rel}")


# ───────────────────────── fakes ─────────────────────────
class _Rec:
    """Records every connect() and every SQL statement seen by the stubs."""
    def __init__(self):
        self.connects = 0
        self.sql = []
        self.skips = 0


def _fake_cursor(rec, holder_row=None, my_pid=999):
    class Cur:
        def execute(self, q, p=None):
            rec.sql.append(" ".join(q.split()))
            self._last = q
        def fetchone(self):
            last = getattr(self, "_last", "")
            if "pg_backend_pid" in last:
                return (my_pid,)
            if "pg_locks" in last:
                return holder_row
            if "pg_terminate_backend" in last:
                return (True,)
            if "pg_try_advisory_lock" in last:
                return (True,)
            return None
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    return Cur()


def _fake_psycopg2(rec, holder_row=None, my_pid=999):
    mod = types.ModuleType("psycopg2")

    class Conn:
        autocommit = False
        def cursor(self):
            return _fake_cursor(rec, holder_row, my_pid)
        def close(self):
            pass

    def connect(*a, **k):
        rec.connects += 1
        return Conn()

    mod.connect = connect
    return mod


def _acquire_ns(rec, role):
    """Namespace to exec _acquire_leader_lock as if in a process of `role`."""
    import os as _os
    return {
        "_LEADER_LOCK_CONN": None,
        "_LEADER_LOCK_ID": 911714323,
        "_ROLE_RUNS_BG": (role != "web"),
        "_leader_lock_url": lambda: "postgres://direct/db",
        "_leader_lock_app_name": lambda: "dchub-leader:test:rep:9",
        "_leader_lock_skip_log": lambda: setattr(rec, "skips", rec.skips + 1),
        "logger": types.SimpleNamespace(info=lambda *a, **k: None),
        "print": lambda *a, **k: None,
        "os": _os,
    }


@pytest.fixture
def patch_psycopg2():
    saved = sys.modules.get("psycopg2")

    def _install(mod):
        sys.modules["psycopg2"] = mod

    yield _install
    if saved is not None:
        sys.modules["psycopg2"] = saved
    else:
        sys.modules.pop("psycopg2", None)


# ───────────────────────── initial acquire ─────────────────────────
class TestAcquireLeaderLock:
    def test_web_role_opens_no_connection_and_issues_no_advisory_lock(self, patch_psycopg2):
        """RED on main: with no in-function guard, _acquire_leader_lock connects
        and runs pg_try_advisory_lock regardless of role. This is the production
        defect (holder application_name 'dchub-leader:web:...:9')."""
        rec = _Rec()
        patch_psycopg2(_fake_psycopg2(rec))
        fn = _exec_fn("main.py", "_acquire_leader_lock", _acquire_ns(rec, "web"))
        got = fn()
        assert got is False, "a web process must fail CLOSED (never become leader)"
        assert rec.connects == 0, "a web process opened the leader-lock connection"
        assert not any("pg_try_advisory_lock" in q for q in rec.sql), \
            "a web process issued pg_try_advisory_lock(911714323)"
        assert rec.skips == 1, "the web refusal must log its one INFO line once"

    def test_worker_role_still_acquires(self, patch_psycopg2):
        """Control: the guard must NOT weaken the worker path."""
        rec = _Rec()
        patch_psycopg2(_fake_psycopg2(rec))
        fn = _exec_fn("main.py", "_acquire_leader_lock", _acquire_ns(rec, "worker"))
        got = fn()
        assert got is True, "the worker role must still win the lock"
        assert rec.connects == 1, "the worker role must open exactly one lock connection"
        assert any("pg_try_advisory_lock" in q for q in rec.sql), \
            "the worker role must still issue pg_try_advisory_lock"


# ───────────────────────── the steal path ─────────────────────────
class TestStealPath:
    def _steal_ns(self, rec, role, holder_row):
        return {
            "_LEADER_LOCK_ID": 911714323,
            "_ROLE_RUNS_BG": (role != "web"),
            "_leader_stale_seconds": lambda: 300.0,
            "_leader_lock_skip_log": lambda: setattr(rec, "skips", rec.skips + 1),
            "print": lambda *a, **k: None,
        }

    def test_web_role_terminates_nothing(self):
        """RED on main: _steal_stale_leader_lock has no role guard, so a web
        process reaches util.leader_election.terminate_stale_holder and issues
        pg_terminate_backend against the holder."""
        rec = _Rec()
        # a provably-dead holder (idle 900s >= 300s stale) that is NOT us
        import datetime as dt
        t0 = dt.datetime(2026, 8, 22, 1, 0, 0)
        holder = (77, t0, "idle", t0, "someone", 900.0, 3600.0)

        class Conn:
            def cursor(self):
                return _fake_cursor(rec, holder_row=holder, my_pid=999)
        fn = _exec_fn("main.py", "_steal_stale_leader_lock",
                      self._steal_ns(rec, "web", holder))
        out = fn(Conn())
        assert out is False, "a web process must never steal the lock"
        assert not any("pg_terminate_backend" in q for q in rec.sql), \
            "a web process issued pg_terminate_backend on the lock holder"
        assert rec.skips == 1

    def test_worker_role_can_still_steal_a_dead_holder(self):
        """Control: the worker still terminates a provably-dead holder."""
        rec = _Rec()
        import datetime as dt
        t0 = dt.datetime(2026, 8, 22, 1, 0, 0)
        holder = (77, t0, "idle", t0, "corpse", 900.0, 3600.0)

        class Conn:
            def cursor(self):
                return _fake_cursor(rec, holder_row=holder, my_pid=999)
        fn = _exec_fn("main.py", "_steal_stale_leader_lock",
                      self._steal_ns(rec, "worker", holder))
        out = fn(Conn())
        assert any("pg_terminate_backend" in q for q in rec.sql), \
            "the worker role must still be able to terminate a dead holder"
        assert out is True


# ─────────────────── keepalive loop (AST — execing the while-loop would hang on main) ───────────────────
class TestKeepaliveLoopRefusesOnWeb:
    def test_web_guard_returns_before_the_while_loop(self):
        """The keepalive re-acquire must refuse in a web process BEFORE entering
        its while-loop. Proven on the source (execing the unguarded main loop
        would spin forever). RED on main: `_ROLE_RUNS_BG` is not referenced in
        the function at all, so .index() raises."""
        src = _fn_src("main.py", "_leader_keepalive_loop")
        i_guard = src.index("_ROLE_RUNS_BG")
        i_loop = src.index("while True")
        i_try = src.index("pg_try_advisory_lock")
        assert i_guard < i_loop < i_try, \
            "the role guard must precede the while-loop and the re-acquire"
        # a `return` must sit between the guard and the loop (fail-closed exit)
        assert "return" in src[i_guard:i_loop], \
            "the web guard must return before the loop (never re-acquire)"
        assert "_leader_lock_skip_log" in src[i_guard:i_loop]


# ───────────────────────── the one INFO line ─────────────────────────
class TestSkipLogMessage:
    def test_logs_the_exact_line_once(self):
        logged = []
        ns = {
            "_LEADER_LOCK_SKIP_LOGGED": False,
            "logger": types.SimpleNamespace(info=lambda m, *a, **k: logged.append(m)),
            "print": lambda *a, **k: None,
        }
        fn = _exec_fn("main.py", "_leader_lock_skip_log", ns)
        fn(); fn(); fn()
        assert logged == ["leader-lock: skipped (DCHUB_ROLE=web)"], \
            "the refusal must log exactly one INFO line with the exact message"


# ───────────────────────── invariants preserved ─────────────────────────
class TestInvariantsPreserved:
    def test_lock_id_unchanged(self):
        src = (ROOT / "main.py").read_text()
        assert "_LEADER_LOCK_ID = 911714323" in src, "the lock id must not change"

    def test_application_name_stamp_preserved_on_both_lock_connections(self):
        for fn_name in ("_acquire_leader_lock", "_leader_keepalive_loop"):
            src = _fn_src("main.py", fn_name)
            assert "application_name=_leader_lock_app_name()" in src, \
                f"{fn_name} must keep the application_name stamp"
