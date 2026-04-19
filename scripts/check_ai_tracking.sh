#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  DC Hub — Quick AI Tracking Health Check
#  Run from any terminal: bash check_ai_tracking.sh
# ═══════════════════════════════════════════════════════════

BASE="https://dchub.cloud"
echo "═══════════════════════════════════════════════════════"
echo "  DC Hub AI Tracking — Quick Health Check"
echo "  $(date)"
echo "═══════════════════════════════════════════════════════"

# 1. Overall AI tracking stats
echo ""
echo "📊 1. AI TRACKING STATS"
echo "───────────────────────────────────────────────────────"
curl -s "$BASE/api/ai/tracking" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(f'  All-time requests:  {d.get(\"total_requests_all_time\", \"?\"):>10}')
    print(f'  Today requests:     {d.get(\"total_requests_today\", \"?\"):>10}')
    print(f'  Platforms active:   {d.get(\"platforms_active\", \"?\"):>10}')
    platforms = d.get('platforms', {})
    if platforms:
        print()
        print(f'  {\"Platform\":<15} {\"Total\":>8} {\"7-day\":>8} {\"Last Seen\":>22}')
        print(f'  {\"─\"*15} {\"─\"*8} {\"─\"*8} {\"─\"*22}')
        for name, info in sorted(platforms.items(), key=lambda x: x[1].get('total_requests',0), reverse=True):
            total = info.get('total_requests', 0)
            week = info.get('requests_7d', 0)
            last = info.get('last_seen', '—')[:19]
            print(f'  {name:<15} {total:>8,} {week:>8,} {last:>22}')
except Exception as e:
    print(f'  ❌ Parse error: {e}')
    print(f'  Raw: {sys.stdin.read()[:200]}')
" 2>/dev/null || echo "  ❌ Failed to reach $BASE/api/ai/tracking"

# 2. MCP endpoint health
echo ""
echo "🔌 2. MCP ENDPOINT STATUS"
echo "───────────────────────────────────────────────────────"
MCP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/mcp" -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","method":"initialize","id":1}')
echo "  MCP endpoint ($BASE/mcp): HTTP $MCP_STATUS"
if [ "$MCP_STATUS" = "200" ]; then
    echo "  ✅ MCP is responding"
elif [ "$MCP_STATUS" = "403" ]; then
    echo "  ⚠️  403 Forbidden — may need auth or POST with correct headers"
else
    echo "  ❌ Unexpected status"
fi

# 3. Discovery files
echo ""
echo "📄 3. DISCOVERY FILES"
echo "───────────────────────────────────────────────────────"
for path in "/llms.txt" "/.well-known/ai-plugin.json" "/AGENTS.md" "/.well-known/openapi.json"; do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "$BASE$path")
    if [ "$STATUS" = "200" ]; then
        echo "  ✅ $path  (HTTP $STATUS)"
    else
        echo "  ❌ $path  (HTTP $STATUS)"
    fi
done

# 4. API health
echo ""
echo "🏥 4. API HEALTH"
echo "───────────────────────────────────────────────────────"
curl -s "$BASE/api/health" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    status = d.get('status', '?')
    emoji = '✅' if status == 'healthy' else '❌'
    print(f'  {emoji} Status: {status}')
    print(f'  Database: {d.get(\"database\", \"?\")}')
    print(f'  Version:  {d.get(\"version\", \"?\")}')
except:
    print('  ❌ Could not parse health response')
" 2>/dev/null || echo "  ❌ Failed to reach $BASE/api/health"

# 5. Recent AI platform activity (via Neon direct if available)
echo ""
echo "🤖 5. AI PLATFORM ACTIVITY (ai_cumulative)"
echo "───────────────────────────────────────────────────────"
curl -s "$BASE/api/ai/platforms" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    if isinstance(d, list):
        for p in sorted(d, key=lambda x: x.get('total_requests',0), reverse=True)[:10]:
            name = p.get('platform', '?')
            total = p.get('total_requests', 0)
            last = p.get('last_seen', '—')[:19] if p.get('last_seen') else '—'
            print(f'  {name:<20} {total:>8,}  last: {last}')
    elif isinstance(d, dict) and 'platforms' in d:
        for name, info in d['platforms'].items():
            total = info.get('total_requests', 0)
            print(f'  {name:<20} {total:>8,}')
    else:
        print(f'  Response: {json.dumps(d)[:200]}')
except:
    print('  Could not parse (endpoint may not exist)')
" 2>/dev/null

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  Done! Run again later to compare."
echo "═══════════════════════════════════════════════════════"
