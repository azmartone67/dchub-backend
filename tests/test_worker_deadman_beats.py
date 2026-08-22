"""The in-process job fleet beats the PUBLIC dead-man board (2026-08-22).

WHAT WAS MEASURED
=================
GET /api/v1/ops/deadman → tracked: 70, all GitHub-Actions crons and shell
lanes. crawler_scheduler.SCHEDULE carries 71 in-process jobs and
dchub-scheduler.JOBS ~34 HTTP-driven ones; NONE beat the board (the 08-18
audit's "trap 3"). Railway listed 40 dchub-worker deployments in ~45h — each
restart re-initialises both schedulers and kills whatever was mid-flight —
and no surface outside the container could say which jobs still completed.

THE CONTRACT being guarded
==========================
- beat ONLY on success (routes/ingest_runs: the ledger records the last
  SUCCESSFUL run; a failing job must go overdue, not look alive)
- crawler_scheduler beats by DIRECT record_beat (no loopback HTTP — the
  2026-07-06 self-request outage); dchub-scheduler beats over the same origin
  and headers its jobs already use
- never send a guessed rows_inserted (a 0 climbs the zero-row alarm on
  healthy idle jobs)
- fail-soft: a ledger failure logs at WARNING and never raises into the job
- kill switch DCHUB_WORKER_DEADMAN_BEATS=0

NO NETWORK, NO DB, never imports main.py or either scheduler module (both
have module-scope side effects); the functions are AST-extracted from the
source text and exec'd with fakes, and placement is proven on the AST so a
comment cannot satisfy it.
"""
import ast
import logging
import pathlib
import sys
import types

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


class TestCrawlerSchedulerCadence:
    def test_slot_pairs_map_to_one_missed_slot_reads_overdue(self):
        f = _exec_fn("crawler_scheduler.py", "_schedule_cadence_hours", {})
        assert f(6, 18) == 18.0      # twice daily, 12h apart
        assert f(14, 2) == 18.0      # wraps midnight
        assert f(5, 5) == 36.0       # single daily slot
        assert f(2, 10) == 12.0      # 8h apart
        assert f(0, 0) == 36.0


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
          "_SCHEDULE_CADENCE_H": cadence if cadence is not None else {"news": 18.0}}
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

    def test_failure_and_timeout_beat_nothing(self, monkeypatch):
        monkeypatch.delenv("DCHUB_WORKER_DEADMAN_BEATS", raising=False)
        sink = []
        _install_ledger(monkeypatch, sink=sink)
        beat = _crawler_beat(monkeypatch)
        assert beat("news", "timeout", 1800.0) is False
        assert beat("news", "error: boom", 2.0) is False
        assert beat("news", "guard_error: x", 2.0) is False
        assert sink == [], "a failing job must go OVERDUE, not look alive"

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

    def test_every_schedule_job_gets_a_cadence(self):
        # The map is built from SCHEDULE itself, so a new job is covered the
        # day it is added — no registry to forget.
        src = _src("crawler_scheduler.py")
        assert "_SCHEDULE_CADENCE_H = {s[2]: _schedule_cadence_hours(s[0], s[1]) for s in SCHEDULE}" in src


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
