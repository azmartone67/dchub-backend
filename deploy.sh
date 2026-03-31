#!/bin/bash
# deploy.sh — DCHub safe deployment script
# ==========================================
# Runs all safety checks before committing and pushing.
# Usage: bash deploy.sh "commit message"
#
# Workflow:
#   1. Python syntax check on all .py files
#   2. Pre-push safety guard (duplicates, corruption, orphans)
#   3. Pre-deploy check (warnings report)
#   4. Git add, commit, push
#   5. Wait for Railway health check

set -e

MSG="${1:-Auto-deploy with safety checks}"
RAILWAY_URL="https://dchub-backend-production.up.railway.app"

echo "============================================"
echo "  DCHub Safe Deploy Pipeline"
echo "============================================"
echo ""

# Step 1: Syntax check
echo "🔍 Step 1: Python syntax check..."
SYNTAX_ERRORS=0
for f in *.py; do
    [[ "$f" == *"("* ]] && continue  # Skip numbered copies
    python -c "import py_compile; py_compile.compile('$f', doraise=True)" 2>/dev/null || {
        echo "  ❌ SYNTAX ERROR: $f"
        SYNTAX_ERRORS=$((SYNTAX_ERRORS + 1))
    }
done

if [ $SYNTAX_ERRORS -gt 0 ]; then
    echo ""
    echo "🚫 BLOCKED: $SYNTAX_ERRORS file(s) have syntax errors."
    echo "   Fix them before deploying."
    exit 1
fi
echo "  ✅ All files compile"

# Step 2: Pre-push guard
echo ""
echo "🔍 Step 2: Pre-push safety guard..."
python pre_push_guard.py
GUARD_EXIT=$?
if [ $GUARD_EXIT -ne 0 ]; then
    echo ""
    echo "🚫 BLOCKED by pre-push guard. Fix errors above."
    exit 1
fi

# Step 3: Pre-deploy check (warnings only, don't block)
echo ""
echo "🔍 Step 3: Pre-deploy warning scan..."
python pre_deploy_check.py 2>/dev/null | tail -5

# Step 4: Git commit & push
echo ""
echo "🚀 Step 4: Committing and pushing..."
git add -A
git diff --cached --quiet && {
    echo "  ℹ️  No changes to commit"
} || {
    git commit -m "$MSG"
    git push
    echo "  ✅ Pushed to GitHub → Railway auto-deploy triggered"
}

# Step 5: Health check (wait for Railway to rebuild)
echo ""
echo "⏳ Step 5: Waiting 90s for Railway rebuild..."
sleep 90
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$RAILWAY_URL/health" 2>/dev/null || echo "000")

if [ "$HTTP_CODE" = "200" ]; then
    echo "  ✅ Health check PASSED (HTTP $HTTP_CODE)"
    echo ""
    echo "============================================"
    echo "  ✅ DEPLOY SUCCESSFUL"
    echo "============================================"
else
    echo "  ⚠️  Health check returned HTTP $HTTP_CODE"
    echo "  Check Railway logs: https://railway.app"
    echo ""
    echo "============================================"
    echo "  ⚠️  DEPLOY NEEDS REVIEW"
    echo "============================================"
fi
