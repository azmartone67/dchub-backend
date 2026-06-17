#!/usr/bin/env bash
# dchub-failover-check.sh — proves the LIVE failover chain is healthy.
#
# Rewritten 2026-06-17 (r-dr): the old version asserted `x-backend-used: replit`
# via a CANARY_SECRET — that was the retired 3-backend Replit topology and proved
# nothing. The live chain is Railway (primary) → Render (failover), routed by the
# standalone ~/dchub-frontend/_worker.js. The worker lets you force a backend with
# the header `X-DCHUB-Force-Backend: render` and tags the response with
# `x-dc-hub-served-by: <backend>-<mode>` (e.g. render-failover, railway-primary,
# kv-stale). So we force Render and assert HTTP 200 + served-by ~ "render" —
# i.e. the failover mirror is actually live AND current.
#
# NOTE: only PROXIED paths carry x-dc-hub-served-by. /api/version is handled
# INLINE by the worker, so we use /mcp (proxied) + /api/v1/stats (proxied).
#
# Modes:
#   ./dchub-failover-check.sh         # canary mode: silent on success (cron-friendly)
#   ./dchub-failover-check.sh drill   # verbose
# Exits 0 on success, 1 on any failure.

set -u

MCP_URL="https://dchub.cloud/mcp"
API_URL="https://dchub.cloud/api/v1/stats"
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"canary","version":"1"}}}'
FAIL=0

# assert_response <label> <headers> <expect_served_by_substr_or_empty>
assert_response() {
  local label="$1" headers="$2" expect="$3" status served
  status=$(printf '%s' "$headers" | head -1 | awk '{print $2}')
  served=$(printf '%s' "$headers" | awk -F': *' 'tolower($1)=="x-dc-hub-served-by"{print tolower($2)}' | tr -d '\r\n')
  if [[ "$status" != "200" ]]; then
    echo "[$label] FAIL status=$status served=${served:-none}" >&2; FAIL=1; return
  fi
  if [[ -n "$expect" && "$served" != *"$expect"* ]]; then
    echo "[$label] FAIL expected served-by~$expect got=${served:-none}" >&2; FAIL=1; return
  fi
  [[ "${VERBOSE:-0}" == "1" ]] && echo "[$label] OK status=200 served=${served:-none}"
}

hit_post() {  # <label> <url> <expect> <extra curl args...>
  local l="$1" u="$2" e="$3"; shift 3
  assert_response "$l" "$(curl -sS -o /dev/null -D - -X POST "$u" \
    -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' \
    --max-time 15 "$@" --data "$INIT")" "$e"
}
hit_get() {   # <label> <url> <expect> <extra curl args...>
  local l="$1" u="$2" e="$3"; shift 3
  assert_response "$l" "$(curl -sS -o /dev/null -D - "$u" --max-time 15 "$@")" "$e"
}

MODE="${1:-canary}"
[[ "$MODE" == "drill" ]] && VERBOSE=1
[[ "$MODE" == "drill" ]] && echo "=== dchub failover drill (Railway primary → Render failover) ==="

# Primary — any healthy backend must answer 200.
hit_post "mcp primary"        "$MCP_URL" ""
hit_get  "api primary"        "$API_URL" ""
# Forced Render — PROVES the failover mirror is live and current (not stale/blocked).
hit_post "mcp forced-render"  "$MCP_URL" "render" -H "X-DCHUB-Force-Backend: render"
hit_get  "api forced-render"  "$API_URL" "render" -H "X-DCHUB-Force-Backend: render"

[[ "$MODE" == "drill" && $FAIL -eq 0 ]] && echo "=== all checks passed (Railway + Render both healthy) ==="
exit $FAIL
