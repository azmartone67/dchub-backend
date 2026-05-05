#!/bin/bash
# v2: race-condition-aware MCP launcher
set -u
echo "=== MCP Server Launcher v2 ==="

# Hit Flask via the SAME Railway $PORT — no Cloudflare round-trip
FLASK_PORT="${PORT:-5000}"
export DCHUB_API_BASE="${DCHUB_API_BASE_OVERRIDE:-http://127.0.0.1:${FLASK_PORT}}"
export BACKEND_BASE_URL="${BACKEND_BASE_URL:-${DCHUB_API_BASE}}"
echo "[MCP] DCHUB_API_BASE=${DCHUB_API_BASE}"

# Wait for Flask before launching MCP (avoids start-order crash)
echo "[MCP] Waiting up to 90s for Flask..."
for i in $(seq 1 45); do
  if curl -fsS "${DCHUB_API_BASE}/api/health" -m 2 >/dev/null 2>&1; then
    echo "[MCP] Flask ready (took ${i} attempts)"; break
  fi
  sleep 2
done

pkill -f "python dchub_mcp_server.py" 2>/dev/null
fuser -k 8888/tcp 2>/dev/null
sleep 1

BACKOFF=3; MAX_BACKOFF=60; ATTEMPT=0; HEALTHY_THRESHOLD=120

while true; do
  ATTEMPT=$((ATTEMPT+1)); START=$(date +%s)
  echo "[MCP] Starting (attempt #$ATTEMPT, backoff=${BACKOFF}s)"
  python dchub_mcp_server.py --port 8888
  RUNTIME=$(( $(date +%s) - START ))
  echo "[MCP] Exited after ${RUNTIME}s"
  if [ $RUNTIME -gt $HEALTHY_THRESHOLD ]; then
    BACKOFF=3
  else
    BACKOFF=$((BACKOFF * 2)); [ $BACKOFF -gt $MAX_BACKOFF ] && BACKOFF=$MAX_BACKOFF
  fi
  fuser -k 8888/tcp 2>/dev/null
  sleep $BACKOFF
done
