#!/bin/bash
# DC Hub Job Endpoint Health Check
# Run from Replit shell: bash test_all_jobs.sh
# Requires DCHUB_ADMIN_KEY env var set

BASE="https://dchub-backend-production.up.railway.app"
KEY="$DCHUB_ADMIN_KEY"

if [ -z "$KEY" ]; then
  echo "❌ DCHUB_ADMIN_KEY not set"
  exit 1
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  DC Hub Job Endpoint Health Check"
echo "  $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

JOBS=(
  "keep-alive"
  "news-refresh"
  "discovery"
  "auto-approve"
  "autopilot"
  "ai-outreach"
  "ai-ecosystem"
  "autonomous-brain"
  "alert-emails"
  "simple-alerts"
  "market-report"
  "infrastructure-sync"
  "content-publish"
  "energy-discovery"
  "capacity-headroom"
  "ambassador"
  "evolution"
  "global-intelligence"
)

PASS=0
FAIL=0
TIMEOUT=0

for job in "${JOBS[@]}"; do
  printf "%-25s → " "$job"
  RESPONSE=$(curl -s -o /tmp/job_resp.txt -w "%{http_code}" \
    -X POST "$BASE/api/jobs/$job" \
    -H "X-Admin-Key: $KEY" \
    -H "Content-Type: application/json" \
    --max-time 15 2>/dev/null)
  
  if [ $? -ne 0 ]; then
    echo "⏱️  TIMEOUT"
    ((TIMEOUT++))
  elif [ "$RESPONSE" = "200" ]; then
    echo "✅ $RESPONSE"
    ((PASS++))
  else
    ERROR=$(cat /tmp/job_resp.txt | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('error','unknown')[:60])" 2>/dev/null || cat /tmp/job_resp.txt | head -c 60)
    echo "❌ $RESPONSE — $ERROR"
    ((FAIL++))
  fi
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Results: ✅ $PASS passed  ❌ $FAIL failed  ⏱️ $TIMEOUT timeout"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
