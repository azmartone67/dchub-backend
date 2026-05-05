#!/usr/bin/env bash
# setup-vectorize.sh
# ----------------------------------------------------------------------
# Stand up a Cloudflare Vectorize index over your 20k+ facilities so AI
# agents can do natural-language semantic search ("30MW data centers in
# PJM with renewable PPAs" instead of structured filter queries).
#
# Pipeline:
#   1. Create index "dchub-facilities" (768-dim, cosine)
#   2. Pull facilities in pages from dchub.cloud/api/v1/facilities
#   3. Build a one-line description per facility
#   4. Embed via Cloudflare Workers AI (@cf/baai/bge-base-en-v1.5 → 768 dim)
#   5. Upsert vectors with metadata (id, name, state, mw, status)
#
# REQUIRED API TOKEN PERMISSIONS (additions to your existing token):
#   Account → Vectorize          → Edit
#   Account → Workers AI         → Read
#
#   If the token rejects with 403/10000, recreate it with the broader scopes.
#
# REQUIRED ENV VARS:
#   CLOUDFLARE_API_TOKEN          (already exported)
#   DCHUB_API_KEY                 (Developer or higher tier; needed to page
#                                  past 2-row anon clamp on /api/v1/facilities)
#
# Usage:
#   chmod +x setup-vectorize.sh
#   ./setup-vectorize.sh             # creates index + upserts ALL facilities
#   ./setup-vectorize.sh --dry-run   # preview without upserting
#   ./setup-vectorize.sh --limit 100 # only first 100 facilities (cheap test)
# ----------------------------------------------------------------------

set -euo pipefail

ACC=4bb33ec40ef02f9f4b41dc97668d5a52
INDEX=dchub-facilities
DIM=768
METRIC=cosine
EMBED_MODEL='@cf/baai/bge-base-en-v1.5'
DCHUB_API="${DCHUB_API:-https://dchub.cloud/api/v1}"
PAGE_SIZE="${PAGE_SIZE:-100}"

DRY_RUN=0
LIMIT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --limit)   LIMIT="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

: "${CLOUDFLARE_API_TOKEN:?need CLOUDFLARE_API_TOKEN}"
: "${DCHUB_API_KEY:?need DCHUB_API_KEY (Developer tier+ to bypass anon clamp)}"

CF_API="https://api.cloudflare.com/client/v4/accounts/$ACC"

# ----------------------------------------------------------------------
# Step 1: Create the Vectorize index (idempotent)
# ----------------------------------------------------------------------
echo "→ Step 1: Creating Vectorize index '$INDEX' ($DIM dim, $METRIC)..."
CREATE_RESP=$(curl -sS -X POST \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  "$CF_API/vectorize/v2/indexes" \
  -d "{
    \"name\": \"$INDEX\",
    \"description\": \"DC Hub facilities semantic search index\",
    \"config\": {\"dimensions\": $DIM, \"metric\": \"$METRIC\"}
  }")

if echo "$CREATE_RESP" | grep -q '"success":true'; then
  echo "  ✓ index created"
elif echo "$CREATE_RESP" | grep -qi 'already exists\|duplicate'; then
  echo "  ✓ index already exists (continuing)"
else
  echo "  ⚠ unexpected response:"
  echo "$CREATE_RESP" | python3 -m json.tool 2>/dev/null | head -20 || echo "$CREATE_RESP"
  echo
  echo "If permission error: token needs 'Account → Vectorize → Edit'"
fi

# ----------------------------------------------------------------------
# Step 2-4: Page through facilities, embed, upsert
# ----------------------------------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

OFFSET=0
TOTAL_DONE=0
PAGE=1

while :; do
  echo
  echo "→ Step 2 (page $PAGE): GET $DCHUB_API/facilities?limit=$PAGE_SIZE&offset=$OFFSET"
  PAGE_FILE="$WORK/page-$PAGE.json"
  HTTP=$(curl -sS -o "$PAGE_FILE" -w '%{http_code}' \
    -H "X-API-Key: $DCHUB_API_KEY" \
    "$DCHUB_API/facilities?limit=$PAGE_SIZE&offset=$OFFSET&country=US")
  echo "  HTTP $HTTP"

  if [[ "$HTTP" != "200" ]]; then
    echo "  ⚠ non-200; stopping pagination"
    head -c 500 "$PAGE_FILE"
    break
  fi

  COUNT=$(python3 -c '
import json, sys
d = json.load(open("'"$PAGE_FILE"'"))
if isinstance(d, list):
    items = d
else:
    items = d.get("data") or d.get("results") or d.get("facilities") or []
print(len(items))
' 2>/dev/null || echo 0)
  echo "  rows: $COUNT"

  if [[ "$COUNT" -eq 0 ]]; then
    echo "  ✓ no more rows"
    break
  fi

  echo "→ Step 3: build descriptions + embed via Workers AI..."

  # Build a JSONL of {id, text, metadata} for this page
  PROMPT_FILE="$WORK/prompts-$PAGE.jsonl"
  python3 - "$PAGE_FILE" "$PROMPT_FILE" <<'PY'
import json, sys, pathlib, hashlib
src, dst = sys.argv[1], sys.argv[2]
d = json.load(open(src))
if isinstance(d, list):
    items = d
else:
    items = d.get("data") or d.get("results") or d.get("facilities") or []
out = []
for f in items:
    name = (f.get("name") or "").strip()
    if not name: continue
    state = (f.get("state") or "").strip()
    city = (f.get("city") or "").strip()
    country = (f.get("country") or "US").strip()
    provider = (f.get("provider") or f.get("operator") or "").strip()
    status = (f.get("status") or "").strip()
    ftype = (f.get("facility_type") or "").strip() if f.get("facility_type") else ""
    mw = f.get("power_mw") or f.get("capacity_mw") or f.get("critical_it_mw")
    lat = f.get("latitude"); lng = f.get("longitude")
    fid = hashlib.sha1(f"{name}|{state}|{city}".lower().encode("utf-8")).hexdigest()[:32]
    parts = [name, city, state, country, provider, status]
    if mw: parts.append(f"{mw} MW")
    if ftype: parts.append(ftype)
    text = ", ".join(p for p in parts if p)[:512]
    meta = {
        "name": name[:128], "state": state[:32], "city": city[:64],
        "country": country[:8], "provider": provider[:64],
        "power_mw": float(mw) if mw else 0.0, "status": status[:32],
        "lat": float(lat) if lat is not None else 0.0,
        "lng": float(lng) if lng is not None else 0.0,
    }
    out.append({"id": fid, "text": text, "metadata": meta})
pathlib.Path(dst).write_text("\n".join(json.dumps(o) for o in out))
print(f"  built {len(out)} prompts", file=sys.stderr)
PY

  # Embed each row (batched to 32 per Workers AI call)
  EMBED_FILE="$WORK/embeds-$PAGE.jsonl"
  : > "$EMBED_FILE"

  python3 - "$PROMPT_FILE" "$EMBED_FILE" "$ACC" "$CLOUDFLARE_API_TOKEN" "$EMBED_MODEL" <<'PY'
import json, sys, urllib.request

src, dst, acc, tok, model = sys.argv[1:6]
batch_size = 32
rows = [json.loads(l) for l in open(src) if l.strip()]
out = open(dst, "w")
ok, fail = 0, 0
for i in range(0, len(rows), batch_size):
    batch = rows[i:i+batch_size]
    body = json.dumps({"text": [r["text"] for r in batch]}).encode()
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/{model}",
        data=body,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        vecs = resp.get("result", {}).get("data", [])
        for r, v in zip(batch, vecs):
            out.write(json.dumps({"id": r["id"], "values": v, "metadata": r["metadata"]}) + "\n")
            ok += 1
    except Exception as e:
        fail += len(batch)
        print(f"  embed batch {i} failed: {e}", file=sys.stderr)
out.close()
print(f"  embedded {ok}, failed {fail}", file=sys.stderr)
PY

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "  [dry-run] would upsert $(wc -l < "$EMBED_FILE") vectors"
  else
    # Step 4: upsert via NDJSON to Vectorize
    echo "→ Step 4: upserting vectors..."
    UPSERT_RESP="$WORK/upsert-$PAGE.json"
    HTTP=$(curl -sS -o "$UPSERT_RESP" -w '%{http_code}' -X POST \
      -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
      -H "Content-Type: application/x-ndjson" \
      --data-binary "@$EMBED_FILE" \
      "$CF_API/vectorize/v2/indexes/$INDEX/upsert")
    echo "  HTTP $HTTP"
    if [[ "$HTTP" != "200" ]]; then
      head -c 400 "$UPSERT_RESP"
      echo
      echo "  ⚠ upsert failed; check token has 'Account → Vectorize → Edit'"
      exit 1
    fi
    echo "  ✓ upserted $(wc -l < "$EMBED_FILE") vectors"
  fi

  TOTAL_DONE=$((TOTAL_DONE + COUNT))
  echo "  cumulative: $TOTAL_DONE rows processed"

  if [[ "$LIMIT" -gt 0 && "$TOTAL_DONE" -ge "$LIMIT" ]]; then
    echo "  ✓ hit --limit $LIMIT, stopping"
    break
  fi

  if [[ "$COUNT" -lt "$PAGE_SIZE" ]]; then
    echo "  ✓ last page (count < page_size)"
    break
  fi

  OFFSET=$((OFFSET + PAGE_SIZE))
  PAGE=$((PAGE + 1))
  sleep 0.5  # be nice to the API
done

echo
echo "================================================================"
echo "Vectorize index '$INDEX' setup complete."
echo "Total rows processed: $TOTAL_DONE"
echo
echo "Test it:"
echo "  curl -sS -X POST -H \"Authorization: Bearer \$CLOUDFLARE_API_TOKEN\" \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    'https://api.cloudflare.com/client/v4/accounts/$ACC/vectorize/v2/indexes/$INDEX/query' \\"
echo "    -d '{\"vector\":[<768 floats from any embedding>],\"topK\":5,\"returnMetadata\":true}'"
echo
echo "Or wire it into your dchubapiproxy worker via a Vectorize binding."
echo "================================================================"
