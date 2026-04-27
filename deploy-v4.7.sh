#!/usr/bin/env bash
set -euo pipefail
ACC=4bb33ec40ef02f9f4b41dc97668d5a52
API="https://api.cloudflare.com/client/v4/accounts/$ACC/workers/scripts/dchubapiproxy"
: "${CLOUDFLARE_API_TOKEN:?need CLOUDFLARE_API_TOKEN}"
[[ ! -f worker.js ]] && { echo "ERROR: worker.js missing"; exit 1; }
grep -q "search/semantic" worker.js || { echo "ERROR: worker.js missing semantic endpoint"; exit 1; }
echo "-> worker.js: $(wc -l < worker.js) lines"

python3 <<'PYEOF' > /tmp/meta-v47.json
import json, os
b = [
  {"type": "kv_namespace", "name": "DCHUB_API_KEYS", "namespace_id": "e8b28fc7935b4047ba865e72e4c339b0"},
  {"type": "kv_namespace", "name": "DCHUB_CACHE",    "namespace_id": "88f7d45862894495967d5f2e438b29c3"},
  {"type": "kv_namespace", "name": "DCHUB_USAGE",    "namespace_id": "3912a99a41b24c3cbf0b6e245beb243c"},
  {"type": "r2_bucket",    "name": "NEWS_ARCHIVE",   "bucket_name": "dchub-news-archive"},
  {"type": "vectorize",    "name": "VECTORIZE",      "index_name": "dchub-facilities"},
  {"type": "ai",           "name": "AI"},
]
for s in ["ADMIN_SECRET","STRIPE_WEBHOOK_SECRET","CANARY_SECRET","PUBLISH_PROXY_SECRET","RAILWAY_PUBLISH_SECRET"]:
    if os.environ.get(s): b.append({"type": "secret_text", "name": s, "text": os.environ[s]})
print(json.dumps({"main_module":"worker.js","compatibility_date":"2026-04-22","compatibility_flags":["nodejs_compat"],"bindings":b}))
PYEOF

echo "-> Bindings:"
python3 <<'PYEOF'
import json
m = json.load(open("/tmp/meta-v47.json"))
for b in m["bindings"]:
    t = b["type"]; n = b["name"]
    if t == "secret_text":  print(f"  secret    {n}")
    elif t == "kv_namespace": print(f"  KV        {n}")
    elif t == "r2_bucket":  print(f"  R2        {n} -> {b['bucket_name']}")
    elif t == "vectorize":  print(f"  Vectorize {n} -> {b['index_name']}")
    elif t == "ai":         print(f"  AI        {n}")
PYEOF

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
echo "-> Deployed v4.7."
