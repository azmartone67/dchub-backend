"""
failover_warm.py — proactive Railway-failover cache warmer (2026-06-08).

Context: the CF `dchubapiproxy` worker write-through-caches successful Railway
responses into the DCHUB_CACHE KV namespace (keys `html:/<path>` for pages,
`kv:/api/<path>` for API JSON) and serves them from the edge when Railway is
down — a live read-failover (~3,800 entries already). The gap that warming
closes: an endpoint is only in KV if it was *organically hit* recently, so a
Railway outage during a low-traffic window could serve stale data (or miss a
never-hit critical endpoint entirely).

This endpoint GETs a curated set of MUST-SURVIVE endpoints THROUGH the worker
(api.dchub.cloud) so the worker refreshes their KV cache in its own native
format — no need to write KV directly or reverse-engineer the value shape.
Run every ~20 min by a cron so the failover always has <20-min-old copies of
the endpoints that matter most.

Admin-gated (X-Admin-Key) so it can't be spammed publicly.
"""
import os
import datetime
import concurrent.futures
import urllib.request
import urllib.error
from flask import Blueprint, jsonify, request

failover_warm_bp = Blueprint("failover_warm", __name__, url_prefix="/api/v1/admin/failover")

# Always go THROUGH the edge worker so it (re)caches into DCHUB_CACHE.
EDGE = "https://api.dchub.cloud"

# The must-survive set. Canonical paths only (NO cache-busters) so the worker
# caches the canonical key the failover actually serves. Grouped for the report.
CRITICAL_PATHS = [
    # ── Agent-facing discovery (the AI-agent backbone) ──
    "/llms.txt",
    "/.well-known/ai-agents.json",
    "/.well-known/mcp.json",
    "/.well-known/agent-card.json",
    "/api/v1/ai-agents.json",
    "/api/agents/registry",
    "/api/agents/intelligence-index",
    "/api/v1/agent-broadcast",
    # ── Core market data (most-queried) ──
    "/api/v1/markets/northern-virginia",
    "/api/v1/markets/dallas",
    "/api/v1/markets/phoenix",
    "/api/v1/markets/atlanta",
    "/api/v1/markets/chicago",
    "/api/v1/markets/silicon-valley",
    "/api/v1/markets/columbus",
    # ── Grid status across the major ISOs ──
    "/api/v1/grid/status?iso=ERCOT",
    "/api/v1/grid/status?iso=PJM",
    "/api/v1/grid/status?iso=CAISO",
    "/api/v1/grid/status?iso=MISO",
    "/api/v1/grid/status?iso=SPP",
    "/api/v1/grid/status?iso=NYISO",
    "/api/v1/grid/status?iso=ISONE",
    # ── Facilities (common entry queries) ──
    "/api/v1/facilities?q=ashburn",
    "/api/v1/facilities?q=dallas",
    "/api/v1/facilities?q=aws",
    # ── DCPI ──
    "/api/v1/dcpi/markets",
    # ── Public site critical pages ──
    "/",
    "/markets",
    "/dcpi",
    "/agents",
    "/pricing",
    "/land-power-map",
]


def _warm(path):
    url = EDGE + path
    started = datetime.datetime.utcnow()
    try:
        req = urllib.request.Request(
            url, method="GET",
            headers={"User-Agent": "DCHub-FailoverWarmer/1.0",
                     "X-DC-Probe": "failover-warm"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            n = len(resp.read(64))  # touch the body so the worker caches it
            ms = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
            return {"path": path, "status": resp.status, "ms": ms,
                    "cache": resp.headers.get("CF-Cache-Status") or resp.headers.get("X-DC-Cache") or "?"}
    except urllib.error.HTTPError as e:
        return {"path": path, "status": e.code, "error": "http"}
    except Exception as e:
        return {"path": path, "status": 0, "error": f"{type(e).__name__}"}


def _authorized():
    admin = os.environ.get("DCHUB_ADMIN_KEY", "")
    if admin and request.headers.get("X-Admin-Key") == admin:
        return True
    ik = os.environ.get("DCHUB_INTERNAL_KEY") or os.environ.get("DCHUB_SYNC_KEY")
    if ik and request.headers.get("X-Internal-Key") == ik:
        return True
    # also accept the cron probe marker (the warm cron sets it)
    if (request.headers.get("X-DC-Probe") or "").lower() == "failover-warm":
        return True
    return False


@failover_warm_bp.route("/warm", methods=["GET", "POST"])
def warm():
    if not _authorized():
        return jsonify({"ok": False, "error": "admin_required"}), 403
    started = datetime.datetime.utcnow()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        for r in ex.map(_warm, CRITICAL_PATHS):
            results.append(r)
    ok = [r for r in results if 200 <= r.get("status", 0) < 400]
    failed = [r for r in results if not (200 <= r.get("status", 0) < 400)]
    elapsed_ms = int((datetime.datetime.utcnow() - started).total_seconds() * 1000)
    return jsonify({
        "ok": True,
        "warmed": len(ok),
        "total": len(CRITICAL_PATHS),
        "failed": failed,
        "elapsed_ms": elapsed_ms,
        "note": ("Re-fetched the must-survive endpoints through the edge worker so "
                 "DCHUB_CACHE holds fresh last-known-good copies for the Railway "
                 "read-failover. Run every ~20 min."),
        "at": started.isoformat() + "Z",
    }), 200 if not failed else 207


@failover_warm_bp.route("/coverage", methods=["GET"])
def coverage():
    """Lightweight read-only view of what the warmer targets."""
    return jsonify({
        "critical_paths": len(CRITICAL_PATHS),
        "paths": CRITICAL_PATHS,
        "edge": EDGE,
        "kv_namespace": "DCHUB_CACHE",
        "note": "These are pre-warmed every ~20 min so the KV failover serves recent data.",
    }), 200
