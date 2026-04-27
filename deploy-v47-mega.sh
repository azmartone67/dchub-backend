#!/usr/bin/env bash
set -e
cd ~/workspace
[ -z "${ACC:-}" ] && export ACC=4bb33ec40ef02f9f4b41dc97668d5a52
[ -z "${DCHUB_API_KEY:-}" ] && export DCHUB_API_KEY=dchub_owner_azmartone_ent_2026
: "${CLOUDFLARE_API_TOKEN:?need CLOUDFLARE_API_TOKEN}"
echo "Env: TOKEN ${#CLOUDFLARE_API_TOKEN} | KEY ${#DCHUB_API_KEY} | ACC=$ACC"
echo "-> Pulling worker.js..."
HTTP=$(curl -sS -o worker.js.tmp -w '%{http_code}' \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  "https://api.cloudflare.com/client/v4/accounts/$ACC/workers/scripts/dchubapiproxy/content")
echo "  HTTP $HTTP"
if [ "$HTTP" != "200" ]; then
  echo "  retrying without /content..."
  curl -sS -o worker.js.tmp -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    "https://api.cloudflare.com/client/v4/accounts/$ACC/workers/scripts/dchubapiproxy"
fi
echo "  downloaded $(wc -c < worker.js.tmp) bytes"
head -c 200 worker.js.tmp
echo
if ! grep -q "DC Hub API Proxy Worker" worker.js.tmp; then
  echo "ERROR: not a worker"; exit 1
fi
mv worker.js.tmp worker.js
echo "  worker.js: $(wc -l < worker.js) lines"
