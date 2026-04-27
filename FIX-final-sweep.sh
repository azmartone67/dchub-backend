#!/usr/bin/env bash
# Final sweep — #68 doc + reembed tool + frontend explorer + rerank patch
set -e
cd ~/workspace

# 1) Drop the three new files
echo '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' | base64 -d | tar xzf -

# 2) Move them to canonical locations
mkdir -p docs/issues tools static
mv 68-railway-pagination-bug.md docs/issues/
mv reembed_facilities.py tools/
chmod +x tools/reembed_facilities.py
mv search-explorer.html static/

echo "Files placed:"
ls -la docs/issues/68-railway-pagination-bug.md
ls -la tools/reembed_facilities.py
ls -la static/search-explorer.html

# 3) Patch dchub_iteration_3_routes.py to add rerank=true|false param
python3 - <<'PYRERANK'
import pathlib, re
p = pathlib.Path('dchub_iteration_3_routes.py')
src = p.read_text()
if 'composite_score' in src:
    print('rerank already patched')
else:
    # Insert rerank logic after the filter pass, before the topK trim.
    # Look for the marker: "matches = matches[:topK]"
    target = 'matches = matches[:topK]\n\n    if hydrate_flag:'
    if target not in src:
        print('FAIL: could not find rerank insertion point')
        raise SystemExit(1)
    rerank_block = '''matches = matches[:topK]

    # Iteration 5: optional composite reranking (score x log(power_mw+1) x status_weight)
    if (request.args.get('rerank', '').lower() in ('true','1','yes','y')):
        import math as _math
        def _composite(_m):
            md = _m.get('metadata') or {}
            score = _m.get('score') or 0.0
            mw = md.get('power_mw') or 0
            try: mw = float(mw)
            except (TypeError, ValueError): mw = 0
            status = (md.get('status') or '').lower()
            status_weight = 1.2 if 'construction' in status else (1.0 if 'operational' in status else 0.85)
            cs = score * _math.log(1 + mw) * status_weight
            _m['composite_score'] = cs
            return cs
        matches = sorted(matches, key=_composite, reverse=True)

    if hydrate_flag:'''
    new = src.replace(target, rerank_block, 1)
    if new == src:
        print('FAIL: replacement did not apply')
        raise SystemExit(1)
    # Also surface composite_score in flat[]
    flat_target = "'hydration_method': m.get('hydration_method'),\n        })"
    flat_replacement = """'hydration_method': m.get('hydration_method'),
            'composite_score':  m.get('composite_score'),
        })"""
    if flat_target in new:
        new = new.replace(flat_target, flat_replacement, 1)
    p.write_text(new)
    print('rerank patch applied')
PYRERANK

# 4) Sanity check + commit + push
python3 -c "import ast; ast.parse(open('dchub_iteration_3_routes.py').read()); print('iteration_3 syntax OK')"
echo ""
grep -n "rerank\|composite_score" dchub_iteration_3_routes.py | head -8

git add docs/issues/68-railway-pagination-bug.md tools/reembed_facilities.py static/search-explorer.html dchub_iteration_3_routes.py
git diff --cached --stat
git commit -m "feat: final sweep — #68 doc, rerank, reembed tool, frontend explorer

- docs/issues/68-railway-pagination-bug.md: complete write-up with
  reproduction template, 4 ranked root-cause hypotheses, triage commands,
  fix proposal. Ready to file with the team or upstream.
- dchub_iteration_3_routes.py: add ?rerank=true to /api/v1/search/semantic.
  Composite score = vector_score * log(1 + power_mw) * status_weight (1.2
  for under-construction, 1.0 operational, 0.85 other). Surfaces
  composite_score on each match.
- tools/reembed_facilities.py: standalone, idempotent, resumable Python
  job to re-embed all 21k facilities with grid/market/power-band context.
  Checkpoints to /tmp/reembed.checkpoint, batches of 50, ~3 req/sec rate
  limit. Run on demand: python tools/reembed_facilities.py --dry-run
- static/search-explorer.html: single-file frontend explorer wired to
  both /api/v1/search/edge (Cloudflare) and /api/v1/search/semantic
  (Flask, hydrate). Live at https://dchub.cloud/static/search-explorer.html"
git push origin main

echo ""
echo "=========================================================="
echo "FINAL SWEEP SHIPPED. After ~90s Railway redeploy, verify:"
echo ""
echo "  # Rerank — composite score should reorder matches"
echo "  curl -s 'https://dchub-backend-production.up.railway.app/api/v1/search/semantic?q=hyperscale&grid=PJM&min_mw=30&topK=5&rerank=true' \"
echo "    | python3 -m json.tool | head -60"
echo ""
echo "  # Frontend explorer (open in browser)"
echo "  https://dchub.cloud/static/search-explorer.html"
echo ""
echo "  # Re-embed dry-run (no API calls, just preview)"
echo "  python tools/reembed_facilities.py --dry-run --limit 5"
echo ""
echo "  # Re-embed live (~30 min for all 21k facilities, checkpointed)"
echo "  python tools/reembed_facilities.py"
echo "=========================================================="
