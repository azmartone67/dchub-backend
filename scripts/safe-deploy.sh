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

# 5) r-harden (2026-06-23): align a CLEAN checkout to origin/main HEAD before deploying.
#    THE deploy-race that silently reverted fixes: a stale local checkout (behind main,
#    no local edits) `railway up`s an OLD commit, clobbering main HEAD. If there are NO
#    modified/staged TRACKED files AND HEAD is strictly an ancestor of origin/main,
#    fast-forward to main HEAD so we always deploy the LATEST committed state. A dirty
#    tree (active local edits) or a branch with un-merged commits deploys AS-IS — this
#    never drops in-progress work; it only stops stale checkouts from reverting main.
#    Fail-soft: if the ff can't apply (e.g. untracked-file collision), deploy as-is.
#    Kill with DCHUB_DEPLOY_NO_FF=1.
if [ "${DCHUB_DEPLOY_NO_FF:-}" != "1" ]; then
  git fetch origin main --quiet 2>/dev/null || true
  _dirty="$(git diff --name-only 2>/dev/null; git diff --cached --name-only 2>/dev/null)"
  if [ -z "$_dirty" ] \
     && git merge-base --is-ancestor HEAD origin/main 2>/dev/null \
     && ! git merge-base --is-ancestor origin/main HEAD 2>/dev/null; then
    echo "   ↪ clean checkout behind origin/main — fast-forwarding to deploy main HEAD"
    if git merge --ff-only origin/main 2>/dev/null; then
      echo "   ✓ now at $(git rev-parse --short HEAD) (origin/main HEAD)"
    else
      echo "   ⚠️  ff blocked (untracked collision?) — deploying the local tree as-is"
    fi
  fi
fi

echo "🚀 railway up --service $SERVICE --detach"
railway up --service "$SERVICE" --detach
