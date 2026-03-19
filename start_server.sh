#!/bin/bash
set -e

echo "============================================"
echo "  DC Hub Nexus — Self-Healing Server v2.0"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "============================================"

python3 -c "import py_compile; py_compile.compile('main.py', doraise=True)" 2>&1
if [ $? -ne 0 ]; then
    echo "[FATAL] main.py has syntax errors — cannot start"
    echo "[FATAL] Waiting 30s then retrying..."
    sleep 30
    exec "$0"
fi
echo "[OK] Syntax check passed"

./start_mcp.sh &
sleep 2

GUNICORN_CMD="gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 4 --timeout 300 --graceful-timeout 30 --keep-alive 5 --max-requests 500 --max-requests-jitter 50 main:app"

MAX_CRASHES=10
CRASH_COUNT=0
CRASH_WINDOW_START=$(date +%s)

while true; do
    echo "[$(date -u '+%H:%M:%S')] Starting gunicorn (crash count: $CRASH_COUNT)..."
    $GUNICORN_CMD
    EXIT_CODE=$?

    NOW=$(date +%s)
    ELAPSED=$((NOW - CRASH_WINDOW_START))

    if [ $ELAPSED -gt 3600 ]; then
        CRASH_COUNT=0
        CRASH_WINDOW_START=$NOW
    fi

    CRASH_COUNT=$((CRASH_COUNT + 1))

    if [ $CRASH_COUNT -ge $MAX_CRASHES ]; then
        echo "[FATAL] $MAX_CRASHES crashes in the last hour — stopping"
        exit 1
    fi

    echo "[$(date -u '+%H:%M:%S')] Gunicorn exited (code $EXIT_CODE). Restarting in 5s... ($CRASH_COUNT/$MAX_CRASHES)"

    fuser -k 5000/tcp 2>/dev/null
    fuser -k 8888/tcp 2>/dev/null
    pkill -f "python dchub_mcp_server.py" 2>/dev/null
    sleep 5

    ./start_mcp.sh &
    sleep 2
done
