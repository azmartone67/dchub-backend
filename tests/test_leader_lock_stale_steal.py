"""The singleton leader lock recovers from a dead holder (2026-08-22).

WHAT WAS MEASURED
=================
dchub-worker's leadership is a SESSION advisory lock (main._LEADER_LOCK_ID) on
a direct Neon connection. After the 01:25Z redeploy BOTH worker processes
logged "not current leader" for 48+ minutes (crawler slots at 02:00 never
fired, publishers held, brain autonomy + self-heal skipped) while the previous
deployment had taken 5 min to win the lock back. The keepalive loop only ever
asked pg_try_advisory_lock and waited — a hard-killed container's backend keeps
the lock until the server notices the dead peer. With ~21 worker redeploys a
day that is a duty cycle, not an incident.

THE CONTRACT being guarded
==========================
- a holder is a corpse only when idle >= stale_seconds (a live leader pings
  every 30 s; default 300 s, floor 60 s); a fresh holder is NEVER terminated
- our own backend is never a target; unknown idle time is never a corpse
- the keepalive loop steals ONLY after pg_try_advisory_lock failed, behind the
  DCHUB_LEADER_STEAL_STALE kill switch, and any error means no steal
- GET /api/v1/ops/leader exposes the holder + the same verdict, read-only
- the crawler loop beats `worker:crawler-scheduler-leader` (cadence 0.5h) while
  leading, throttled to once per 10 min, so a leaderless fleet reads OVERDUE

NO NETWORK, NO DB, never imports main.py (module scope opens pools);
main.py and crawler_scheduler.py are read as text / AST; the pure functions
are exec'd with fakes where they live in importable modules.
"""
import ast
import datetime as dt
import pathlib
import re
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from util import leader_election as le  # noqa: E402


class FakeCursor:
    """Scripted cursor: answers by substring of the last SQL."""

    def __init__(self, holder=None, terminate_ok=True, my_pid=999):
        self.holder, self.terminate_ok, self.my_pid = holder, terminate_ok, my_pid
        self.sql = []
        self._last = ""

    def execute(self, sql, params=None):
        self._last = sql
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        if "pg_locks" in self._last:
            return self.holder
        if "pg_terminate_backend" in self._last:
            return (self.terminate_ok,)
        if "pg_backend_pid" in self._last:
            return (self.my_pid,)
        if "pg_try_advisory_lock" in self._last:
            return (True,)
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _row(pid=77, idle_s=900.0, age_s=3600.0, state="idle", app="worker"):
    t0 = dt.datetime(2026, 8, 22, 1, 0, 0)
    sc = t0 + dt.timedelta(seconds=age_s - (idle_s or 0.0))
    return (pid, t0, state, sc, app, idle_s, age_s)


# ───────────────────────── util.leader_election ─────────────────────────
class TestLockHolder:
    def test_parses_the_holder_row(self):
        cur = FakeCursor(holder=_row())
        h = le.lock_holder(cur)
        assert h["pid"] == 77 and h["idle_s"] == 900.0 and h["age_s"] == 3600.0
        assert h["backend_start"] == "2026-08-22T01:00:00"
        sql, params = cur.sql[0]
        assert "pg_locks" in sql and "pg_stat_activity" in sql and "granted" in sql
        assert params == (le.LOCK_ID,)
        assert "classid = 0" in sql and "objsubid = 1" in sql, \
            "a bigint advisory key lands in objid with classid 0 / objsubid 1"

    def test_no_holder_is_none(self):
        assert le.lock_holder(FakeCursor(holder=None)) is None

    def test_lock_id_matches_main(self):
        src = (ROOT / "main.py").read_text()
        m = re.search(r"^_LEADER_LOCK_ID = (\d+)", src, re.M)
        assert m and int(m.group(1)) == le.LOCK_ID, "util.LOCK_ID drifted from main._LEADER_LOCK_ID"


class TestStaleHolder:
    def test_a_fresh_holder_is_never_a_corpse(self):
        assert le.stale_holder(FakeCursor(holder=_row(idle_s=20.0))) is None

    def test_a_holder_idle_past_threshold_is_a_corpse(self):
        h = le.stale_holder(FakeCursor(holder=_row(idle_s=900.0)))
        assert h and h["pid"] == 77

    def test_own_backend_is_never_a_target(self):
        assert le.stale_holder(FakeCursor(holder=_row(pid=77, idle_s=900.0)), my_pid=77) is None

    def test_unknown_idle_is_never_a_corpse(self):
        assert le.stale_holder(FakeCursor(holder=_row(idle_s=None))) is None

    def test_threshold_has_a_floor(self):
        # env could say 1s; the floor (60s) must win — a GC pause is not death
        assert le.stale_holder(FakeCursor(holder=_row(idle_s=50.0)), stale_seconds=1) is None
        assert le.stale_holder(FakeCursor(holder=_row(idle_s=61.0)), stale_seconds=1) is not None

    def test_exact_threshold_boundary(self):
        assert le.stale_holder(FakeCursor(holder=_row(idle_s=299.9))) is None
        assert le.stale_holder(FakeCursor(holder=_row(idle_s=300.0))) is not None


class TestTerminateStaleHolder:
    def test_fresh_holder_issues_no_terminate(self):
        cur = FakeCursor(holder=_row(idle_s=20.0))
        assert le.terminate_stale_holder(cur) is None
        assert not any("pg_terminate_backend" in s for s, _ in cur.sql)

    def test_stale_holder_is_terminated_by_pid(self):
        cur = FakeCursor(holder=_row(pid=77, idle_s=900.0))
        out = le.terminate_stale_holder(cur)
        assert out["pid"] == 77 and out["terminated"] is True
        assert any("pg_terminate_backend" in s and p == (77,) for s, p in cur.sql)

    def test_termination_refusal_is_reported_not_hidden(self):
        cur = FakeCursor(holder=_row(idle_s=900.0), terminate_ok=False)
        assert le.terminate_stale_holder(cur)["terminated"] is False


# ───────────────────────── main.py keepalive wiring (AST, never imported) ─────────────────────────
def _fn_src(rel, name):
    src = (ROOT / rel).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node), node
    raise AssertionError(f"{name} not in {rel}")


def _calls(node, name):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name) and n.func.id == name]


class TestKeepaliveLoopSteals:
    def test_steal_happens_only_after_a_failed_try_lock_and_behind_the_kill_switch(self):
        src, node = _fn_src("main.py", "_leader_keepalive_loop")
        assert _calls(node, "_steal_stale_leader_lock"), "the keepalive loop never tries to steal"
        # ordering: try_lock first, then the guarded steal, then the promote branch
        i_try = src.index("pg_try_advisory_lock")
        i_steal = src.index("_steal_stale_leader_lock(")
        i_promote = src.index("promoted to LEADER")
        assert i_try < i_steal < i_promote
        guard = src[src.rindex("if", 0, i_steal):i_steal]
        assert "not _got" in guard and "_leader_steal_enabled()" in guard

    def test_steal_helper_never_targets_itself_and_fails_closed(self):
        src, node = _fn_src("main.py", "_steal_stale_leader_lock")
        assert _calls(node, "terminate_stale_holder")
        assert "pg_backend_pid" in src and "my_pid=" in src
        assert "except Exception" in src and "return False" in src

    def test_kill_switch_and_threshold_are_env_driven_with_floor(self):
        src, _ = _fn_src("main.py", "_leader_steal_enabled")
        assert "DCHUB_LEADER_STEAL_STALE" in src
        src2, _ = _fn_src("main.py", "_leader_stale_seconds")
        assert "DCHUB_LEADER_STALE_SECONDS" in src2 and "max(60.0" in src2


# ───────────────────────── /api/v1/ops/leader ─────────────────────────
class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass


def _client(monkeypatch, holder):
    flask = pytest.importorskip("flask")
    import routes.ingest_runs as ir
    monkeypatch.setattr(ir, "_dsn", lambda: "postgres://x")
    monkeypatch.setattr(ir.psycopg2, "connect", lambda *a, **k: _FakeConn(FakeCursor(holder=holder)))
    monkeypatch.delenv("DCHUB_LEADER_STALE_SECONDS", raising=False)
    app = flask.Flask(__name__)
    app.register_blueprint(ir.ingest_runs_bp)
    return app.test_client()


class TestOpsLeaderRoute:
    def test_zombie_holder_is_reported(self, monkeypatch):
        r = _client(monkeypatch, _row(idle_s=900.0)).get("/api/v1/ops/leader")
        b = r.get_json()
        assert r.status_code == 200 and b["ok"] is True
        assert b["zombie_suspected"] is True and b["leader_present"] is False
        assert b["holder"]["pid"] == 77 and b["stale_seconds"] == 300.0

    def test_live_leader_is_reported(self, monkeypatch):
        b = _client(monkeypatch, _row(idle_s=12.0)).get("/api/v1/ops/leader").get_json()
        assert b["leader_present"] is True and b["zombie_suspected"] is False

    def test_no_holder_means_no_leader_not_an_error(self, monkeypatch):
        b = _client(monkeypatch, None).get("/api/v1/ops/leader").get_json()
        assert b["ok"] is True and b["holder"] is None and b["leader_present"] is False

    def test_db_failure_is_503_not_a_fake_verdict(self, monkeypatch):
        flask = pytest.importorskip("flask")
        import routes.ingest_runs as ir
        monkeypatch.setattr(ir, "_dsn", lambda: "postgres://x")

        def boom(*a, **k):
            raise RuntimeError("refused")
        monkeypatch.setattr(ir.psycopg2, "connect", boom)
        app = flask.Flask(__name__)
        app.register_blueprint(ir.ingest_runs_bp)
        r = app.test_client().get("/api/v1/ops/leader")
        assert r.status_code == 503 and r.get_json()["leader_present"] is None


# ───────────────────────── crawler_scheduler leader beat ─────────────────────────
def _exec_fn(rel, name, ns):
    src = (ROOT / rel).read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(ast.get_source_segment(src, fn), ns)
    return ns[name]


class TestSchedulerLeaderBeat:
    def test_beat_sits_right_after_the_leader_gate(self):
        src, node = _fn_src("crawler_scheduler.py", "_scheduler_loop")
        assert _calls(node, "_beat_leader_tick")
        assert src.index("_is_scheduler_leader()") < src.index("_beat_leader_tick()") < src.index("for hour1, hour2, name, _ in SCHEDULE")

    def test_beat_is_throttled_and_names_the_feed_with_a_half_hour_cadence(self):
        calls = []
        ns = {"time": types.SimpleNamespace(time=lambda: 0.0), "_LEADER_BEAT_EVERY_S": 600,
              "_last_leader_beat": 0.0,
              "_beat_deadman": lambda name, status, duration_s=None, cadence_h=None: calls.append((name, status, cadence_h)) or True}
        tick = _exec_fn("crawler_scheduler.py", "_beat_leader_tick", ns)
        assert tick(now_s=1000.0) is True
        assert tick(now_s=1300.0) is False, "second tick inside 10 min must not beat"
        assert tick(now_s=1700.0) is True
        assert calls == [("crawler-scheduler-leader", "success", 0.5)] * 2

    def test_beat_deadman_honours_the_cadence_override(self, monkeypatch):
        import os
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        sink = []
        fake = types.ModuleType("routes.ingest_runs")
        fake.record_beat = lambda feed, **kw: sink.append((feed, kw))
        monkeypatch.setitem(sys.modules, "routes.ingest_runs", fake)
        import logging
        ns = {"os": os, "logger": logging.getLogger("t"), "_SCHEDULE_CADENCE_H": {"news": 18.0}}
        beat = _exec_fn("crawler_scheduler.py", "_beat_deadman", ns)
        assert beat("crawler-scheduler-leader", "success", cadence_h=0.5) is True
        assert beat("news", "success", 3.0) is True
        assert sink[0][0] == "worker:crawler-scheduler-leader" and sink[0][1]["cad"] == 0.5
        assert sink[1][0] == "worker:news" and sink[1][1]["cad"] == 18.0
