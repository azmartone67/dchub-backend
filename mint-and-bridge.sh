#!/usr/bin/env bash
# mint-and-bridge.sh
# Mint a new DC Hub API key via the worker, then bridge it into Railway's
# api_keys table so it works against ALL endpoints (CF + Railway).
#
# Usage: ./mint-and-bridge.sh <email> <plan>
#   plan: free | developer | pro | enterprise
#
# Env required: ADMIN_SECRET, DCHUB_DATABASE_URL
set -e

EMAIL="${1:-}"
PLAN="${2:-developer}"
[ -z "$EMAIL" ] && { echo "Usage: $0 <email> [plan]"; exit 1; }
: "${ADMIN_SECRET:?need ADMIN_SECRET}"
: "${DCHUB_DATABASE_URL:?need DCHUB_DATABASE_URL}"

echo "-> Minting key for $EMAIL (plan=$PLAN)..."
RESPONSE=$(curl -sS -X POST -H "X-Admin-Key: $ADMIN_SECRET" -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"plan\":\"$PLAN\"}" \
  https://dchub.cloud/api/admin/create-api-key)
echo "$RESPONSE" | python3 -m json.tool

KEY=$(echo "$RESPONSE" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("api_key",""))')
[ -z "$KEY" ] && { echo "ERROR: mint failed"; exit 1; }

KEY_HASH=$(echo -n "$KEY" | sha256sum | awk '{print $1}')
KEY_PREFIX="${KEY:0:12}"
USER_ID=$(echo "$EMAIL" | tr '@.' '__' | head -c 32)

echo
echo "-> Bridging into Railway api_keys (user_id=$USER_ID, hash=${KEY_HASH:0:12}...)..."
psql "$DCHUB_DATABASE_URL" -P pager=off <<SQL
INSERT INTO api_keys (key_hash, key_prefix, user_id, plan, is_active, created_at)
VALUES ('$KEY_HASH', '$KEY_PREFIX', '$USER_ID', '$PLAN', 1, NOW()::text)
ON CONFLICT (key_hash) DO UPDATE 
  SET is_active = 1, plan = '$PLAN';
SELECT key_prefix, plan, is_active, user_id FROM api_keys WHERE key_hash = '$KEY_HASH';
SQL

echo
echo "-> Smoke test: hitting /api/v1/facilities..."
HTTP=$(curl -sS -o /tmp/test.json -w '%{http_code}' -H "X-API-Key: $KEY" "https://dchub.cloud/api/v1/facilities?limit=1")
echo "  HTTP $HTTP"
if [ "$HTTP" = "200" ]; then
  echo "  ✓ Key works against Railway"
else
  cat /tmp/test.json | head -c 300
fi

echo
echo "================================================================"
echo "Key minted + bridged. Save this:"
echo "  $KEY"
echo "Plan: $PLAN | Email: $EMAIL"
echo "================================================================"
