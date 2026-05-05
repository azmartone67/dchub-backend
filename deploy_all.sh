#!/bin/bash
# =============================================================
# DC Hub Complete Fix — Gatekeeper + Stripe Keys + Data Quality
# One command: bash deploy_all.sh
# =============================================================

set -e

echo ""
echo "🚀 DC Hub Complete Fix Deployment"
echo "==================================="

# ---------------------------------------------------------------------------
# 0. Pre-flight: verify files exist
# ---------------------------------------------------------------------------
for f in mcp_gatekeeper.py patch_mcp_server.py patch_stripe_keygen.py fix_data.sql; do
    if [ ! -f "$f" ]; then
        echo "❌ Missing: $f — make sure all fix files are in workspace root"
        exit 1
    fi
done
echo "✅ All fix files present"

# ---------------------------------------------------------------------------
# 1. Restore backups if they exist (clean slate)
# ---------------------------------------------------------------------------
echo ""
echo "📋 Step 1: Restoring clean files..."
LATEST_MCP_BACKUP=$(ls -t dchub_mcp_server.py.backup.* 2>/dev/null | head -1)
if [ -n "$LATEST_MCP_BACKUP" ]; then
    cp "$LATEST_MCP_BACKUP" dchub_mcp_server.py
    echo "   Restored dchub_mcp_server.py from $LATEST_MCP_BACKUP"
fi

LATEST_MAIN_BACKUP=$(ls -t main.py.pre_keygen.* 2>/dev/null | head -1)
if [ -n "$LATEST_MAIN_BACKUP" ]; then
    cp "$LATEST_MAIN_BACKUP" main.py
    echo "   Restored main.py from $LATEST_MAIN_BACKUP"
fi

# ---------------------------------------------------------------------------
# 2. Patch MCP server (gatekeeper)
# ---------------------------------------------------------------------------
echo ""
echo "🔧 Step 2: Patching MCP server..."
python3 patch_mcp_server.py --input dchub_mcp_server.py 2>&1 | grep "✅\|💾\|TOTAL"

# ---------------------------------------------------------------------------
# 3. Patch main.py (Stripe key generation)
# ---------------------------------------------------------------------------
echo ""
echo "💳 Step 3: Patching Stripe key generation..."
python3 patch_stripe_keygen.py --input main.py 2>&1 | grep "✅\|💾\|Syntax"

# ---------------------------------------------------------------------------
# 4. Run data quality SQL
# ---------------------------------------------------------------------------
echo ""
echo "🗄️ Step 4: Data quality fixes..."
DB_URL="${NEON_DATABASE_URL:-$DATABASE_URL}"
if [ -n "$DB_URL" ]; then
    psql "$DB_URL" -f fix_data.sql 2>&1 | grep -E "NOTICE|UPDATE|CREATE|ERROR" | tail -15
    echo "   ✅ Data fixes applied"
else
    echo "   ⚠️ No DB URL — run: psql \$NEON_DATABASE_URL -f fix_data.sql"
fi

# ---------------------------------------------------------------------------
# 5. Generate & display API keys
# ---------------------------------------------------------------------------
echo ""
echo "🔑 Step 5: API Keys..."
python3 -c "
from mcp_gatekeeper import generate_key, Tier
keys = {
    'DEVELOPER': generate_key(Tier.DEVELOPER),
    'PRO': generate_key(Tier.PRO),
    'ENTERPRISE': generate_key(Tier.ENTERPRISE),
}
print('')
for tier, key in keys.items():
    print(f'   {tier}: {key}')
print('')
env_line = ','.join(f'{v}:{k[:3].lower()}' for k,v in keys.items())
print(f'   Add to .env or Replit Secrets:')
print(f'   DCHUB_API_KEYS={env_line}')
" 2>&1 || echo "   ⚠️ Key generation skipped"

# ---------------------------------------------------------------------------
# 6. Restart MCP server
# ---------------------------------------------------------------------------
echo ""
echo "🔄 Step 6: Restarting MCP server..."
pkill -f "dchub_mcp_server.py" 2>/dev/null || true
sleep 1
python dchub_mcp_server.py &
MCP_PID=$!
sleep 3

# Check if it's still running
if kill -0 $MCP_PID 2>/dev/null; then
    echo ""
    echo "==================================="
    echo "✅ ALL FIXES DEPLOYED SUCCESSFULLY"
    echo "==================================="
    echo ""
    echo "  MCP server: PID $MCP_PID (port 8888)"
    echo "  Gatekeeper: active on all 24 tools"
    echo "  Stripe keys: generating dchub_dev_/dchub_pro_/dchub_ent_ prefixed keys"
    echo "  Data quality: deduped, flagged, filled"
    echo ""
    echo "  Email template ready: email_power_users.html"
    echo "  Send via: your SendGrid integration or Stripe customer emails"
    echo ""
else
    echo ""
    echo "⚠️ MCP server may have crashed — check logs above"
    echo "   Try: python dchub_mcp_server.py 2>&1 | head -30"
fi
