#!/usr/bin/env bash
set -euo pipefail
ACC=4bb33ec40ef02f9f4b41dc97668d5a52
API="https://api.cloudflare.com/client/v4/accounts/$ACC/workers/scripts/dchubapiproxy"
: "${CLOUDFLARE_API_TOKEN:?need CLOUDFLARE_API_TOKEN}"
[[ ! -f worker.js ]] && { echo "ERROR: worker.js missing"; exit 1; }
grep -q "search/semantic" worker.js || { echo "ERROR: worker.js missing semantic endpoint"; exit 1; }
echo "-> worker.js: $(wc -l < worker.js) lines"

# Use keep_bindings so we don't need KV/R2 write perms — only adding Vectorize + AI
python3 <<'PYEOF' > /tmp/meta-v47.json
import json
print(json.dumps({
  "main_module": "worker.js",
  "compatibility_date": "2026-04-22",
  "compatibility_flags": ["nodejs_compat"],
  "keep_bindings": ["kv_namespace", "r2_bucket", "secret_text"],
  "bindings": [
    {"type": "vectorize", "name": "VECTORIZE", "index_name": "dchub-facilities"},
    {"type": "ai",        "name": "AI"},
  ]
}))
PYEOF

echo "-> Adding new bindings (preserving existing KV/R2/secrets):"
echo "  Vectorize VECTORIZE -> dchub-facilities"
echo "  AI        AI"

echo
echo "-> Uploading v4.7..."
RESP=$(curl -sS -w '\nHTTP=%{http_code}' -X PUT \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -F "metadata=</tmp/meta-v47.json;type=application/json" \
  -F "worker.js=@worker.js;type=application/javascript+module" "$API")
HTTP=$(echo "$RESP" | tail -1 | sed 's/HTTP=//')
echo "  HTTP $HTTP"
echo "$RESP" | sed '$d' | python3 -m json.tool 2>/dev/null | head -25 || true
[[ "$HTTP" != "200" ]] && exit 1
echo "-> Deployed v4.7 (KV/R2/secrets preserved from previous deployment)."
