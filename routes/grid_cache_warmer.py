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
"""
import datetime
import urllib.request
from flask import Blueprint, jsonify

grid_warmer_bp = Blueprint("grid_warmer", __name__,
                            url_prefix="/api/v1/grid-warmer")


HOT_ISOS = ["PJM", "CAISO", "ERCOT", "MISO", "NYISO", "SPP",
             "ISONE", "TVA", "SOCO", "FRCC", "BPA", "AESO"]

BASE = "https://api.dchub.cloud"


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
    results = {}
    for iso in HOT_ISOS:
        # Hit both /grid/<ISO> HTML AND /api/v1/grid/intelligence/<ISO>
        results[f"/grid/{iso}"] = _hit(f"{BASE}/grid/{iso}", timeout=15)
        results[f"/api/v1/grid/intelligence/{iso}"] = _hit(
            f"{BASE}/api/v1/grid/intelligence/{iso}", timeout=15)
    elapsed_ms = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
    healthy = sum(1 for r in results.values() if 200 <= r.get("status", 0) < 400)
    total = len(results)
    return jsonify({
        "warmed_count": total,
        "healthy":      healthy,
        "elapsed_ms":   elapsed_ms,
        "isos":         HOT_ISOS,
        "at":           started.isoformat() + "Z",
        "results":      results,
    }), 200 if healthy == total else 207


@grid_warmer_bp.route("/health", methods=["GET"])
def health():
    return jsonify({"blueprint": "grid_warmer_bp",
                    "isos": HOT_ISOS,
                    "endpoints_per_iso": 2,
                    "total_endpoints": len(HOT_ISOS) * 2}), 200
