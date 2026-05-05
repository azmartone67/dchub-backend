#!/usr/bin/env bash
# DC Hub post-deploy smoke test
# ==============================
# Run this after every Railway deploy. Asserts:
#   1. /health returns 200
#   2. /mcp returns 200 with proper Accept header
#   3. Enterprise key resolves to Enterprise tier (NOT free)
#   4. power_mw returns a real number (not a lock emoji)
# Exit 0 = healthy. Exit 1 = regression — page someone.

set -u

BASE="${DCHUB_SMOKE_BASE:-https://dchub.cloud}"
KEY="${DCHUB_SMOKE_ENTERPRISE_KEY:?set DCHUB_SMOKE_ENTERPRISE_KEY in env}"

fail() { echo "❌ SMOKE FAIL: $1" >&2; exit 1; }
ok()   { echo "✅ $1"; }

# 1. Health endpoint
H=$(curl -sS -o /dev/null -w "%{http_code}" "$BASE/health")
[[ "$H" == "200" ]] || fail "/health returned $H (expected 200)"
ok "/health → 200"

# 2. MCP initialize with Enterprise key
R=$(curl -sS -X POST "$BASE/mcp" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -H "X-API-Key: $KEY" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_facilities","arguments":{"city":"Ashburn","limit":3}}}')

echo "$R" | python3 -c "
import sys, json
try:
    outer = json.loads(sys.stdin.read())
    inner = json.loads(outer['result']['content'][0]['text'])
    tier  = inner.get('_meta', {}).get('tier', '?')
    mw    = inner.get('data', [{}])[0].get('power_mw', '?')
    if tier != 'Enterprise':
        print(f'❌ SMOKE FAIL: tier={tier} (expected Enterprise)'); sys.exit(1)
    if not isinstance(mw, (int, float)):
        print(f'❌ SMOKE FAIL: power_mw={mw!r} (expected a number, got lock placeholder)'); sys.exit(1)
    print(f'✅ MCP tier resolution → Enterprise (power_mw={mw})')
except SystemExit:
    raise
except Exception as e:
    print(f'❌ SMOKE FAIL: could not parse MCP response: {e}'); sys.exit(1)
" || exit 1

echo ""
echo "✅ ALL SMOKE TESTS PASSED"
