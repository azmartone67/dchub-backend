"""Energy report stale-while-revalidate (2026-09-25).

/reports/energy/quarterly paid a 17s foreground rebuild (4 leaderboard + 5 grid
fetches + an LLM narrative) once an hour, because every cache under it expired
on the same clock. `_full_report` now serves the last good copy and refreshes
it in the background. These tests execute the shipped functions (pulled out of
the source with `ast`, like the rest of the suite) against stubs.
"""
import ast
import pathlib
import threading
import time
import types

SRC = pathlib.Path(__file__).resolve().parents[1] / "routes" / "energy_report.py"
NAMES = {"_swr_key", "_swr_read", "_swr_build", "_swr_refresh_in_background",
         "_full_report"}


class _Redis:
    def __init__(self):
        self.store = {}

    def cache_get(self, key):
        return self.store.get(key)

    def cache_set(self, key, data, ttl=300):
        self.store[key] = data
        return True


def _load(monkeypatch, gather, redis=None):
    tree = ast.parse(SRC.read_text())
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in NAMES]
    assert {f.name for f in fns} == NAMES, "SWR helpers missing from energy_report.py"
    redis = redis or _Redis()
    started = []

    class _Thread:
        def __init__(self, target, name=None, daemon=None):
            self.target = target
            started.append(self)

        def start(self):
            pass  # run explicitly from the test, so ordering is deterministic

    ns = {
        "time": time,
        "threading": types.SimpleNamespace(Lock=threading.Lock, Thread=_Thread),
        "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
        "_GATHER_TTL": 3600,
        "_SWR_KEEP_SECONDS": 7 * 24 * 3600,
        "_SWR_LOCAL": {},
        "_SWR_REFRESHING": set(),
        "_SWR_LOCK": threading.Lock(),
        "_gather_energy": gather,
        "_attach_narrative_safe": lambda d, kind=None: {**d, "narrative_summary": {"text": kind}},
    }
    import sys
    monkeypatch.setitem(sys.modules, "redis_cache", types.SimpleNamespace(
        cache_get=redis.cache_get, cache_set=redis.cache_set))
    exec(compile(ast.Module(body=fns, type_ignores=[]), str(SRC), "exec"), ns)
    return ns, redis, started


def test_cold_builds_inline_and_stores_last_good(monkeypatch):
    calls = []
    ns, redis, started = _load(monkeypatch, lambda w: calls.append(w) or {"v": 1})
    d = ns["_full_report"]("quarterly")
    assert d["v"] == 1 and d["narrative_summary"]["text"] == "energy_quarterly"
    assert calls == ["quarterly"] and not started
    assert redis.store["dchub:energy_report:v2:full:quarterly"]["data"]["v"] == 1


def test_fresh_copy_is_served_without_rebuilding(monkeypatch):
    calls = []
    ns, redis, started = _load(monkeypatch, lambda w: calls.append(w) or {"v": 2})
    redis.store["dchub:energy_report:v2:full:quarterly"] = {
        "data": {"v": 1}, "computed_at": time.time() - 60}
    assert ns["_full_report"]("quarterly")["v"] == 1
    assert calls == [] and not started


def test_stale_copy_is_served_immediately_and_refreshed_once_in_background(monkeypatch):
    calls = []
    ns, redis, started = _load(monkeypatch, lambda w: calls.append(w) or {"v": 2})
    redis.store["dchub:energy_report:v2:full:quarterly"] = {
        "data": {"v": 1}, "computed_at": time.time() - 7200}
    assert ns["_full_report"]("quarterly")["v"] == 1   # stale, served now
    assert ns["_full_report"]("quarterly")["v"] == 1   # no second refresh queued
    assert calls == [] and len(started) == 1
    started[0].target()                                 # the background refresh
    assert calls == ["quarterly"]
    assert ns["_full_report"]("quarterly")["v"] == 2
    assert ns["_SWR_REFRESHING"] == set()


def test_poisoned_gather_is_never_stored_as_last_good(monkeypatch):
    ns, redis, _ = _load(monkeypatch, lambda w: {"v": 9, "_partial_cache_poison": True})
    assert ns["_full_report"]("monthly")["v"] == 9
    assert "dchub:energy_report:v2:full:monthly" not in redis.store
    assert "monthly" not in ns["_SWR_LOCAL"]


def test_every_report_route_goes_through_full_report():
    src = SRC.read_text()
    assert src.count('_full_report("quarterly")') == 4
    assert src.count('_full_report("monthly")') == 4
    assert src.count("_attach_narrative_safe(_gather_energy(") == 1  # _swr_build only
