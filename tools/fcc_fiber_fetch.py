#!/usr/bin/env python3
"""Fetch FCC BDC fiber-to-the-premises availability and push it to DC Hub.

Runs on the GitHub Actions runner (reliable egress), NOT on Railway —
Railway's network drops FCC downloads intermittently (TX/CA-class states
fail), which is the whole reason this exists. The runner does the heavy
download + parse, then POSTs a small gzipped aggregate to the backend's
/api/v1/admin/fcc-fiber/ingest endpoint, which writes it to Neon. Railway
never touches the FCC.

Env:
  DCHUB_ADMIN_KEY    backend admin key (required)
  FCC_BDC_USERNAME   FCC BDC API username (required)
  FCC_BDC_TOKEN      FCC BDC API hash_value token (required)
  DCHUB_ORIGIN       backend origin (default: Railway production)

Usage:
  python tools/fcc_fiber_fetch.py                # load whatever /status says is missing
  python tools/fcc_fiber_fetch.py 48 06          # load specific state FIPS
  python tools/fcc_fiber_fetch.py --force-all    # reload all 52 jurisdictions
"""
import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.request
import zipfile

FCC_BASE = "https://broadbandmap.fcc.gov/api/public/map"
ORIGIN = os.environ.get("DCHUB_ORIGIN", "https://dchub-backend-production.up.railway.app").rstrip("/")
ADMIN = "".join((os.environ.get("DCHUB_ADMIN_KEY") or "").split())
FCC_USER = "".join((os.environ.get("FCC_BDC_USERNAME") or "").split())
FCC_TOKEN = "".join((os.environ.get("FCC_BDC_TOKEN") or "").split())

ALL_FIPS = ["01", "02", "04", "05", "06", "08", "09", "10", "11", "12", "13",
            "15", "16", "17", "18", "19", "20", "21", "22", "23", "24", "25",
            "26", "27", "28", "29", "30", "31", "32", "33", "34", "35", "36",
            "37", "38", "39", "40", "41", "42", "44", "45", "46", "47", "48",
            "49", "50", "51", "53", "54", "55", "56", "72"]


def fcc_get(path, rng=None, timeout=120):
    h = {"username": FCC_USER, "hash_value": FCC_TOKEN,
         "User-Agent": "dchub-bdc-fiber-runner/1.0 (+https://dchub.cloud)"}
    if rng:
        h["Range"] = rng
    return urllib.request.urlopen(urllib.request.Request(FCC_BASE + path, headers=h), timeout=timeout)


def fcc_get_retry(path, rng=None, timeout=120, tries=5):
    last = None
    for a in range(tries):
        try:
            with fcc_get(path, rng=rng, timeout=timeout) as r:
                return getattr(r, "status", r.getcode()), r.headers, r.read()
        except Exception as e:
            last = e
            print(f"    fcc {path[:50]} {rng or ''} attempt {a+1}/{tries}: {str(e)[:90]}", flush=True)
            time.sleep(min(2 * (a + 1), 12))
    raise last


def backend(method, path, body=None, headers=None, timeout=120):
    url = ORIGIN + path
    h = {"X-Admin-Key": ADMIN}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return getattr(r, "status", r.getcode()), json.loads(r.read())


def get_status():
    _, j = backend("GET", "/api/v1/admin/fcc-fiber/status", timeout=90)
    return j


def find_file_id(as_of, fips):
    _, _, raw = fcc_get_retry(f"/downloads/listAvailabilityData/{as_of}?category=State", timeout=90)
    files = json.loads(raw).get("data", [])
    m = [f for f in files
         if str(f.get("state_fips")) == fips
         and str(f.get("technology_code")) == "50"
         and f.get("file_type") == "csv"]
    return str(m[0]["file_id"]) if m else None


def download_zip(file_id):
    """Range-chunked download to a bytes buffer (reliable on GH egress)."""
    path = f"/downloads/downloadFile/availability/{file_id}"
    st, hd, first = fcc_get_retry(path, rng="bytes=0-0", timeout=60)
    crange = hd.get("Content-Range") or ""
    if st != 206 or "/" not in crange:
        _, _, blob = fcc_get_retry(path, timeout=900)
        return blob
    total = int(crange.rsplit("/", 1)[1])
    buf = bytearray(first)
    CHUNK = 16 << 20
    while len(buf) < total:
        end = min(len(buf) + CHUNK, total) - 1
        _, _, piece = fcc_get_retry(path, rng=f"bytes={len(buf)}-{end}", timeout=180)
        if not piece:
            raise IOError(f"empty range at {len(buf)}/{total}")
        buf.extend(piece)
    return bytes(buf)


def aggregate(blob):
    """Parse the FTTP CSV into the compact ingest format."""
    agg = {}  # (brand, h3) -> [locations, max_down, provider_id]
    rows = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(name) as fh:
            rdr = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8"))
            for row in rdr:
                rows += 1
                brand = (row.get("brand_name") or row.get("provider_name") or "").strip()
                h3 = (row.get("h3_res8_id") or "").strip()
                if not brand or not h3:
                    continue
                try:
                    down = int(float(row.get("max_advertised_download_speed") or 0))
                except Exception:
                    down = 0
                k = (brand, h3)
                e = agg.get(k)
                if e:
                    e[0] += 1
                    if down > e[1]:
                        e[1] = down
                else:
                    agg[k] = [1, down, (row.get("provider_id") or "")[:24]]
    providers, pidx, hexes = [], {}, []
    for (brand, h3), (locs, down, pid) in agg.items():
        if brand not in pidx:
            pidx[brand] = len(providers)
            providers.append([brand, pid])
        hexes.append([pidx[brand], h3, locs, down])
    return rows, providers, hexes


def push(fips, as_of, providers, hexes):
    payload = {"state_fips": fips, "as_of": as_of, "providers": providers, "hexes": hexes}
    body = gzip.compress(json.dumps(payload).encode("utf-8"))
    st, j = backend("POST", f"/api/v1/admin/fcc-fiber/ingest?async=1", body=body,
                    headers={"Content-Type": "application/json",
                             "Content-Encoding": "gzip"}, timeout=120)
    return st, j, len(body)


def wait_loaded(fips, timeout_s=600):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(15)
        try:
            s = get_status()
        except Exception:
            continue
        if fips not in (s.get("missing_state_fips") or []):
            return True
    return False


def main():
    if not (ADMIN and FCC_USER and FCC_TOKEN):
        print("::error::missing DCHUB_ADMIN_KEY / FCC_BDC_USERNAME / FCC_BDC_TOKEN")
        return 1

    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    force = "--force-all" in sys.argv

    status = get_status()
    target = status.get("target_as_of")
    print(f"status: loaded={status.get('loaded')} latest={status.get('latest_available')} "
          f"needs_refresh={status.get('needs_refresh')}", flush=True)

    if args:
        fips_list = args
    elif force:
        fips_list = list(ALL_FIPS)
    elif status.get("needs_refresh"):
        fips_list = status.get("missing_state_fips") or []
    else:
        print("already current — nothing to do")
        return 0

    if not target:
        print("::error::no target vintage")
        return 1
    print(f"loading vintage {target}; {len(fips_list)} state(s): {' '.join(fips_list)}", flush=True)

    ok, fail, failed = 0, 0, []
    for fips in fips_list:
        print(f"--- state {fips} ---", flush=True)
        try:
            file_id = find_file_id(target, fips)
            if not file_id:
                print(f"  no FTTP csv for {fips}@{target}; skipping")
                fail += 1
                failed.append(fips)
                continue
            t0 = time.time()
            blob = download_zip(file_id)
            rows, providers, hexes = aggregate(blob)
            st, j, gz = push(fips, target, providers, hexes)
            print(f"  downloaded {len(blob)//(1<<20)}MB, {rows:,} rows -> "
                  f"{len(hexes):,} hexes / {len(providers)} providers, "
                  f"posted {gz//1024}KB gz (HTTP {st}) in {time.time()-t0:.0f}s", flush=True)
            if wait_loaded(fips):
                ok += 1
                print(f"  ✅ {fips} ingested")
            else:
                fail += 1
                failed.append(fips)
                print(f"  ⚠️ {fips} did not land within timeout")
        except Exception as e:
            fail += 1
            failed.append(fips)
            print(f"  ❌ {fips} error: {str(e)[:160]}", flush=True)

    print(f"== summary: {ok} loaded, {fail} failed ({' '.join(failed) or 'none'}) ==")
    try:
        print("final:", json.dumps(get_status().get("loaded")))
    except Exception:
        pass
    if ok == 0 and fail > 0:
        print("::error::FCC refresh loaded 0 states")
        return 1
    if fail:
        print(f"::warning::{fail} state(s) failed ({' '.join(failed)}); next run retries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
