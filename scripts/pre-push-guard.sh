#!/bin/bash
# Phase FF+7-meta (2026-05-19) — pre-push deploy guard.
#
# Run BEFORE `git push` to catch the failure modes that have repeatedly
# caused production outages this week:
#
#   1. Multiple commits ahead of origin (deploy queue churn)
#   2. Recently-deployed commits showing failed smoke tests
#   3. Current commit touches files known to be deploy-fragile
#   4. Syntax / import errors that the test gauntlet would catch later
#
# Usage:
#   bash scripts/pre-push-guard.sh
#   # if exit 0, push is safe
#   # if exit 1, fix the issue first OR pass --force to override
#
# Install as a git hook (optional):
#   ln -s ../../scripts/pre-push-guard.sh .git/hooks/pre-push

set -u

FORCE=""
for arg in "$@"; do
  if [ "$arg" = "--force" ] || [ "$arg" = "-f" ]; then
    FORCE="1"
  fi
done

cd "$(dirname "$0")/.."

red()    { printf "\033[31m%s\033[0m\n" "$*"; }
green()  { printf "\033[32m%s\033[0m\n" "$*"; }
yellow() { printf "\033[33m%s\033[0m\n" "$*"; }
bold()   { printf "\033[1m%s\033[0m\n" "$*"; }

FAIL=0

bold "═══════════════════════════════════════════════════════════════"
bold "  PRE-PUSH DEPLOY GUARD — Phase FF+7-meta"
bold "═══════════════════════════════════════════════════════════════"
echo

# ─── 1. How many commits am I about to push? ─────────────────────
echo "[1/4] Checking commit queue depth..."
ahead=$(git rev-list --count origin/main..HEAD 2>/dev/null || echo "0")
if [ "$ahead" -ge 3 ]; then
  red "  ✗ Pushing $ahead commits at once. Railway serializes deploys"
  red "    at 2-3 min each — queue churn has caused 2 outages this week."
  red "    Recommend: push commits 1 at a time, wait for Railway to settle."
  FAIL=1
elif [ "$ahead" -ge 2 ]; then
  yellow "  ⚠ Pushing $ahead commits. OK but watch Railway."
else
  green "  ✓ $ahead commit(s) ahead of origin"
fi
echo

# ─── 2. Is Railway healthy RIGHT NOW? ────────────────────────────
echo "[2/4] Checking Railway health..."
# Phase FF+7-meta (2026-05-19): on curl total-failure, %{http_code}
# can be empty which the OR-echo replaces with "000". But sometimes
# the empty echo concatenates with a prior "000" giving "000000".
# Treat anything non-200 as risky.
http=$(/usr/bin/curl -s -m 8 -o /dev/null -w "%{http_code}" \
  "https://dchub-backend-production.up.railway.app/api/health" 2>/dev/null)
[ -z "$http" ] && http="000"
if [ "$http" = "200" ]; then
  green "  ✓ Railway origin: HTTP $http"
elif [ "$http" = "503" ] || [ "$http" = "000" ] || [ "$http" = "000000" ] || [ "$http" = "502" ]; then
  red "  ✗ Railway origin: HTTP $http (degraded or down)"
  red "    Pushing now will pile on top of a broken deploy. WAIT for recovery."
  FAIL=1
else
  yellow "  ⚠ Railway origin: HTTP $http (unusual — treating as risky)"
  FAIL=1
fi
echo

# ─── 3. Local syntax check on changed files ──────────────────────
echo "[3/4] Local syntax check on staged Python files..."
syntax_fail=0
changed_py=$(git diff --cached --name-only --diff-filter=AM | /usr/bin/grep '\.py$' || true)
if [ -z "$changed_py" ]; then
  # also check committed-but-not-pushed
  changed_py=$(git diff origin/main..HEAD --name-only --diff-filter=AM | /usr/bin/grep '\.py$' || true)
fi
if [ -z "$changed_py" ]; then
  green "  ✓ no Python changes to verify"
else
  for f in $changed_py; do
    if [ -f "$f" ]; then
      if ! python3 -m py_compile "$f" 2>/dev/null; then
        red "  ✗ syntax error in $f"
        syntax_fail=1
      fi
    fi
  done
  if [ "$syntax_fail" = "0" ]; then
    green "  ✓ all changed .py files compile"
  else
    FAIL=1
  fi
fi
echo

# ─── 4. Are publishers / long-running threads touched? ───────────
echo "[4/4] Checking for deploy-fragile changes..."
fragile=$(git diff origin/main..HEAD --name-only 2>/dev/null | \
  /usr/bin/grep -E "content_publisher\.py|ai_wars_automation\.py|api_monetization\.py|main\.py|brain_consistency_radar\.py" || true)
if [ -n "$fragile" ]; then
  yellow "  ⚠ Touching deploy-fragile files:"
  echo "$fragile" | /usr/bin/sed 's/^/      /'
  yellow "    These have caused outages before. Push 1 at a time + monitor."
else
  green "  ✓ no deploy-fragile files touched"
fi
echo

bold "═══════════════════════════════════════════════════════════════"
if [ "$FAIL" = "0" ]; then
  green "  ✓ Pre-push guard PASSED — safe to push"
  exit 0
elif [ -n "$FORCE" ]; then
  yellow "  ⚠ Pre-push guard FAILED but --force given. Pushing anyway."
  exit 0
else
  red "  ✗ Pre-push guard FAILED"
  red "  Override with: bash scripts/pre-push-guard.sh --force"
  exit 1
fi
