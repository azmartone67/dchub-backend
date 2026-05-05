#!/usr/bin/env bash
# Iteration 5 — edge search via Cloudflare worker (Vectorize + AI bindings).
# Patches /home/runner/workspace/worker.js to add /api/v1/search/edge with
# iteration 4 filter parity, then deploys via your existing deploy-v4.7.sh.
set -e
cd ~/workspace

# 1) Drop the helpers JS file
echo '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' | base64 -d | tar xzf -

# 2) Patch worker.js: insert helpers before `export default`, add route check at top of fetch()
python3 - <<'PYPATCH'
import pathlib, re
p = pathlib.Path('worker.js')
src = p.read_text()

if 'IT5_GRID_TERRITORIES' in src:
    print('iteration 5 already patched in worker.js')
else:
    helpers = pathlib.Path('iteration5_helpers.js').read_text()

    # Insertion 1: helpers right before `export default {`
    new = src.replace('export default {', helpers + '\n\nexport default {', 1)
    if new == src:
        print('FAIL: could not find `export default {` in worker.js')
        raise SystemExit(1)
    src = new

    # Insertion 2: route check at top of fetch handler
    route_block = '''    // === Iteration 5 routes (edge fast-path) ===
    {
      const _it5_url = new URL(request.url);
      if (_it5_url.pathname === '/api/v1/search/edge')        return it5HandleEdgeSearch(request, env);
      if (_it5_url.pathname === '/api/v1/search/grids/edge')  return it5HandleGrids();
    }
    // === end iteration 5 routes ===
'''
    pat = re.compile(r'(async fetch\(request, env, ctx\)\s*\{\n)', re.M)
    new, n = pat.subn(r'\1' + route_block, src, count=1)
    if n != 1:
        print('FAIL: could not match async fetch(request, env, ctx) {')
        raise SystemExit(1)
    p.write_text(new)
    print(f'Patched worker.js (now {len(new.splitlines())} lines, was {len(src.splitlines())})')
PYPATCH

# 3) Sanity check the patches landed
echo ""
echo "=== Helpers + routes present? ==="
grep -nE "IT5_GRID_TERRITORIES|it5HandleEdgeSearch|search/edge" worker.js | head -8

# 4) Deploy via existing script
echo ""
echo "=== Deploying via deploy-v4.7.sh ==="
bash deploy-v4.7.sh

# 5) Verify edge endpoints
echo ""
echo "=== Sleeping 15s for edge propagation, then verifying ==="
sleep 15
echo ""
echo "--- /api/v1/search/grids/edge ---"
curl -s 'https://dchub.cloud/api/v1/search/grids/edge' | python3 -m json.tool | head -20
echo ""
echo "--- /api/v1/search/edge with PJM filter ---"
curl -s 'https://dchub.cloud/api/v1/search/edge?q=hyperscale&grid=PJM&min_mw=30&topK=5' | python3 -c "
import sys, json
d = json.load(sys.stdin)
if 'matches' in d:
    print(f'Query: {d.get(\"query\")}')
    print(f'Runtime: {d.get(\"runtime\")}')
    print(f'Filters: {d.get(\"filters\")}')
    print(f'Filter stats: {d.get(\"filter_stats\")}')
    print(f'Timing: {d.get(\"timing_ms\")}')
    print()
    for m in d['matches']:
        print(f\"  {m['score']:.3f}  {m.get('name','?'):50s}  {m.get('state','?'):4s}  {m.get('power_mw',0)}MW\")
else:
    print(json.dumps(d, indent=2))
"
