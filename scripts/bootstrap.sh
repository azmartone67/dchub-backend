#!/usr/bin/env bash
# Run once after cloning: wires git hooks and verifies auth.
set -euo pipefail
git config core.hooksPath .githooks
echo "hooks enabled"
unset GH_TOKEN GITHUB_TOKEN
if command -v gh >/dev/null && gh auth status 2>&1 | grep -q workflow; then
  echo "gh auth has workflow scope"
else
  echo "WARN: gh auth missing workflow scope - run: gh auth refresh -s workflow"
fi
echo
echo "See docs/RUNBOOK.md for operations."
