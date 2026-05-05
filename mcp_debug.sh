#!/bin/bash
# DC Hub MCP Debug Script — Run in Railway Shell
# Purpose: Diagnose why MCP process crashes every 10-15 minutes
# while REST API stays healthy

echo "=== DC Hub MCP Debug ==="
echo "Date: $(date)"
echo ""

echo "1. Check if main.py is running"
ps aux | grep -i "python\|uvicorn\|gunicorn\|main.py" | grep -v grep
echo ""

echo "2. Memory usage"
free -m
echo ""

echo "3. Check Railway logs for MCP errors (last 50 lines with 'mcp' or 'error')"
# Railway logs are typically available via railway logs command
# In shell, check stderr/stdout
echo "--- Looking for crash patterns ---"
grep -i "mcp\|error\|traceback\|exception\|killed\|OOM\|connection\|pool" /tmp/*.log 2>/dev/null | tail -30
echo ""

echo "4. Check database connections"
psql $DATABASE_URL -c "SELECT count(*) as active_connections FROM pg_stat_activity WHERE state = 'active';" 2>/dev/null
psql $DATABASE_URL -c "SELECT count(*) as total_connections FROM pg_stat_activity;" 2>/dev/null
psql $DATABASE_URL -c "SELECT max_conn FROM (SELECT setting::int as max_conn FROM pg_settings WHERE name='max_connections') t;" 2>/dev/null
echo ""

echo "5. Check MCP endpoint directly"
curl -s -o /dev/null -w "HTTP %{http_code} (%{time_total}s)" http://localhost:8080/mcp 2>/dev/null
echo ""
curl -s -o /dev/null -w "HTTP %{http_code} (%{time_total}s)" http://localhost:8080/api/v1/stats 2>/dev/null
echo ""

echo "6. Check DCHUB_API_BASE (must NOT be localhost)"
echo "DCHUB_API_BASE = $DCHUB_API_BASE"
grep -n "DCHUB_API_BASE\|api_base\|127.0.0.1" /app/dchub_mcp_server.py 2>/dev/null | head -5
echo ""

echo "7. Check for connection pool settings"
grep -n "pool\|max_conn\|pool_size\|create_pool\|_pg_pool\|asyncpg" /app/main.py 2>/dev/null | head -10
echo ""

echo "8. MCP proxy handler — look for connection leak"
grep -n "async def.*mcp\|await.*pool\|await.*connect\|finally:\|\.close()\|release()" /app/main.py 2>/dev/null | head -20
echo ""

echo "=== KEY THINGS TO CHECK ==="
echo "- If connections are near max: connection pool leak in MCP handler"
echo "- If DCHUB_API_BASE is localhost: deadlock (MCP calls itself)"
echo "- If memory is high: memory leak in MCP response handling"
echo "- If process keeps restarting: check Railway deployment logs"
echo ""
echo "=== QUICK FIX: Restart the service ==="
echo "If MCP is down right now, the fastest fix is:"
echo "  railway service restart"
echo "or redeploy from the Railway dashboard"
