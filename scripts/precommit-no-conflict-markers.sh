#!/usr/bin/env bash
# precommit-no-conflict-markers.sh — block committing unresolved conflict markers.
#
# Defense-in-depth companion to scripts/safe-deploy.sh: that guards the DEPLOY
# boundary; this guards the COMMIT boundary so a half-resolved stash-pop conflict
# (the brain-autopilot churn, 2026-06-21) never gets committed in the first place.
#
# FAIL-OPEN: any internal error → allow the commit. Only ever blocks on an actual
# '<<<<<<< ' / '>>>>>>> ' marker in STAGED content. Bypass: git commit --no-verify.
#
# Install (local, per-machine — like .git/hooks/pre-push):
#   ln -sf ../../scripts/precommit-no-conflict-markers.sh .git/hooks/pre-commit
set -uo pipefail

staged="$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null)" || exit 0
[ -z "$staged" ] && exit 0

bad=""
while IFS= read -r f; do
  [ -z "$f" ] && continue
  if git show ":$f" 2>/dev/null | grep -qE '^(<<<<<<< |>>>>>>> )' 2>/dev/null; then
    bad="$bad $f"
  fi
done <<< "$staged"

if [ -n "$bad" ]; then
  echo "❌ pre-commit blocked: unresolved conflict markers in:$bad" >&2
  echo "   resolve first (git checkout HEAD -- <file>, or fix the markers)." >&2
  echo "   bypass (only if you're sure): git commit --no-verify" >&2
  exit 1
fi
exit 0
