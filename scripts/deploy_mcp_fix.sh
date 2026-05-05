#!/bin/bash
# ============================================================
# DC Hub MCP Pool Leak Fix — Full GitHub Deploy Script
# ============================================================
# Downloads main.py + dchub_mcp_server.py from GitHub,
# applies 3 fixes, commits and pushes.
#
# Run in Railway shell:
#   curl -sS -o /tmp/deploy_fix.sh https://... && bash /tmp/deploy_fix.sh
#   OR just paste this entire script
# ============================================================

set -e

echo "=== DC Hub MCP Pool Leak Fix ==="
echo ""

# Clean up any previous attempt
rm -rf /tmp/dchub-repo

# Step 1: Clone repo
echo "Step 1: Cloning repo..."
git clone --depth 1 https://$GITHUB_TOKEN@github.com/azmartone67/dchub-backend.git /tmp/dchub-repo 2>&1 | tail -1
cd /tmp/dchub-repo
echo "  ✅ Cloned to /tmp/dchub-repo"
echo "  main.py: $(wc -l < main.py) lines"
echo "  dchub_mcp_server.py: $(wc -l < dchub_mcp_server.py) lines"

# Step 2: Show BEFORE state
echo ""
echo "=== BEFORE ==="
echo "BUG 1 — pg_e.close() in get_energy_prices teaser:"
grep -n "pg_e.close()" main.py || echo "  (not found)"
echo "BUG 2 — _tc.close() in fallback teaser:"
grep -n "_tc.close()" main.py || echo "  (not found)"
echo "BUG 3 — http:// endpoint:"
grep -c "http://dchub-backend-production" main.py dchub_mcp_server.py 2>/dev/null || true

# Step 3: Apply fixes using Python (handles multi-line replacements safely)
echo ""
echo "Step 3: Applying fixes..."

python3 << 'PYFIX'
import sys

# ---- Fix main.py ----
with open('main.py', 'r') as f:
    content = f.read()

fixes_applied = 0

# FIX 1: pg_e.close() → return_pg_connection(pg_e) in get_energy_prices teaser
# This is in the finally block around line 3095
old1 = "                    pg_e.close()"
new1 = "                    return_pg_connection(pg_e)"
if old1 in content:
    content = content.replace(old1, new1)
    fixes_applied += 1
    print("  ✅ FIX 1: pg_e.close() → return_pg_connection(pg_e)")
else:
    print("  ⚠️  FIX 1: pg_e.close() pattern not found (maybe already fixed?)")

# FIX 2: fallback teaser _tc.close() → return_pg_connection(_tc) with finally block
# Current code:
#         _tcur.close()
#         _tc.close()
#     except Exception as _teaser_err:
#         import traceback as _ttb
#         logger.error(...)
#         teaser_data = {"note": f"Preview data temporarily unavailable ({type(_teaser_err).__name__})"}
#
# Fixed code: remove _tc.close(), add finally block

old2a = "        _tcur.close()\n        _tc.close()\n    except Exception as _teaser_err:"
new2a = "        _tcur.close()\n    except Exception as _teaser_err:"

if old2a in content:
    content = content.replace(old2a, new2a)
    print("  ✅ FIX 2a: Removed _tc.close() from try block")
else:
    print("  ⚠️  FIX 2a: _tc.close() pattern not found")

# Now add finally block after the teaser_data error assignment
old2b = '        teaser_data = {"note": f"Preview data temporarily unavailable ({type(_teaser_err).__name__})"}'
new2b = old2b + '\n    finally:\n        if _tc:\n            try: return_pg_connection(_tc)\n            except Exception: pass'

if old2b in content and 'finally:\n        if _tc:\n            try: return_pg_connection(_tc)' not in content:
    content = content.replace(old2b, new2b, 1)  # Only first occurrence
    fixes_applied += 1
    print("  ✅ FIX 2b: Added finally block with return_pg_connection(_tc)")
elif 'finally:\n        if _tc:\n            try: return_pg_connection(_tc)' in content:
    print("  ⚠️  FIX 2b: finally block already exists (already fixed?)")
else:
    print("  ⚠️  FIX 2b: teaser_data error pattern not found")

# FIX 3: http:// → https:// in endpoint URL
old3 = "http://dchub-backend-production.up.railway.app"
new3 = "https://dchub-backend-production.up.railway.app"
count3 = content.count(old3)
if count3 > 0:
    content = content.replace(old3, new3)
    fixes_applied += 1
    print(f"  ✅ FIX 3: Replaced {count3} http:// → https:// in main.py")
else:
    print("  ⚠️  FIX 3: No http:// endpoint found in main.py")

with open('main.py', 'w') as f:
    f.write(content)

# ---- Fix dchub_mcp_server.py ----
with open('dchub_mcp_server.py', 'r') as f:
    mcp_content = f.read()

count3b = mcp_content.count(old3)
if count3b > 0:
    mcp_content = mcp_content.replace(old3, new3)
    print(f"  ✅ FIX 3: Replaced {count3b} http:// → https:// in dchub_mcp_server.py")
    with open('dchub_mcp_server.py', 'w') as f:
        f.write(mcp_content)
else:
    print("  ℹ️  No http:// endpoint in dchub_mcp_server.py")

print(f"\n  Total fixes applied: {fixes_applied + (1 if count3b > 0 else 0)}")
PYFIX

# Step 4: Show AFTER state
echo ""
echo "=== AFTER ==="
echo "FIX 1 — should show return_pg_connection(pg_e):"
grep -n "return_pg_connection(pg_e)" main.py || echo "  (not found!)"
echo "FIX 2 — should show finally block:"
grep -n -A3 "Preview data temporarily unavailable" main.py | head -8
echo "FIX 3 — should show 0 http:// remaining:"
grep -c "http://dchub-backend-production" main.py dchub_mcp_server.py 2>/dev/null && echo "  ⚠️ Still has http://" || echo "  ✅ All converted to https://"

# Step 5: Pool connection audit in teaser zone
echo ""
echo "=== POOL AUDIT (teaser functions) ==="
echo "get_pg_connection() checkouts:"
awk 'NR>=2990 && NR<=3550' main.py | grep -n "get_pg_connection" || echo "  none"
echo "return_pg_connection() returns:"
awk 'NR>=2990 && NR<=3550' main.py | grep -n "return_pg_connection" || echo "  none"
echo "Remaining .close() (should be cursors + psycopg2 direct only):"
awk 'NR>=2990 && NR<=3550' main.py | grep -n "\.close()" || echo "  none"

# Step 6: Git commit and push
echo ""
echo "Step 6: Committing and pushing..."
git config user.email "azmartone@gmail.com"
git config user.name "Jonathan Martone"
git add main.py dchub_mcp_server.py
git status
git commit -m "fix: MCP pool leak — return_pg_connection in teasers + https endpoint

- get_energy_prices teaser: pg_e.close() → return_pg_connection(pg_e)
- fallback teaser (tax/water/grid_intel/backup): _tc.close() → return_pg_connection(_tc) in finally block
- MCP endpoint: http:// → https://
- Root cause of 19/20 tools failing via Claude.ai MCP client"

git push origin main

echo ""
echo "============================================"
echo "=== DEPLOYED! Railway will auto-redeploy ==="
echo "============================================"
echo ""
echo "After Railway finishes deploying (~2 min), open a NEW Claude.ai chat and test:"
echo "  'Test all DC Hub MCP tools'"
echo ""
echo "Expected: 20/20 tools should now work."
