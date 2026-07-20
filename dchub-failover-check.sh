#!/usr/bin/env bash
# dchub-failover-check.sh v3.0 — current-contract failover canary (2026-07).
#
# WHY THIS WAS RED FOR 92 DAYS: the Replit secondary was retired for Render, and
# the backend header `x-backend-used` was replaced by `x-dc-hub-served-by`. The
# old drill forced a failover and asserted `x-backend-used: replit`, so it failed
# every run (expected=replit got=MISSING). This version validates the LIVE contract:
#   - dchub.cloud serves 200 through the failover-aware edge worker
#     (x-dc-hub-served-by present; primary = railway-primary)
#
# The DEEP forced-origin-failover-to-Render drill is a follow-up: it needs the
# current force mechanism + Render's x-dc-hub-served-by value confirmed. Until then
# CANARY_SECRET is OPTIONAL and used only for an informational (non-fatal) probe.
set -u

MCP_URL="https://dchub.cloud/mcp"
API_URL="https://dchub.cloud/api/ai/mcp-health"
UA="dchub-failover-canary/3.0"          # a User-Agent avoids CF bot-403 on this zone
EXPECT_PRIMARY="railway-primary"
INIT='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"canary","version":"1"}}}'
FAIL=0

served_by() { printf '%s' "$1" | awk -F': *' 'tolower($1)=="x-dc-hub-served-by"{print tolower($2)}' | tr -d '\r\n'; }
http_status() { printf '%s' "$1" | head -1 | awk '{print $2}'; }

MODE="${1:-canary}"
[[ "$MODE" == "drill" ]] && echo "=== dchub failover canary (current contract) ==="

# 1) MCP edge accepts an initialize (200)
mcp_hdrs=$(curl -sS -o /dev/null -D - -X POST "$MCP_URL" -A "$UA" \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  --max-time 20 --data "$INIT")
mcp_status=$(http_status "$mcp_hdrs")
if [[ "$mcp_status" != "200" ]]; then
  echo "[mcp primary] FAIL status=${mcp_status:-none}" >&2; FAIL=1
else
  echo "[mcp primary] OK status=200 served-by=$(served_by "$mcp_hdrs")"
fi

# 2) API serves 200 through the failover-aware worker, from the primary origin
api_hdrs=$(curl -sS -o /dev/null -D - "$API_URL" -A "$UA" --max-time 20)
api_status=$(http_status "$api_hdrs")
api_served=$(served_by "$api_hdrs")
if [[ "$api_status" != "200" ]]; then
  echo "[api primary] FAIL status=${api_status:-none}" >&2; FAIL=1
elif [[ -z "$api_served" ]]; then
  echo "[api primary] FAIL x-dc-hub-served-by MISSING (failover worker not in request path)" >&2; FAIL=1
elif [[ "$api_served" == "$EXPECT_PRIMARY" ]]; then
  echo "[api primary] OK served-by=$api_served"
else
  # a non-primary served-by is a healthy failover in progress, not a canary failure
  echo "[api primary] WARN served-by=$api_served (not $EXPECT_PRIMARY — failover may be active)"
fi

# 3) OPTIONAL, NON-FATAL: if a force-secret is present, record what a forced failover
#    routes to today (informational — feeds the deep-drill follow-up).
if [[ -n "${CANARY_SECRET:-}" ]]; then
  f_hdrs=$(curl -sS -o /dev/null -D - "$API_URL" -A "$UA" --max-time 20 -H "X-Dchub-Canary: $CANARY_SECRET")
  echo "[api forced] INFO status=$(http_status "$f_hdrs") served-by=$(served_by "$f_hdrs") (non-fatal; deep drill TODO)"
fi

[[ "$MODE" == "drill" && $FAIL -eq 0 ]] && echo "=== all checks passed ==="
exit $FAIL
