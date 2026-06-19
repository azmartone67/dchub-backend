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


def _raw_get(path: str, timeout: int = 6) -> dict:
    """Read-only internal GET. Fail-soft to {}. The engine never writes."""
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
    returning = int(ret.get("latest_returning_ips") or 0)
    try:
        reuse = float(ret.get("pct_reused_30d") or 0)
    except Exception:
        reuse = 0.0
    score = _clamp(0.5 * (100.0 * returning / 10.0) + 0.5 * (100.0 * reuse / 25.0))
    return {
        "dimension": "retention", "weight": 0.30, "score": round(score, 1),
        "current": f"{returning} returning IPs/wk · {reuse:.1f}% key-reuse",
        "target": "10+ returning IPs/wk · 25%+ reuse",
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
        "top_move": "downstream of retention — surface usage-based ($5 pack / metered) at the value moment",
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
    "tool_quality":    ("ai_platform_tool_tuner — per-platform tool descriptions", True, "tool-tuner",
                        "tune low-demand tool descriptions per platform (reversible)"),
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
_RESP_TTL = 45.0  # response cache: the engine makes 6 internal calls — cache so
                  # repeat hits (dashboard 60s refresh) are instant and never trip
                  # the CF worker's primary-timeout → failover to the stale backend.


@mcp_leadership_bp.route("/api/v1/mcp/leadership", methods=["GET"])
def mcp_leadership():
    """MCP Leadership Index + shadow optimize-loop. Measures + proposes the
    actuator per dimension; executes nothing. See module docstring."""
    import time as _t
    _now = _t.time()
    if _RESP["v"] is not None and (_now - _RESP["at"]) < _RESP_TTL:
        return jsonify(_RESP["v"]), 200
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
        phase="shadow", decides=True, executes=False, armed=_optimize_armed(),
        mcp_leadership_index=index, verdict=verdict,
        top_priority={"dimension": top.get("dimension"), "score": top.get("score"),
                      "move": top.get("top_move")} if top else None,
        dimensions=dims,
        shadow_actions=actions,
        note=("MEASURES category leadership + names the actuator it WOULD fire per "
              "dimension; executes nothing. Arming is per-actuator (the `armable_now` "
              "ones first), gated by BRAIN_MCP_OPTIMIZE_ARMED + fix_success_rate>=50%."),
    )
    # Only cache a COMPLETE response — if any internal prefetch timed out / returned
    # {} (e.g. the slow /api/v1/ai/reach), a dimension degrades to 0; don't freeze
    # that for 45s, let the next request retry.
    if all(_REQ.cache.get(p) for p in _PREFETCH):
        _RESP["at"] = _now
        _RESP["v"] = payload
        try:  # snapshot the headline for trend tracking (rate-limited ~hourly, fail-soft)
            from routes.engine_trends import record as _rec
            _rec("leadership", index, {"verdict": verdict,
                                       "top_priority": (top or {}).get("dimension")})
        except Exception:
            pass
    return jsonify(payload), 200
