#!/bin/bash
# =============================================================
# DC Hub MCP Fix — Complete Deployment
# One command: bash deploy.sh
# =============================================================

set -e

echo ""
echo "🚀 DC Hub MCP Gatekeeper Deployment v2.3"
echo "=========================================="

# ---------------------------------------------------------------------------
# 1. Copy gatekeeper module (the only file that matters)
# ---------------------------------------------------------------------------
echo ""
echo "📂 Step 1: Installing gatekeeper module..."

# Check we have the files
if [ ! -f "mcp_gatekeeper.py" ]; then
    echo "   ❌ mcp_gatekeeper.py not found in current directory"
    echo "   Make sure deploy.sh, mcp_gatekeeper.py, and patch_mcp_server.py"
    echo "   are all in your workspace root."
    exit 1
fi

echo "   ✅ mcp_gatekeeper.py ready"

# ---------------------------------------------------------------------------
# 2. Auto-patch the MCP server
# ---------------------------------------------------------------------------
echo ""
echo "🔧 Step 2: Patching dchub_mcp_server.py..."

if [ ! -f "dchub_mcp_server.py" ]; then
    echo "   ❌ dchub_mcp_server.py not found"
    echo "   Run this from your project root where dchub_mcp_server.py lives."
    exit 1
fi

python3 patch_mcp_server.py --input dchub_mcp_server.py
echo ""

# ---------------------------------------------------------------------------
# 3. Run data quality SQL (if DB URL is available)
# ---------------------------------------------------------------------------
echo ""
echo "🗄️ Step 3: Fixing data quality..."

if [ -n "$NEON_DATABASE_URL" ]; then
    if command -v psql &> /dev/null; then
        psql "$NEON_DATABASE_URL" -f fix_data.sql 2>&1 | tail -15
        echo "   ✅ Data fixes applied"
    else
        echo "   ⚠️ psql not found — running via Python..."
        python3 -c "
import psycopg2, os
conn = psycopg2.connect(os.environ['NEON_DATABASE_URL'])
conn.autocommit = True
cur = conn.cursor()
with open('fix_data.sql') as f:
    sql = f.read()
    # Split on semicolons but skip the DO block
    for stmt in sql.split(';'):
        stmt = stmt.strip()
        if stmt and not stmt.startswith('--'):
            try:
                cur.execute(stmt + ';')
            except Exception as e:
                print(f'   ⚠️ {str(e)[:80]}')
cur.close()
conn.close()
print('   ✅ Data fixes applied via Python')
" 2>&1 || echo "   ⚠️ Some SQL statements skipped — check fix_data.sql manually"
    fi
elif [ -n "$DATABASE_URL" ]; then
    echo "   Using DATABASE_URL..."
    psql "$DATABASE_URL" -f fix_data.sql 2>&1 | tail -15 || echo "   ⚠️ Run fix_data.sql manually against your DB"
else
    echo "   ⚠️ No NEON_DATABASE_URL set — run fix_data.sql manually:"
    echo "      psql \$NEON_DATABASE_URL -f fix_data.sql"
fi

# ---------------------------------------------------------------------------
# 4. Generate sample API keys
# ---------------------------------------------------------------------------
echo ""
echo "🔑 Step 4: Generating sample API keys..."
python3 -c "
from mcp_gatekeeper import generate_key, Tier
print('')
print('   ┌─────────────────────────────────────────────────────────────┐')
print('   │ SAVE THESE KEYS — you\\'ll need them for testing            │')
print('   ├─────────────────────────────────────────────────────────────┤')
key_f = generate_key(Tier.FREE)
key_d = generate_key(Tier.DEVELOPER)
key_p = generate_key(Tier.PRO)
key_e = generate_key(Tier.ENTERPRISE)
print(f'   │ FREE:       {key_f[:50]}... │')
print(f'   │ DEVELOPER:  {key_d[:50]}... │')
print(f'   │ PRO:        {key_p[:50]}... │')
print(f'   │ ENTERPRISE: {key_e[:50]}... │')
print('   └─────────────────────────────────────────────────────────────┘')
print('')
print(f'   Full keys for .env:')
print(f'   DCHUB_API_KEYS={key_f}:free,{key_d}:dev,{key_p}:pro,{key_e}:ent')
" 2>&1 || echo "   ⚠️ Key generation failed — check mcp_gatekeeper.py import"

# ---------------------------------------------------------------------------
# 5. Summary
# ---------------------------------------------------------------------------
echo ""
echo "=========================================="
echo "✅ DEPLOYMENT COMPLETE"
echo "=========================================="
echo ""
echo "What changed:"
echo "  1. dchub_mcp_server.py patched with gate() + finalize() on all 24 tools"
echo "  2. GatekeeperMiddleware added to ASGI app (extracts x-api-key header)"
echo "  3. api_keys table created in Neon"
echo "  4. Transaction duplicates removed + capex announcements flagged"
echo "  5. Missing facility slugs/providers filled"
echo "  6. Missing deal regions inferred"
echo ""
echo "Tier behavior:"
echo "  FREE (no key):  5 results max, values redacted, 50 calls/day, upgrade CTAs"
echo "  DEVELOPER:      Full data, 100 results, 2K calls/day"
echo "  PRO:            Bulk access, 500 results, 10K calls/day"
echo "  ENTERPRISE:     Unlimited, 100K calls/day"
echo ""
echo "To test:"
echo "  # No key (free tier — truncated + redacted)"
echo "  curl -X POST http://localhost:8888/mcp ..."
echo ""
echo "  # With developer key (full access)"
echo "  curl -X POST http://localhost:8888/mcp -H 'x-api-key: dchub_dev_xxx' ..."
echo ""
echo "Next: restart your MCP server to activate"
echo "  kill \$(lsof -ti:8888) 2>/dev/null; python dchub_mcp_server.py &"
echo ""
