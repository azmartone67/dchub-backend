#!/usr/bin/env python3
"""Fetch infrastructure layers from external sources and push to DC Hub.

Runs on the GitHub Actions runner (reliable egress), NOT on Railway —
Railway's network can't reach several infra sources (geo.dot.gov for gas
pipelines times out; the daily server-side ingest fails every run). The
runner fetches the source, builds compact rows, and POSTs them to the
backend's ingest endpoint, which writes to Neon. Railway never fetches.

Env:
  DCHUB_ADMIN_KEY   backend admin key (required)
  DCHUB_ORIGIN      backend origin (default: Railway production)

Usage:
  python tools/infra_fetch.py gas-pipelines        # default cap
  python tools/infra_fetch.py gas-pipelines 30000  # explicit cap
"""
import gzip
import json
import os
import sys
import time
import urllib.parse
import urllib.request

ORIGIN = os.environ.get("DCHUB_ORIGIN", "https://dchub-backend-production.up.railway.app").rstrip("/")
ADMIN = "".join((os.environ.get("DCHUB_ADMIN_KEY") or "").split())

# EIA Natural Gas Interstate + Intrastate Pipelines (national, ~32,892
# polylines). The old geo.dot.gov service died 2026-06 (its backend DB
# refuses connections → 500) and the HIFLD Hp6G80Pky0om7QvQ copy was
# deleted (400) — this FEMA-published EIA org service (FiaPA4ga0iQKduv3)
# is the live replacement. Fields: Operator, TYPEPIPE, Status.
GAS_SVC = ("https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
           "Natural_Gas_Interstate_and_Intrastate_Pipelines_1/FeatureServer/0/query")


def _get(url, timeout=90, tries=5):
    last = None
    for a in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
            print(f"    fetch attempt {a+1}/{tries}: {str(e)[:90]}", flush=True)
            time.sleep(min(2 * (a + 1), 12))
    raise last


def _first_point(geom):
    try:
        paths = geom.get("paths") or []
        if paths and paths[0]:
            x, y = paths[0][0][0], paths[0][0][1]
            return float(y), float(x)  # lat, lng
    except Exception:
        pass
    return None, None


def fetch_gas_pipelines(cap):
    """Paginate the geo.dot.gov gas-pipeline service → [[lat,lng,operator,type],...]."""
    rows, offset, page = [], 0, 2000   # service maxRecordCount = 2000
    while len(rows) < cap:
        params = urllib.parse.urlencode({
            "where": "1=1",
            "outFields": "Operator,TYPEPIPE,Status",
            "returnGeometry": "true",
            "outSR": "4326",
            "maxAllowableOffset": "0.05",   # simplify; we keep 1 vertex/line
            "geometryPrecision": "4",
            "resultOffset": offset,
            "resultRecordCount": page,
            "f": "json",
        })
        data = json.loads(_get(GAS_SVC + "?" + params).decode())
        feats = data.get("features") or []
        if not feats:
            break
        for f in feats:
            lat, lng = _first_point(f.get("geometry") or {})
            if lat is None:
                continue
            a = f.get("attributes") or {}
            rows.append([lat, lng, (a.get("Operator") or "")[:200], (a.get("TYPEPIPE") or "")[:80]])
        offset += page
        if len(feats) < page:
            break
    return rows[:cap]


def post_rows(path, rows, cap):
    payload = {"rows": rows}
    body = gzip.compress(json.dumps(payload).encode("utf-8"))
    url = f"{ORIGIN}{path}?cap={cap}"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "X-Admin-Key": ADMIN, "Content-Type": "application/json",
        "Content-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return getattr(r, "status", r.getcode()), json.loads(r.read()), len(body)


LAYERS = {
    "gas-pipelines": {
        "fetch": fetch_gas_pipelines,
        "ingest": "/api/v1/admin/ingest/gas-pipelines",
        "default_cap": 30000,
    },
}


def main():
    if not ADMIN:
        print("::error::missing DCHUB_ADMIN_KEY"); return 1
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    layer = args[0] if args else "gas-pipelines"
    if layer not in LAYERS:
        print(f"::error::unknown layer {layer}; known: {list(LAYERS)}"); return 1
    cfg = LAYERS[layer]
    cap = int(args[1]) if len(args) > 1 else cfg["default_cap"]

    t0 = time.time()
    print(f"== {layer}: fetching (cap {cap}) ==", flush=True)
    rows = cfg["fetch"](cap)
    print(f"  fetched {len(rows):,} points in {time.time()-t0:.0f}s", flush=True)
    if not rows:
        print("::error::source returned 0 rows"); return 1
    st, j, gz = post_rows(cfg["ingest"], rows, cap)
    print(f"  posted {gz//1024}KB gz (HTTP {st}) → {json.dumps(j)[:200]}", flush=True)
    if st != 200 or not j.get("ok"):
        print(f"::error::ingest failed (HTTP {st})"); return 1
    print(f"== {layer} done: {j.get('inserted')} rows inserted ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
