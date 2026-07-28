"""Shell #38 lane 4 — get_climate_intel latency + static-source cache.

WHY THIS TEST EXTRACTS CODE WITH AST INSTEAD OF IMPORTING IT:
`import site_planner` transitively imports main.py, which opens DB pools, starts
keepalive threads and registers ~200 blueprints — the documented house rule is
that tests NEVER import main. So this pulls the REAL `site_planner_climate_intel`
function plus the REAL `_ci_key/_ci_rkey/_ci_get/_ci_put` helpers out of the
source with ast, strips the decorators, and executes them against stub upstreams.
It is the shipped code that runs here, not a re-implementation of it.

Mutation-checked: forcing the two branches back to sequential makes the PARALLEL
assertion fail (~1,500ms against the 1,350ms bound).

★ HISTORY — WHY EVERY STATEMENT HERE LIVES INSIDE A FUNCTION:
this file originally shipped as a standalone script: a module body that printed
a report and ended in a bare `sys.exit(1 if fail else 0)`. Because it sits in
tests/ under a `test_` prefix, pytest imported it at COLLECTION time, the module
body ran, and the SystemExit aborted the entire session — INTERNALERROR, exit 3,
zero of the ~2,285 tests collected. The backend had no unit-test gate at all for
the window that bug was on main, and it was invisible because every PR failed
identically to main. Module scope must stay side-effect-free.

Run: python3 -m pytest tests/test_climate_intel_cache.py -v
"""
import ast
import datetime
import functools
import json
import logging
import math
import os
import sys
import time
import types
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, 'site_planner.py')

# Every stubbed upstream fetch costs this much wall time. The route makes three:
# the seismic branch (1 call) runs concurrently with the NOAA branch (2 calls,
# sequential inside the branch — StnData needs the station id StnMeta mints).
# So parallel ≈ 2×DELAY = 1.00s and sequential ≈ 3×DELAY = 1.50s.
UPSTREAM_DELAY_S = 0.5
PARALLEL_BOUND_S = 1.35     # sits between the two; only a serial route trips it

COLD = (39.0, -77.5)        # near the stubbed NOAA station
FAR = (33.45, -112.07)      # ~3,000km away — outside any sane radius


@functools.lru_cache(maxsize=1)
def _extract():
    """Compile the real cache helpers + climate route out of site_planner.py.

    Lazy + cached: parsing a 2MB source file is not something to do at import
    time in a file pytest collects.
    """
    with open(SOURCE) as fh:
        tree = ast.parse(fh.read())
    reg = next(n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == 'register_site_planner_routes')

    want_helpers = {'_ci_key', '_ci_rkey', '_ci_get', '_ci_put'}
    want_consts = {'_CI_CACHE', '_CI_TTL_S', '_CI_MAX'}
    helpers, route, assigns = [], None, []
    for st in reg.body:
        if isinstance(st, ast.FunctionDef) and st.name in want_helpers:
            helpers.append(st)
        elif isinstance(st, ast.FunctionDef) and st.name == 'site_planner_climate_intel':
            route = st
        elif isinstance(st, ast.Assign) and getattr(st.targets[0], 'id', '') in want_consts:
            assigns.append(st)

    # These three assertions are the canary: if the cache is refactored out from
    # under the test, it fails loudly here instead of silently testing nothing.
    assert route is not None, 'site_planner_climate_intel not found in site_planner.py'
    assert {h.name for h in helpers} == want_helpers, \
        f'cache helpers changed: found {sorted(h.name for h in helpers)}, want {sorted(want_helpers)}'
    assert len(assigns) == len(want_consts), \
        f'cache constants changed: found {len(assigns)} of {sorted(want_consts)}'

    route.decorator_list = []          # strip @app.route / @require_pro
    mod = ast.Module(body=assigns + helpers + [route], type_ignores=[])
    ast.fix_missing_locations(mod)
    return compile(mod, '<extracted from site_planner.py>', 'exec')


class _FakeResponse:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, body):
        self._body = body

    def read(self, *a):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_redis_cache(store):
    """Stand-in for the redis_cache module — the L2 that 6fe71245 added.

    Round-trips through JSON exactly like the real one, so a value served from
    L2 is compared in the same shape production would serve it.
    """
    mod = types.ModuleType('redis_cache')

    def cache_get(key):
        raw = store.get(key)
        return json.loads(raw) if raw is not None else None

    def cache_set(key, data, ttl=300):
        store[key] = json.dumps(data, default=str)
        return True

    mod.cache_get, mod.cache_set = cache_get, cache_set
    return mod


class _Env:
    """One isolated instance of the extracted route + its caches."""

    def __init__(self, ns, state, l2):
        self.ns = ns
        self.state = state
        self.l2 = l2                       # the fake shared/Redis layer
        self.handler = ns['site_planner_climate_intel']

    def call(self, lat, lon):
        """Invoke the route. Returns (body, wall_seconds, upstream_call_count)."""
        self.ns['flask_request'].values = {'lat': lat, 'lon': lon}
        self.state['calls'] = 0
        t0 = time.time()
        body = self.handler()
        return body, time.time() - t0, self.state['calls']

    def set_upstream_up(self, up):
        self.state['up'] = up

    def drop_l1(self):
        """Simulate the next request landing on a different, cold replica."""
        self.ns['_CI_CACHE'].clear()


@pytest.fixture
def env(monkeypatch):
    """A fresh route + empty L1/L2 caches per test — no cross-test ordering."""
    state = {'calls': 0, 'up': True}

    def fake_urlopen(req, timeout=None):
        state['calls'] += 1
        time.sleep(UPSTREAM_DELAY_S)
        if not state['up']:
            raise RuntimeError('upstream down')
        url = req.full_url
        if 'earthquake.usgs.gov' in url:
            return _FakeResponse(json.dumps(
                {'response': {'data': {'pga': 0.12, 'ss': 0.3, 's1': 0.1, 'sdc': 'C'}}}).encode())
        if 'StnMeta' in url:
            return _FakeResponse(json.dumps(
                {'meta': [{'name': 'TEST', 'll': [-77.5, 39.0], 'sids': ['449999 2']}]}).encode())
        return _FakeResponse(json.dumps({'data': [['2024', '1500', '99']]}).encode())

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)

    l2 = {}
    monkeypatch.setitem(sys.modules, 'redis_cache', _fake_redis_cache(l2))
    monkeypatch.delenv('CLIMATE_INTEL_CACHE', raising=False)

    captured = {}

    def jsonify(o=None, **kw):
        captured['body'] = o if o is not None else kw
        return captured['body']

    class _Req:
        def __init__(self):
            self.values = {}

        def get_json(self, silent=False):
            return dict(self.values)

    ns = {
        'os': os, 'time': time, 'math': math, 'json': json,
        'logger': logging.getLogger('test.climate_intel'),
        'jsonify': jsonify, 'flask_request': _Req(),
        'datetime': datetime.datetime,
    }
    exec(_extract(), ns)
    return _Env(ns, state, l2)


def test_cold_call_parses_both_federal_sources(env):
    """A cold call returns a 200-shaped body with both sources declared available."""
    body, _, calls = env.call(*COLD)
    assert body.get('success') is True
    assert body['seismic_hazard_usgs']['status'] == 'available'
    assert body['seismic_hazard_usgs']['peak_ground_acceleration_g'] == 0.12
    assert body['climate_normals_noaa']['status'] == 'available'
    assert calls == 3, f'expected 3 upstream fetches on a cold call, got {calls}'


def test_seismic_and_noaa_branches_run_concurrently(env):
    """★ The two independent branches must overlap: wall time is max(a,b), not a+b.

    Mutation-checked — reverting the ThreadPoolExecutor to sequential calls makes
    this ~1,500ms and the assertion fails.
    """
    _, elapsed, calls = env.call(*COLD)
    assert calls == 3
    assert elapsed < PARALLEL_BOUND_S, (
        f'cold call took {elapsed * 1000:.0f}ms — over the {PARALLEL_BOUND_S * 1000:.0f}ms '
        f'bound, i.e. the USGS and NOAA branches ran sequentially '
        f'(~{3 * UPSTREAM_DELAY_S * 1000:.0f}ms) instead of concurrently '
        f'(~{2 * UPSTREAM_DELAY_S * 1000:.0f}ms)')


def test_warm_call_serves_from_cache_without_touching_upstream(env):
    """A repeat of the same coordinate hits zero upstreams and is byte-identical."""
    first, _, _ = env.call(*COLD)
    seismic, climate = first['seismic_hazard_usgs'], first['climate_normals_noaa']

    second, warm, calls = env.call(*COLD)
    assert calls == 0, f'warm call still fetched {calls} upstreams'
    assert second['seismic_hazard_usgs'] == seismic
    assert second['climate_normals_noaa'] == climate
    assert warm < UPSTREAM_DELAY_S, f'warm call took {warm * 1000:.0f}ms — it fetched something'


def test_l2_survives_a_cold_replica(env):
    """★ The cache must be SHARED, not per-process.

    The in-process dict alone measured ZERO hits in production (10/10 live calls
    missed) because the backend runs several replicas and a repeat request lands
    on a cold one — that is what 6fe71245 fixed by backing it with Redis. Drop
    L1 to stand in for a different replica; L2 must still answer.
    """
    first, _, _ = env.call(*COLD)
    assert env.l2, 'nothing was written to the shared (Redis) layer — L2 is inert'

    env.drop_l1()
    second, _, calls = env.call(*COLD)
    assert calls == 0, f'a cold replica re-fetched {calls} upstreams — L2 is not shared'
    assert second['seismic_hazard_usgs'] == first['seismic_hazard_usgs']
    assert second['climate_normals_noaa'] == first['climate_normals_noaa']


def test_a_failure_is_never_cached(env):
    """★ A cached 'unavailable' would pin a permanent hole in the data.

    Same lesson as the MCP keyCache, which refuses to cache a validation failure
    because it silently downgrades a paid tier to free.
    """
    env.set_upstream_up(False)
    body, _, _ = env.call(1.0, 1.0)
    assert body['seismic_hazard_usgs']['status'] == 'unavailable', \
        'a dead upstream must be declared unavailable, never estimated'

    _, _, calls = env.call(1.0, 1.0)
    assert calls > 0, 'the failure was cached — that coordinate is now a pinned hole'


def test_kill_switch_bypasses_the_cache_without_a_deploy(env, monkeypatch):
    """CLIMATE_INTEL_CACHE=0 turns the cache off on a warm coordinate."""
    _, _, cold_calls = env.call(*COLD)
    assert cold_calls == 3

    monkeypatch.setenv('CLIMATE_INTEL_CACHE', '0')
    _, _, calls = env.call(*COLD)
    assert calls == 3, f'kill switch did not bypass the cache (got {calls} upstream fetches)'


def test_distinct_coordinates_do_not_collide(env):
    """A different lat/lon misses; an out-of-radius answer is declared, not cached."""
    env.call(*COLD)

    far, _, far_calls = env.call(*FAR)
    assert far_calls >= 2, f'a distant coordinate served the cached answer (got {far_calls})'
    assert far['climate_normals_noaa']['status'] == 'unavailable_exceeds_radius', \
        'beyond the radius the normals must be declared unavailable, never interpolated'

    # Seismic succeeded for FAR so it caches; the exceeds-radius NOAA answer must
    # NOT — so the repeat costs exactly the one StnMeta lookup.
    _, _, repeat_calls = env.call(*FAR)
    assert repeat_calls == 1, (
        f'expected 1 upstream (seismic cached, out-of-radius NOAA deliberately not) '
        f'— got {repeat_calls}')

    _, _, original_calls = env.call(*COLD)
    assert original_calls == 0, 'the original coordinate lost its cache entry'
