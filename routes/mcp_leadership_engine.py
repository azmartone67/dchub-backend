"""
mcp_leadership_engine.py — the MCP Leadership Engine (2026-06-18).

Goal: WIN the MCP category (#1 MCP server for data-center / power / energy /
infrastructure intelligence — and adjacent). Closed optimization loop:

    MEASURE leadership → DIAGNOSE the biggest gap → OPTIMIZE → VERIFY → repeat.

★ PHASE = SHADOW: it MEASURES leadership across the 6 dimensions of category
leadership and, for each, names the actuator it WOULD fire to close the gap — but
EXECUTES NOTHING (read-only internal GETs). The ACTING half arms per-actuator,
reliability-gated (BRAIN_MCP_OPTIMIZE_ARMED, default off; only after autopilot
fix_success_rate >= 50% — the recovery watch). Same crawl→walk→run discipline as
the Brain v4 ownership loop: an engine that "automatically optimizes us" with its
hands off the controls until it has earned them.

Leadership Index = 0-100 weighted score. Retention carries the highest weight
because it is the binding constraint (agents arrive but ~1 returns/wk).

Endpoint:
  GET /api/v1/mcp/leadership — live Leadership Index + per-dimension moves + the
                               shadow optimize-loop (what it WOULD fire).
"""
from __future__ import annotations
import os, json, urllib.request, threading
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, jsonify

_REQ = threading.local()  # per-request prefetch cache so dimension reads are instant

mcp_leadership_bp = Blueprint("mcp_leadership", __name__)
_BASE = "http://127.0.0.1:8080"


@mcp_leadership_bp.after_request
def _no_store(resp):
    # live engine — never CF-cache (a stale 404/200 poisons the dashboard)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


def _raw_get(path: str, timeout: int = 18) -> dict:
    """Read-only internal GET. Fail-soft to {}. The engine never writes.
    2026-06-22: timeout 6s→18s. /api/v1/ai/reach's agent_requests scan takes ~12s
    on a cold per-replica cache (the table is large) — at 6s it always timed out →
    {} → adoption read 0 → the all-success cache gate never tripped → the WHOLE
    engine froze at 0.0. 18s lets the cold scan complete + populate reach's 30-min
    cache; subsequent engine runs hit the warm cache and are instant."""
    try:
        req = urllib.request.Request(_BASE + path, method="GET",
                                     headers={"User-Agent": "dchub-mcp-leadership/shadow"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return {}


def _get(path: str, timeout: int = 6) -> dict:
    """Return the per-request prefetched response if present, else fetch live."""
    cache = getattr(_REQ, "cache", None)
    if cache is not None and path in cache:
        return cache[path]
    return _raw_get(path, timeout)


# All endpoints the 6 dimensions read — prefetched in PARALLEL per request so the
# engine responds in ~2s (was ~7.5s sequential → CF worker timed out → failover
# to a stale backend → 404 on dchub.cloud).
_PREFETCH = ["/api/v1/brain/mcp-registries", "/api/v1/mcp/standing", "/api/v1/ai/reach",
             "/api/v1/mcp/retention", "/api/v1/mcp/funnel", "/api/v1/citations/by-agent"]


def _clamp(x):
    try:
        return max(0.0, min(100.0, float(x)))
    except Exception:
        return 0.0


def _count(v):
    return len(v) if isinstance(v, list) else int(v or 0)


def _status(score):
    return ("leading" if score >= 75 else "contending" if score >= 50
            else "trailing" if score >= 25 else "absent")


# ── The 6 dimensions of MCP category leadership ───────────────────────────

def _dim_discoverability():
    reg = _get("/api/v1/brain/mcp-registries")
    n_present, n_missing = _count(reg.get("present")), _count(reg.get("missing"))
    total = int(reg.get("total") or 0) or (n_present + n_missing) or 1
    rank1s = len(_get("/api/v1/mcp/standing").get("rank_highlights") or [])
    score = _clamp(100.0 * n_present / total)
    return {
        "dimension": "discoverability", "weight": 0.15, "score": round(score, 1),
        "current": f"{n_present}/{total} tracked registries · {rank1s} #1 category ranks",
        "target": "on every agent-client directory (Cline, Cursor, Anthropic Connector, Windsurf)",
        "status": _status(score),
        "top_move": (f"submit to the {n_missing} missing registries + finish the pending Cline/Cursor/Anthropic submissions"
                     if n_missing else "complete the pending Cline/Cursor/Anthropic submissions"),
    }


def _dim_adoption():
    reach = _get("/api/v1/ai/reach")
    agents, plats = int(reach.get("distinct_agents_7d") or 0), int(reach.get("distinct_platforms") or 0)
    TARGET = 50
    return {
        "dimension": "adoption", "weight": 0.15, "score": round(_clamp(100.0 * agents / TARGET), 1),
        "current": f"{agents} distinct external agents/wk across {plats} platforms",
        "target": f"{TARGET}+ distinct agents/wk",
        "status": _status(_clamp(100.0 * agents / TARGET)),
        "top_move": "grow NEW external agents — directory placements + 'best MCP' roundups + SEO",
    }


def _dim_retention():
    ret = _get("/api/v1/mcp/retention").get("summary") or {}
    # 2026-07-16 metric-truth fix: score on the CANONICAL retention metric —
    # the mature cross-week return rate (summary.pct_returned_next_week_mature:
    # durable api_key, keys minted 8-30d ago, first used again in a LATER ISO
    # week — mcp_retention.py's declared primary_metric). The old inputs
    # (latest_returning_ips + pct_reused_30d = "reused at least once in 30d")
    # scored this dimension 100 "leading" while /api/v1/agents/utilization
    # scored the SAME lifecycle stage the biggest leak on the mature metric
    # (~4.9%) — two engines, two answers. The engines now converge; the score
    # dropping from 100 is the point. pct_reused_30d stays visible as a
    # SECONDARY detail, clearly labeled — it is engagement-within-session(s),
    # not retention.
    try:
        mature_rate = float(ret.get("pct_returned_next_week_mature") or 0)
    except Exception:
        mature_rate = 0.0
    returned_mature = int(ret.get("returned_next_week_mature") or 0)
    try:
        reused_30d = float(ret.get("pct_reused_30d") or 0)
    except Exception:
        reused_30d = 0.0
    score = _clamp(100.0 * mature_rate / 25.0)
    return {
        "dimension": "retention", "weight": 0.30, "score": round(score, 1),
        "current": (f"{mature_rate:.1f}% mature cross-week return "
                    f"({returned_mature} keys returned, 8-30d cohort) · "
                    f"secondary: {reused_30d:.1f}% reused at least once (30d)"),
        "target": "25%+ mature cross-week return",
        "status": _status(score),
        "top_move": "THE binding constraint — measure r-return hook effect, then durable identity / OAuth connectors",
    }


def _dim_tool_quality():
    pool = _get("/api/v1/mcp/funnel").get("paid_tool_demand_30d") or []
    with_demand = sum(1 for t in pool if isinstance(t, dict) and int(t.get("users") or 0) >= 20)
    TARGET = 8
    return {
        "dimension": "tool_quality", "weight": 0.10, "score": round(_clamp(100.0 * with_demand / TARGET), 1),
        "current": f"{with_demand} tools with real distinct demand (>=20 users)",
        "target": f"{TARGET}+ flagship tools each pulling recurring demand",
        "status": _status(_clamp(100.0 * with_demand / TARGET)),
        "top_move": "deepen high-demand tools (grid/fiber); auto-tune descriptions per platform; dogfood-probe each (phase 2)",
    }


def _dim_conversion():
    conv = int(_get("/api/v1/mcp/funnel").get("conversions_30d") or 0)
    TARGET = 30
    return {
        "dimension": "conversion", "weight": 0.20, "score": round(_clamp(100.0 * conv / TARGET), 1),
        "current": f"{conv} conversions / 30d",
        "target": f"{TARGET}+ /30d",
        "status": _status(_clamp(100.0 * conv / TARGET)),
        "top_move": "downstream of retention — surface the $10 one-time / 1,000 API calls pack at the value moment",
    }


def _dim_authority():
    cit = _get("/api/v1/citations/by-agent").get("by_agent") or []
    n = _count(cit)
    TARGET = 6
    return {
        "dimension": "authority", "weight": 0.10, "score": round(_clamp(100.0 * n / TARGET), 1),
        "current": f"cited by {n} distinct agents",
        "target": f"{TARGET}+ distinct agents citing DC Hub",
        "status": _status(_clamp(100.0 * n / TARGET)),
        "top_move": "keep the citation footer on full responses; feed the source-of-truth media channel",
    }


# ── Shadow optimize-loop: each dimension → the actuator it WOULD fire ──────
# SHADOW = logs the actuator + executed:false; fires nothing. Arming is per
# actuator (only the `armable` ones), gated by BRAIN_MCP_OPTIMIZE_ARMED (off)
# AND the recovery watch (fix_success_rate >= 50%).
def _optimize_armed():
    return str(os.environ.get("BRAIN_MCP_OPTIMIZE_ARMED", "")).lower() in ("1", "true", "yes")

_ACTUATORS = {
    "discoverability": ("POST /api/v1/admin/outreach/mcp-registry/submit-all", True, "registry-outreach",
                        "auto-submit / refresh listings on the missing registries"),
    "tool_quality":    ("ai_platform_tool_tuner — per-platform tool descriptions (WIRED LIVE 2026-06-19)", True, "tool-tuner",
                        "per-platform descriptions now reach the live tools/list; re-seed/refine variants to lift low-demand tools (reversible)"),
    "conversion":      ("depth-tease / unlock_more_data (mcp-server server.mjs)", False, "mcp-server",
                        "surface usage-billing at the value moment — lives in the MCP server, not a backend actuator"),
    "retention":       ("r-return hook (shipped) + durable-identity build", False, "engineering",
                        "r-return is live; durable identity / OAuth is an engineering build, not a one-call actuator"),
    "adoption":        ("directory submissions + 'best MCP' roundups", False, "human",
                        "human levers — Cline submitted; Cursor / Anthropic / Windsurf pending"),
    "authority":       ("citation footer (live) + source-of-truth media channel", False, "media",
                        "citation footer already on; feed the media source-of-truth loop"),
}

def _shadow_actions(dims):
    out = []
    for d in dims:
        a = _ACTUATORS.get(d.get("dimension"))
        if not a:
            continue
        actuator, armable, kind, note = a
        out.append({
            "dimension": d.get("dimension"), "score": d.get("score"),
            "would_fire": actuator, "kind": kind,
            "armable_now": armable, "executed": False,  # SHADOW — fires nothing
            "rationale": d.get("top_move"), "note": note,
        })
    return sorted(out, key=lambda x: (x.get("score") if x.get("score") is not None else 100))


_RESP = {"at": 0.0, "v": None}
_RESP_TTL = 300.0  # response cache. 2026-07-16: 45s→300s — a COLD tick costs ~13.7s
                   # (6 internal prefetches on cold source caches), so at 45s nearly
                   # every CF-worker origin attempt after a deploy paid the cold tick,
                   # tripped the worker's per-attempt timeout and failed over to the
                   # stale backend. These are diagnostic engines — a 5-min-stale score
                   # is fine (the dashboard refreshes every 60s), and the cron prewarm
                   # (routes/engine_prewarm.py) re-ticks in-process before it expires.


def _compute():
    """One full engine tick: parallel prefetch + the 6 dimension scores.

    Returns (payload, complete, dead):
      complete — every prefetched source returned data → safe to cache.
      dead     — NO prefetched source returned data AND every dimension scored 0:
                 the all-sources-dead frame a stale/failed-over backend produces
                 (Render's loopback self-requests all fail server-side). Must be
                 served as 503, never a valid-looking zeros-200 — the CF worker
                 caches 200s, so a zeros-200 poisons the KV lane for a day. A
                 legitimately-zero SINGLE dimension never trips this: its source
                 responded, so sources_up > 0."""
    try:
        with ThreadPoolExecutor(max_workers=len(_PREFETCH)) as ex:
            _REQ.cache = dict(zip(_PREFETCH, ex.map(_raw_get, _PREFETCH)))
    except Exception:
        _REQ.cache = {}
    dims = []
    for fn in (_dim_discoverability, _dim_adoption, _dim_retention,
               _dim_tool_quality, _dim_conversion, _dim_authority):
        try:
            dims.append(fn())
        except Exception as e:
            dims.append({"dimension": fn.__name__, "error": str(e)[:120], "weight": 0, "score": 0})
    wsum = sum(d.get("weight", 0) for d in dims) or 1.0
    index = round(sum(d.get("score", 0) * d.get("weight", 0) for d in dims) / wsum, 1)
    verdict = ("leading" if index >= 75 else "contending" if index >= 50
               else "trailing" if index >= 25 else "early")
    ranked = sorted([d for d in dims if d.get("weight", 0) > 0],
                    key=lambda d: (100 - d.get("score", 0)) * d.get("weight", 0), reverse=True)
    top = ranked[0] if ranked else None
    actions = _shadow_actions(dims)
    payload = dict(
        engine="MCP Leadership Engine",
        phase="diagnostic", decides=True, executes=False,
        armed=_optimize_armed(), execution_wired=False,  # NOTE: no executor reads `armed` — diagnostic-only
        mcp_leadership_index=index, verdict=verdict,
        top_priority={"dimension": top.get("dimension"), "score": top.get("score"),
                      "move": top.get("top_move")} if top else None,
        dimensions=dims,
        shadow_actions=actions,
        note=("DIAGNOSTIC-ONLY: measures category leadership + names the actuator it "
              "WOULD fire per dimension, but NO code path executes any actuator from this "
              "engine — `armed`/fix_success_rate are NOT wired to an executor. The named "
              "actuators run via their own crons/endpoints (e.g. the tool-tuner re-seed "
              "cron) or remain human-gated; treat this as a scoreboard, not an autopilot."),
    )
    sources_up = sum(1 for p in _PREFETCH if _REQ.cache.get(p))
    complete = sources_up == len(_PREFETCH)
    dead = sources_up == 0 and all((d.get("score") or 0) == 0 for d in dims)
    return payload, complete, dead


def _store(payload, now):
    _RESP["at"] = now
    _RESP["v"] = payload
    try:  # snapshot the headline for trend tracking (rate-limited ~hourly, fail-soft)
        from routes.engine_trends import record as _rec
        top = payload.get("top_priority") or {}
        _rec("leadership", payload.get("mcp_leadership_index"),
             {"verdict": payload.get("verdict"), "top_priority": top.get("dimension")})
    except Exception:
        pass


_STALE_MAX = 3600.0   # serve-stale ceiling: an expired-but-present cache is served
                      # instantly while a background refresh runs (stale-while-
                      # revalidate) — but never past this age; beyond it, block and
                      # recompute so a long source outage can't serve hour-old data
                      # forever. Safe by construction: the dead-compute guard means
                      # only GOOD frames ever enter _RESP.
_REFRESH_LOCK = threading.Lock()  # single-flight for the background refresh


def _refresh_async():
    """Kick one background recompute (non-blocking; deduped by _REFRESH_LOCK).
    2026-07-16: with 2 web replicas, the ~4-min heartbeat prewarm only reaches
    each replica every other fire, so a replica's cache can expire between
    warms — the first request after idle was paying the recompute (~8s). Now
    it serves the last GOOD copy instantly and refreshes behind the scenes."""
    if not _REFRESH_LOCK.acquire(blocking=False):
        return
    def _run():
        try:
            import time as _t
            payload, complete, dead = _compute()
            if complete and not dead:
                _store(payload, _t.time())
        finally:
            _REFRESH_LOCK.release()
    threading.Thread(target=_run, daemon=True).start()


def warm(force: bool = False) -> dict:
    """In-process pre-warm — called by routes/engine_prewarm.py off the cron
    heartbeat (a direct function call, NOT an HTTP self-request) so no user /
    CF-worker request ever pays the ~13.7s cold tick. Honors _RESP_TTL: a
    fresh cache makes this a cheap no-op, so every-heartbeat fires are safe."""
    import time as _t
    _now = _t.time()
    if not force and _RESP["v"] is not None and (_now - _RESP["at"]) < _RESP_TTL:
        return {"engine": "leadership", "warmed": False, "reason": "fresh",
                "age_s": round(_now - _RESP["at"], 1)}
    payload, complete, dead = _compute()
    if complete and not dead:
        _store(payload, _now)
    return {"engine": "leadership", "warmed": bool(complete and not dead),
            "complete": complete, "dead": dead}


@mcp_leadership_bp.route("/api/v1/mcp/leadership", methods=["GET"])
def mcp_leadership():
    """MCP Leadership Index + shadow optimize-loop. Measures + proposes the
    actuator per dimension; executes nothing. See module docstring."""
    import time as _t
    _now = _t.time()
    if _RESP["v"] is not None:
        age = _now - _RESP["at"]
        if age < _RESP_TTL:
            return jsonify(_RESP["v"]), 200
        if age < _STALE_MAX:
            # stale-while-revalidate: answer <1s from the last GOOD copy and
            # recompute in the background (single-flight). Only good frames
            # ever enter _RESP, so stale here is safe — same semantic as the
            # CF worker's KV-stale lane, one hop earlier.
            _refresh_async()
            return jsonify(dict(_RESP["v"], cache_age_s=round(age, 1),
                                served="stale-while-revalidate")), 200
    payload, complete, dead = _compute()
    if dead:
        # dead-compute guard (2026-07-16): every source failed → this frame is
        # garbage, not a measurement. 503 so the CF worker's failover chain
        # skips this backend and serves KV-stale (the last GOOD copy) instead
        # of caching an all-zeros 200 that self-refreshes for a day.
        return jsonify({"ok": False, "error": "engine sources unavailable",
                        "engine": "MCP Leadership Engine",
                        "detail": ("all internal prefetch sources failed; "
                                   "refusing to serve an all-zeros frame")}), 503
    # Only cache a COMPLETE response — if any internal prefetch timed out / returned
    # {} (e.g. the slow /api/v1/ai/reach), a dimension degrades to 0; don't freeze
    # that for the TTL, let the next request retry.
    if complete:
        _store(payload, _now)
    return jsonify(payload), 200
