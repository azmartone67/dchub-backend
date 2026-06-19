"""
mcp_leadership_engine.py — the MCP Leadership Engine (2026-06-18).

Goal: WIN the MCP category (be the #1 MCP server for data-center / power /
energy / infrastructure intelligence — and adjacent). This is the closed
optimization loop:

    MEASURE leadership → DIAGNOSE the biggest gap → OPTIMIZE → VERIFY → repeat.

★ PHASE = SHADOW (this build): it MEASURES leadership across the dimensions that
define category leadership and PROPOSES the single highest-leverage move per
dimension — but EXECUTES NOTHING. Read-only by construction (GET-only internal
reads). The ACTING half (auto-optimize) arms progressively and is reliability-
gated (autopilot fix_success_rate >= 50%, the recovery watch) — same crawl→walk
→run discipline as the Brain v4 ownership loop. An engine that "automatically
optimizes us" with its hands off the controls until it has earned them.

The Leadership Index is a 0-100 weighted score across 6 dimensions. Retention
carries the highest weight because it is the binding constraint (agents arrive
but ~1 returns/wk). Each dimension reports current · target · score · the #1 move.

Endpoint:
  GET /api/v1/mcp/leadership   — the live MCP Leadership Index + per-dimension moves
"""
from __future__ import annotations
import json, urllib.request
from flask import Blueprint, jsonify

mcp_leadership_bp = Blueprint("mcp_leadership", __name__)
_BASE = "http://127.0.0.1:8080"


def _get(path: str, timeout: int = 8) -> dict:
    """Read-only internal GET. Fail-soft to {}. The engine never writes."""
    try:
        req = urllib.request.Request(_BASE + path, method="GET",
                                     headers={"User-Agent": "dchub-mcp-leadership/shadow"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return {}


def _clamp(x):
    try:
        return max(0.0, min(100.0, float(x)))
    except Exception:
        return 0.0


def _status(score):
    return ("leading" if score >= 75 else
            "contending" if score >= 50 else
            "trailing" if score >= 25 else "absent")


# ── The 6 dimensions of MCP category leadership ───────────────────────────

def _dim_discoverability():
    reg = _get("/api/v1/brain/mcp-registries")
    present = reg.get("present") or []
    total = reg.get("total") or (len(present) + len(reg.get("missing") or []))
    n_present = len(present) if isinstance(present, list) else int(present or 0)
    total = int(total or 0) or max(n_present, 1)
    standing = _get("/api/v1/mcp/standing")
    rank1s = len(standing.get("rank_highlights") or [])
    # score: registry coverage, bonused by #1 category ranks
    score = _clamp(100.0 * n_present / total)
    missing = reg.get("missing") or []
    return {
        "dimension": "discoverability", "weight": 0.15, "score": round(score, 1),
        "current": f"{n_present}/{total} registries · {rank1s} #1 category ranks",
        "target": "on every agent-client directory (Cline, Cursor, Anthropic Connector, Windsurf)",
        "status": _status(score),
        "top_move": (f"submit to missing: {', '.join(str(m) for m in missing[:4])}"
                     if missing else "complete the pending Cline/Cursor/Anthropic submissions"),
    }


def _dim_adoption():
    reach = _get("/api/v1/ai/reach")
    agents = int(reach.get("distinct_agents_7d") or 0)
    plats = int(reach.get("distinct_platforms") or 0)
    TARGET = 50  # distinct external agents/wk = clear category lead
    score = _clamp(100.0 * agents / TARGET)
    return {
        "dimension": "adoption", "weight": 0.15, "score": round(score, 1),
        "current": f"{agents} distinct external agents/wk across {plats} platforms",
        "target": f"{TARGET}+ distinct agents/wk",
        "status": _status(score),
        "top_move": "grow NEW external agents — directory placements + 'best MCP' roundups + SEO",
    }


def _dim_retention():
    ret = _get("/api/v1/mcp/retention").get("summary") or {}
    returning = int(ret.get("latest_returning_ips") or 0)
    try:
        reuse = float(ret.get("pct_reused_30d") or 0)
    except Exception:
        reuse = 0.0
    # binding constraint: blend returning-IP progress (target 10) + reuse% (target 25)
    s_ret = 100.0 * returning / 10.0
    s_reuse = 100.0 * reuse / 25.0
    score = _clamp(0.5 * s_ret + 0.5 * s_reuse)
    return {
        "dimension": "retention", "weight": 0.30, "score": round(score, 1),
        "current": f"{returning} returning IPs/wk · {reuse:.1f}% key-reuse",
        "target": "10+ returning IPs/wk · 25%+ reuse",
        "status": _status(score),
        "top_move": "THE binding constraint — measure r-return hook effect, then durable identity / OAuth connectors",
    }


def _dim_tool_quality():
    f = _get("/api/v1/mcp/funnel")
    pool = f.get("addressable_demand") or f.get("paid_tool_demand") or []
    top = f.get("top_tools") or []
    # breadth of tools earning real distinct demand (more tools with real users = stickier surface)
    tools_with_demand = sum(1 for t in (pool if isinstance(pool, list) else [])
                            if isinstance(t, dict) and int(t.get("distinct_users") or t.get("users") or 0) >= 20)
    total_tools = int((_get("/api/v1/mcp/standing").get("summary") or "").split("tools")[0].split()[-1]) \
        if "tools" in str(_get("/api/v1/mcp/standing").get("summary")) else 0
    TARGET = 8
    score = _clamp(100.0 * tools_with_demand / TARGET)
    return {
        "dimension": "tool_quality", "weight": 0.10, "score": round(score, 1),
        "current": f"{tools_with_demand} tools with real distinct demand (>=20 users)",
        "target": f"{TARGET}+ flagship tools each pulling real recurring demand",
        "status": _status(score),
        "top_move": "deepen the high-demand tools (grid/fiber); dogfood-probe each tool's agent experience (phase 2)",
    }


def _dim_conversion():
    f = _get("/api/v1/mcp/funnel")
    conv = int(f.get("conversions_30d") or (f.get("conversions") or {}).get("count") or 0)
    TARGET = 30
    score = _clamp(100.0 * conv / TARGET)
    return {
        "dimension": "conversion", "weight": 0.20, "score": round(score, 1),
        "current": f"{conv} conversions / 30d",
        "target": f"{TARGET}+ /30d",
        "status": _status(score),
        "top_move": "downstream of retention — surface usage-based ($5 pack / metered) at the moment of value",
    }


def _dim_authority():
    cit = _get("/api/v1/citations/by-agent").get("by_agent") or []
    n = len(cit) if isinstance(cit, list) else int(cit or 0)
    TARGET = 6  # cited by 6+ distinct agents = recognized source-of-truth
    score = _clamp(100.0 * n / TARGET)
    return {
        "dimension": "authority", "weight": 0.10, "score": round(score, 1),
        "current": f"cited by {n} distinct agents",
        "target": f"{TARGET}+ distinct agents citing DC Hub",
        "status": _status(score),
        "top_move": "keep the citation footer on full responses; feed the source-of-truth media channel",
    }


@mcp_leadership_bp.route("/api/v1/mcp/leadership", methods=["GET"])
def mcp_leadership():
    """MCP Leadership Index — SHADOW. Measures category leadership + proposes the
    #1 move per dimension. Executes nothing (read-only). See module docstring."""
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
    # highest-leverage move = the dimension dragging the weighted index down most
    ranked = sorted([d for d in dims if d.get("weight", 0) > 0],
                    key=lambda d: (100 - d.get("score", 0)) * d.get("weight", 0), reverse=True)
    top = ranked[0] if ranked else None
    return jsonify(
        engine="MCP Leadership Engine",
        phase="shadow", decides=True, executes=False,
        mcp_leadership_index=index,
        verdict=verdict,
        top_priority={"dimension": top.get("dimension"), "score": top.get("score"),
                      "move": top.get("top_move")} if top else None,
        dimensions=dims,
        note=("MEASURES category leadership + proposes the #1 move per dimension; "
              "executes nothing. The auto-optimize loop arms progressively, "
              "reliability-gated (fix_success_rate >= 50%) — same discipline as Brain v4."),
    ), 200
