#!/bin/bash

echo "=== Self-healing startup ==="

# Kill any stale MCP server on port 8888
echo "Clearing port 8888..."
pkill -f "python dchub_mcp_server.py" 2>/dev/null
fuser -k 8888/tcp 2>/dev/null
sleep 1

MAX_RETRIES=3
RETRY=0
while [ $RETRY -lt $MAX_RETRIES ]; do
    echo "Starting MCP server on port 8888 (attempt $((RETRY+1))/$MAX_RETRIES)..."
    python dchub_mcp_server.py --port 8888
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 0 ]; then
        break
    fi
    RETRY=$((RETRY+1))
    if [ $RETRY -lt $MAX_RETRIES ]; then
        echo "MCP server exited (code $EXIT_CODE), restarting in 3 seconds..."
        fuser -k 8888/tcp 2>/dev/null
        sleep 3
    else
        echo "MCP server failed after $MAX_RETRIES attempts, giving up."
    fi
done
