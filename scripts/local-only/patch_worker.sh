#!/usr/bin/env bash
set -u
cd ~/dchub-frontend || exit 1
git checkout main
git pull --rebase origin main

cp _worker.js _worker.js.bak

python3 <<'PY'
import pathlib, sys, re
p = pathlib.Path('_worker.js')
src = p.read_text()

# --- PATCH 1: alias /press-release -> /news inside handleNewsRoute ---
anchor1 = 'async function handleNewsRoute(pathname, request, env) {\n  // /news or /news/'
insert1 = '''async function handleNewsRoute(pathname, request, env) {
  // v4.5.13: alias /press-release* -> /news* (same renderer handles both shapes)
  if (pathname.startsWith('/press-release')) {
    pathname = pathname.replace(/^\\/press-release/, '/news');
  }
  // /news or /news/'''

if anchor1 in src:
    src = src.replace(anchor1, insert1, 1)
    print('PATCH 1 OK (handleNewsRoute alias)')
else:
    print('PATCH 1 FAIL - anchor not found', file=sys.stderr); sys.exit(2)

# --- PATCH 2: main fetch dispatcher - also match /press-release ---
# Line ~1787: if (pathname.startsWith('/news')) {
old2 = "if (pathname.startsWith('/news'))"
new2 = "if (pathname.startsWith('/news') || pathname.startsWith('/press-release'))"
n = src.count(old2)
if n == 1:
    src = src.replace(old2, new2)
    print(f'PATCH 2 OK (dispatcher, 1 occurrence)')
elif n > 1:
    # replace only the first - but warn
    src = src.replace(old2, new2, 1)
    print(f'PATCH 2 WARN ({n} occurrences found, replaced first only)')
else:
    print('PATCH 2 FAIL - call site not found', file=sys.stderr); sys.exit(2)

# --- PATCH 3: bump version marker if present ---
if "'4.5.12'" in src:
    src = src.replace("'4.5.12'", "'4.5.13'", 1)
    print('PATCH 3 OK (version bumped to 4.5.13)')

p.write_text(src)
print('wrote _worker.js')
PY

# Sanity check - file still has the old hot paths
echo "=== sanity: still has buildPressReleaseHtml? ==="
grep -c "buildPressReleaseHtml" _worker.js
echo "=== sanity: new alias present? ==="
grep -n "alias /press-release" _worker.js
echo "=== sanity: dispatcher updated? ==="
grep -n "pathname.startsWith('/press-release')" _worker.js | head -3

# Commit and push
git diff --stat _worker.js
git add _worker.js
git commit -m "_worker.js v4.5.13: route /press-release* through handleNewsRoute

/press-release and /press-release/<slug> were hitting the worker's
403 fallback because nothing routed them. The same buildPressReleaseHtml
renderer already handles this content under /news/<slug>. This patch:

1. In handleNewsRoute, aliases /press-release* -> /news* so every
   existing branch (archive, slug detail, redirect) applies.
2. In the main fetch dispatcher, expands the /news prefix check to
   also catch /press-release.

No new render code, no _routes.json juggling, no functions/ dir."

git push origin main

echo ""
echo "=== DONE. CF Pages auto-deploys in ~60s ==="
echo "Test: curl -sI https://dchub.cloud/press-release | head -5"
