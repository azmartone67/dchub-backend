#!/usr/bin/env python3
"""tests/test_health_sees_a_stalled_crm_queue.py

★ THE DEFECT (measured 2026-09-20). `/api/v1/admin/crm/health` already computed
this exactly right — `stalled: true`, with the reason spelled out. **Nothing
read it.** 31 rows had been sitting in `crm_outbound_queue` since 2026-06-07
(2,512 hours) and 24 were `paid_conversion`. Three of the four payers found
that day with no MCP access were IN that queue at intent_score 100, captured
seconds after they paid. The follow-up path for paying customers was stalled
for three and a half months and the only surface that knew said so to nobody.

So the signal moved to `/api/v1/health`, which IS watched. This file executes
the real handler out of main.py's AST — not a mirror, per
feedback_execute_main_py_handler_from_ast — and drives it with a fake cursor so
the four verdicts are exercised against the shipped code.

MUST-FAIL CONTROLS included.
"""
import ast
import copy
import datetime as _dt
import os
import sys

from flask import Flask, jsonify

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
_TREE = ast.parse(_SRC)          # ~1s; parsed once for the whole module


class _Cur:
    """Answers the crm_export query under test, and answers every OTHER lane
    with a benignly HEALTHY row.

    ★ It used to raise for the other lanes. That looked tidy and it silently
    broke the most important assertion in this file: telemetry's `except` sets
    `out['status'] = 'red'`, so `body["status"] == "red"` passed no matter what
    the crm lane did. Proven by mutation — deleting the crm lane's own
    aggregation left all 7 tests green. A fixture that makes the other lanes
    fail is a fixture that pre-answers the question.
    """

    def __init__(self, n, oldest, paid):
        self._n, self._oldest, self._paid = n, oldest, paid
        self._fresh = _dt.datetime.utcnow()
        self._what = None

    def execute(self, sql, *a, **k):
        if "crm_outbound_queue" in sql:
            self._what = "crm"
        elif "mcp_upgrade_signals" in sql:
            self._what = "funnel"
        else:
            self._what = "fresh"          # telemetry / land_power / user_acq

    def fetchone(self):
        if self._what == "crm":
            return {"n": self._n, "oldest": self._oldest, "paid": self._paid}
        if self._what == "funnel":
            # no signals at all -> the funnel lane has nothing to call a leak
            return {"signals_7d": 0, "signals_30d": 0,
                    "conversions_30d": 0, "conversions": 0}
        return {"m": self._fresh, "n": 100}

    def fetchall(self):
        return []

    def close(self):
        pass


def _run(n, age_hours, paid=0):
    """Execute the real handler with a queue of depth n whose oldest row is
    age_hours old, and return its crm_export check."""
    fn = next(x for x in _TREE.body
              if isinstance(x, ast.FunctionDef)
              and x.name == "phase14c_health_aggregate")
    fn = copy.deepcopy(fn)
    fn.decorator_list = []
    oldest = (None if age_hours is None
              else _dt.datetime.utcnow() - _dt.timedelta(hours=age_hours))

    class _Conn:
        def cursor(self, **kw):
            return _Cur(n, oldest, paid)

        def close(self):
            pass

    class _Extras:
        RealDictCursor = object

    class _Psy:
        extras = _Extras()

    ns = {
        "get_read_db": lambda: _Conn(),
        "psycopg2": _Psy(),
        "utc_iso_z": lambda: "2026-09-20T00:00:00Z",
        "jsonify": jsonify,
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    with Flask(__name__).test_request_context("/api/v1/health"):
        resp = ns["phase14c_health_aggregate"]()
    body = resp.get_json() if hasattr(resp, "get_json") else resp
    return body


def _lane(body):
    assert "crm_export" in (body.get("checks") or {}), (
        "the crm_export lane is not in /api/v1/health at all — the 105-day "
        "silence is back")
    return body["checks"]["crm_export"]


def test_the_fixture_baseline_is_green():
    """Guards the guard: with a fresh queue every OTHER lane must be green too,
    or a later "overall went red" assertion is reading someone else's red."""
    body = _run(1, 1.0)
    assert body["status"] == "green", body.get("checks")


def test_a_fresh_queue_is_green():
    lane = _lane(_run(3, 2.0, paid=1))
    assert lane["status"] == "green", lane
    assert lane["queued"] == 3
    assert lane["paid_conversions_queued"] == 1


def test_an_empty_queue_is_green():
    assert _lane(_run(0, None))["status"] == "green"


def test_two_days_stale_is_yellow():
    lane = _lane(_run(5, 50.0))
    assert lane["status"] == "yellow", lane


def test_the_real_incident_is_RED():
    """2,512 hours / 31 rows / 24 paid — the state that went unseen."""
    body = _run(31, 2512.4, paid=24)
    lane = _lane(body)
    assert lane["status"] == "red", lane
    assert lane["paid_conversions_queued"] == 24
    assert lane["oldest_age_hours"] > 2500
    assert body["status"] == "red", (
        "the lane went red but the OVERALL verdict did not — a red lane nobody "
        "aggregates is the same silence in a new place")


def test_depth_alone_never_trips_it():
    """A busy queue that DRAINS is healthy. Age is the signal, not depth —
    otherwise a working destination alarms the moment volume rises."""
    assert _lane(_run(5000, 1.0, paid=4000))["status"] == "green"


def test_an_unreadable_queue_is_unknown_never_green():
    """never PASS uncheckable. An unreadable queue is not an empty one."""
    fn = next(x for x in _TREE.body
              if isinstance(x, ast.FunctionDef)
              and x.name == "phase14c_health_aggregate")
    fn = copy.deepcopy(fn)
    fn.decorator_list = []

    class _Boom:
        def cursor(self, **kw):
            class C:
                def execute(self, *a, **k):
                    raise RuntimeError("relation does not exist")

                def fetchone(self):
                    raise RuntimeError("nope")

                def close(self):
                    pass
            return C()

        def close(self):
            pass

    class _Extras:
        RealDictCursor = object

    class _Psy:
        extras = _Extras()

    ns = {"get_read_db": lambda: _Boom(), "psycopg2": _Psy(),
          "utc_iso_z": lambda: "2026-09-20T00:00:00Z", "jsonify": jsonify}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    with Flask(__name__).test_request_context("/api/v1/health"):
        body = ns["phase14c_health_aggregate"]().get_json()
    lane = _lane(body)
    assert lane["status"] == "unknown", lane
    assert lane["status"] != "green"


def test_MUST_FAIL_CONTROL_the_thresholds_are_really_what_gates_it():
    """Walk the boundary. If these all return the same verdict the lane is not
    reading age at all and every test above passes for the wrong reason."""
    seen = {h: _lane(_run(9, h))["status"] for h in (1.0, 47.9, 48.1, 167.9, 168.1)}
    assert seen[1.0] == "green" and seen[47.9] == "green", seen
    assert seen[48.1] == "yellow" and seen[167.9] == "yellow", seen
    assert seen[168.1] == "red", seen
    assert len(set(seen.values())) == 3, (
        "age is not gating the verdict: %s" % seen)
