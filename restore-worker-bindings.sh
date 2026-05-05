#!/usr/bin/env bash
# restore-worker-bindings.sh
# ----------------------------------------------------------------------
# Re-uploads the dchubapiproxy worker with all KV/R2/secret bindings
# that got dropped during the original API upload.
#
# Bindings restored:
#   KV: DCHUB_API_KEYS, DCHUB_CACHE, DCHUB_USAGE  (namespace IDs hardcoded below)
#   R2: NEWS_ARCHIVE → bucket dchub-news-archive
#   Secrets: ADMIN_SECRET, STRIPE_WEBHOOK_SECRET, CANARY_SECRET,
#            PUBLISH_PROXY_SECRET, RAILWAY_PUBLISH_SECRET
#
# Requires:
#   - worker.js in the current directory (paste from yesterday's session)
#   - CLOUDFLARE_API_TOKEN exported with Workers Scripts: Edit
#   - All the secret values exported (script will tell you which)
#
# Usage:
#   chmod +x restore-worker-bindings.sh
#   ./restore-worker-bindings.sh
# ----------------------------------------------------------------------

set -euo pipefail

ACC=4bb33ec40ef02f9f4b41dc97668d5a52
SCRIPT_NAME=dchubapiproxy
API="https://api.cloudflare.com/client/v4/accounts/$ACC/workers/scripts/$SCRIPT_NAME"

: "${CLOUDFLARE_API_TOKEN:?need CLOUDFLARE_API_TOKEN}"

if [[ ! -f "worker.js" ]]; then
  echo "ERROR: worker.js not found in current directory."
  echo "Copy your worker source from yesterday's session into ./worker.js first."
  exit 1
fi

# Verify worker.js looks legit
if ! grep -q "DC Hub API Proxy Worker" worker.js; then
  echo "ERROR: worker.js doesn't look like the proxy worker (missing header)."
  exit 1
fi

WJS_LINES=$(wc -l < worker.js | tr -d ' ')
echo "→ worker.js: $WJS_LINES lines"

# Secrets are optional — if not set, they'll just not be created/updated.
# (Existing secrets on the worker stay if not overwritten.)
SECRETS_JSON='[]'
SECRET_NAMES=()
for name in ADMIN_SECRET STRIPE_WEBHOOK_SECRET CANARY_SECRET PUBLISH_PROXY_SECRET RAILWAY_PUBLISH_SECRET; do
  val="${!name:-}"
  if [[ -n "$val" ]]; then
    SECRET_NAMES+=("$name")
  fi
done
echo "→ Secrets to set: ${SECRET_NAMES[*]:-(none — set them via env vars to update)}"

# Bindings metadata — KV, R2, and any secrets that are exported
python3 - "$@" <<PYEOF > /tmp/metadata.json
import json, os, sys

bindings = [
    {"type": "kv_namespace", "name": "DCHUB_API_KEYS", "namespace_id": "e8b28fc7935b4047ba865e72e4c339b0"},
    {"type": "kv_namespace", "name": "DCHUB_CACHE",    "namespace_id": "88f7d45862894495967d5f2e438b29c3"},
    {"type": "kv_namespace", "name": "DCHUB_USAGE",    "namespace_id": "3912a99a41b24c3cbf0b6e245beb243c"},
    {"type": "r2_bucket",    "name": "NEWS_ARCHIVE",   "bucket_name": "dchub-news-archive"},
]

# Add any exported secrets
for s in ["ADMIN_SECRET","STRIPE_WEBHOOK_SECRET","CANARY_SECRET","PUBLISH_PROXY_SECRET","RAILWAY_PUBLISH_SECRET"]:
    if os.environ.get(s):
        bindings.append({"type": "secret_text", "name": s, "text": os.environ[s]})

meta = {
    "main_module": "worker.js",
    "compatibility_date": "2026-04-22",
    "compatibility_flags": ["nodejs_compat"],
    "bindings": bindings,
}
print(json.dumps(meta))
PYEOF

echo "→ Bindings being restored:"
python3 <<'PYEOF'
import json
m = json.load(open("/tmp/metadata.json"))
for b in m["bindings"]:
    name = b["name"]
    if b["type"] == "secret_text":
        print(f"  secret  {name}")
    elif b["type"] == "kv_namespace":
        print(f"  KV      {name:20s} -> {b['namespace_id']}")
    elif b["type"] == "r2_bucket":
        print(f"  R2      {name:20s} -> {b['bucket_name']}")
PYEOF

echo
echo "→ Uploading worker with bindings..."
RESP=$(curl -sS -w '\nHTTP_CODE=%{http_code}' -X PUT \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -F "metadata=</tmp/metadata.json;type=application/json" \
  -F "worker.js=@worker.js;type=application/javascript+module" \
  "$API")

HTTP=$(echo "$RESP" | tail -1 | sed 's/HTTP_CODE=//')
BODY=$(echo "$RESP" | sed '$d')

echo "  HTTP $HTTP"
echo "$BODY" | python3 -m json.tool 2>/dev/null | head -25 || echo "$BODY"

if [[ "$HTTP" != "200" ]]; then
  echo
  echo "ERROR: upload failed."
  exit 1
fi

echo
echo "→ Verify bindings stuck:"
sleep 3
curl -sS -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  "$API" > /tmp/worker-info.json

python3 <<'PYEOF'
import json
d = json.load(open("/tmp/worker-info.json"))
r = d.get("result", {})
hm = r.get("has_modules")
ha = r.get("has_assets")
hd = r.get("handlers")
print(f"  has_modules: {hm}, has_assets: {ha}")
print(f"  handlers:    {hd}")
PYEOF

echo
echo "✓ Done. Now retry:"
echo "  curl -sS -X POST -H \"X-Admin-Key: \$ADMIN_SECRET\" \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{\"email\":\"vector-pipeline@dchub.cloud\",\"plan\":\"developer\"}' \\"
echo "    https://dchub.cloud/api/admin/create-api-key | python3 -m json.tool"
