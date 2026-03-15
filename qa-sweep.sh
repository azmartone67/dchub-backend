#!/bin/bash
# DC Hub Production QA Sweep
# Run from Replit shell: bash qa-sweep.sh
# Tests API, auth, plan display, and key validation against PRODUCTION

PROD="https://dchub.cloud"
EJ_KEY="dchub_pro_2b328908164a63507bca662d38c23a3c"
SCOTT_EMAIL="theterrills@gmail.com"
EJ_EMAIL="m18563991063@126.com"

PASS=0
FAIL=0
WARN=0

pass() { echo "  ✅ PASS: $1"; ((PASS++)); }
fail() { echo "  ❌ FAIL: $1"; ((FAIL++)); }
warn() { echo "  ⚠️  WARN: $1"; ((WARN++)); }

echo "============================================"
echo "  DC Hub Production QA Sweep"
echo "  $(date)"
echo "  Target: $PROD"
echo "============================================"
echo ""

# ---- 1. BASIC HEALTH ----
echo "── 1. BASIC HEALTH ──"

STATS=$(curl -s --max-time 15 "$PROD/api/v1/stats")
if echo "$STATS" | python3 -c "import sys,json; d=json.load(sys.stdin); assert d.get('stats') or d.get('total_facilities')" 2>/dev/null; then
    FAC_COUNT=$(echo "$STATS" | python3 -c "import sys,json; d=json.load(sys.stdin); s=d.get('stats',d); print(s.get('facilities',s.get('total_facilities','?')))")
    pass "Stats endpoint returns data ($FAC_COUNT facilities)"
else
    fail "Stats endpoint broken or empty"
    echo "    Response: $(echo $STATS | head -c 200)"
fi

HEALTH=$(curl -s --max-time 10 "$PROD/api/ecosystem/health")
if echo "$HEALTH" | grep -q "status"; then
    pass "Ecosystem health endpoint responding"
else
    warn "Ecosystem health endpoint not responding"
fi

echo ""

# ---- 2. API KEY VALIDATION ----
echo "── 2. API KEY VALIDATION ──"

# EJ's new Pro key
EJ_RESP=$(curl -s --max-time 15 -H "X-API-Key: $EJ_KEY" "$PROD/api/v1/facilities?limit=3")
EJ_TIER=$(echo "$EJ_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tier','none'))" 2>/dev/null)
EJ_NOTE=$(echo "$EJ_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('note',''))" 2>/dev/null)
EJ_COUNT=$(echo "$EJ_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',[])))" 2>/dev/null)
EJ_HAS_LAT=$(echo "$EJ_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print('latitude' in d.get('data',[{}])[0])" 2>/dev/null)

if echo "$EJ_RESP" | grep -q "invalid_api_key"; then
    fail "EJ's key ($EJ_KEY) returns INVALID API KEY"
elif echo "$EJ_NOTE" | grep -qi "free tier"; then
    fail "EJ's key returns FREE TIER results (rate_limit_tier not synced)"
elif [ "$EJ_HAS_LAT" = "True" ]; then
    pass "EJ's key returns Pro-tier fields (latitude, longitude present)"
else
    warn "EJ's key returns data but may be missing Pro fields"
fi

# Invalid key should be rejected
INVALID_RESP=$(curl -s --max-time 10 -H "X-API-Key: dchub_pro_fakekeynotreal" "$PROD/api/v1/facilities?limit=1")
if echo "$INVALID_RESP" | grep -q "invalid_api_key"; then
    pass "Invalid API key correctly rejected"
else
    fail "Invalid API key NOT rejected — possible security issue"
fi

# No key should return free-tier preview
NO_KEY_RESP=$(curl -s --max-time 10 "$PROD/api/v1/facilities?limit=3")
NO_KEY_COUNT=$(echo "$NO_KEY_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',[])))" 2>/dev/null)
if [ "$NO_KEY_COUNT" -le 5 ] 2>/dev/null; then
    pass "No-key request returns limited preview ($NO_KEY_COUNT results)"
else
    warn "No-key request returned $NO_KEY_COUNT results (expected ≤5)"
fi

echo ""

# ---- 3. AUTH / LOGIN ----
echo "── 3. AUTH / LOGIN ──"

# Scott's login
SCOTT_LOGIN=$(curl -s --max-time 15 -X POST "$PROD/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$SCOTT_EMAIL\",\"password\":\"DCHub2026!\"}")
SCOTT_PLAN=$(echo "$SCOTT_LOGIN" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('user',d).get('plan','MISSING'))" 2>/dev/null)
SCOTT_TOKEN=$(echo "$SCOTT_LOGIN" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token',''))" 2>/dev/null)

if [ "$SCOTT_PLAN" = "pro" ] || [ "$SCOTT_PLAN" = "founding" ]; then
    pass "Scott login returns plan=$SCOTT_PLAN"
elif echo "$SCOTT_LOGIN" | grep -qi "invalid\|error\|unauthorized"; then
    fail "Scott login FAILED: $(echo $SCOTT_LOGIN | head -c 200)"
else
    fail "Scott login returns plan=$SCOTT_PLAN (expected pro or founding)"
fi

# Test /api/auth/me with Scott's token
if [ -n "$SCOTT_TOKEN" ] && [ "$SCOTT_TOKEN" != "" ]; then
    ME_RESP=$(curl -s --max-time 10 -H "Authorization: Bearer $SCOTT_TOKEN" "$PROD/api/auth/me")
    ME_PLAN=$(echo "$ME_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('plan','MISSING'))" 2>/dev/null)
    if [ "$ME_PLAN" = "pro" ] || [ "$ME_PLAN" = "founding" ]; then
        pass "/api/auth/me returns plan=$ME_PLAN for Scott"
    else
        fail "/api/auth/me returns plan=$ME_PLAN for Scott (expected pro/founding)"
    fi
else
    warn "No token from Scott login — skipping /api/auth/me test"
fi

# EJ's login
EJ_LOGIN=$(curl -s --max-time 15 -X POST "$PROD/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$EJ_EMAIL\",\"password\":\"DCHub2026!\"}")
EJ_PLAN=$(echo "$EJ_LOGIN" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('user',d).get('plan','MISSING'))" 2>/dev/null)

if [ "$EJ_PLAN" = "pro" ] || [ "$EJ_PLAN" = "founding" ]; then
    pass "EJ login returns plan=$EJ_PLAN"
elif echo "$EJ_LOGIN" | grep -qi "invalid\|error\|unauthorized"; then
    warn "EJ login failed (may use Google OAuth, not password): $(echo $EJ_LOGIN | head -c 100)"
else
    fail "EJ login returns plan=$EJ_PLAN (expected pro or founding)"
fi

echo ""

# ---- 4. STATE FILTER ----
echo "── 4. FACILITY FILTERS ──"

VA_RESP=$(curl -s --max-time 15 -H "X-API-Key: $EJ_KEY" "$PROD/api/v1/facilities?state=VA&limit=3")
VA_STATES=$(echo "$VA_RESP" | python3 -c "
import sys,json
d=json.load(sys.stdin)
states=set(f.get('state','?') for f in d.get('data',[]))
print(','.join(states))
" 2>/dev/null)

if echo "$VA_STATES" | grep -q "VA"; then
    pass "State filter (VA) returns Virginia facilities"
elif [ -n "$VA_STATES" ]; then
    fail "State filter (VA) returns wrong states: $VA_STATES"
else
    warn "State filter returned no parseable results"
fi

# Market filter
MKT_RESP=$(curl -s --max-time 15 -H "X-API-Key: $EJ_KEY" "$PROD/api/v1/facilities?market=Phoenix&limit=3")
MKT_COUNT=$(echo "$MKT_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('data',[])))" 2>/dev/null)
if [ "$MKT_COUNT" -gt 0 ] 2>/dev/null; then
    pass "Market filter (Phoenix) returns $MKT_COUNT results"
else
    warn "Market filter (Phoenix) returned no results"
fi

echo ""

# ---- 5. NEWS ENDPOINT ----
echo "── 5. NEWS & TRANSACTIONS ──"

NEWS=$(curl -s --max-time 15 "$PROD/api/news/live?limit=5")
NEWS_COUNT=$(echo "$NEWS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('articles',[])))" 2>/dev/null)
if [ "$NEWS_COUNT" -gt 0 ] 2>/dev/null; then
    pass "News feed returns $NEWS_COUNT articles"
else
    warn "News feed returned 0 articles"
fi

TXNS=$(curl -s --max-time 15 "$PROD/api/transactions?limit=5")
TXN_COUNT=$(echo "$TXNS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('transactions',[])))" 2>/dev/null)
if [ "$TXN_COUNT" -gt 0 ] 2>/dev/null; then
    pass "Transactions returns $TXN_COUNT deals"
else
    warn "Transactions returned 0 deals"
fi

echo ""

# ---- 6. FRONTEND FILES ----
echo "── 6. FRONTEND FILES ──"

# api-config.js
API_CONFIG=$(curl -s --max-time 10 "$PROD/static/js/api-config.js")
if echo "$API_CONFIG" | grep -q "DCHUB_API_BASE"; then
    API_URL=$(echo "$API_CONFIG" | grep -o "https://[^'\"]*")
    pass "api-config.js loads (API_BASE=$API_URL)"
    if echo "$API_URL" | grep -q "replit"; then
        fail "api-config.js still points to REPLIT — should be Railway or dchub.cloud"
    fi
else
    fail "api-config.js NOT LOADING — dashboard will break for all users"
fi

# Check /js/api-config.js (should also work or 404 gracefully)
API_CONFIG_ALT=$(curl -s --max-time 10 "$PROD/js/api-config.js")
if echo "$API_CONFIG_ALT" | grep -q "404\|Not Found"; then
    warn "/js/api-config.js returns 404 (some pages may reference this path)"
elif echo "$API_CONFIG_ALT" | grep -q "DCHUB_API_BASE"; then
    pass "/js/api-config.js also loads correctly"
fi

# Dashboard accessible
DASH=$(curl -s --max-time 10 "$PROD/dashboard.html" | head -c 500)
if echo "$DASH" | grep -qi "dashboard\|DC Hub"; then
    pass "dashboard.html is accessible"
else
    warn "dashboard.html may not be loading correctly"
fi

echo ""

# ---- 7. STRIPE WEBHOOK ENDPOINT ----
echo "── 7. STRIPE & PAYMENT ──"

STRIPE_RESP=$(curl -s --max-time 10 -X POST "$PROD/api/stripe/webhook" -d '{}')
if echo "$STRIPE_RESP" | grep -qi "signature\|webhook\|error"; then
    pass "Stripe webhook endpoint exists and responds (expects signature)"
else
    warn "Stripe webhook endpoint response: $(echo $STRIPE_RESP | head -c 100)"
fi

echo ""

# ---- 8. MCP SERVER ----
echo "── 8. MCP SERVER ──"

MCP_RESP=$(curl -s --max-time 10 "$PROD/.well-known/mcp.json")
if echo "$MCP_RESP" | grep -q "dchub"; then
    pass "MCP manifest accessible"
else
    warn "MCP manifest not loading"
fi

echo ""

# ---- 9. DATABASE CONSISTENCY ----
echo "── 9. PLAN SYNC CHECK ──"
echo "  (Run these manually in Neon to verify)"
echo ""
echo "  -- Users with Pro/Founding plan but no API key:"
echo "  SELECT u.email, u.plan FROM users u"
echo "    LEFT JOIN api_keys ak ON u.id = ak.user_id"
echo "    WHERE u.plan IN ('pro','founding','enterprise')"
echo "    AND ak.id IS NULL;"
echo ""
echo "  -- API keys where rate_limit_tier doesn't match user plan:"
echo "  SELECT u.email, u.plan as user_plan, ak.rate_limit_tier, ak.key_prefix"
echo "    FROM users u JOIN api_keys ak ON u.id = ak.user_id"
echo "    WHERE u.plan != ak.rate_limit_tier"
echo "    AND u.plan IN ('pro','founding','enterprise');"
echo ""

# ---- SUMMARY ----
echo "============================================"
echo "  QA SWEEP RESULTS"
echo "============================================"
echo "  ✅ Passed: $PASS"
echo "  ❌ Failed: $FAIL"
echo "  ⚠️  Warnings: $WARN"
echo "============================================"

if [ $FAIL -gt 0 ]; then
    echo ""
    echo "  🚨 FAILURES DETECTED — DO NOT DEPLOY UNTIL FIXED"
elif [ $WARN -gt 0 ]; then
    echo ""
    echo "  ⚠️  Warnings present — review before proceeding"
else
    echo ""
    echo "  🎉 All clear — production is healthy"
fi
echo ""
