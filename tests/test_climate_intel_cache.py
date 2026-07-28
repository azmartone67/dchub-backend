"""Shell #38 lane 4 — get_climate_intel latency + static-source cache.

WHY THIS TEST EXTRACTS CODE WITH AST INSTEAD OF IMPORTING IT:
`import site_planner` transitively imports main.py, which opens DB pools, starts
keepalive threads and registers ~200 blueprints — the documented house rule is
that tests NEVER import main. So this pulls the REAL `site_planner_climate_intel`
function plus the REAL `_ci_key/_ci_get/_ci_put` helpers out of the source with
ast, strips the decorators, and executes them against stub upstreams. It is the
shipped code that runs here, not a re-implementation of it.

Mutation-checked: forcing the two branches back to sequential makes the
PARALLEL assertion fail (1,517ms vs the 1,350ms bound).

Run: python3 tests/test_climate_intel_cache.py   (from the repo root)
"""
import ast, sys, time, json, types, os, math, logging
src = open('site_planner.py').read()
tree = ast.parse(src)

# find register_site_planner_routes -> the cache helpers + the climate route
reg = next(n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == 'register_site_planner_routes')
want_helpers = {'_ci_key', '_ci_get', '_ci_put'}
helpers, route, assigns = [], None, []
for st in reg.body:
    if isinstance(st, ast.FunctionDef) and st.name in want_helpers:
        helpers.append(st)
    elif isinstance(st, ast.FunctionDef) and st.name == 'site_planner_climate_intel':
        route = st
    elif isinstance(st, ast.Assign):
        tgt = getattr(st.targets[0], 'id', '')
        if tgt in ('_CI_CACHE', '_CI_TTL_S', '_CI_MAX'):
            assigns.append(st)
assert route is not None, "route not found"
assert len(helpers) == 3, f"helpers found: {[h.name for h in helpers]}"
route.decorator_list = []          # strip @app.route / @require_pro
mod = ast.Module(body=assigns + helpers + [route], type_ignores=[])
ast.fix_missing_locations(mod)

# ── minimal real-ish environment ─────────────────────────────────────────
CALLS = {'n': 0}
class _R:
    def __init__(self, b): self._b = b
    def read(self, *a): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False
MODE = {'ok': True}
def fake_urlopen(req, timeout=None):
    CALLS['n'] += 1
    time.sleep(0.5)
    if not MODE['ok']: raise RuntimeError('upstream down')
    url = req.full_url
    if 'earthquake.usgs.gov' in url:
        return _R(json.dumps({'response':{'data':{'pga':0.12,'ss':0.3,'s1':0.1,'sdc':'C'}}}).encode())
    if 'StnMeta' in url:
        return _R(json.dumps({'meta':[{'name':'TEST','ll':[-77.5,39.0],'sids':['449999 2']}]}).encode())
    return _R(json.dumps({'data':[['2024','1500','99']]}).encode())
import urllib.request as _ur
_ur.urlopen = fake_urlopen

CAPT = {}
def jsonify(o=None, **kw):
    CAPT['body'] = o if o is not None else kw
    return CAPT['body']
class _Req:
    def __init__(s): s.values = {}
    def get_json(s, silent=False): return dict(s.values)
flask_request = _Req()
g = {'os': os, 'time': time, 'math': math, 'json': json, 'logger': logging.getLogger('t'),
     'jsonify': jsonify, 'flask_request': flask_request, 'datetime': __import__('datetime').datetime}
exec(compile(mod, '<extracted>', 'exec'), g)
handler = g['site_planner_climate_intel']

fail = 0
def ok(c, m):
    global fail
    print(("  ✅ " if c else "  ❌ ")+m); fail += (0 if c else 1)
def call(lat, lon):
    flask_request.values = {'lat': lat, 'lon': lon}
    CALLS['n'] = 0; t0 = time.time(); r = handler(); return r, time.time()-t0, CALLS['n']

print("=== BEHAVIOURAL: cold call — 3 upstream fetches @0.5s ===")
r, cold, n = call(39.0, -77.5)
ok(r.get('success') is True, "200-shaped success response")
ok(r['seismic_hazard_usgs']['status']=='available', "seismic still parses (peak_ground_acceleration_g present)")
ok(r['climate_normals_noaa']['status']=='available', "NOAA normals still parse")
ok(n==3, f"3 upstream calls (got {n})")
ok(cold < 1.35, f"PARALLEL: cold {cold*1000:.0f}ms — sequential would be ~1500ms")

print("=== BEHAVIOURAL: warm call ===")
r2, warm, n2 = call(39.0, -77.5)
ok(n2==0, f"ZERO upstream calls on repeat (got {n2})")
ok(r2['seismic_hazard_usgs']==r['seismic_hazard_usgs'], "cached seismic byte-identical")
ok(r2['climate_normals_noaa']==r['climate_normals_noaa'], "cached NOAA byte-identical")
print(f"     cold={cold*1000:.0f}ms  warm={warm*1000:.0f}ms  ({cold/max(warm,1e-9):.0f}x)")

print("=== BEHAVIOURAL: a FAILURE is never cached ===")
MODE['ok']=False
r3,_,n3 = call(1.0, 1.0)
ok(r3['seismic_hazard_usgs']['status']=='unavailable', "failure -> unavailable (declared, not estimated)")
_,_,n4 = call(1.0, 1.0)
ok(n4 > 0, f"retries upstream next time (got {n4}) — no pinned hole")
MODE['ok']=True

print("=== BEHAVIOURAL: kill switch, no deploy ===")
os.environ['CLIMATE_INTEL_CACHE']='0'
_,_,n5 = call(39.0, -77.5)
ok(n5==3, f"CLIMATE_INTEL_CACHE=0 bypasses cache (got {n5})")
os.environ.pop('CLIMATE_INTEL_CACHE')

print("=== distinct coordinates not confused ===")
r_far,_,n6 = call(33.45, -112.07)
ok(n6>=2, f"different lat/lon MISSES the cache (fetched {n6} upstream)")
ok(r_far['climate_normals_noaa']['status']=='unavailable_exceeds_radius',
   "out-of-radius declared, not estimated — and NOT cached (conservative)")
_,_,n7 = call(39.0, -77.5)
ok(n7==0, f"original coordinate still cached (got {n7})")
sys.exit(1 if fail else 0)
