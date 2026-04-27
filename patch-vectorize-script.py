#!/usr/bin/env python3
import pathlib
p = pathlib.Path("setup-vectorize.sh")
src = p.read_text()

old_count = """COUNT=$(python3 -c '
import json, sys
d = json.load(open("'"$PAGE_FILE"'"))
items = d.get("data") or d.get("results") or d.get("facilities") or d if isinstance(d, list) else d
print(len(items if isinstance(items, list) else items.get("data", [])))
' 2>/dev/null || echo 0)"""

new_count = """COUNT=$(python3 -c '
import json, sys
d = json.load(open("'"$PAGE_FILE"'"))
if isinstance(d, list):
    items = d
else:
    items = d.get("data") or d.get("results") or d.get("facilities") or []
print(len(items))
' 2>/dev/null || echo 0)"""

if old_count in src:
    src = src.replace(old_count, new_count); print("[OK] Patched COUNT block")
elif new_count in src:
    print("[skip] COUNT already patched")
else:
    print("[WARN] COUNT block not matched")

start = 'python3 - "$PAGE_FILE" "$PROMPT_FILE" <<\'PY\'\n'
end = 'print(f"  built {len(out)} prompts", file=sys.stderr)\nPY\n'
i = src.find(start); j = src.find(end, i) if i != -1 else -1
if i != -1 and j != -1:
    old_block = src[i:j+len(end)]
    if "hashlib" in old_block:
        print("[skip] prompt-builder already patched")
    else:
        new_block = start + '''import json, sys, pathlib, hashlib
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
pathlib.Path(dst).write_text("\\n".join(json.dumps(o) for o in out))
print(f"  built {len(out)} prompts", file=sys.stderr)
PY
'''
        src = src.replace(old_block, new_block); print("[OK] Patched prompt-builder")
else:
    print("[WARN] prompt-builder block not matched")

p.write_text(src)
print(f"Wrote {p} ({len(src)} bytes)")
