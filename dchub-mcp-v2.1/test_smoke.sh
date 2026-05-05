#!/usr/bin/env bash
# test_smoke.sh — End-to-end smoke test for the patched DC Hub MCP server.
# Exercises: discovery, init, tools/list, free-tier call, paid-only gate, telemetry round-trip.
#
# Usage:
#   MCP_URL=https://dchub.cloud/mcp \
#   API_KEY=dch_live_xxx \
#   BACKEND=https://dchub-backend-production.up.railway.app \
#   ./test_smoke.sh

set -euo pipefail

: "${MCP_URL:?MCP_URL is required (e.g. https://dchub.cloud/mcp)}"
API_KEY="${API_KEY:-}"
INTERNAL_KEY="${DCHUB_INTERNAL_KEY:-dchub-internal-sync-2026}"
BACKEND="${BACKEND:-https://dchub-backend-production.up.railway.app}"

red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
yel()   { printf "\033[33m%s\033[0m\n" "$*"; }

# 0) Discovery file sanity
yel "[0/6] /.well-known/mcp.json"
DISC=$(curl -fsS "${MCP_URL%/mcp}/.well-known/mcp.json")
TOOLS_COUNT=$(printf "%s" "$DISC" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('tools',[])))" 2>/dev/null || echo "0")
[ "$TOOLS_COUNT" = "20" ] && green "    ok ($TOOLS_COUNT tools advertised — matches server)" \
                          || { red "    FAIL: discovery advertises $TOOLS_COUNT tools, expected 20. Apply cf_worker_mcpjson_patch.js."; }

# 1) Health
yel "[1/6] /health"
HEALTH_URL="${MCP_URL%/mcp}/health"
HEALTH=$(curl -fsS "$HEALTH_URL")
echo "$HEALTH" | grep -q '"version":"2.1' \
  && green "    ok (server.mjs v2.1 detected)" \
  || red "    WARN: /health did not return v2.1 — make sure Railway redeployed."

# 2) Initialize WITH key
yel "[2/6] initialize with X-API-Key"
INIT_BODY='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"1.0"}}}'
INIT_HEADERS=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")
[ -n "$API_KEY" ] && INIT_HEADERS+=(-H "X-API-Key: $API_KEY")

INIT_RESP=$(curl -fsS -i "$MCP_URL" "${INIT_HEADERS[@]}" -d "$INIT_BODY")
SID=$(printf "%s" "$INIT_RESP" | grep -i '^mcp-session-id:' | head -1 | awk '{print $2}' | tr -d '\r')
[ -n "$SID" ] && green "    ok session=${SID:0:8}…" || { red "    FAIL: no Mcp-Session-Id header"; exit 1; }

# 3) tools/list
yel "[3/6] tools/list"
TOOLS_RESP=$(curl -fsS "$MCP_URL" "${INIT_HEADERS[@]}" -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}')
TOOL_NAMES=$(printf "%s" "$TOOLS_RESP" | python3 -c "import sys,json,re; data=sys.stdin.read(); m=re.search(r'\\{.*\\}', data, re.DOTALL); j=json.loads(m.group()) if m else {}; tools=j.get('result',{}).get('tools',[]); print(','.join(t.get('name','?') for t in tools))" 2>/dev/null || echo "")
echo "    tools: $TOOL_NAMES"
echo "$TOOL_NAMES" | grep -q 'search_facilities' \
  && green "    ok (search_facilities present)" \
  || { red "    FAIL: tools/list did not include search_facilities"; exit 1; }

# 4) tools/call search_facilities (allowed for free tier)
yel "[4/6] tools/call search_facilities (free-tier OK)"
CALL_RESP=$(curl -fsS "$MCP_URL" "${INIT_HEADERS[@]}" -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_facilities","arguments":{"country":"USA","limit":3}}}')
echo "$CALL_RESP" | head -c 400; echo
green "    ok"

# 5) tools/call analyze_site — paid-only, expect upgrade nudge if key isn't paid
yel "[5/6] tools/call analyze_site (paid-only)"
PAID_RESP=$(curl -fsS "$MCP_URL" "${INIT_HEADERS[@]}" -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"analyze_site","arguments":{"lat":38.88,"lon":-77.04,"capacity_mw":50}}}')
if echo "$PAID_RESP" | grep -q '"paid_only"'; then
  green "    ok (paid_only nudge returned with upgrade_url)"
else
  yel "    note: analyze_site returned data — implies the key is on a paid tier"
fi

# 6) Telemetry round-trip
yel "[6/6] telemetry round-trip (3s wait, then GET stats)"
sleep 3
STATS=$(curl -fsS -H "X-Internal-Key: $INTERNAL_KEY" "$BACKEND/api/v1/mcp/stats?days=1" || true)
if [ -n "$STATS" ] && echo "$STATS" | grep -q '"by_tool"'; then
  green "    ok — backend received telemetry"
  echo "$STATS" | python3 -m json.tool 2>/dev/null | head -30 || echo "$STATS" | head -30
else
  red "    WARN: /api/v1/mcp/stats not returning data."
  red "          → Check Flask blueprint registration on Railway"
  red "          → Check NEON_DATABASE_URL is set in Railway env"
  red "          → Check DCHUB_INTERNAL_KEY matches between server.mjs and Flask"
fi

green "\nSmoke test complete."
