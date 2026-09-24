"""GET /api/v1/stats warms its memo in the background during the boot window.

House rule: tests never import main.py. get_stats(), the warm functions and the
warm-state assignments are lifted out by AST and run against a fake slow path,
so nothing here touches a database. A NameError means the lifted code grew a new
free name: add it to _ns(), don't catch it.

★ WHY (2026-09-24). get_stats() answers a young process (< STATS_BOOT_GRACE_S,
210s) with the degradation-cache payload so the ~15-query slow path stays off
the request thread during boot — and nothing warmed the memo meanwhile. Railway
redeployed ~15x in an hour that day (once per merge); sampling
admin/build-info uptime_s against the route showed BOOT until ~225s, then one
1.5s compute, then memo, then the next deploy. The first boot-window request
now starts ONE background run of the same slow path.
"""
import ast
import logging
import os
import threading
import time

import flask
import pytest

_MAIN = os.path.join(os.path.dirname(__file__), "..", "main.py")
_LIFT_DEFS = {"get_stats", "_stats_boot_warm_run", "_kick_stats_boot_warm"}
_LIFT_ASSIGNS = {"_STATS_WARM", "_STATS_WARM_LOCK", "_STATS_WARM_DELAY_S"}


@pytest.fixture(scope="module")
def lifted():
    tree = ast.parse(open(_MAIN, encoding="utf-8").read())
    nodes = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in _LIFT_DEFS:
            n.decorator_list = []
            nodes.append(n)
        elif isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _LIFT_ASSIGNS for t in n.targets):
            nodes.append(n)
    got = {getattr(n, "name", None) or n.targets[0].id for n in nodes}
    assert got == _LIFT_DEFS | _LIFT_ASSIGNS, f"lifted {got}"
    return compile(ast.Module(nodes, []), _MAIN, "exec")


class Slow:
    """Stands in for _stats_compute_response. Real one: stores the memo and
    returns a jsonify() response — which needs an app context, so this does
    too (a warm without app_context fails here exactly as it would live)."""

    def __init__(self, ns, store=True, raise_=None):
        self.ns, self.store, self.raise_, self.calls = ns, store, raise_, 0

    def __call__(self):
        self.calls += 1
        resp = flask.jsonify({"total_facilities": 24500})  # RuntimeError outside a context
        if self.raise_:
            raise self.raise_
        if self.store:
            self.ns["_STATS_CACHE"]["value"] = {"total_facilities": 24500}
            self.ns["_STATS_CACHE"]["ts"] = time.monotonic()
        resp.headers["X-Cache"] = "MISS"
        return resp


def _ns(lifted, *, age_s=5.0, **slow_kw):
    app = flask.Flask(__name__)
    ns = {
        "os": os, "threading": threading, "_t_stats": time, "logger": logging.getLogger("stats-warm-test"),
        "app": app, "jsonify": flask.jsonify,
        "_STATS_CACHE": {"value": None, "ts": 0}, "_STATS_TTL": 300,
        "_STATS_PROC_START": time.monotonic() - age_s, "_STATS_BOOT_GRACE_S": 210,
        "get_degraded_data": lambda key: ({"total_facilities": 24400, "_cache": {}}, 12.0),
        "utc_iso_z": lambda: "2026-09-24T00:00:00Z",
    }
    exec(lifted, ns)
    ns["_STATS_WARM_DELAY_S"] = 0
    ns["_stats_compute_response"] = Slow(ns, **slow_kw)
    return ns


def _get(ns):
    with ns["app"].test_request_context("/api/v1/stats"):
        r = ns["get_stats"]()
        return r, r.get_json()


def _join_warm(timeout=5):
    for t in threading.enumerate():
        if t.name == "stats-boot-warm":
            t.join(timeout)


@pytest.fixture(autouse=True)
def _no_kill_switch(monkeypatch):
    monkeypatch.delenv("STATS_BOOT_WARM_DISABLE", raising=False)
    yield
    _join_warm()


def test_boot_request_stays_fast_and_the_memo_lands_behind_it(lifted):
    ns = _ns(lifted)
    r, body = _get(ns)
    assert r.headers["X-Cache"] == "BOOT" and body["_cache"]["served_from"] == "boot-degraded"
    assert r.headers["Cache-Control"] == "no-store"
    _join_warm()
    assert ns["_stats_compute_response"].calls == 1
    assert ns["_STATS_WARM"]["finished"] and ns["_STATS_WARM"]["error"] is None
    # still inside the 210s window — and now served from the memo, not BOOT
    r2, body2 = _get(ns)
    assert r2.headers["X-Cache"] == "HIT" and body2["_cache"]["served_from"] == "memo"


def test_a_burst_of_boot_requests_starts_exactly_one_warm(lifted):
    ns = _ns(lifted)
    barrier = threading.Barrier(16)

    def hit():
        barrier.wait()
        _get(ns)
    ts = [threading.Thread(target=hit) for _ in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    _join_warm()
    assert ns["_stats_compute_response"].calls == 1


def test_kill_switch_keeps_the_old_behaviour(lifted, monkeypatch):
    monkeypatch.setenv("STATS_BOOT_WARM_DISABLE", "1")
    ns = _ns(lifted)
    for _ in range(3):
        r, _ = _get(ns)
        assert r.headers["X-Cache"] == "BOOT"
    _join_warm()
    assert ns["_stats_compute_response"].calls == 0 and not ns["_STATS_WARM"]["started"]


def test_a_warm_that_stores_nothing_is_an_error_and_is_not_retried(lifted):
    """The real slow path swallows its own errors and answers from the
    degradation cache WITHOUT storing the memo — nothing raises."""
    ns = _ns(lifted, store=False)
    _get(ns)
    _join_warm()
    assert "without storing the memo" in ns["_STATS_WARM"]["error"]
    r, _ = _get(ns)
    _join_warm()
    assert r.headers["X-Cache"] == "BOOT", "the window still ends on its own clock"
    assert ns["_stats_compute_response"].calls == 1, "one attempt per process, no retry storm"


def test_a_warm_that_raises_is_contained(lifted):
    ns = _ns(lifted, raise_=RuntimeError("db unreachable"))
    r, _ = _get(ns)
    _join_warm()
    assert r.status_code == 200 and r.headers["X-Cache"] == "BOOT"
    assert ns["_STATS_WARM"]["error"].startswith("RuntimeError: db unreachable")


def test_outside_the_window_the_request_runs_the_same_function(lifted):
    """The warmer and a post-boot request share ONE slow path — no copy to drift."""
    ns = _ns(lifted, age_s=1000)
    r, _ = _get(ns)
    _join_warm()
    assert r.headers["X-Cache"] == "MISS" and ns["_stats_compute_response"].calls == 1
    assert not ns["_STATS_WARM"]["started"], "no warm once the process is past the window"


def test_the_split_kept_the_side_effects_in_the_function_the_warmer_calls():
    tree = ast.parse(open(_MAIN, encoding="utf-8").read())
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    slow = ast.unparse(fns["_stats_compute_response"])
    assert '_STATS_CACHE["value"] = result' in slow or "_STATS_CACHE['value'] = result" in slow
    assert "cache_for_degradation('v1_stats', result)" in slow
    assert "get_read_db()" in slow
    assert "get_read_db()" not in ast.unparse(fns["get_stats"]), "slow path must not be duplicated"
