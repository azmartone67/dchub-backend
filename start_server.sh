#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "============================================"
echo "  DC Hub Nexus — Self-Healing Server v2.1"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "============================================"

# ===================================================================
# GitHub auto-sync
# - Source of truth: origin/main on azmartone67/dchub-backend
# - Preserves local uncommitted work (skips sync if dirty)
# - Honors a .no-sync kill-switch file for manual debugging sessions
# ===================================================================
sync_with_github() {
    if ! git rev-parse --git-dir >/dev/null 2>&1; then
        echo "[sync] Not a git repo — skipping"
        return 1
    fi
    if [ -f .no-sync ]; then
        echo "[sync] .no-sync flag present — skipping"
        return 1
    fi
    if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
        echo "[sync] Uncommitted local changes — skipping to preserve local work"
        return 1
    fi
    if ! git fetch origin main 2>&1 | sed 's/^/[sync] /'; then
        echo "[sync] Fetch failed — proceeding with current code"
        return 1
    fi
    local before="$(git rev-parse --short HEAD)"
    if git merge --ff-only origin/main >/dev/null 2>&1; then
        local after="$(git rev-parse --short HEAD)"
        if [ "$before" != "$after" ]; then
            echo "[sync] Fast-forwarded $before → $after"
        else
            echo "[sync] Already up to date ($after)"
        fi
        return 0
    else
        echo "[sync] Cannot fast-forward (diverged from origin/main) — skipping"
        return 1
    fi
}

# Initial sync before any code runs
sync_with_github || true

# ===================================================================
# Syntax guard — bail early if main.py is broken
# ===================================================================
python3 -c "import py_compile; py_compile.compile('main.py', doraise=True)" 2>&1
if [ $? -ne 0 ]; then
    echo "[FATAL] main.py has syntax errors — cannot start"
    echo "[FATAL] Waiting 30s then retrying..."
    sleep 30
    exec "$0"
fi
echo "[OK] Syntax check passed"

# ===================================================================
# Start MCP sidecar
# ===================================================================
./start_mcp.sh &
sleep 2

# ===================================================================
# Background GitHub watcher
# - Polls origin/main every 5 minutes
# - On new commit: ff-merges, validates syntax, signals gunicorn restart
# - Signal mechanism: touches .watcher-restart, kills gunicorn on :5000
#   Main supervisor loop sees the flag and doesn't count it as a crash
# ===================================================================
WATCHER_INTERVAL=300

watcher_loop() {
    while true; do
        sleep "$WATCHER_INTERVAL"
        local local_sha remote_sha
        local_sha="$(git rev-parse HEAD 2>/dev/null || echo '')"
        if ! git fetch origin main 2>/dev/null; then
            continue
        fi
        remote_sha="$(git rev-parse origin/main 2>/dev/null || echo '')"
        [ -z "$remote_sha" ] && continue
        [ "$local_sha" = "$remote_sha" ] && continue

        echo "[watcher] Upstream changed: ${local_sha:0:7} → ${remote_sha:0:7}"

        if ! sync_with_github; then
            echo "[watcher] Sync refused — staying on current revision"
            continue
        fi

        if ! python3 -c "import py_compile; py_compile.compile('main.py', doraise=True)" 2>&1; then
            echo "[watcher] New code fails syntax check — staying on current revision"
            continue
        fi

        echo "[watcher] New code validated — restarting gunicorn"
        touch .watcher-restart
        fuser -k 5000/tcp 2>/dev/null || true
    done
}

# Kill any watcher left over from a previous invocation
if [ -f .watcher.pid ] && kill -0 "$(cat .watcher.pid)" 2>/dev/null; then
    kill "$(cat .watcher.pid)" 2>/dev/null || true
fi

watcher_loop &
WATCHER_PID=$!
echo "$WATCHER_PID" > .watcher.pid
echo "[OK] GitHub watcher started (pid $WATCHER_PID, every ${WATCHER_INTERVAL}s)"

# ===================================================================
# Gunicorn supervisor
# ===================================================================
GUNICORN_CMD="gunicorn --bind 0.0.0.0:5000 --workers 1 --threads 4 --timeout 300 --graceful-timeout 30 --keep-alive 5 --max-requests 500 --max-requests-jitter 50 main:app"

MAX_CRASHES=10
CRASH_COUNT=0
CRASH_WINDOW_START=$(date +%s)

while true; do
    echo "[$(date -u '+%H:%M:%S')] Starting gunicorn (crash count: $CRASH_COUNT)..."
    $GUNICORN_CMD
    EXIT_CODE=$?

    # Was this a watcher-triggered restart? If so, don't count it as a crash.
    if [ -f .watcher-restart ]; then
        rm -f .watcher-restart
        echo "[$(date -u '+%H:%M:%S')] Gunicorn exited for GitHub-triggered deploy — not counted as crash"
    else
        NOW=$(date +%s)
        ELAPSED=$((NOW - CRASH_WINDOW_START))
        if [ $ELAPSED -gt 3600 ]; then
            CRASH_COUNT=0
            CRASH_WINDOW_START=$NOW
        fi
        CRASH_COUNT=$((CRASH_COUNT + 1))
        if [ $CRASH_COUNT -ge $MAX_CRASHES ]; then
            echo "[FATAL] $MAX_CRASHES crashes in the last hour — stopping"
            kill "$WATCHER_PID" 2>/dev/null || true
            rm -f .watcher.pid
            exit 1
        fi
        echo "[$(date -u '+%H:%M:%S')] Gunicorn exited (code $EXIT_CODE). Restarting in 5s... ($CRASH_COUNT/$MAX_CRASHES)"
    fi

    fuser -k 5000/tcp 2>/dev/null || true
    fuser -k 8888/tcp 2>/dev/null || true
    pkill -f "python dchub_mcp_server.py" 2>/dev/null || true
    sleep 5

    ./start_mcp.sh &
    sleep 2
done
