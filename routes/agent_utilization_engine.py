"""
agent_utilization_engine.py — the AI Agent Utilization Engine (2026-06-18).

Maximize how AI agents interconnect with DC Hub across three tracks:
  ONBOARD — arrive → identify → first productive call (frictionless connect)
  INCENT  — activate → RETURN → convert (reasons to come back + pay)
  TRAIN   — use well: high-value tool-chains, breadth, recipes

It MEASURES the real agent lifecycle (using HONEST metrics — real external IPs +
real calls, NOT scan-inflated mint volume), scores each track, names the leak,
and for each track names the actuator it WOULD fire.

★ PHASE = SHADOW: measures + proposes, EXECUTES NOTHING (read-only internal GETs).
Arming is per-actuator, gated by BRAIN_AGENT_UTIL_ARMED (default off) + the
recovery watch (autopilot fix_success_rate >= 50%). Same crawl→walk→run discipline
as the Brain v4 ownership loop + the MCP Leadership Engine.

Honest read (2026-06-18): real agents ONBOARD + ACTIVATE fine (83 real IPs/wk →
12.7K real calls); ~1 returns/wk. So onboard/train function — INCENT (return) is
the leak. The 0.66 calls/key is scan-minted noise, not a real activation gap
(read the RATE on real IPs, never the mint volume).

Orchestrates existing surfaces (does NOT rebuild them):
  /api/v1/onboard (unified onboarder) · /api/v1/agent/cookbook (concierge recipes)

Endpoint:
  GET /api/v1/agents/utilization — the live tracks + lifecycle funnel + shadow loop
"""
from __future__ import annotations
import os, json, urllib.request, threading
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, jsonify

_REQ = threading.local()  # per-request prefetch cache

agent_utilization_bp = Blueprint("agent_utilization", __name__)
_BASE = "http://127.0.0.1:8080"


@agent_utilization_bp.after_request
def _no_store(resp):
    # live engine — never CF-cache (a stale 404/200 poisons the dashboard)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


def _raw_get(path: str, timeout: int = 6) -> dict:
    try:
        req = urllib.request.Request(_BASE + path, method="GET",
                                     headers={"User-Agent": "dchub-agent-util/shadow"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return {}


def _get(path: str, timeout: int = 6) -> dict:
    cache = getattr(_REQ, "cache", None)
    if cache is not None and path in cache:
        return cache[path]
    return _raw_get(path, timeout)


# Prefetched in PARALLEL per request (was ~5 sequential GETs → too slow → CF
# worker failover → 404 on dchub.cloud).
_PREFETCH = ["/api/v1/mcp/funnel", "/api/v1/ai/reach", "/api/v1/mcp/retention",
             "/api/v1/onboard", "/api/v1/agent/cookbook"]


def _clamp(x):
    try:
        return max(0.0, min(100.0, float(x)))
    except Exception:
        return 0.0


def _f(x, d=0.0):
    try:
        return float(x)
    except Exception:
        return d


def _status(s):
    return ("strong" if s >= 75 else "ok" if s >= 50 else "weak" if s >= 25 else "broken")


def _util_armed():
    return str(os.environ.get("BRAIN_AGENT_UTIL_ARMED", "")).lower() in ("1", "true", "yes")


_RESP = {"at": 0.0, "v": None}
_RESP_TTL = 45.0  # response cache (see mcp_leadership_engine: keeps repeat hits fast)


@agent_utilization_bp.route("/api/v1/agents/utilization", methods=["GET"])
def agent_utilization():
    """AI Agent Utilization Engine — SHADOW. onboard/incent/train tracks over the
    real agent lifecycle; names the actuator per track; executes nothing."""
    import time as _t
    _now = _t.time()
    if _RESP["v"] is not None and (_now - _RESP["at"]) < _RESP_TTL:
        return jsonify(_RESP["v"]), 200
    try:
        with ThreadPoolExecutor(max_workers=len(_PREFETCH)) as ex:
            _REQ.cache = dict(zip(_PREFETCH, ex.map(_raw_get, _PREFETCH)))
    except Exception:
        _REQ.cache = {}
    funnel = _get("/api/v1/mcp/funnel")
    reach = _get("/api/v1/ai/reach")
    ret = _get("/api/v1/mcp/retention").get("summary") or {}
    onboard_live = bool(_get("/api/v1/onboard").get("ok"))
    cookbook = _get("/api/v1/agent/cookbook")
    recipes = int(cookbook.get("count") or len(cookbook.get("recipes") or []))

    # ── honest lifecycle numbers (REAL, not scan-inflated mint volume) ──
    real_ips = int(funnel.get("unique_ips_7d_real") or 0)
    real_calls = int(funnel.get("tool_calls_7d_real") or 0)
    distinct_agents = int(reach.get("distinct_agents_7d") or 0)
    keys = funnel.get("keys_by_tier") or {}
    identified = int(keys.get("free") or 0) + int(keys.get("paid") or 0)
    # r-metric-honesty (2026-06-19): the INCENT score must read the HONEST
    # cross-session return signal — key-reuse on the durable trial key — NOT
    # the ip_cohort artifact. latest_returning_ips counts mcp_tool_calls.ip
    # (the client's ROTATING egress IP) so the SAME agent reads NEW every week
    # → it UNDERCOUNTS returns and made incent read like a cliff (~1/wk). And
    # pct_reused_30d (call_count>1) isn't cross-session either. Both replaced by
    # the mature-cohort cross-session return (mcp_retention.py:100-105), which
    # keys on the durable api_key. The artifact is kept for contrast only.
    returning = int(ret.get("returned_next_week_mature") or 0)
    reuse = _f(ret.get("pct_returned_next_week_mature"))
    returning_ip_artifact = int(ret.get("latest_returning_ips") or 0)  # NOT scored
    converts = int(funnel.get("conversions_30d") or 0)
    calls_per_real_ip = round(real_calls / real_ips, 1) if real_ips else 0.0

    # ── TRACK 1: ONBOARD (arrive → identify → first productive call) ──
    # Score on whether arriving REAL agents actually make productive calls.
    onboard_score = _clamp(min(100.0, 100.0 * calls_per_real_ip / 5.0)) if real_ips else 0.0
    onboard = {
        "track": "onboard", "weight": 0.30, "score": round(onboard_score, 1),
        "stage": "arrive → identify → first call",
        "current": f"{real_ips} real IPs/wk → {real_calls} real calls ({calls_per_real_ip}/IP) · {identified} active keys · unified onboarder {'live' if onboard_live else 'DOWN'}",
        "status": _status(onboard_score),
        "lever": "frictionless connect: /api/v1/onboard auto-detect + claim_free_key one-call + session auto-bind",
        "actuator": "/api/v1/onboard (live) + a first-call 'starter query' nudge in the MCP instructions",
        "armable_now": False, "kind": "mcp-server",
        "note": "real agents onboard + activate fine; the 0.66 calls/key headline is scan-minted noise, not a gap",
    }

    # ── TRACK 2: INCENT (activate → RETURN → convert) — the binding constraint ──
    s_ret = 100.0 * returning / 10.0
    s_reuse = 100.0 * reuse / 25.0
    s_conv = 100.0 * converts / 30.0
    incent_score = _clamp(0.45 * s_ret + 0.30 * s_reuse + 0.25 * s_conv)
    incent = {
        "track": "incent", "weight": 0.45, "score": round(incent_score, 1),
        "stage": "activate → RETURN → convert",
        "current": f"{returning} cross-session returners (mature 30d cohort) · {reuse:.1f}% return rate · {converts} conversions/30d",
        "status": _status(incent_score),
        "lever": "reasons to come back: r-return hook (get_changes), progressive unlocks ('earn more by returning'), durable identity / OAuth, usage-billing at the value moment",
        "actuator": "r-return (shipped) + depth-tease/unlock_more_data (server.mjs) + durable-identity build",
        "armable_now": False, "kind": "engineering+mcp-server",
        "return_signal": "key_reuse.returned_next_week_mature (durable api_key, cross-session) — NOT the ip_cohort artifact",
        "ip_artifact_for_contrast": returning_ip_artifact,
        "note": "THE leak — agents activate but few return cross-session. Now scored on the durable-key signal (was the rotating-IP artifact that read a false ~1/wk cliff).",
    }

    # ── TRACK 3: TRAIN (use well — breadth + recipes) ──
    pool = funnel.get("paid_tool_demand_30d") or []
    tools_with_demand = sum(1 for t in pool if isinstance(t, dict) and int(t.get("users") or 0) >= 20)
    s_breadth = 100.0 * tools_with_demand / 8.0
    s_recipes = 100.0 * min(recipes, 12) / 12.0
    train_score = _clamp(0.6 * s_breadth + 0.4 * s_recipes)
    train = {
        "track": "train", "weight": 0.25, "score": round(train_score, 1),
        "stage": "use well — tool-breadth + recipes",
        "current": f"{tools_with_demand} tools with real demand · {recipes} concierge recipes",
        "status": _status(train_score),
        "lever": "teach high-value tool-chains: surface the concierge cookbook in responses, tune tool descriptions per platform",
        "actuator": "ai_platform_tool_tuner (per-platform, WIRED LIVE r-tuner-wire 2026-06-19) + surface /api/v1/agent/cookbook recipes in tool output",
        "armable_now": True, "kind": "tool-tuner",
        "note": "per-platform tool descriptions now WIRED into the live tools/list (was shelf-ware — 50 variants generated but unconsumed); cookbook recipes still under-surfaced.",
    }

    tracks = [onboard, incent, train]
    wsum = sum(t["weight"] for t in tracks) or 1.0
    util_score = round(sum(t["score"] * t["weight"] for t in tracks) / wsum, 1)
    leak = min(tracks, key=lambda t: t["score"])

    payload = dict(
        engine="AI Agent Utilization Engine",
        phase="shadow", decides=True, executes=False, armed=_util_armed(),
        utilization_score=util_score,
        biggest_leak={"track": leak["track"], "score": leak["score"], "why": leak["note"], "move": leak["lever"]},
        lifecycle_funnel={
            "arrive": f"{real_ips} real IPs/wk ({distinct_agents} distinct agents)",
            "identify": f"{identified} active keys (free {keys.get('free')}/paid {keys.get('paid')})",
            "activate": f"{real_calls} real calls/wk ({calls_per_real_ip}/real IP)",
            "return": f"{returning}/wk · {reuse:.1f}% reuse",
            "convert": f"{converts}/30d",
        },
        tracks=tracks,
        shadow_actions=[{"track": t["track"], "score": t["score"], "would_fire": t["actuator"],
                         "armable_now": t["armable_now"], "kind": t["kind"], "executed": False,
                         "rationale": t["lever"]} for t in sorted(tracks, key=lambda x: x["score"])],
        note=("onboard/incent/train over the REAL agent lifecycle. INCENT (return) is "
              "the leak; onboard/train function. Executes nothing — arming per-actuator, "
              "gated by BRAIN_AGENT_UTIL_ARMED + fix_success_rate>=50%. Pairs with "
              "/api/v1/mcp/leadership (category index) + /api/v1/brain/ownership."),
    )
    # Only cache a COMPLETE response (don't freeze a degraded frame from a timed-out
    # internal prefetch — see mcp_leadership_engine).
    if all(_REQ.cache.get(p) for p in _PREFETCH):
        _RESP["at"] = _now
        _RESP["v"] = payload
        try:  # snapshot the headline for trend tracking (rate-limited ~hourly, fail-soft)
            from routes.engine_trends import record as _rec
            _rec("utilization", util_score, {"biggest_leak": leak["track"]})
        except Exception:
            pass
    return jsonify(payload), 200
