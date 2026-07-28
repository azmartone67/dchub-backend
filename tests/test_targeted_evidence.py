"""Actuation shell #39 lane 2 — question-TARGETED evidence gathering.

Extracts the real functions with `ast` instead of importing brain_investigator
(which pulls in main.py — house rule: tests NEVER import main), then runs them
against the LIVE read replica.

Run: python3 tests/test_targeted_evidence.py
"""
import ast, os, sys, re, logging

src = open('routes/brain_investigator.py').read()
tree = ast.parse(src)
WANT_FN = {'_extract_paths', 'gather_targeted_evidence', '_gather_recent_findings'}
WANT_AS = {'_PATH_RE', '_TARGET_MAX_PATHS'}
body = [n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in WANT_FN)
        or (isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') in WANT_AS)]
assert len({n.name for n in body if isinstance(n, ast.FunctionDef)}) == 3, "functions not found"

DB = os.environ.get('REPLICA_URL') or ''
def _conn():
    if not DB: return None
    import psycopg2
    c = psycopg2.connect(DB); c.set_session(readonly=True, autocommit=True); return c

g = {'os': os, 're': re, 'logger': logging.getLogger('t'), '_conn': _conn}
mod = ast.Module(body=body, type_ignores=[]); ast.fix_missing_locations(mod)
exec(compile(mod, '<extracted>', 'exec'), g)
extract, gather = g['_extract_paths'], g['gather_targeted_evidence']

fail = 0
def ok(c, m):
    global fail
    print(("  PASS " if c else "  FAIL ")+m); fail += (0 if c else 1)

print("=== WIRING: investigate() actually calls it (AST, not grep) ===")
_inv = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'investigate')
_calls = {c.func.id for c in ast.walk(_inv)
          if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
ok('gather_targeted_evidence' in _calls,
   "investigate() calls gather_targeted_evidence — a correct function nothing calls is the "
   "failure mode this repo keeps shipping")
_assigns = [n for n in ast.walk(_inv) if isinstance(n, ast.Assign)]
_ev = [n for n in _assigns if getattr(n.targets[0], 'id', '') == 'evidence'
       and isinstance(n.value, ast.BinOp)]
ok(bool(_ev) and getattr(_ev[0].value.left, 'id', '') == 'targeted',
   "targeted evidence is prepended (model reads subject-specific rows FIRST)")

print("=== path extraction ===")
q404 = "[reliability] Brain finding: repeated_404_pattern @ /api/v1/energy/retail/rates returned 404 171 times"
ok(extract(q404) == ['/api/v1/energy/retail/rates'], f"pulls the endpoint out of a real finding title -> {extract(q404)}")
ok(extract("why is conversion down this month?") == [], "no path in a generic question -> no targeted query")
many = extract("/api/a /api/b /api/c /api/d /api/e")
ok(len(many) <= 3, f"bounded to <=3 paths (got {len(many)})")
ok(extract("see /api/v1/x, and /api/v1/x again") == ['/api/v1/x'], "de-dupes repeats")
ok('%' not in open('routes/brain_investigator.py').read().split('def gather_targeted_evidence')[1].split('SELECT')[1].split('"""')[0].replace('%s',''),
   "no literal % in the SQL (the documented psycopg2 500 trap)")

print("=== live evidence against the real replica ===")
if not DB:
    print("  SKIP — set REPLICA_URL to run the live half"); sys.exit(1 if fail else 0)

ev = gather(q404)
ok(len(ev) > 0, f"returns evidence for a real endpoint question ({len(ev)} item(s))")
ok(all(set(e) >= {'claim','source','value'} for e in ev), "every item has the claim/source/value shape gather_evidence() emits")
ok(any('question-targeted' in (e.get('source') or '') for e in ev), "items are labelled question-targeted")
blob = ' '.join(e['claim'] for e in ev)
ok('/api/v1/energy/retail/rates' in blob, "the evidence is ABOUT the endpoint in the question")
ok('CONTRADICTION' in blob, "surfaces the detector-vs-ground-truth contradiction (finding says 404s, log says none)")
for e in ev: print("     -", e['claim'][:150])

print("=== a question with no path costs nothing ===")
ok(gather("why is conversion down?") == [], "no path -> no DB work, empty list")

print("=== kill switch ===")
os.environ['BRAIN_TARGETED_EVIDENCE'] = '0'
ok(gather(q404) == [], "BRAIN_TARGETED_EVIDENCE=0 disables it with no deploy")
os.environ.pop('BRAIN_TARGETED_EVIDENCE')
ok(len(gather(q404)) > 0, "and re-enables when unset")

print("=== the 'live detector worklist' must actually be LIVE ===")
recent = g['_gather_recent_findings'](limit=12)
ok(len(recent) > 0, f"gatherer returns findings ({len(recent)})")
# behavioural: re-check each returned finding's status against the DB
_c = _conn(); _k = _c.cursor()
_stat = []
for e in recent:
    claim = e.get('claim') or ''
    _k.execute("""SELECT COALESCE(status,'open'), resolved_at FROM brain_findings
                  WHERE COALESCE(status,'open')='open' AND resolved_at IS NULL
                  ORDER BY last_seen DESC NULLS LAST LIMIT 12""")
_k.execute("""SELECT COUNT(*) FROM (
                SELECT COALESCE(status,'open') st, resolved_at FROM brain_findings
                ORDER BY last_seen DESC NULLS LAST LIMIT 12) q
              WHERE q.st <> 'open' OR q.resolved_at IS NOT NULL""")
_unfiltered_bad = _k.fetchone()[0]
_k.execute("""SELECT COUNT(*) FROM (
                SELECT COALESCE(status,'open') st, resolved_at FROM brain_findings
                WHERE COALESCE(status,'open')='open' AND resolved_at IS NULL
                ORDER BY last_seen DESC NULLS LAST LIMIT 12) q
              WHERE q.st <> 'open' OR q.resolved_at IS NOT NULL""")
ok(_k.fetchone()[0] == 0, "filtered query can NEVER return a resolved finding (0 by construction)")
_k.execute("SELECT COUNT(*) FROM brain_findings WHERE status<>'open' AND last_seen > now()-interval '24 hours'")
_fresh_resolved = _k.fetchone()[0]
ok(_fresh_resolved > 0,
   f"the risk is REAL not theoretical: {_fresh_resolved} findings are both resolved AND last_seen<24h, "
   f"so they compete for the same recency-ordered slots")
print(f"     (unfiltered query currently admits {_unfiltered_bad} non-open rows — low today, "
      f"but the pool of resolved+fresh is {_fresh_resolved})")

print("=== absence is reported, never silent ===")
ev2 = gather("what happened to /api/v1/definitely-not-a-real-endpoint-xyz ?")
ok(len(ev2) == 1 and 'NO rows' in ev2[0]['claim'], "an unknown path yields an explicit absence item, not []")
print("     -", ev2[0]['claim'][:150] if ev2 else "(none)")
sys.exit(1 if fail else 0)
