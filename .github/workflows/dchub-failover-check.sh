#!/usr/bin/env bash
# dchub-failover-check.sh — proves the failover chain is READY.
#
# Rewritten 2026-06-17 (r-dr v2). History: the original asserted the retired
# Replit `x-backend-used` header (proved nothing). A v1 rewrite used
# `X-DCHUB-Force-Backend: render` — but the LIVE edge worker (4.34.40-r80) IGNORES
# that header (it predates the force feature / CF Pages worker is drifted), so
# that test always saw railway-primary. So v2 proves readiness the way that
# actually works:
#   1. Edge primary up: dchub.cloud /mcp + /api/v1/stats return 200 (Railway live).
#   2. Render MIRROR up: hit the Render origin DIRECTLY
#      (dchub-backend-render.onrender.com) and assert 200 + facilities>0 — i.e. the
#      failover TARGET is live and serving. (Render shares Neon, so data is always
#      current; CODE-staleness is the brain's render_pipeline_blocked detector's job,
#      now self-healing via the deploy hook.)
#
# Modes: `canary` (silent on success) | `drill` (verbose). Exit 0 ok, 1 on failure.

set -u

EDGE="https://dchub.cloud"
RENDER="https://dchub-backend-render.onrender.com"
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"canary","version":"1"}}}'
FAIL=0
MODE="${1:-canary}"
VERBOSE=0; [[ "$MODE" == "drill" ]] && VERBOSE=1
[[ "$VERBOSE" == "1" ]] && echo "=== dchub failover readiness drill (Railway primary + Render mirror) ==="

ok() { [[ "$VERBOSE" == "1" ]] && echo "[$1] OK $2"; }
bad() { echo "[$1] FAIL $2" >&2; FAIL=1; }

# 1. Edge primary (Railway) — /mcp (POST) + /api/v1/stats (GET)
mcp_status=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$EDGE/mcp" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  --max-time 15 --data "$INIT")
[[ "$mcp_status" == "200" ]] && ok "edge /mcp" "status=200" || bad "edge /mcp" "status=$mcp_status"

api_status=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$EDGE/api/v1/stats")
[[ "$api_status" == "200" ]] && ok "edge /api/v1/stats" "status=200" || bad "edge /api/v1/stats" "status=$api_status"

# 2. Render MIRROR direct — must be live + serving real data (the failover target)
rstatus=$(curl -sS -o /tmp/_canary_render.json -w '%{http_code}' --max-time 30 "$RENDER/api/v1/stats")
rfac=$(python3 -c "import json;d=json.load(open('/tmp/_canary_render.json'));print(int(d.get('facilities') or d.get('total_facilities') or d.get('total') or 0))" 2>/dev/null || echo 0)
if [[ "$rstatus" == "200" && "${rfac:-0}" -gt 1000 ]]; then
  ok "render mirror" "status=200 facilities=$rfac"
else
  bad "render mirror" "status=$rstatus facilities=${rfac:-0} (failover target down or empty)"
fi

[[ "$VERBOSE" == "1" && $FAIL -eq 0 ]] && echo "=== READY: Railway edge + Render mirror both healthy ==="
exit $FAIL
