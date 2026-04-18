#!/usr/bin/env bash
# DC Hub — Pre-deploy check. Run from project root before `wrangler deploy`
# or `cloudflare pages deploy`. Blocks deploy if squasher finds issues.
set -euo pipefail

cd "$(dirname "$0")/.."

echo "▶ Running static squasher…"
python3 qa/squasher.py . || {
    echo ""
    echo "✗ Squasher found issues. Fix them, or run"
    echo "     python3 qa/squasher.py --fix"
    echo "   for safe auto-fixes on R1 findings."
    exit 1
}

echo "▶ Syntax-checking JavaScript (node -c equivalent via a quick lint)…"
# Best-effort JS parse — skips if node not present
if command -v node >/dev/null 2>&1; then
    find js -name '*.js' -not -path 'js/vendor/*' -print0 | while IFS= read -r -d '' f; do
        node --check "$f" >/dev/null 2>&1 || { echo "    ✗ parse error in $f"; exit 1; }
    done
    echo "  ✓ JS parse OK"
fi

echo "✓ Pre-deploy checks passed"
