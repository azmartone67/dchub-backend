#!/usr/bin/env bash
set -euo pipefail

ACC=4bb33ec40ef02f9f4b41dc97668d5a52
INDEX=dchub-facilities
EMBED_MODEL='@cf/baai/bge-base-en-v1.5'
DCHUB_API="${DCHUB_API:-https://dchub.cloud/api/v1}"
FETCH_LIMIT="${FETCH_LIMIT:-5000}"
EMBED_BATCH=32
UPSERT_BATCH=500

: "${CLOUDFLARE_API_TOKEN:?need CLOUDFLARE_API_TOKEN}"
: "${DCHUB_API_KEY:?need DCHUB_API_KEY}"

CF_API="https://api.cloudflare.com/client/v4/accounts/$ACC"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "-> Step 1: ensure index exists..."
curl -sS -X POST -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" "$CF_API/vectorize/v2/indexes" \
  -d "{\"name\": \"$INDEX\", \"description\": \"DC Hub facilities semantic search\", \"config\": {\"dimensions\": 768, \"metric\": \"cosine\"}}" \
  > /dev/null
echo "  ok (created or already-exists)"

echo
echo "-> Step 2: GET facilities (limit=$FETCH_LIMIT)..."
PAGE_FILE="$WORK/all.json"
HTTP=$(curl -sS -o "$PAGE_FILE" -w '%{http_code}' \
  -H "X-API-Key: $DCHUB_API_KEY" \
  "$DCHUB_API/facilities?limit=$FETCH_LIMIT")
echo "  HTTP $HTTP"
[[ "$HTTP" != "200" ]] && { head -c 500 "$PAGE_FILE"; exit 1; }

echo
echo "-> Step 3: build deduplicated prompts..."
PROMPT_FILE="$WORK/prompts.jsonl"
python3 - "$PAGE_FILE" "$PROMPT_FILE" <<'PY'
import json, sys, pathlib, hashlib
src, dst = sys.argv[1], sys.argv[2]
d = json.load(open(src))
items = d.get("data") if isinstance(d, dict) else d
items = items or []
seen, out = set(), []
for f in items:
    name = (f.get("name") or "").strip()
    if not name: continue
    state = (f.get("state") or "").strip()
    city = (f.get("city") or "").strip()
    fid = hashlib.sha1(f"{name}|{state}|{city}".lower().encode("utf-8")).hexdigest()[:32]
    if fid in seen: continue
    seen.add(fid)
    country = (f.get("country") or "US").strip()
    provider = (f.get("provider") or f.get("operator") or "").strip()
    status = (f.get("status") or "").strip()
    ftype = (f.get("facility_type") or "").strip() if f.get("facility_type") else ""
    mw = f.get("power_mw") or f.get("capacity_mw") or f.get("critical_it_mw")
    lat = f.get("latitude"); lng = f.get("longitude")
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
print(f"  built {len(out)} unique prompts (from {len(items)} rows)", file=sys.stderr)
PY

echo "  $(wc -l < "$PROMPT_FILE" | tr -d ' ') prompts"

echo
echo "-> Step 4: embed via Workers AI..."
EMBED_FILE="$WORK/embeds.jsonl"
python3 - "$PROMPT_FILE" "$EMBED_FILE" "$ACC" "$CLOUDFLARE_API_TOKEN" "$EMBED_MODEL" "$EMBED_BATCH" <<'PY'
import json, sys, urllib.request, time
src, dst, acc, tok, model, bs = sys.argv[1:7]
bs = int(bs)
rows = [json.loads(l) for l in open(src) if l.strip()]
out = open(dst, "w")
ok, fail = 0, 0
t0 = time.time()
for i in range(0, len(rows), bs):
    batch = rows[i:i+bs]
    body = json.dumps({"text": [r["text"] for r in batch]}).encode()
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/run/{model}",
        data=body,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        method="POST")
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        vecs = resp.get("result", {}).get("data", [])
        for r, v in zip(batch, vecs):
            out.write(json.dumps({"id": r["id"], "values": v, "metadata": r["metadata"]}) + "\n")
            ok += 1
    except Exception as e:
        fail += len(batch); print(f"  batch {i}: {e}", file=sys.stderr)
    if (i // bs) % 10 == 0:
        print(f"  progress: {ok}/{len(rows)} ({time.time()-t0:.1f}s)", file=sys.stderr)
out.close()
print(f"  done. {ok}/{len(rows)} embedded (fail {fail}) in {time.time()-t0:.1f}s", file=sys.stderr)
PY

echo
echo "-> Step 5: upsert in chunks of $UPSERT_BATCH..."
split -l "$UPSERT_BATCH" "$EMBED_FILE" "$WORK/chunk-"
TOTAL=0
for chunk in "$WORK"/chunk-*; do
  N=$(wc -l < "$chunk" | tr -d ' ')
  RESP=$(curl -sS -w '\nHTTP=%{http_code}' -X POST \
    -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
    -H "Content-Type: application/x-ndjson" \
    --data-binary "@$chunk" \
    "$CF_API/vectorize/v2/indexes/$INDEX/upsert")
  HTTP=$(echo "$RESP" | tail -1 | sed 's/HTTP=//')
  if [[ "$HTTP" == "200" ]]; then
    TOTAL=$((TOTAL + N))
    echo "  ✓ $(basename $chunk): $N vectors"
  else
    echo "  ⚠ $(basename $chunk) HTTP $HTTP"
    echo "$RESP" | sed '$d' | head -c 200
    echo
  fi
done
echo
echo "Submitted $TOTAL vectors. Wait 60s then check vectorCount."
