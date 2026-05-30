"""
grid_cache_warmer.py — pre-warm hot grid pages so 429s drop.

Phase ZZZZZ-round37 (2026-05-24). CF analytics showed /grid/PJM,
/grid/CAISO, /grid/ERCOT each getting 2,100+ 429s per 24h — these
are the highest-traffic grid pages and they're hitting per-IP rate
limits because every visitor recomputes the response.

This warmer endpoint walks the 12 ISOs and hits each page once with
an internal UA that bypasses rate limiting (DCHub-Warmer/1.0). Once
the response lands, the CF edge cache (5-min TTL per Pages worker
config) serves subsequent visitors without hitting Flask. Net effect:
visitor 429s drop to near-zero, Flask CPU drops too.

Wire to Railway cron @ */5 * * * * to keep cache warm.

r46.5 (2026-05-25): switched from sequential urlopen to
ThreadPoolExecutor with 12 workers. Sequential walk hit 44.4s wall
clock (12 ISOs × 2 URLs × ~2s each with cold backend). Concurrent
walk completes in ~2-3s for the same workload, freeing the gunicorn
thread that the /warm caller was holding.
"""
import datetime
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, jsonify

grid_warmer_bp = Blueprint("grid_warmer", __name__,
                            url_prefix="/api/v1/grid-warmer")


HOT_ISOS = ["PJM", "CAISO", "ERCOT", "MISO", "NYISO", "SPP",
             "ISONE", "TVA", "SOCO", "FRCC", "BPA", "AESO"]

BASE = "https://api.dchub.cloud"

# 12 ISOs × 2 URLs = 24 fetches. 24 workers means each fetch runs in
# its own thread — total wall clock ≈ max single-fetch latency.
WARMER_WORKERS = 24


def _hit(url, timeout=10):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "DCHub-Warmer/1.0 (+https://dchub.cloud/internal)",
            "X-DC-Internal-Warmup": "1",
            "Accept": "text/html,application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(2048)
            return {"status": resp.status, "bytes": len(body)}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "error": "http"}
    except Exception as e:
        return {"status": 0, "error": f"{type(e).__name__}"}


@grid_warmer_bp.route("/warm", methods=["GET", "POST"])
def warm():
    started = datetime.datetime.utcnow()
    # Build the full URL list up front so we can dispatch all 24 at once
    targets = []
    for iso in HOT_ISOS:
        targets.append((f"/grid/{iso}",                     f"{BASE}/grid/{iso}"))
        targets.append((f"/api/v1/grid/intelligence/{iso}", f"{BASE}/api/v1/grid/intelligence/{iso}"))

    results = {}
    with ThreadPoolExecutor(max_workers=WARMER_WORKERS) as ex:
        # Per-fetch timeout 15s; ThreadPoolExecutor.shutdown waits for all
        # workers, so worst-case wall clock is 15s (one stuck fetch) — not
        # 24 * 15s = 360s like the sequential version.
        futures = {ex.submit(_hit, url, 15): label for (label, url) in targets}
        for fut in as_completed(futures):
            label = futures[fut]
            try:
                results[label] = fut.result()
            except Exception as e:
                results[label] = {"status": 0, "error": f"future:{type(e).__name__}"}

    elapsed_ms = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
    healthy = sum(1 for r in results.values() if 200 <= r.get("status", 0) < 400)
    total = len(results)
    return jsonify({
        "warmed_count":  total,
        "healthy":       healthy,
        "elapsed_ms":    elapsed_ms,
        "isos":          HOT_ISOS,
        "concurrency":   WARMER_WORKERS,
        "at":            started.isoformat() + "Z",
        "results":       results,
    }), 200 if healthy == total else 207


# AUTO-REPAIR: duplicate route '/health' also in main.py:3819 — review and remove one
@grid_warmer_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"blueprint": "grid_warmer_bp",
                    "isos": HOT_ISOS,
                    "endpoints_per_iso": 2,
                    "total_endpoints": len(HOT_ISOS) * 2}), 200
