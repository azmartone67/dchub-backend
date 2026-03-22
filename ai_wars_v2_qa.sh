#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# AI Wars v2 — Full QA Script
# ═══════════════════════════════════════════════════════════════
# Run from Railway shell, Replit shell, or local terminal
# Usage: bash ai_wars_v2_qa.sh
# ═══════════════════════════════════════════════════════════════

BASE="https://dchub.cloud"
ADMIN_KEY="f4f961b15334c7b3a570681354638ed5"
PASS=0
FAIL=0

echo "═══════════════════════════════════════════════════════════"
echo "  AI Wars v2 QA — $(date)"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ─── TEST 1: Schedule endpoint (shows API keys + platform count) ───
echo "TEST 1: Schedule endpoint"
SCHED=$(curl -s "$BASE/api/v1/ai-wars/schedule")
if echo "$SCHED" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('success')==True; print('  ✅ Schedule OK — battles:', d.get('total_battles'), '  keys:', d.get('api_keys_available',[])); print('  Platforms w/ MCP:', d.get('platforms_with_mcp',[]))" 2>/dev/null; then
    PASS=$((PASS+1))
else
    echo "  ❌ Schedule failed: $SCHED"
    FAIL=$((FAIL+1))
fi
echo ""

# ─── TEST 2: Async submit-challenge (should return 202 with queue_id) ───
echo "TEST 2: Async submit-challenge (should return 202)"
SUBMIT=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/api/v1/ai-wars/submit-challenge" \
    -H "Content-Type: application/json" \
    -d '{"question":"Which US market is best for a 200MW AI training campus in 2026?","category":"site-selection"}')
echo "  HTTP status: $SUBMIT"
if [ "$SUBMIT" = "202" ]; then
    echo "  ✅ Got 202 Accepted (async working)"
    PASS=$((PASS+1))
else
    echo "  ⚠️  Expected 202, got $SUBMIT (may still be sync mode)"
    FAIL=$((FAIL+1))
fi

# Get the queue_id
SUBMIT_BODY=$(curl -s -X POST "$BASE/api/v1/ai-wars/submit-challenge" \
    -H "Content-Type: application/json" \
    -d '{"question":"Compare Dallas vs Phoenix for a 100MW hyperscale data center using DC Hub data","category":"site-selection"}')
QUEUE_ID=$(echo "$SUBMIT_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('queue_id',''))" 2>/dev/null)
echo "  Queue ID: $QUEUE_ID"
echo "  Full response: $SUBMIT_BODY"
echo ""

# ─── TEST 3: Battle status polling ───
echo "TEST 3: Battle status polling"
if [ -n "$QUEUE_ID" ]; then
    echo "  Polling $QUEUE_ID (will check 6 times, 5s apart)..."
    for i in 1 2 3 4 5 6; do
        STATUS=$(curl -s "$BASE/api/v1/ai-wars/battle-status/$QUEUE_ID")
        STATE=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','unknown'))" 2>/dev/null)
        echo "  Poll $i: status=$STATE"
        if [ "$STATE" = "completed" ]; then
            echo "  ✅ Battle completed!"
            echo "$STATUS" | python3 -c "
import sys,json
d=json.load(sys.stdin)
b=d.get('battle',{})
print('  Winner:', b.get('winner','?'))
for r in b.get('results',[]):
    flags = []
    if r.get('had_real_response'): flags.append('LIVE')
    if r.get('used_mcp'): flags.append('MCP')
    flag_str = ' [' + ','.join(flags) + ']' if flags else ''
    print(f\"    {r['platform']:15s} score={r['overall']}{flag_str}\")
" 2>/dev/null
            PASS=$((PASS+1))
            break
        elif [ "$STATE" = "failed" ]; then
            echo "  ❌ Battle failed"
            echo "  Error: $(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error',''))" 2>/dev/null)"
            FAIL=$((FAIL+1))
            break
        fi
        sleep 5
    done
    if [ "$STATE" != "completed" ] && [ "$STATE" != "failed" ]; then
        echo "  ⏳ Still running after 30s — battle may take up to 2 min with all platforms"
    fi
else
    echo "  ⚠️  No queue_id — skipping poll test"
    FAIL=$((FAIL+1))
fi
echo ""

# ─── TEST 4: Validation - reject short questions ───
echo "TEST 4: Validation (short question rejected)"
VAL=$(curl -s -X POST "$BASE/api/v1/ai-wars/submit-challenge" \
    -H "Content-Type: application/json" \
    -d '{"question":"hi"}')
if echo "$VAL" | grep -q '"success":false'; then
    echo "  ✅ Short question correctly rejected"
    PASS=$((PASS+1))
else
    echo "  ❌ Should have rejected: $VAL"
    FAIL=$((FAIL+1))
fi
echo ""

# ─── TEST 5: Leaderboard ───
echo "TEST 5: Leaderboard"
curl -s "$BASE/api/ai-wars/leaderboard" | python3 -c "
import sys,json
d=json.load(sys.stdin)
lb = d.get('leaderboard',[])
print(f'  Platforms on leaderboard: {len(lb)}')
for p in lb[:8]:
    print(f\"    {p.get('rank','-'):>2}. {p.get('name','?'):15s} score={p.get('overall_score','?'):>5}  battles={p.get('total_battles','?'):>3}  wins={p.get('total_wins','?'):>3}\")
if len(lb) >= 2:
    print('  ✅ Leaderboard has data')
else:
    print('  ⚠️  Leaderboard may need more battles')
" 2>/dev/null
echo ""

# ─── TEST 6: Sync run-battle (admin) ───
echo "TEST 6: Sync run-battle (admin)"
RUN=$(curl -s -X POST "$BASE/api/v1/ai-wars/run-battle" \
    -H "Content-Type: application/json" \
    -H "X-Admin-Key: $ADMIN_KEY" \
    -d '{"question":"What is the most undervalued US data center market right now?","category":"stump-the-ai"}')
if echo "$RUN" | python3 -c "
import sys,json
d=json.load(sys.stdin)
if d.get('success'):
    print('  ✅ Battle ran:', d.get('battle_id'))
    print('  Winner:', d.get('winner'))
    for r in d.get('results',[])[:5]:
        real = '🟢 LIVE' if r.get('had_real_response') else '🟡 SIM'
        mcp = ' +MCP' if r.get('used_mcp') else ''
        print(f\"    {r['platform']:15s} score={r['overall']:>3} {real}{mcp}\")
else:
    print('  ❌ Error:', d.get('error','unknown'))
    exit(1)
" 2>/dev/null; then
    PASS=$((PASS+1))
else
    echo "  ❌ run-battle failed: $(echo "$RUN" | head -c 200)"
    FAIL=$((FAIL+1))
fi
echo ""

# ─── TEST 7: Check wars_battle_queue table ───
echo "TEST 7: Battle queue table exists"
QUEUE_CHECK=$(curl -s "$BASE/api/v1/ai-wars/schedule")
ACTIVE_Q=$(echo "$QUEUE_CHECK" | python3 -c "import sys,json; print(json.load(sys.stdin).get('active_queue','-1'))" 2>/dev/null)
if [ "$ACTIVE_Q" != "-1" ]; then
    echo "  ✅ Queue table exists (active_queue: $ACTIVE_Q)"
    PASS=$((PASS+1))
else
    echo "  ❌ Queue table may not exist"
    FAIL=$((FAIL+1))
fi
echo ""

# ─── TEST 8: Platforms check ───
echo "TEST 8: Active platforms"
curl -s "$BASE/api/v1/ai-wars/schedule" | python3 -c "
import sys,json
d=json.load(sys.stdin)
keys = d.get('api_keys_available',[])
mcp = d.get('platforms_with_mcp',[])
print(f'  API keys active: {len(keys)} — {keys}')
print(f'  MCP platforms:   {mcp}')
if len(keys) >= 1:
    print('  ✅ At least 1 API key configured')
else:
    print('  ⚠️  No API keys found — all responses will be simulated')
" 2>/dev/null
echo ""

# ─── SUMMARY ───
echo "═══════════════════════════════════════════════════════════"
echo "  QA Results: $PASS passed, $FAIL failed"
echo "═══════════════════════════════════════════════════════════"
