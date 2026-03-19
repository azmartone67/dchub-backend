#!/bin/bash
# =============================================================================
# MCP Server Launcher — Self-healing infinite restart loop
# Replaces the old 3-retry-then-die version.
# The MCP server MUST stay alive for the /mcp proxy in main.py to work.
# =============================================================================

echo "=== MCP Server Launcher (self-healing) ==="

# Kill any stale MCP server on port 8888
echo "Clearing port 8888..."
pkill -f "python dchub_mcp_server.py" 2>/dev/null
fuser -k 8888/tcp 2>/dev/null
sleep 1

BACKOFF=3          # Initial restart delay (seconds)
MAX_BACKOFF=60     # Cap backoff at 60 seconds
ATTEMPT=0
HEALTHY_THRESHOLD=120  # If process runs >120s, reset backoff (it was healthy)

while true; do
    ATTEMPT=$((ATTEMPT+1))
    START_TIME=$(date +%s)
    echo "[MCP] Starting dchub_mcp_server.py on port 8888 (attempt #$ATTEMPT, backoff=${BACKOFF}s)..."
    
    python dchub_mcp_server.py --port 8888
    EXIT_CODE=$?
    END_TIME=$(date +%s)
    RUNTIME=$((END_TIME - START_TIME))
    
    echo "[MCP] Process exited (code $EXIT_CODE) after ${RUNTIME}s"
    
    # If it ran for a while, it was healthy — reset backoff
    if [ $RUNTIME -gt $HEALTHY_THRESHOLD ]; then
        BACKOFF=3
        echo "[MCP] Was healthy for ${RUNTIME}s — resetting backoff to ${BACKOFF}s"
    else
        # Exponential backoff for rapid crashes
        BACKOFF=$((BACKOFF * 2))
        if [ $BACKOFF -gt $MAX_BACKOFF ]; then
            BACKOFF=$MAX_BACKOFF
        fi
        echo "[MCP] Crashed quickly (${RUNTIME}s) — increasing backoff to ${BACKOFF}s"
    fi
    
    # Clean up port before restart
    fuser -k 8888/tcp 2>/dev/null
    
    echo "[MCP] Restarting in ${BACKOFF}s..."
    sleep $BACKOFF
done
