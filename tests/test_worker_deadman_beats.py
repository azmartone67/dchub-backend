"""The in-process job fleet beats the PUBLIC dead-man board (2026-08-22).

WHAT WAS MEASURED
=================
GET /api/v1/ops/deadman → tracked: 70, all GitHub-Actions crons and shell
lanes. crawler_scheduler.SCHEDULE carries 71 in-process jobs and
dchub-scheduler.JOBS ~34 HTTP-driven ones; NONE beat the board (the 08-18
audit's "trap 3"). Railway listed 40 dchub-worker deployments in ~45h — each
restart re-initialises both schedulers and kills whatever was mid-flight —
and no surface outside the container could say which jobs still completed.

★ Step 7 (the same night): #3037 merged and ZERO `worker:` rows landed in the
next three hours. Not the kill switch (DCHUB_WORKER_DEADMAN_BEATS unset in
the worker env), not the ledger path (the first row landed 47 s after a
replica was finally promoted at 04:09:09Z): six consecutive worker
deployments (01:26Z→04:09Z) booted FOLLOWER on both replicas while an
out-of-fleet session held the singleton lock, and every beat sat BEHIND the
leader gate — so a leaderless fleet produced nothing and read exactly like a
dead one. A beat skipped on failure is likewise indistinguishable from a beat
that never fired.

THE CONTRACT being guarded
==========================
- every completed slot beats its HONEST final status: success → `success`;
  an exception → `error: <msg>`; a hard timeout → `timeout` (neither is an
  OK status, so the board reads OVERDUE at once, not 2x cadence later)
- `worker:crawler-scheduler` beats from EVERY replica whose loop is alive,
  leader or follower, BEFORE the leader gate; `worker:crawler-scheduler-
  leader` beats only while leading — the pair separates "nobody leads" from
  "worker dead"
- every LANDED beat logs at INFO (diagnosable from the container log alone)
- crawler_scheduler beats by DIRECT record_beat (no loopback HTTP — the
  2026-07-06 self-request outage); dchub-scheduler beats over the same origin
  and headers its jobs already use
- never send a guessed rows_inserted (a 0 climbs the zero-row alarm on
  healthy idle jobs)
- fail-soft: a ledger failure logs at WARNING and never raises into the job
- kill switch DCHUB_WORKER_DEADMAN_BEATS=0
- the PUBLIC read (/api/v1/ops/deadman) is uncurated — no whitelist, no
  prefix filter — so a `worker:` row is visible the moment it lands

NO NETWORK, NO DB, never imports main.py or either scheduler module (both
have module-scope side effects); the functions are AST-extracted from the
source text and exec'd with fakes, and placement is proven on the AST so a
comment cannot satisfy it.
"""
import ast
import datetime as dt
import logging
import os
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _src(rel):
    return (ROOT / rel).read_text()


def _fn(rel, name):
    tree = ast.parse(_src(rel))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {rel}")


def _exec_fn(rel, name, ns):
    src = _src(rel)
    exec(ast.get_source_segment(src, _fn(rel, name)), ns)
    return ns[name]


def _const(rel, name):
    """A module-scope `NAME = <literal>` pulled from the source — no import."""
    tree = ast.parse(_src(rel))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found at module scope in {rel}")


def _calls(node, name):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name]


# ───────────────────────── crawler_scheduler ─────────────────────────
class TestCrawlerSchedulerPlacement:
    def test_the_beat_is_in_the_guards_finally_block(self):
        fn = _fn("crawler_scheduler.py", "_run_with_guard")
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
        assert tries, "_run_with_guard lost its try/finally"
        in_finally = [c for t in tries for stmt in t.finalbody for c in _calls(stmt, "_beat_deadman")]
        assert in_finally, "the beat must run in `finally` — after status is final, on every exit path"
        args = [a.id for a in in_finally[0].args if isinstance(a, ast.Name)]
        assert "status" in args and "name" in args, "the beat must carry the job name and its final status"

    def test_the_beat_runs_after_the_cross_replica_claim_is_released(self):
        src = ast.get_source_segment(_src("crawler_scheduler.py"), _fn("crawler_scheduler.py", "_run_with_guard"))
        assert src.index("_release_crawler_run(name)") < src.index("_beat_deadman(name, status, duration)")

    def test_the_loop_alive_beat_fires_before_the_leader_gate(self):
        """A follower must still prove its loop is running. The gate is the
        `if not leading: … continue`; the loop beat must be an EARLIER
        statement of the same try body and the leader beat a LATER one —
        proven on the AST, so a commented-out call cannot satisfy it."""
        fn = _fn("crawler_scheduler.py", "_scheduler_loop")
        loop = next(n for n in ast.walk(fn) if isinstance(n, ast.While))
        tries = [n for n in loop.body if isinstance(n, ast.Try)]
        assert tries, "_scheduler_loop lost its per-tick try block"
        body = tries[0].body

        def _idx(pred):
            return next((i for i, stmt in enumerate(body) if pred(stmt)), None)

        i_loop = _idx(lambda s: bool(_calls(s, "_beat_loop_tick")))
        i_gate = _idx(lambda s: isinstance(s, ast.If) and isinstance(s.test, ast.UnaryOp)
                      and isinstance(s.test.op, ast.Not) and isinstance(s.test.operand, ast.Name)
                      and s.test.operand.id == "leading"
                      and any(isinstance(x, ast.Continue) for x in ast.walk(s)))
        i_lead = _idx(lambda s: bool(_calls(s, "_beat_leader_tick")))
        assert i_loop is not None, "_scheduler_loop no longer beats worker:crawler-scheduler"
        assert i_gate is not None, "the leader gate (`if not leading: … continue`) is gone"
        assert i_lead is not None, "_scheduler_loop no longer beats the leader feed"
        assert i_loop < i_gate < i_lead, (i_loop, i_gate, i_lead)
        call = _calls(body[i_loop], "_beat_loop_tick")[0]
        assert [a.id for a in call.args if isinstance(a, ast.Name)] == ["leading"], \
            "the loop beat must carry the same leadership verdict the gate uses"


class TestCrawlerSchedulerCadence:
    # ★2026-08-28: the namespace now needs `os`. _schedule_cadence_hours reads
    # CRAWLER_SCHEDULE, because that env var decides whether hour2 is a real
    # slot at all — see the once-mode case below and
    # tests/test_schedule_once_mode.py. Passing {} exec'd a body referencing a
    # name that was not there.
    def test_slot_pairs_map_to_one_missed_slot_reads_overdue(self, monkeypatch):
        monkeypatch.delenv("CRAWLER_SCHEDULE", raising=False)
        f = _exec_fn("crawler_scheduler.py", "_schedule_cadence_hours", {"os": os})
        assert f(6, 18) == 18.0      # twice daily, 12h apart
        assert f(14, 2) == 18.0      # wraps midnight
        assert f(5, 5) == 36.0       # single daily slot
        assert f(2, 10) == 12.0      # 8h apart
        assert f(0, 0) == 36.0

    def test_once_mode_collapses_every_pair_to_the_daily_gap(self, monkeypatch):
        """CRAWLER_SCHEDULE=once is the DEPLOYED value: _should_run_now uses
        [hour1] only, so hour2 never fires and a (6, 18) job runs ONCE a day.
        Deriving 18.0h from the tuple pair made 33 healthy feeds read OVERDUE
        every night between the threshold and the next morning's slot."""
        monkeypatch.setenv("CRAWLER_SCHEDULE", "once")
        f = _exec_fn("crawler_scheduler.py", "_schedule_cadence_hours", {"os": os})
        assert f(6, 18) == 36.0
        assert f(14, 2) == 36.0
        assert f(2, 10) == 36.0
        assert f(5, 5) == 36.0       # already single-slot — unchanged


def _install_ledger(monkeypatch, sink=None, boom=None):
    fake = types.ModuleType("routes.ingest_runs")

    def record_beat(feed, **kw):
        if boom is not None:
            raise boom
        if sink is not None:
            sink.append((feed, kw))
    fake.record_beat = record_beat
    monkeypatch.setitem(sys.modules, "routes.ingest_runs", fake)


def _crawler_beat(monkeypatch, cadence=None):
    import os
    ns = {"os": os, "logger": logging.getLogger("t"),
          "_SCHEDULE_CADENCE_H": cadence if cadence is not None else {"news": 18.0},
          "_BEAT_STATUS_MAX": _const("crawler_scheduler.py", "_BEAT_STATUS_MAX")}
    _exec_fn("crawler_scheduler.py", "_beat_status", ns)
    return _exec_fn("crawler_scheduler.py", "_beat_deadman", ns)


class TestCrawlerSchedulerBeat:
    def test_success_beats_worker_prefixed_feed_with_its_cadence(self, monkeypatch):
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        sink = []
        _install_ledger(monkeypatch, sink=sink)
        beat = _crawler_beat(monkeypatch)
        assert beat("news", "success", 44.2) is True
        assert sink == [("worker:news", {"status": "success", "cad": 18.0,
                                         "note": "in-process crawler_scheduler slot; 44s"})]

    def test_rows_inserted_is_never_guessed(self, monkeypatch):
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        sink = []
        _install_ledger(monkeypatch, sink=sink)
        _crawler_beat(monkeypatch)("news", "success", 1.0)
        assert "rows" not in sink[0][1] and "rows_inserted" not in sink[0][1]

    def test_failure_and_timeout_beat_an_honest_non_ok_status(self, monkeypatch):
        """Step 7 reversal of #3037's "success only": a failing slot used to
        beat NOTHING, which is indistinguishable from a beat that never fired
        (exactly the 0-rows night). The job's final status goes on the
        board verbatim, and none of these words is an OK status there."""
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        sink = []
        _install_ledger(monkeypatch, sink=sink)
        beat = _crawler_beat(monkeypatch)
        assert beat("news", "timeout", 1800.0) is True
        assert beat("news", "error: boom", 2.0) is True
        assert beat("news", "guard_error: x", 2.0) is True
        assert [(f, kw["status"]) for f, kw in sink] == [
            ("worker:news", "timeout"), ("worker:news", "error: boom"), ("worker:news", "guard_error: x")]
        ok_status = _const("routes/ingest_runs.py", "_OK_STATUS")   # the board's real vocabulary
        assert "success" in ok_status and "warn" not in ok_status
        for _, kw in sink:
            assert kw["status"].lower() not in ok_status, \
                f"{kw['status']!r} would read GREEN on the board — a failing job must read overdue"

    def test_status_is_clamped_to_the_wire_contract(self, monkeypatch):
        """POST /beat clamps status to 40 chars; the direct path must not
        write a longer value than the HTTP path could."""
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        sink = []
        _install_ledger(monkeypatch, sink=sink)
        _crawler_beat(monkeypatch)("news", "error: " + "x" * 200, 1.0)
        assert len(sink[0][1]["status"]) == _const("crawler_scheduler.py", "_BEAT_STATUS_MAX") == 40
        assert sink[0][1]["status"].startswith("error: ")

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("DCHUB_WORKER_DEADMAN_BEATS", "0")
        sink = []
        _install_ledger(monkeypatch, sink=sink)
        assert _crawler_beat(monkeypatch)("news", "success", 1.0) is False
        assert sink == []

    def test_a_ledger_failure_never_reaches_the_job(self, monkeypatch, caplog):
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        _install_ledger(monkeypatch, boom=RuntimeError("no DATABASE_URL"))
        with caplog.at_level(logging.WARNING, logger="t"):
            assert _crawler_beat(monkeypatch)("news", "success", 1.0) is False
        assert any("deadman beat failed for worker:news" in r.getMessage() for r in caplog.records)

    def test_a_landed_beat_is_logged_at_info(self, monkeypatch, caplog):
        """Before step 7 a landed beat and a silently skipped one produced the
        same container log: nothing. The 0-rows night could not be
        diagnosed from outside."""
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        _install_ledger(monkeypatch, sink=[])
        with caplog.at_level(logging.INFO, logger="t"):
            assert _crawler_beat(monkeypatch)("news", "success", 1.0) is True
        landed = [r.getMessage() for r in caplog.records
                  if r.levelno == logging.INFO and "deadman beat landed" in r.getMessage()]
        assert landed == ["🕒 deadman beat landed: worker:news status=success cadence=18.0h"]

    def test_nothing_is_logged_as_landed_unless_the_upsert_succeeded(self, monkeypatch, caplog):
        monkeypatch.setenv("DCHUB_WORKER_DEADMAN_BEATS", "0")
        _install_ledger(monkeypatch, sink=[])
        with caplog.at_level(logging.INFO, logger="t"):
            _crawler_beat(monkeypatch)("news", "success", 1.0)
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        _install_ledger(monkeypatch, boom=RuntimeError("ledger down"))
        with caplog.at_level(logging.INFO, logger="t"):
            _crawler_beat(monkeypatch)("news", "success", 1.0)
        assert not any("deadman beat landed" in r.getMessage() for r in caplog.records)

    def test_every_schedule_job_gets_a_cadence(self):
        # The map is built from SCHEDULE itself, so a new job is covered the
        # day it is added — no registry to forget.
        src = _src("crawler_scheduler.py")
        assert "_SCHEDULE_CADENCE_H = {s[2]: _schedule_cadence_hours(s[0], s[1]) for s in SCHEDULE}" in src


def _guard(monkeypatch, sink, hard_timeout=5.0):
    """The REAL _run_with_guard + _beat_deadman, exec'd with fakes for the
    module globals they touch (locks, claims, history, clock).

    ★ This namespace is the function's global scope. Every module-level name
    _run_with_guard reads must be listed here or it raises NameError from
    `<string>` at call time — which is how r-timeout-mutex (2026-08-26) broke
    all four TestRunWithGuardBeats cases at once. If you add a global to
    _run_with_guard, add it here too.
    """
    import os
    import threading
    import time as _time
    _install_ledger(monkeypatch, sink=sink)
    ns = {"os": os, "threading": threading,
          "time": types.SimpleNamespace(time=_time.time, sleep=lambda s: None),
          "datetime": dt.datetime, "timezone": dt.timezone,
          "logger": logging.getLogger("t"),
          "HARD_TIMEOUT_SECONDS": hard_timeout, "OVERLAP_GUARD_SECONDS": 0,
          "_active_crawler": None, "_lock": threading.Lock(), "_run_history": [],
          "_claim_crawler_run": lambda name: True,
          "_release_crawler_run": lambda name: None,
          "_SCHEDULE_CADENCE_H": {"news": 18.0},
          # r-timeout-mutex: the abandoned-thread registry the guard consults
          # before starting, and the status it beats when it refuses. Real dict,
          # real constant — a fake here would let the guard's own logic drift.
          "_abandoned_runs": {},
          # Mirrors the real `_CLAIM_TTL_SECONDS = HARD_TIMEOUT_SECONDS + 120`.
          # Not readable via _const — it is computed, not a literal — so the
          # RELATIONSHIP is reproduced here rather than a hardcoded 1920, which
          # would stop tracking hard_timeout and quietly go wrong.
          "_CLAIM_TTL_SECONDS": hard_timeout + 120,
          "_SKIPPED_ABANDONED_STATUS": _const("crawler_scheduler.py",
                                              "_SKIPPED_ABANDONED_STATUS"),
          "_BEAT_STATUS_MAX": _const("crawler_scheduler.py", "_BEAT_STATUS_MAX")}
    _exec_fn("crawler_scheduler.py", "_beat_status", ns)
    _exec_fn("crawler_scheduler.py", "_beat_deadman", ns)
    _exec_fn("crawler_scheduler.py", "_reap_abandoned", ns)
    return _exec_fn("crawler_scheduler.py", "_run_with_guard", ns), ns


class TestRunWithGuardBeats:
    """End to end through the real guard: the feed is `worker:<job>` and the
    beat fires on EVERY exit path with the job's final status."""

    def test_success_lands_worker_job_success(self, monkeypatch):
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        sink = []
        guard, ns = _guard(monkeypatch, sink)
        guard("news", lambda: None)
        assert [(f, kw["status"]) for f, kw in sink] == [("worker:news", "success")]
        assert sink[0][1]["cad"] == 18.0
        assert ns["_run_history"][-1]["status"] == "success"
        assert ns["_active_crawler"] is None

    def test_an_exception_lands_worker_job_error(self, monkeypatch):
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        sink = []
        guard, ns = _guard(monkeypatch, sink)

        def boom():
            raise RuntimeError("boom")
        guard("news", boom)
        assert [(f, kw["status"]) for f, kw in sink] == [("worker:news", "error: boom")]
        assert ns["_run_history"][-1]["status"] == "error: boom"

    def test_a_hard_timeout_lands_worker_job_timeout(self, monkeypatch):
        import time as _time
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        sink = []
        guard, _ = _guard(monkeypatch, sink, hard_timeout=0.05)
        guard("news", lambda: _time.sleep(0.6))
        assert [(f, kw["status"]) for f, kw in sink] == [("worker:news", "timeout")]

    def test_a_slot_skipped_for_the_other_replica_does_not_beat(self, monkeypatch):
        """The replica that does not hold the cross-replica claim never ran the
        job; beating would claim a success it cannot vouch for."""
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        sink = []
        guard, ns = _guard(monkeypatch, sink)
        ns["_claim_crawler_run"] = lambda name: False
        guard("news", lambda: None)
        assert sink == [] and ns["_active_crawler"] is None


class TestSchedulerLoopBeat:
    """`worker:crawler-scheduler` — the leadership-INDEPENDENT heartbeat."""

    def _tick(self, calls, logger=None):
        import os
        ns = {"os": os, "time": types.SimpleNamespace(time=lambda: 0.0),
              "_LOOP_BEAT_EVERY_S": _const("crawler_scheduler.py", "_LOOP_BEAT_EVERY_S"),
              "_last_loop_beat": 0.0, "logger": logger or logging.getLogger("t"),
              "_replica_label": lambda: "r1",
              "_beat_deadman": lambda name, status, duration_s=None, cadence_h=None, note=None:
                  calls.append((name, status, cadence_h, note)) or True}
        return _exec_fn("crawler_scheduler.py", "_beat_loop_tick", ns)

    def test_a_follower_beats_the_loop_feed(self):
        calls = []
        tick = self._tick(calls)
        assert tick(False, now_s=1000.0) is True
        assert calls == [("crawler-scheduler", "success", 0.5,
                          "scheduler loop alive; leader=False; replica=r1")]

    def test_throttled_to_once_per_ten_minutes_and_names_the_leader_state(self):
        calls = []
        tick = self._tick(calls)
        assert tick(False, now_s=1000.0) is True
        assert tick(True, now_s=1300.0) is False, "second tick inside 10 min must not beat"
        assert tick(True, now_s=1700.0) is True
        assert _const("crawler_scheduler.py", "_LOOP_BEAT_EVERY_S") == 600
        assert [c[3] for c in calls] == ["scheduler loop alive; leader=False; replica=r1",
                                         "scheduler loop alive; leader=True; replica=r1"]

    def test_a_follower_says_so_at_info(self, caplog):
        calls = []
        tick = self._tick(calls, logger=logging.getLogger("t"))
        with caplog.at_level(logging.INFO, logger="t"):
            tick(False, now_s=1000.0)
            tick(True, now_s=2000.0)
        said = [r.getMessage() for r in caplog.records if "follower" in r.getMessage()]
        assert len(said) == 1 and "loop alive" in said[0] and "replica r1" in said[0]

    def test_loop_and_leader_feeds_are_distinct_rows(self):
        calls = []
        self._tick(calls)(False, now_s=1000.0)
        ns = {"time": types.SimpleNamespace(time=lambda: 0.0), "_LEADER_BEAT_EVERY_S": 600,
              "_last_leader_beat": 0.0,
              "_beat_deadman": lambda name, status, duration_s=None, cadence_h=None, note=None:
                  calls.append((name, status, cadence_h, note)) or True}
        _exec_fn("crawler_scheduler.py", "_beat_leader_tick", ns)(now_s=1000.0)
        assert [c[0] for c in calls] == ["crawler-scheduler", "crawler-scheduler-leader"]

    def test_replica_label_prefers_the_railway_replica_id(self, monkeypatch):
        import os
        label = _exec_fn("crawler_scheduler.py", "_replica_label", {"os": os})
        monkeypatch.setenv("RAILWAY_REPLICA_ID", "abcdef1234567890")
        assert label() == "abcdef12"
        monkeypatch.delenv("RAILWAY_REPLICA_ID")
        assert isinstance(label(), str) and label()


class TestPublicDeadmanReadIsUncurated:
    """GET /api/v1/ops/deadman lists whatever the ledger holds — no whitelist,
    no prefix filter — so a `worker:` row shows the moment it lands, and an
    honest error status reads overdue. Exercises the REAL route with a fake
    connection (the real psycopg2 is never dialled)."""

    def _client(self, monkeypatch, rows):
        flask = pytest.importorskip("flask")
        import routes.ingest_runs as ir
        executed = []

        class _Cur:
            def execute(self, sql, params=None):
                executed.append(" ".join(str(sql).split()))

            def fetchall(self):
                return rows

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Conn:
            def cursor(self):
                return _Cur()

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setenv("DATABASE_URL", "postgresql://ledger.example/db")
        monkeypatch.setattr(ir.psycopg2, "connect", lambda *a, **k: _Conn())
        app = flask.Flask(__name__)
        app.register_blueprint(ir.ingest_runs_bp)
        return app.test_client(), executed

    def test_a_worker_row_is_listed_without_any_whitelist(self, monkeypatch):
        now = dt.datetime.now(dt.timezone.utc)
        rows = [("worker:news", now, "success", None, None, 18.0, 0, "in-process crawler_scheduler slot; 44s"),
                ("iso-lmp-pjm", now, "success", 5, None, 3.0, 0, None)]
        client, executed = self._client(monkeypatch, rows)
        body = client.get("/api/v1/ops/deadman").get_json()
        assert body["ok"] is True and body["tracked"] == 2
        by = {f["feed"]: f for f in body["feeds"]}
        assert "worker:news" in by, "the public read filtered the worker row out"
        assert by["worker:news"]["overdue"] is False
        assert by["worker:news"]["note"].startswith("in-process crawler_scheduler")
        reads = [q for q in executed if q.startswith("SELECT") and "FROM ingest_runs" in q]
        assert reads and all("WHERE" not in q for q in reads), reads

    def test_an_honest_error_status_reads_red_at_once(self, monkeypatch):
        """★2026-09-02 (D2): a feed that RAN and reported a fault is RED, not
        overdue — `overdue` now means LATE only. It is still not green on the
        wire: red/unhealthy/kinds carry it (tests/test_deadman_late_vs_red.py)."""
        now = dt.datetime.now(dt.timezone.utc)
        rows = [("worker:news", now, "error: boom", None, None, 18.0, 0, None),
                ("worker:deals", now, "timeout", None, None, 18.0, 0, None)]
        client, _ = self._client(monkeypatch, rows)
        body = client.get("/api/v1/ops/deadman").get_json()
        assert body["any_overdue"] is False and body["overdue_count"] == 0
        assert body["any_red"] is True and body["red_count"] == 2
        assert body["unhealthy_count"] == 2
        assert sorted(body["red"]) == ["worker:deals", "worker:news"]
        by = {r["feed"]: r for r in body["feeds"]}
        assert by["worker:news"]["reasons"] == ["status=error: boom"]
        assert by["worker:deals"]["reasons"] == ["status=timeout"]
        assert all(r["red"] and r["unhealthy"] and not r["overdue"] for r in by.values())


# ───────────────────────── dchub-scheduler ─────────────────────────
REL = "dchub-scheduler.py"


class TestHttpSchedulerPlacement:
    def test_the_beat_sits_inside_the_2xx_branch_of_run_job(self):
        fn = _fn(REL, "run_job")
        src = _src(REL)
        ifs = [n for n in ast.walk(fn) if isinstance(n, ast.If)
               and "200 <= status < 300" in (ast.get_source_segment(src, n.test) or "")]
        assert ifs, "run_job lost its 2xx branch"
        assert any(_calls(stmt, "_beat_deadman") for stmt in ifs[0].body), \
            "the beat must fire only on a 2xx — a 503/401/timeout job must go overdue"
        # and NOT anywhere else in the function
        assert len(_calls(fn, "_beat_deadman")) == 1


class TestHttpSchedulerCadence:
    def test_longest_gap_rule(self):
        f = _exec_fn(REL, "_job_cadence_hours", {})
        assert f({"hours": [0, 4, 8, 12, 16, 20]}) == 6.0
        assert f({"hours": [6, 12, 18]}) == 18.0        # 18→06 is the 12h gap
        assert f({"hours": [2]}) == 36.0
        assert f({"hours": [2], "day_of_week": 6}) == 252.0
        assert f({"hours": [3], "day_of_month": 1}) == 1116.0


class _FakeReq:
    last = None

    def __init__(self, url, method=None, headers=None, data=None):
        self.url, self.method, self.headers, self.data = url, method, headers, data
        _FakeReq.last = self


class _FakeResp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_beat(monkeypatch, status=200, boom=None, admin_key="k"):
    import json, os
    opened = []

    def urlopen(req, timeout=None):
        if boom is not None:
            raise boom
        opened.append((req, timeout))
        return _FakeResp(status)
    ns = {"os": os, "json": json, "log": logging.getLogger("t"),
          "API_BASE": "https://origin.example/", "ADMIN_KEY": admin_key,
          "Request": _FakeReq, "urlopen": urlopen,
          "get_internal_key_for_client": lambda: "ik"}
    _exec_fn(REL, "_job_cadence_hours", ns)
    return _exec_fn(REL, "_beat_deadman", ns), opened


class TestHttpSchedulerBeat:
    JOB = {"name": "News/RSS Refresh", "endpoint": "/api/jobs/news-refresh",
           "hours": [0, 4, 8, 12, 16, 20], "minute": 0, "timeout": 300}

    def test_posts_to_the_beat_route_with_the_jobs_own_auth(self, monkeypatch):
        import json
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        _FakeReq.last = None
        beat, opened = _http_beat(monkeypatch)
        assert beat("news", self.JOB, {"new_articles": 313}, 44.0) is True
        req = _FakeReq.last
        assert req.url == "https://origin.example/api/v1/admin/ingest-runs/beat"
        assert req.method == "POST"
        assert req.headers["X-Admin-Key"] == "k" and req.headers["X-Internal-Key"] == "ik"
        body = json.loads(req.data.decode())
        assert body["feed"] == "worker-http:news"
        assert body["status"] == "success"
        assert body["cadence_hours"] == 6.0
        assert "rows_inserted" not in body, "never guess rows — the counter is the producer's"
        assert "new_articles=313" in body["note"]
        assert opened[0][1] == 15

    def test_no_admin_key_means_no_beat(self, monkeypatch):
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        _FakeReq.last = None
        beat, opened = _http_beat(monkeypatch, admin_key="")
        assert beat("news", self.JOB, {}, 1.0) is False
        assert _FakeReq.last is None and opened == []

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("DCHUB_WORKER_DEADMAN_BEATS", "false")
        _FakeReq.last = None
        beat, opened = _http_beat(monkeypatch)
        assert beat("news", self.JOB, {}, 1.0) is False
        assert opened == []

    def test_a_network_failure_is_a_warning_not_an_exception(self, monkeypatch, caplog):
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        beat, _ = _http_beat(monkeypatch, boom=OSError("connection refused"))
        with caplog.at_level(logging.WARNING, logger="t"):
            assert beat("news", self.JOB, {}, 1.0) is False
        assert any("deadman beat failed for worker-http:news" in r.getMessage() for r in caplog.records)

    def test_a_non_2xx_from_the_ledger_is_reported_false(self, monkeypatch):
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        beat, _ = _http_beat(monkeypatch, status=401)
        assert beat("news", self.JOB, {}, 1.0) is False
