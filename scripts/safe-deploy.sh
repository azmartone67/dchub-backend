#!/usr/bin/env bash
# safe-deploy.sh — pre-flight the working tree, THEN `railway up`.
#
# Why: the backend deploys from the LOCAL working tree (`railway up`), but a
# brain-autopilot loop edits the same tree concurrently. A `git stash pop` of its
# wip can leave CONFLICT MARKERS in main.py + sibling files, and a deploy of that
# tree fails to compile (it won't promote — but it burns a build cycle and looks
# like an outage). This guard makes deploys RESILIENT to that churn: it hard-
# aborts on conflict markers or a non-compiling tree, so a broken tree is never
# uploaded. Protects interactive AND autopilot deploys.
#
# Usage:
#   scripts/safe-deploy.sh [service]          # pre-flight + railway up --detach
#   scripts/safe-deploy.sh [service] --check   # pre-flight only (no deploy)
#   DEPLOY_EXTRA_COMPILE="a.py b.py" scripts/safe-deploy.sh   # also compile-check these
set -uo pipefail

SERVICE="${1:-dchub-backend}"
CHECK_ONLY="false"
[ "${2:-}" = "--check" ] && CHECK_ONLY="true"
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT" || { echo "❌ ABORT: cannot cd to repo root"; exit 1; }

fail() { echo "❌ ABORT: $1"; exit 1; }

echo "🛡️  safe-deploy pre-flight  (service=$SERVICE, root=$ROOT)"

# 1) Conflict markers in any tracked file (the exact failure that bit us 3× on
#    2026-06-21). '<<<<<<< ' and '>>>>>>> ' are unambiguous — no false positives.
MARKED="$(git grep -lE '^(<<<<<<< |>>>>>>> )' -- . 2>/dev/null || true)"
if [ -n "$MARKED" ]; then
  echo "   conflict markers in:"; echo "$MARKED" | sed 's/^/     /'
  echo "   recover with: git checkout HEAD -- <file>   (do NOT 'git stash pop' the autopilot wip)"
  fail "conflict markers present — refusing to deploy a broken tree"
fi

# 2) main.py must compile.
python3 -m py_compile main.py 2>/dev/null || fail "main.py does not compile"

# 3) Every changed-vs-HEAD python file must compile (catches a half-written edit).
CHANGED="$(git diff --name-only HEAD -- '*.py' 2>/dev/null; git diff --name-only --cached -- '*.py' 2>/dev/null)"
CHANGED="$(printf '%s\n' "$CHANGED" ${DEPLOY_EXTRA_COMPILE:-} | sort -u)"
for f in $CHANGED; do
  [ -f "$f" ] || continue
  python3 -m py_compile "$f" 2>/dev/null || fail "$f does not compile"
done

# 4) Lineage: warn (not fatal) if HEAD isn't on origin/main — deploy still allowed
#    so a quick local iteration isn't blocked, but you're told to push.
if ! git merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
  echo "   ⚠️  warn: HEAD is not on origin/main (push so the deploy matches the repo)"
fi

echo "✅ pre-flight clean$([ -n "$CHANGED" ] && echo " ($(printf '%s\n' "$CHANGED" | grep -c .) changed .py compiled)")"

if [ "$CHECK_ONLY" = "true" ]; then
  echo "   (--check: not deploying)"
  exit 0
fi

echo "🚀 railway up --service $SERVICE --detach"
railway up --service "$SERVICE" --detach
