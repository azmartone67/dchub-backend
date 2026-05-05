#!/usr/bin/env bash
# Refuses commits that delete > 20 files unless GUARD_MASS_DELETE_OK=1 is set.
set -euo pipefail
DELETED=$(git diff --cached --name-only --diff-filter=D | wc -l)
LIMIT=${GUARD_MASS_DELETE_LIMIT:-20}
if [ "$DELETED" -gt "$LIMIT" ]; then
  if [ "${GUARD_MASS_DELETE_OK:-0}" != "1" ]; then
    echo "ERROR: commit deletes $DELETED files (limit=$LIMIT)." >&2
    echo "If intentional, re-run with: GUARD_MASS_DELETE_OK=1 git commit ..." >&2
    exit 1
  fi
fi
