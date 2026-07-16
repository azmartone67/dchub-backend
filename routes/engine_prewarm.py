"""
engine_prewarm.py — never-cold tick for the optimization engines (2026-07-16).

Diagnosed live 2026-07-16: after any backend deploy, apex /api/v1/mcp/leadership
served all-zeros for ~40 minutes. A COLD engine tick takes ~13.7s (6 internal
prefetches on cold source caches) — longer than the CF worker's per-attempt
timeout — so nearly every origin attempt failed over to the stale Render
backend, whose valid-looking zeros-200 got cached in the worker's KV lane
(fresh 300s / stale 86400s): a self-refreshing poison loop.

This endpoint is the FIX-2 half (the FIX-1 half is the dead-compute 503 guard
in the engines themselves): the cron heartbeat fires it on EVERY invocation and
it warms BOTH engines by calling their compute functions IN-PROCESS (a direct
function call — not an HTTP self-request through the edge), each honoring its
own _RESP_TTL (300s). Fresh caches make a fire a sub-millisecond no-op, so the
sporadic (~hourly) heartbeat cadence is safe and a post-deploy cold start is
paid HERE, in the background, never by a user/CF-worker request.

SELF-WARM LOOP (2026-07-16, same day): live verify showed the cron dispatch
alone cannot keep the WEB tier warm — the dominant heartbeat caller is
main.py's self-heartbeat, which runs on the WORKER role and dispatches over
loopback, so its engine_prewarm fires warm the WORKER's in-process caches;
only the ~69/day external heartbeats reach a web replica (~every 40 min per
replica, far past the 300s TTL). The engines' caches are per-process, so the
only topology-proof warmer is each process warming ITSELF: a daemon thread
per (Railway) process calls both engines' warm() every 240s — just inside
the TTL, so the caches never expire in steady state and a boot/worker-recycle
cold tick is paid in the background within ~2 min. The cron dispatch entry
stays as a harmless belt (TTL makes it a no-op wherever the loop is alive).

Registered via cron_heartbeat's record_once (frozen-main.py pattern).
Kill: ENGINE_PREWARM_DISABLE=1 (gates the endpoint AND the self-warm loop).
"""
from __future__ import annotations
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, jsonify

engine_prewarm_bp = Blueprint("engine_prewarm", __name__)

_SELFWARM = {"started": False}
_SELFWARM_BOOT_DELAY = 90.0   # let blueprints/DB pools finish booting first
_SELFWARM_INTERVAL = 240.0    # < _RESP_TTL (300s) so the cache never expires


def _selfwarm_loop():
    time.sleep(_SELFWARM_BOOT_DELAY + random.uniform(0, 60))  # jitter across replicas
    while True:
        try:
            if os.environ.get("ENGINE_PREWARM_DISABLE") != "1":
                from routes import mcp_leadership_engine as _mle
                from routes import agent_utilization_engine as _aue
                for eng in (_mle, _aue):
                    try:
                        eng.warm()
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(_SELFWARM_INTERVAL)


def _start_selfwarm_thread() -> bool:
    """Start the per-process self-warm loop (once). Railway-only: on Render
    (read-only failover) every source is dead — warming would just burn CPU;
    local/dev/tests must never grow background threads."""
    if not (os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID")):
        return False
    if os.environ.get("ENGINE_PREWARM_DISABLE") == "1":
        return False
    if _SELFWARM["started"]:
        return False
    _SELFWARM["started"] = True
    threading.Thread(target=_selfwarm_loop, name="engine-selfwarm",
                     daemon=True).start()
    return True


@engine_prewarm_bp.record_once
def _boot_selfwarm(state):
    try:
        if _start_selfwarm_thread():
            print("[engine_prewarm] per-process self-warm loop started "
                  f"(every {int(_SELFWARM_INTERVAL)}s)", flush=True)
    except Exception as _sw_e:
        print(f"[engine_prewarm] selfwarm thread skipped: {_sw_e}", flush=True)


@engine_prewarm_bp.route("/api/v1/engines/prewarm", methods=["GET", "POST"])
def engines_prewarm():
    """Warm both optimization engines in-process. TTL-throttled per engine, so
    repeat fires are cheap no-ops; safe unauthenticated (compute-only, no
    writes, and an attacker hammering it just reads 'fresh' no-ops)."""
    if os.environ.get("ENGINE_PREWARM_DISABLE") == "1":
        return jsonify({"ok": False, "disabled": True}), 200
    from routes import mcp_leadership_engine as _mle
    from routes import agent_utilization_engine as _aue
    results = {}
    # the two cold ticks overlap their (shared) source prefetches — run them
    # concurrently so a fully-cold prewarm costs ~14s, not ~28s
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {"leadership": ex.submit(_mle.warm),
                "utilization": ex.submit(_aue.warm)}
        for name, fut in futs.items():
            try:
                results[name] = fut.result(timeout=25)
            except Exception as e:
                results[name] = {"engine": name, "warmed": False,
                                 "error": f"{type(e).__name__}: {str(e)[:80]}"}
    return jsonify({"ok": True, "results": results}), 200
