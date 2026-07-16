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

Registered via cron_heartbeat's record_once (frozen-main.py pattern).
Kill: ENGINE_PREWARM_DISABLE=1.
"""
from __future__ import annotations
import os
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, jsonify

engine_prewarm_bp = Blueprint("engine_prewarm", __name__)


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
