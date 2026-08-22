"""The leader-lock holder was never a web-ROLE process — it was a Railway SERVICE
named `web` (2026-08-22, step 7d).

WHAT WAS MEASURED (live prod, on code that already had #3055 AND #3059)
======================================================================
- pg_locks ⋈ pg_stat_activity: advisory lock 911714323 held by
  application_name 'dchub-leader:web:4554c392:9', backend_start 08:55:25.019Z,
  'SELECT 1' every 30.07 s (= main._leader_keepalive_loop's time.sleep(30)).
- Both production web replicas REFUSED at import ("leader-lock: skipped
  (DCHUB_ROLE=web)" at 08:52:33 / 08:52:39), started no keepalive thread, and
  enumerated as replicas c7d22399 / dad86725 via
  /api/v1/admin/agent-request-writer/stats — the holder (4554c392) was neither.
- Railway workspace scan: project `giving-art` has a service literally named
  `web`: same repo, auto-deploys every push to main (same commits, same
  minute), 2 replicas, `bash start_web.sh` → "start_web: DCHUB_ROLE=unset",
  gunicorn worker pid 9, DATABASE_URL fingerprint identical to production.
  Its own log: '👑 [leader-election] re-acquired lock → promoted to LEADER' at
  08:55:25.134Z. Its /api/v1/ops/leader says this_replica_role 'unset'; its
  thread list carries leader-keepalive, linkedin-auto-publisher,
  bluesky-auto-publisher, brain-l21-autopilot.
- pg_terminate_backend once → lock free 21 s → a REAL worker took it
  ('dchub-leader:dchub-worker:f89b7670:5'); the other stray replica then
  logged "lock held elsewhere → stepping DOWN".

WHY #3055/#3059 could not have fixed it: the stamp was
`RAILWAY_SERVICE_NAME or DCHUB_ROLE`, so 'web' was read as the ROLE. The
stray's role is 'all' (unset) — bg-machinery ON — and every guard keyed on
"role == web" was simply not about this process.

THE CONTRACT being guarded (this file)
======================================
- main._leader_lock_eligible(): decided LIVE from os.environ. False for a web
  role (import-time flag OR live env); False for a Railway process whose role
  is not `worker` (the stray-fleet shape: RAILWAY_ENVIRONMENT set, DCHUB_ROLE
  unset), naming service + project in the reason; True for DCHUB_ROLE=worker on
  Railway; True for 'all' OFF Railway (local/CI fail-open unchanged);
  DCHUB_LEADER_ALLOW_ROLE_ALL=1 is the deliberate opt-in.
- _leader_lock_connect() refuses (None, no connect, no stamp) on that verdict,
  re-read at the moment of connecting — not only from _ROLE_RUNS_BG.
- _acquire_leader_lock() fails CLOSED for an ineligible process even with no DB
  (the legacy no-DB fail-OPEN must not crown a stray).
- _leader_keepalive_loop() re-checks eligibility before its while-loop; the
  thread-start gate is keyed on _LEADER_ELIGIBLE.
- the stamp names every axis so the next holder is unambiguous:
  dchub-leader:<service>[<role>]:<replica8>:<pid>@<project>, with
  RAILWAY_SERVICE_ID[:8] then the hostname as fallbacks for <service>; ≤ 63.
- the refusal log line carries the reason; the web-role line is unchanged.

NO NETWORK, NO DB; main.py is never imported (module scope opens pools and
starts threads). Functions are AST-extracted and exec'd against fakes, the
technique of tests/test_web_never_takes_leader_lock.py. Tests marked RED-on-main
fail on origin/main by the named assertion, not by an import error.
"""
import ast
import os
import pathlib
import socket
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ENV_KEYS = ("DCHUB_ROLE", "RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME",
            "RAILWAY_SERVICE_ID", "RAILWAY_PROJECT_NAME", "RAILWAY_PROJECT_ID",
            "RAILWAY_REPLICA_ID", "DCHUB_LEADER_ALLOW_ROLE_ALL")


# ───────────────────────── AST helpers ─────────────────────────
_PARSED = {}


def _main_src_and_tree():
    """main.py is ~45K lines; parse it once per (mtime_ns, size) — a mutation
    pass rewrites the file and must see the new bytes, so key on the stat."""
    st = MAIN.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _PARSED.get("main")
    if hit and hit[0] == key:
        return hit[1], hit[2]
    src = MAIN.read_text()
    tree = ast.parse(src)
    _PARSED["main"] = (key, src, tree)
    return src, tree


def _fn_src(name):
    src, tree = _main_src_and_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    return None


def _exec_fn(name, ns, required=True):
    s = _fn_src(name)
    if s is None:
        if required:
            raise AssertionError(f"{name} not found in main.py (the fix that introduces it is not applied)")
        return None
    exec(s, ns)
    return ns[name]


# ───────────────────────── fakes ─────────────────────────
class _Rec:
    def __init__(self):
        self.connects = 0
        self.sql = []
        self.skips = []        # the reason passed to _leader_lock_skip_log
        self.appname_calls = 0


def _fake_psycopg2(rec):
    mod = types.ModuleType("psycopg2")

    class Cur:
        def execute(self, q, p=None):
            rec.sql.append(" ".join(q.split()))
        def fetchone(self):
            return (True,)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class Conn:
        autocommit = False
        def cursor(self):
            return Cur()
        def close(self):
            pass

    def connect(*a, **k):
        rec.connects += 1
        return Conn()

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


@pytest.fixture
def env(monkeypatch):
    """A clean slate for every variable the predicate and the stamp read."""
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _chokepoint_ns(rec, runs_bg=True):
    """Namespace for the REAL _leader_lock_connect with the REAL os module, so
    the live env read is exercised. `_leader_lock_eligible` is exec'd in only
    when it exists — on origin/main it does not, and the chokepoint then runs
    with nothing but the import-time flag (which is the defect)."""
    def _appname():
        rec.appname_calls += 1
        return "dchub-leader:test[x]:rep:9"
    ns = {
        "os": os,
        "_ROLE_RUNS_BG": runs_bg,
        "_leader_lock_url": lambda: "postgres://direct/db",
        "_leader_lock_app_name": _appname,
        "_leader_lock_skip_log": lambda *a: rec.skips.append(a[0] if a else "DCHUB_ROLE=web"),
    }
    _exec_fn("_leader_lock_eligible", ns, required=False)
    return ns


# ───────────────────────── the chokepoint, LIVE ─────────────────────────
class TestChokepointDecidesLive:
    def test_railway_service_with_no_role_is_refused_the_giving_art_web_shape(self, env, patch_psycopg2):
        """RED on main: the stray's exact shape — RAILWAY_ENVIRONMENT set, a
        service called `web`, DCHUB_ROLE unset (so _ROLE_RUNS_BG is True) —
        opens the stamped connection on main and wins the crown."""
        env.setenv("RAILWAY_ENVIRONMENT", "production")
        env.setenv("RAILWAY_SERVICE_NAME", "web")
        env.setenv("RAILWAY_PROJECT_NAME", "giving-art")
        rec = _Rec()
        patch_psycopg2(_fake_psycopg2(rec))
        fn = _exec_fn("_leader_lock_connect", _chokepoint_ns(rec))
        got = fn()
        assert got is None, \
            "a Railway process with NO DCHUB_ROLE opened the leader-lock connection (the giving-art/web stray)"
        assert rec.connects == 0, "the stray opened a stamped leader connection"
        assert rec.appname_calls == 0, "the stray built the dchub-leader stamp"
        assert len(rec.skips) == 1, "the refusal must log exactly once"
        why = rec.skips[0]
        assert "DCHUB_ROLE=all" in why and "service=web" in why and "project=giving-art" in why, \
            f"the refusal must name role, service and project; got {why!r}"

    def test_live_web_role_is_refused_even_when_the_import_time_flag_says_bg(self, env, patch_psycopg2):
        """RED on main: the chokepoint trusted only _ROLE_RUNS_BG. The live env
        must be re-read at the moment of connecting."""
        env.setenv("DCHUB_ROLE", "web")
        rec = _Rec()
        patch_psycopg2(_fake_psycopg2(rec))
        fn = _exec_fn("_leader_lock_connect", _chokepoint_ns(rec, runs_bg=True))
        assert fn() is None, "a process whose LIVE DCHUB_ROLE is web opened the leader-lock connection"
        assert rec.connects == 0 and rec.appname_calls == 0
        assert rec.skips == ["DCHUB_ROLE=web"]

    def test_worker_on_railway_still_opens_exactly_one_stamped_connection(self, env, patch_psycopg2):
        """Control (GREEN on main and after): the real fleet's shape is untouched."""
        env.setenv("RAILWAY_ENVIRONMENT", "production")
        env.setenv("RAILWAY_SERVICE_NAME", "dchub-worker")
        env.setenv("DCHUB_ROLE", "worker")
        rec = _Rec()
        patch_psycopg2(_fake_psycopg2(rec))
        fn = _exec_fn("_leader_lock_connect", _chokepoint_ns(rec))
        got = fn()
        assert got is not None, "the worker role must still open the lock connection"
        assert rec.connects == 1 and rec.appname_calls == 1
        assert got.autocommit is True
        assert rec.skips == []

    def test_role_all_off_railway_keeps_the_fail_open_semantics(self, env, patch_psycopg2):
        """Control (GREEN on main and after): local dev / CI with no role and no
        RAILWAY_ENVIRONMENT still connects — the rule is about Railway strays."""
        rec = _Rec()
        patch_psycopg2(_fake_psycopg2(rec))
        fn = _exec_fn("_leader_lock_connect", _chokepoint_ns(rec))
        assert fn() is not None
        assert rec.connects == 1 and rec.skips == []

    def test_explicit_opt_in_lets_a_deliberate_single_service_railway_deploy_lead(self, env, patch_psycopg2):
        """Control: DCHUB_LEADER_ALLOW_ROLE_ALL=1 is the documented escape hatch."""
        env.setenv("RAILWAY_ENVIRONMENT", "production")
        env.setenv("DCHUB_LEADER_ALLOW_ROLE_ALL", "1")
        rec = _Rec()
        patch_psycopg2(_fake_psycopg2(rec))
        fn = _exec_fn("_leader_lock_connect", _chokepoint_ns(rec))
        assert fn() is not None
        assert rec.connects == 1 and rec.skips == []


# ───────────────────────── the predicate itself ─────────────────────────
class TestEligibilityPredicate:
    def _elig(self, runs_bg=True):
        """Exec the REAL predicate with the real os module and CALL it."""
        return _exec_fn("_leader_lock_eligible", {"os": os, "_ROLE_RUNS_BG": runs_bg})()

    def test_web_role_live(self, env):
        env.setenv("DCHUB_ROLE", "web")
        assert self._elig() == (False, "DCHUB_ROLE=web")

    def test_import_time_web_flag_wins_over_a_later_env_change(self, env):
        env.setenv("DCHUB_ROLE", "worker")
        assert self._elig(runs_bg=False)[0] is False

    def test_railway_with_no_role_is_a_stray_fleet(self, env):
        env.setenv("RAILWAY_ENVIRONMENT", "production")
        env.setenv("RAILWAY_SERVICE_NAME", "web")
        env.setenv("RAILWAY_PROJECT_NAME", "giving-art")
        ok, why = self._elig()
        assert ok is False
        assert "only DCHUB_ROLE=worker may lead" in why
        assert "service=web" in why and "project=giving-art" in why

    def test_railway_with_explicit_all_is_still_a_stray(self, env):
        env.setenv("RAILWAY_ENVIRONMENT", "production")
        env.setenv("DCHUB_ROLE", "all")
        assert self._elig()[0] is False

    def test_worker_on_railway_is_eligible(self, env):
        env.setenv("RAILWAY_ENVIRONMENT", "production")
        env.setenv("DCHUB_ROLE", "worker")
        assert self._elig() == (True, "")

    def test_all_off_railway_is_eligible(self, env):
        assert self._elig() == (True, "")

    def test_opt_in_env(self, env):
        env.setenv("RAILWAY_ENVIRONMENT", "production")
        env.setenv("DCHUB_LEADER_ALLOW_ROLE_ALL", "true")
        assert self._elig() == (True, "")


# ───────────────────────── initial acquire ─────────────────────────
class TestAcquireFailsClosedForAStray:
    def test_stray_fleet_fails_closed_even_with_no_db(self, env):
        """RED on main: with no DATABASE_URL _acquire_leader_lock fails OPEN
        (returns True → IS_LEADER) for any bg-role process. A stray fleet must
        never become leader by that route either."""
        env.setenv("RAILWAY_ENVIRONMENT", "production")
        env.setenv("RAILWAY_SERVICE_NAME", "web")
        rec = _Rec()
        ns = {
            "os": os,
            "_ROLE_RUNS_BG": True,
            "_LEADER_LOCK_CONN": None,
            "_LEADER_LOCK_ID": 911714323,
            "_leader_lock_url": lambda: "",     # no DB configured → legacy fail-OPEN branch
            "_leader_lock_connect": lambda: pytest.fail("a stray must never reach the chokepoint"),
            "_leader_lock_skip_log": lambda *a: rec.skips.append(a[0] if a else "DCHUB_ROLE=web"),
            "print": lambda *a, **k: None,
        }
        _exec_fn("_leader_lock_eligible", ns, required=False)
        fn = _exec_fn("_acquire_leader_lock", ns)
        assert fn() is False, \
            "a Railway process with no role must fail CLOSED (never IS_LEADER), even with no DB"
        assert len(rec.skips) == 1 and "service=web" in rec.skips[0]

    def test_worker_with_no_db_still_fails_open(self, env):
        """Control (GREEN on main and after): the worker's no-DB fail-OPEN is kept."""
        env.setenv("RAILWAY_ENVIRONMENT", "production")
        env.setenv("DCHUB_ROLE", "worker")
        rec = _Rec()
        ns = {
            "os": os,
            "_ROLE_RUNS_BG": True,
            "_LEADER_LOCK_CONN": None,
            "_LEADER_LOCK_ID": 911714323,
            "_leader_lock_url": lambda: "",
            "_leader_lock_connect": lambda: None,
            "_leader_lock_skip_log": lambda *a: rec.skips.append(a),
            "print": lambda *a, **k: None,
        }
        _exec_fn("_leader_lock_eligible", ns, required=False)
        fn = _exec_fn("_acquire_leader_lock", ns)
        assert fn() is True and rec.skips == []


# ───────────── keepalive: the loop and its thread-start gate ─────────────
class TestKeepaliveIsGatedOnEligibility:
    def test_loop_rechecks_eligibility_before_the_while_loop(self):
        """RED on main: the loop only knew _ROLE_RUNS_BG, so a stray fleet's
        keepalive re-acquired every 30 s (measured: the other giving-art/web
        replica re-stamped a connection 60 s after the terminate)."""
        src = _fn_src("_leader_keepalive_loop")
        assert src and "_leader_lock_eligible" in src, \
            "_leader_keepalive_loop never re-checks eligibility LIVE"
        i_guard = src.index("_leader_lock_eligible")
        i_loop = src.index("while True")
        assert i_guard < i_loop, "the eligibility check must precede the while-loop"
        assert "return" in src[i_guard:i_loop], "an ineligible process must return before the loop"

    def test_thread_start_is_gated_on_eligibility(self):
        """RED on main: the keepalive thread started for every bg-role process on
        Railway, stray or not."""
        src, tree = _main_src_and_tree()
        tests = []
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                body = "\n".join(ast.get_source_segment(src, b) or "" for b in node.body)
                if 'name="leader-keepalive"' in body:
                    tests.append(ast.get_source_segment(src, node.test))
        assert tests, "the leader-keepalive thread start was not found"
        assert all("_LEADER_ELIGIBLE" in t for t in tests), \
            f"the keepalive thread-start gate ignores eligibility: {tests}"


# ───────────────────────── the stamp ─────────────────────────
class TestStampIsUnambiguous:
    def _fn(self):
        return _exec_fn("_leader_lock_app_name", {"os": os})

    def test_names_service_role_replica_pid_and_project_the_stray_shape(self, env):
        """RED on main: main stamps 'dchub-leader:web:4554c392:<pid>' — the
        service name in the role's slot, which is the misread that cost two fixes."""
        env.setenv("RAILWAY_SERVICE_NAME", "web")
        env.setenv("RAILWAY_PROJECT_NAME", "giving-art")
        env.setenv("RAILWAY_REPLICA_ID", "4554c392-aaaa-bbbb-cccc")
        assert self._fn()() == f"dchub-leader:web[all]:4554c392:{os.getpid()}@giving-art"

    def test_the_real_worker_shape(self, env):
        env.setenv("RAILWAY_SERVICE_NAME", "dchub-worker")
        env.setenv("DCHUB_ROLE", "worker")
        env.setenv("RAILWAY_PROJECT_NAME", "resourceful-essence")
        env.setenv("RAILWAY_REPLICA_ID", "f89b7670-1234")
        got = self._fn()()
        expect = f"dchub-leader:dchub-worker[worker]:f89b7670:{os.getpid()}@resourceful-essence"[:63]
        assert got == expect and len(got) <= 63
        assert f":f89b7670:{os.getpid()}@" in got, "truncation may only eat the project suffix"

    def test_falls_back_to_service_id_then_hostname_never_the_role(self, env):
        """RED on main: with no RAILWAY_SERVICE_NAME main puts the ROLE in the
        service slot ('dchub-leader:worker:local:…')."""
        env.setenv("DCHUB_ROLE", "worker")
        env.setenv("RAILWAY_SERVICE_ID", "4cd676da-3ef0-449c-a577-6a4973d95776")
        fn = self._fn()
        assert fn().startswith(f"dchub-leader:4cd676da[worker]:local:{os.getpid()}")
        env.delenv("RAILWAY_SERVICE_ID")
        got = fn()
        host = (socket.gethostname() or "").strip()[:16] or "dchub"
        assert got.startswith(f"dchub-leader:{host}[worker]:local:{os.getpid()}"), got
        assert not got.startswith("dchub-leader:worker:"), "the role must never be mistaken for the service"

    def test_fits_namedatalen(self, env):
        env.setenv("RAILWAY_SERVICE_NAME", "x" * 200)
        assert len(self._fn()()) == 63


# ───────────────────────── the one INFO line ─────────────────────────
class TestSkipLogCarriesTheReason:
    def _ns(self, logged):
        return {
            "_LEADER_LOCK_SKIP_LOGGED": False,
            "logger": types.SimpleNamespace(info=lambda m, *a, **k: logged.append(m)),
            "print": lambda *a, **k: None,
        }

    def test_reason_is_logged(self):
        """RED on main: _leader_lock_skip_log() takes no reason."""
        logged = []
        fn = _exec_fn("_leader_lock_skip_log", self._ns(logged))
        fn("DCHUB_ROLE=all on Railway — only DCHUB_ROLE=worker may lead; service=web project=giving-art")
        fn("second call is silent")
        assert logged == ["leader-lock: skipped (DCHUB_ROLE=all on Railway — only DCHUB_ROLE=worker "
                          "may lead; service=web project=giving-art)"]

    def test_web_line_unchanged(self):
        logged = []
        fn = _exec_fn("_leader_lock_skip_log", self._ns(logged))
        fn()
        assert logged == ["leader-lock: skipped (DCHUB_ROLE=web)"]


# ───────────────────────── boot lines + invariants ─────────────────────────
class TestBootLinesAndInvariants:
    def test_a_role_less_railway_container_names_itself_at_boot(self):
        """RED on main: a DCHUB_ROLE=unset Railway container printed nothing about
        its role — the stray's first log line was indistinguishable from a
        single-service dev boot."""
        src = MAIN.read_text()
        assert "DCHUB_ROLE unset on Railway" in src
        assert "NOT leader-eligible" in src, "the boot line must say the process can never lead"

    def test_lock_id_unchanged(self):
        assert "_LEADER_LOCK_ID = 911714323" in MAIN.read_text()

    def test_only_the_chokepoint_opens_a_stamped_connection(self):
        src, tree = _main_src_and_tree()
        offenders = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for c in ast.walk(fn):
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                        and c.func.attr == "connect" and "application_name" in {k.arg for k in c.keywords}):
                    offenders.append(fn.name)
        assert offenders == ["_leader_lock_connect"], offenders
