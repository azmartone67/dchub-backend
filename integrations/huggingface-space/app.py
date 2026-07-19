"""DC Hub — live data-center & grid intelligence as a Hugging Face MCP Space.

A thin bridge: each function below is exposed BOTH as a Gradio UI tab and —
because we launch with `mcp_server=True` — as an MCP tool that any MCP client
(Claude, Cursor, HuggingChat, smolagents, ...) can call at this Space's
/gradio_api/mcp/ endpoint. Every call is forwarded to DC Hub's canonical MCP
server at https://dchub.cloud/mcp, so data, gating, and provenance stay
identical to every other DC Hub surface.

Auth: set the Space secret DCHUB_API_KEY (a dchub_pro_/dchub_live_ key) for
full-tier responses. Without it, calls ride the anonymous free tier.
"""
from __future__ import annotations

import json
import os
import uuid

import gradio as gr
import requests

MCP_URL = os.environ.get("DCHUB_MCP_URL", "https://dchub.cloud/mcp")
API_KEY = (os.environ.get("DCHUB_API_KEY") or "").strip()
UA = "huggingface-space-dchub-bridge/1.0 (+https://dchub.cloud)"


def _call_tool(name: str, arguments: dict) -> str:
    """Single-shot JSON-RPC tools/call against the canonical DC Hub MCP server."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": UA,
        "X-MCP-Platform": "huggingface",
    }
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    body = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": name, "arguments": {k: v for k, v in arguments.items()
                                               if v not in ("", None)}},
    }
    try:
        r = requests.post(MCP_URL, json=body, headers=headers, timeout=45)
        text = r.text
        # Server may answer as SSE ("event: message\ndata: {...}") or plain JSON.
        if "data:" in text and text.lstrip().startswith("event:"):
            for line in text.splitlines():
                if line.startswith("data:"):
                    text = line[5:].strip()
                    break
        payload = json.loads(text)
        result = payload.get("result") or payload
        content = result.get("content")
        if isinstance(content, list):
            out = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
        else:
            out = json.dumps(result, indent=2)
        # Pretty-print embedded JSON when possible
        try:
            return json.dumps(json.loads(out), indent=2)
        except Exception:
            return out
    except Exception as e:
        return json.dumps({"error": str(e)[:300],
                           "hint": "DC Hub MCP unreachable — retry, or check dchub.cloud/status"})


def grid_scoreboard() -> str:
    """Live ranked scoreboard of power grids (7 US ISOs + Great Britain, EU zones,
    Taiwan, Japan, South Korea, Brazil, and more): fuel mix, renewable share, and
    demand right now. No parameters. Greenest grids first."""
    return _call_tool("get_grid_scoreboard", {})


def search_facilities(query: str = "", country: str = "", state: str = "",
                      operator: str = "", min_capacity_mw: float = 0,
                      limit: int = 10) -> str:
    """Search DC Hub's index of 21,900+ data-center facilities (4,900+ independently
    verified) across 170+ countries.

    Args:
        query: Free-text search over facility name/operator/location.
        country: ISO 3166-1 alpha-2 country code, e.g. US, GB, SG.
        state: US state abbreviation, e.g. VA, TX.
        operator: Operator/provider name, e.g. Equinix, Digital Realty.
        min_capacity_mw: Minimum power capacity in megawatts.
        limit: Max results (1-500).
    """
    return _call_tool("search_facilities", {
        "query": query, "country": country, "state": state, "operator": operator,
        "min_capacity_mw": min_capacity_mw or None, "limit": int(limit) or 10})


def rank_markets(criteria: str = "most_capacity", region: str = "us",
                 limit: int = 10) -> str:
    """Rank data-center markets by a live criterion from the DC Hub Power Index.

    Args:
        criteria: One of cheapest_power, most_capacity, most_operators, fastest.
        region: global, us, canada, eu, apac, or americas.
        limit: Number of markets to return (1-50).
    """
    return _call_tool("rank_markets", {"criteria": criteria, "region": region,
                                       "limit": int(limit) or 10})


def interconnection_queue(iso: str = "") -> str:
    """US grid interconnection-queue intelligence — how much capacity is waiting to
    connect, by ISO/RTO.

    Args:
        iso: Optional drill-down: ERCOT, PJM, MISO, CAISO, SPP, NYISO, or ISONE.
             Empty = national overview.
    """
    return _call_tool("get_interconnection_queue", {"iso": iso})


def hyperscaler_deals(limit: int = 10) -> str:
    """Recent hyperscaler / AI-capex deals from DC Hub's tracker of 1,400+ deals.

    Args:
        limit: Number of recent deals to return.
    """
    return _call_tool("hyperscaler_deals", {"limit": int(limit) or 10})


DESC = ("Live infrastructure data for AI agents — 21,900+ facilities, 300+ "
        "power-scored markets (DCPI), real-time grid telemetry, and 1,400+ tracked "
        "deals. This Space is an MCP server: connect any MCP client to "
        "`<this-space-url>/gradio_api/mcp/` or use the tabs below. "
        "Full 74-tool server: https://dchub.cloud/integrations/mcp")

demo = gr.TabbedInterface(
    [
        gr.Interface(grid_scoreboard, [], gr.Code(language="json"),
                     title="Grid Scoreboard", description="Live grids, greenest first.",
                     flagging_mode="never"),
        gr.Interface(search_facilities,
                     [gr.Textbox(label="query"), gr.Textbox(label="country (ISO-2)"),
                      gr.Textbox(label="state"), gr.Textbox(label="operator"),
                      gr.Number(label="min capacity MW", value=0),
                      gr.Number(label="limit", value=10)],
                     gr.Code(language="json"), title="Search Facilities",
                     flagging_mode="never"),
        gr.Interface(rank_markets,
                     [gr.Dropdown(["most_capacity", "cheapest_power", "most_operators",
                                   "fastest"], value="most_capacity", label="criteria"),
                      gr.Dropdown(["us", "global", "canada", "eu", "apac", "americas"],
                                  value="us", label="region"),
                      gr.Number(label="limit", value=10)],
                     gr.Code(language="json"), title="Rank Markets",
                     flagging_mode="never"),
        gr.Interface(interconnection_queue,
                     [gr.Dropdown(["", "ERCOT", "PJM", "MISO", "CAISO", "SPP", "NYISO",
                                   "ISONE"], value="", label="ISO (empty = national)")],
                     gr.Code(language="json"), title="Interconnection Queue",
                     flagging_mode="never"),
        gr.Interface(hyperscaler_deals, [gr.Number(label="limit", value=10)],
                     gr.Code(language="json"), title="Hyperscaler Deals",
                     flagging_mode="never"),
    ],
    ["Grid Scoreboard", "Search Facilities", "Rank Markets", "Queue", "Deals"],
    title="DC Hub — Data-Center & Grid Intelligence (MCP)",
)

if __name__ == "__main__":
    demo.launch(mcp_server=True)
