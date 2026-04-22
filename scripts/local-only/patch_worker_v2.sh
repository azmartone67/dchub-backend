#!/usr/bin/env bash
set -u
cd ~/dchub-frontend || exit 1

# Clean up any stale .bak from the previous failed run so git is tidy
rm -f _worker.js.bak

git checkout main
git pull --rebase origin main

cp _worker.js _worker.js.bak

python3 <<'PY'
import pathlib, sys, re
p = pathlib.Path('_worker.js')
src = p.read_text()

# --- PATCH 1: inside handleNewsRoute, alias /press-release -> /news ---
# Regex: match the function declaration + the opening brace + any whitespace
# up to the first line of body. Insert the alias right after the {.
pat1 = re.compile(
    r'(async function handleNewsRoute\(pathname, request, env\)\s*\{[^\n]*\n)'
)
ins1 = r'''\1  // v4.5.13: alias /press-release* -> /news* (same renderer handles both)
  if (pathname.startsWith('/press-release')) {
    pathname = pathname.replace(/^\/press-release/, '/news');
  }
'''
src2, n1 = pat1.subn(ins1, src, count=1)
if n1 != 1:
    print(f'PATCH 1 FAIL - expected 1 match, got {n1}', file=sys.stderr)
    # debug: show context around handleNewsRoute
    m = re.search(r'.{0,80}handleNewsRoute.{0,200}', src, re.DOTALL)
    if m: print('CONTEXT:', repr(m.group(0)[:400]), file=sys.stderr)
    sys.exit(2)
src = src2
print('PATCH 1 OK (handleNewsRoute alias injected)')

# --- PATCH 2: main fetch dispatcher - expand /news check to also match /press-release ---
# Find the dispatcher's startsWith('/news') check. There may be more than one
# occurrence of "startsWith('/news')" in the file; replace them all that don't
# already include '/press-release' in the same line.
pat2 = re.compile(r"(if\s*\(\s*pathname\.startsWith\(\s*'/news'\s*\)\s*\))")
def repl2(m):
    return "if (pathname.startsWith('/news') || pathname.startsWith('/press-release'))"
src2, n2 = pat2.subn(repl2, src)
if n2 == 0:
    print('PATCH 2 FAIL - no dispatcher match found', file=sys.stderr); sys.exit(2)
src = src2
print(f'PATCH 2 OK ({n2} dispatcher occurrence(s) expanded)')

# --- PATCH 3: bump version ---
src2, n3 = re.subn(r"'4\.5\.12'", "'4.5.13'", src, count=1)
if n3: print('PATCH 3 OK (version bumped to 4.5.13)')
src = src2

p.write_text(src)
print('wrote _worker.js')
PY

PYRC=$?
if [ $PYRC -ne 0 ]; then
  echo "Python patch failed (exit $PYRC). Restoring backup."
  cp _worker.js.bak _worker.js
  exit $PYRC
fi

echo "=== sanity: alias line present? ==="
grep -n "alias /press-release" _worker.js || echo "MISSING"
echo "=== sanity: dispatcher expanded? ==="
grep -n "pathname.startsWith('/press-release')" _worker.js | head -5
echo "=== sanity: version bumped? ==="
grep -n "4\.5\.1[23]" _worker.js | head -3

# Optional: JS syntax check if node exists
if command -v node >/dev/null 2>&1; then
  node --check _worker.js && echo "node --check OK" || { echo "SYNTAX ERROR - restoring backup"; cp _worker.js.bak _worker.js; exit 3; }
fi

git diff --stat _worker.js
git add _worker.js
git rm --cached _worker.js.bak 2>/dev/null || true
rm -f _worker.js.bak
echo "_worker.js.bak" >> .gitignore
git add .gitignore

git commit -m "_worker.js v4.5.13: route /press-release* through handleNewsRoute

/press-release and /press-release/<slug> were hitting the worker's
403 fallback. The buildPressReleaseHtml renderer already handles this
content under /news/<slug>. Two edits:

1. Top of handleNewsRoute: rewrite /press-release* -> /news* so every
   existing branch (archive, slug, redirect) applies unchanged.
2. Main dispatcher: expand startsWith('/news') to also match /press-release.

No new render code, no _routes.json juggling, no functions/ dir."

git push origin main
echo ""
echo "=== DONE. CF Pages auto-deploys in ~60s ==="
echo "Test: sleep 70 && curl -sI https://dchub.cloud/press-release | head -5"
