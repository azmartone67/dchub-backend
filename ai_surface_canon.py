"""
ai_surface_canon.py — THE single source of truth for AI-agent-facing surfaces.
==============================================================================

Every AI-agent surface (llms.txt, the .well-known manifests, AGENTS.md,
integration configs, /connect, /ai, robots.txt, the registry) currently
hand-types the same numbers, so they drift + contradict each other (v2.1.22 vs
2.3.3 vs 2.1.0; "24 tools" and "48 tools" on the SAME page; 232 vs 300+ markets;
50,000+ facilities on /connect). This module is the fix: one canon, with the
MOVING numbers resolved LIVE at read time so the canon itself never goes stale.

Used by ai_surface_sentinel.py to audit (and later auto-refresh) every surface.
"""
from __future__ import annotations

import json
import os
import urllib.request

_BASE = os.environ.get("DCHUB_BACKEND_BASE",
                       "https://dchub-backend-production.up.railway.app")

# ── Pinned structural canon (changes rarely; edit HERE, nowhere else) ──
PINNED = {
    "version": "2.4.3",                       # == registry isLatest record
    "tools_advertised": 53,                   # canonical advertised count == registry 2.4.3 == live tools/list (incl. Deep Research search/fetch)
    "mcp_endpoint": "https://dchub.cloud/mcp",
    "registry_id": "cloud.dchub/mcp-server",
    "rest_base": "https://dchub.cloud/api/v1",     # canonical host (NOT api.dchub.cloud)
    "free_tier_calls_per_day": 10,                 # NOT 100
    "platforms": ["Claude", "ChatGPT", "Gemini", "Perplexity", "Copilot", "Meta AI", "Grok"],
    "tool_names": [
        "search_facilities", "get_facility", "get_market_intel", "rank_markets",
        "get_grid_intelligence", "get_interconnection_queue", "get_fiber_intel",
        "get_gas_intelligence", "list_transactions", "hyperscaler_deals",
        "analyze_site", "compare_sites", "score_facility", "get_news",
    ],
    "fake_tool_denylist": ["get_market_data", "search_deals"],
    "crawlers_required": ["GrokBot", "xAI-Grok", "Grok-DeepSearch"],
    "public": {                                    # public-facing rounded strings
        "facilities": "21,000+",
        "markets": "300+",
        "deals": "2,000+",
        "countries": "170+",
    },
    # Values known to be STALE/WRONG on some surface — the sentinel flags these.
    "stale_markers": ["10,706", "10706", "50,000+", "50000", "317 ", "332 ",
                      "232 ", "100 calls/day", "3,000+ M&A", "DeepSeek", "Mistral",
                      "24 tools", "48 tools", "49 tools", "51 tools",
                      "2.1.22", "2.3.3", "2.1.0"],
}


def _get(path, timeout=15):
    req = urllib.request.Request(_BASE.rstrip("/") + path, method="GET")
    req.add_header("X-DC-Probe", "ai-surface-canon")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _mcp_tool_count(timeout=20):
    """Live count from the MCP server (tools/list)."""
    hdr = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "canon", "version": "1"}}}).encode()
    req = urllib.request.Request(_BASE.rstrip("/") + "/mcp", data=init, headers=hdr)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sid = r.headers.get("mcp-session-id")
    hdr2 = dict(hdr)
    if sid:
        hdr2["mcp-session-id"] = sid
    body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
    req2 = urllib.request.Request(_BASE.rstrip("/") + "/mcp", data=body, headers=hdr2)
    with urllib.request.urlopen(req2, timeout=timeout) as r:
        out = r.read().decode("utf-8", "replace")
    for ln in out.splitlines():
        if ln.startswith("data: "):
            ln = ln[6:]
        if ln.startswith("{") and '"id":2' in ln.replace(" ", ""):
            d = json.loads(ln)
            return len((d.get("result") or {}).get("tools", []))
    return None


def resolve_canon() -> dict:
    """Return the canon with the MOVING numbers resolved LIVE, so the canon
    itself is never stale. Falls back to public strings if a resolver fails."""
    c = json.loads(json.dumps(PINNED))  # deep copy
    c["resolved_at_note"] = "moving numbers resolved live"
    # facilities + markets from /api/v1/stats
    try:
        s = _get("/api/v1/stats")
        c["facilities_live"] = s.get("facilities")
        c["markets_live"] = s.get("markets")
        c["deals_live"] = s.get("deals")
    except Exception as e:
        c["_stats_error"] = str(e)[:120]
    # live tool count from the MCP server
    try:
        c["tools_live"] = _mcp_tool_count()
    except Exception as e:
        c["_tools_error"] = str(e)[:120]
    return c
