"""The fourth leader-lock path: close it at the single connection chokepoint
(2026-08-22, step 7c).

WHAT WAS MEASURED (live prod, on the code that already had #3055's per-function
guards, deploy a871b860)
========================================================================
GET https://dchub.cloud/api/v1/ops/leader and a read-only
`pg_locks ⋈ pg_stat_activity` probe both showed the singleton advisory lock
911714323 held by a DCHUB_ROLE=web process:

    application_name = 'dchub-leader:web:3037abc4:9'   (pid-suffix 9 = the web
    gunicorn worker; worker containers boot pid 5), state idle, last query
    'SELECT 1', query_start advancing every ~30 s — i.e. a live keepalive loop
    was pinging the lock connection from a web replica, while BOTH worker
    replicas logged FOLLOWER and the whole in-process fleet idled. Terminating
    the holder handed the crown to the OTHER web replica within ~15 s, never to a
    worker. So a web replica keeps WINNING lock 911714323 even after #3055.

WHY #3055's guards did not cover it
===================================
#3055 put `if not _ROLE_RUNS_BG: return` at the TOP of the three leader
FUNCTIONS (_acquire_leader_lock, _leader_keepalive_loop, _steal_stale_leader_lock)
but left the actual `psycopg2.connect(..., application_name=_leader_lock_app_name())`
INLINED in two of them. So the invariant "a web process must never OPEN a stamped
leader connection / reach pg_try_advisory_lock(911714323)" was enforced in
scattered per-function copies and was ABSENT from the connection primitive
itself. Any path that reaches a stamped connect other than through those exact
guarded entry points — a re-acquire, a future caller, a promotion path — bypasses
the rule. The publisher recheck seam
(content_publisher._wait_for_publish_leadership → _is_publish_leader →
main.is_current_leader) is NOT that path: is_current_leader() is a pure read of
main._LEADERSHIP and opens nothing (proven below). The durable fix is a single
chokepoint.

THE CONTRACT being guarded (this file)
======================================
- main._leader_lock_connect() — the ONE function that builds+opens the stamped
  connection — refuses fail-CLOSED for DCHUB_ROLE=web: returns None, opens NO
  connection, builds NO stamp, logs one INFO line. (Mutation target.)
- the worker role STILL opens exactly one stamped connection via the chokepoint
  (the guard does not weaken the worker path).
- BOTH _acquire_leader_lock and _leader_keepalive_loop delegate to
  _leader_lock_connect() and open NO psycopg2 connection of their own — so there
  is exactly one place a stamped leader connection can be opened.
- the application_name stamp (dchub-leader:service:replica:pid) is preserved,
  now on the chokepoint, and the lock id 911714323 is unchanged.
- the publisher recheck seam is a pure read: is_current_leader() and
  content_publisher._is_publish_leader() open no connection and issue no
  advisory lock under DCHUB_ROLE=web.
- register_content_publisher() does NOT start the auto-publisher threads on a
  DCHUB_ROLE=web replica (they park forever there — pure waste and the same
  role-leak class this change closes).

NO NETWORK, NO DB; main.py is never imported (module scope opens pools and starts
threads). The functions are AST-extracted from the source text and exec'd against
fakes — the same technique as tests/test_web_never_takes_leader_lock.py.
"""
import ast
import os
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ───────────────────────── AST helpers ─────────────────────────
def _fn_src(rel, name):
    src = (ROOT / rel).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in {rel} "
                         f"(the fix that introduces it has not been applied)")


def _fn_node(rel, name):
    src = (ROOT / rel).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {rel}")


def _exec_fn(rel, name, ns):
    exec(_fn_src(rel, name), ns)
    return ns[name]


# ───────────────────────── fakes ─────────────────────────
class _Rec:
    def __init__(self):
        self.connects = 0
        self.sql = []
        self.skips = 0
        self.appname_calls = 0


def _fake_cursor(rec):
    class Cur:
        def execute(self, q, p=None):
            rec.sql.append(" ".join(q.split()))
        def fetchone(self):
            return (True,)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    return Cur()


def _fake_psycopg2(rec):
    mod = types.ModuleType("psycopg2")

    class Conn:
        autocommit = False
        def cursor(self):
            return _fake_cursor(rec)
        def close(self):
            pass

    def connect(*a, **k):
        rec.connects += 1
        c = Conn()
        return c

    mod.connect = connect
    return mod


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


def _connect_ns(rec, role):
    """Namespace to exec _leader_lock_connect as if in a process of `role`."""
    def _appname():
        rec.appname_calls += 1
        return "dchub-leader:test:rep:9"
    return {
        "_ROLE_RUNS_BG": (role != "web"),
        "_leader_lock_url": lambda: "postgres://direct/db",
        "_leader_lock_app_name": _appname,
        "_leader_lock_skip_log": lambda: setattr(rec, "skips", rec.skips + 1),
    }


# ───────────────────────── the chokepoint ─────────────────────────
class TestLeaderLockConnectChokepoint:
    def test_web_role_opens_no_connection_no_stamp_no_lock(self, patch_psycopg2):
        """RED on main: _leader_lock_connect does not exist there. GREEN after:
        a web process opens NO connection and builds NO stamp — the crown can
        never be taken because pg_try_advisory_lock is never reached."""
        rec = _Rec()
        patch_psycopg2(_fake_psycopg2(rec))
        fn = _exec_fn("main.py", "_leader_lock_connect", _connect_ns(rec, "web"))
        got = fn()
        assert got is None, "the chokepoint must refuse (None) for a web process"
        assert rec.connects == 0, "a web process opened the leader-lock connection"
        assert rec.appname_calls == 0, "a web process built the dchub-leader stamp"
        assert rec.skips == 1, "the web refusal must log its one INFO line once"

    def test_worker_role_opens_exactly_one_stamped_connection(self, patch_psycopg2):
        """Control: the chokepoint must NOT weaken the worker path."""
        rec = _Rec()
        patch_psycopg2(_fake_psycopg2(rec))
        fn = _exec_fn("main.py", "_leader_lock_connect", _connect_ns(rec, "worker"))
        got = fn()
        assert got is not None, "the worker role must still open the lock connection"
        assert rec.connects == 1, "the worker must open exactly one lock connection"
        assert rec.appname_calls == 1, "the worker connection must carry the stamp"
        assert got.autocommit is True, "the lock connection must be autocommit"
        assert rec.skips == 0


# ─────────────── single chokepoint (structural: no other stamped connect) ───────────────
class TestSingleChokepoint:
    def _opens_own_connection(self, name):
        node = _fn_node("main.py", name)
        return [n for n in ast.walk(node) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "connect"]

    def _calls(self, name, callee):
        node = _fn_node("main.py", name)
        return [n for n in ast.walk(node) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == callee]

    def test_acquire_delegates_to_the_chokepoint(self):
        """RED on main: _acquire_leader_lock opens its own psycopg2.connect."""
        assert self._calls("_acquire_leader_lock", "_leader_lock_connect"), \
            "_acquire_leader_lock must open the lock connection via _leader_lock_connect()"
        assert not self._opens_own_connection("_acquire_leader_lock"), \
            "_acquire_leader_lock must NOT open its own connection (single chokepoint)"

    def test_keepalive_delegates_to_the_chokepoint(self):
        """RED on main: _leader_keepalive_loop opens its own psycopg2.connect."""
        assert self._calls("_leader_keepalive_loop", "_leader_lock_connect"), \
            "the keepalive re-acquire must go through _leader_lock_connect()"
        assert not self._opens_own_connection("_leader_keepalive_loop"), \
            "the keepalive must NOT open its own connection (single chokepoint)"

    def test_the_stamp_lives_on_the_chokepoint(self):
        """The application_name stamp is preserved — now on the one primitive."""
        src = _fn_src("main.py", "_leader_lock_connect")
        assert "application_name=_leader_lock_app_name()" in src, \
            "the chokepoint must carry the dchub-leader stamp"

    def test_only_the_chokepoint_opens_a_stamped_connection(self):
        """Across the whole leader block, exactly ONE function calls
        psycopg2.connect with the application_name stamp."""
        src = (ROOT / "main.py").read_text()
        tree = ast.parse(src)
        offenders = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for c in ast.walk(fn):
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                        and c.func.attr == "connect"):
                    kw = {k.arg for k in c.keywords}
                    if "application_name" in kw:
                        offenders.append(fn.name)
        assert offenders == ["_leader_lock_connect"], \
            f"exactly one function may open a stamped leader connection; saw {offenders}"


# ─────────────── the fail-closed guard is load-bearing (behaviour) ───────────────
class TestChokepointGuardIsLoadBearing:
    def test_guard_precedes_the_connect(self):
        """The role refusal must sit BEFORE the connect in _leader_lock_connect,
        so no reordering leaves a window where web connects first."""
        src = _fn_src("main.py", "_leader_lock_connect")
        # Anchor on the guard STATEMENT (the docstring also mentions
        # "psycopg2.connect(...)" and "returns None", so index from the guard).
        i_guard = src.index("if not _ROLE_RUNS_BG")
        i_return_none = src.index("return None", i_guard)
        i_connect = src.index(".connect(", i_guard)
        assert i_guard < i_return_none < i_connect, \
            "the web guard must return None before the connect"
        assert "_leader_lock_skip_log" in src[i_guard:i_connect]


# ─────────────── the publisher recheck seam never acquires (finding) ───────────────
class TestPublisherRecheckSeamIsAPureRead:
    def test_is_current_leader_opens_no_connection(self, patch_psycopg2):
        """The task's literal suspect. is_current_leader() is a pure read of
        main._LEADERSHIP — it opens no connection and issues no advisory lock,
        on web or anywhere. GREEN on main and after (documents the finding)."""
        rec = _Rec()
        patch_psycopg2(_fake_psycopg2(rec))
        ns = {"_LEADERSHIP": {"is_leader": False}, "IS_LEADER": False}
        fn = _exec_fn("main.py", "is_current_leader", ns)
        assert fn() is False
        assert rec.connects == 0, "is_current_leader must never open a connection"
        assert rec.sql == [], "is_current_leader must never issue SQL"

    def test_publish_leader_seam_reads_and_opens_nothing(self, patch_psycopg2):
        """content_publisher._is_publish_leader → from main import is_current_leader.
        With a fake `main` whose is_current_leader is a recording pure-read, the
        seam returns that value and opens nothing."""
        rec = _Rec()
        patch_psycopg2(_fake_psycopg2(rec))
        cp = pytest.importorskip("content_publisher")
        saved = sys.modules.get("main")
        calls = {"n": 0}

        def _icl():
            calls["n"] += 1
            return False
        sys.modules["main"] = types.SimpleNamespace(is_current_leader=_icl)
        try:
            assert cp._is_publish_leader() is False
        finally:
            if saved is not None:
                sys.modules["main"] = saved
            else:
                sys.modules.pop("main", None)
        assert calls["n"] == 1, "the seam must consult is_current_leader"
        assert rec.connects == 0, "the publisher seam must open no connection"


# ─────────────── publishers must not start on a web replica (bug b) ───────────────
class TestPublishersDoNotStartOnWeb:
    def _run_register(self, role):
        cp = pytest.importorskip("content_publisher")
        started = []
        fakes = {
            "init_content_tables": lambda: None,
            "start_auto_publisher": lambda: started.append("linkedin"),
            "start_twitter_publisher": lambda: started.append("twitter"),
            "start_bluesky_publisher": lambda: started.append("bluesky"),
            "content_bp": object(),
            "logger": types.SimpleNamespace(info=lambda *a, **k: None,
                                            warning=lambda *a, **k: None),
            "os": types.SimpleNamespace(environ={"DCHUB_ROLE": role}),
        }
        app = types.SimpleNamespace(register_blueprint=lambda bp: None)
        fn = _exec_fn("content_publisher.py", "register_content_publisher", fakes)
        fn(app)
        return started

    def test_web_role_starts_no_publisher_threads(self):
        """RED on main: register_content_publisher starts all three publishers
        unconditionally, so a web replica runs them (they park forever)."""
        assert self._run_register("web") == [], \
            "a DCHUB_ROLE=web replica must not start the auto-publisher threads"

    def test_worker_role_starts_all_three_publishers(self):
        """Control: the worker still starts LinkedIn + X + Bluesky."""
        assert sorted(self._run_register("worker")) == ["bluesky", "linkedin", "twitter"]


# ───────────────────────── invariants preserved ─────────────────────────
class TestInvariantsPreserved:
    def test_lock_id_unchanged(self):
        src = (ROOT / "main.py").read_text()
        assert "_LEADER_LOCK_ID = 911714323" in src, "the lock id must not change"

    def test_worker_no_db_still_fails_open(self, patch_psycopg2):
        """A worker with no DB configured must still fail OPEN in
        _acquire_leader_lock (unchanged) — the chokepoint's None for 'no DB' must
        not turn the worker fail-open into a fail-closed."""
        rec = _Rec()
        patch_psycopg2(_fake_psycopg2(rec))
        ns = {
            "_LEADER_LOCK_CONN": None,
            "_LEADER_LOCK_ID": 911714323,
            "_ROLE_RUNS_BG": True,  # worker
            "_leader_lock_url": lambda: "",  # no DB
            "_leader_lock_connect": lambda: None,
            "_leader_lock_app_name": lambda: "x",
            "_leader_lock_skip_log": lambda: None,
            "print": lambda *a, **k: None,
        }
        fn = _exec_fn("main.py", "_acquire_leader_lock", ns)
        assert fn() is True, "worker with no DB must fail OPEN (return True)"
        assert rec.connects == 0
