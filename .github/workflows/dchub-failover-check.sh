#!/usr/bin/env bash
# dchub-failover-check.sh
# Verifies dchub.cloud failover chain is healthy across /mcp (mcp-proxy) and
# /api/* (dchubapiproxy v4.5.10+).
#
# Modes:
#   ./dchub-failover-check.sh              # canary mode: primary + forced-Replit on both surfaces
#   ./dchub-failover-check.sh drill        # drill mode: verbose output, same checks
#
# Requires env: CANARY_SECRET  (Worker Secret bound on BOTH mcp-proxy and dchubapiproxy)
#
# Exits 0 on success, 1 on any failure. Silent on success in canary mode for
# cron friendliness. GitHub Actions / cron captures stderr and emails on fail.

set -u

MCP_URL="https://dchub.cloud/mcp"
API_URL="https://dchub.cloud/api/version"
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"canary","version":"1"}}}'
FAIL=0

# hit_post <label> <url> <expected_backend_or_empty> <extra_curl_args...>
hit_post() {
  local label="$1" url="$2" expect_backend="$3"; shift 3
  local headers
  headers=$(curl -sS -o /dev/null -D - -X POST "$url" \
    -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' \
    --max-time 15 "$@" --data "$INIT")
  assert_response "$label" "$headers" "$expect_backend"
}

# hit_get <label> <url> <expected_backend_or_empty> <extra_curl_args...>
hit_get() {
  local label="$1" url="$2" expect_backend="$3"; shift 3
  local headers
  headers=$(curl -sS -o /dev/null -D - "$url" --max-time 15 "$@")
  assert_response "$label" "$headers" "$expect_backend"
}

assert_response() {
  local label="$1" headers="$2" expect_backend="$3"
  local status backend
  status=$(printf '%s' "$headers" | head -1 | awk '{print $2}')
  backend=$(printf '%s' "$headers" | awk -F': *' 'tolower($1)=="x-backend-used"{print tolower($2)}' | tr -d '\r\n')

  if [[ "$status" != "200" ]]; then
    echo "[$label] FAIL status=$status backend=${backend:-none}" >&2
    FAIL=1; return
  fi
  if [[ -n "$expect_backend" && "$backend" != "$expect_backend" ]]; then
    echo "[$label] FAIL expected backend=$expect_backend got=${backend:-none}" >&2
    FAIL=1; return
  fi
  [[ "${VERBOSE:-0}" == "1" ]] && echo "[$label] OK status=200 backend=$backend"
}

MODE="${1:-canary}"
[[ "$MODE" == "drill" ]] && VERBOSE=1
if [[ "$MODE" == "drill" ]]; then
  echo "=== dchub failover drill ==="
fi

# --- /mcp surface (mcp-proxy) ---
hit_post "mcp primary" "$MCP_URL" ""
if [[ -n "${CANARY_SECRET:-}" ]]; then
  hit_post "mcp canary -> replit" "$MCP_URL" "replit" -H "X-Dchub-Canary: $CANARY_SECRET"
else
  echo "[mcp canary] SKIP: CANARY_SECRET not set" >&2
fi

# --- /api/* surface (dchubapiproxy v4.5.10+) ---
hit_get "api primary" "$API_URL" ""
if [[ -n "${CANARY_SECRET:-}" ]]; then
  hit_get "api canary -> replit" "$API_URL" "replit" -H "X-Dchub-Canary: $CANARY_SECRET"
else
  echo "[api canary] SKIP: CANARY_SECRET not set" >&2
fi

[[ "$MODE" == "drill" && $FAIL -eq 0 ]] && echo "=== all checks passed ==="
exit $FAIL
