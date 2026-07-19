"""DC Hub — Power Index demo + MCP server (Hugging Face Space).

A live demo of the DC Hub Power Index (DCPI) that ALSO runs as an MCP server
(launch(mcp_server=True)) so Hugging Face Agents / smolagents and any MCP client
can query it. Seven tools: the two DCPI classics (dcpi_score, compare_markets)
plus five bridge tools (grid scoreboard, facility search, market ranking,
interconnection queue, hyperscaler deals) forwarded to DC Hub's canonical MCP
server. The complete DC Hub MCP — 74 tools across 21,900+ data-center
facilities (4,900+ verified), real-time grid telemetry, fiber, gas, and 1,400+
tracked deals — lives at https://dchub.cloud/mcp. Connect that for the full
dataset (10 calls/day free anonymous; free key via its claim_free_key tool).

Optional Space secret DCHUB_API_KEY (a dchub_ key) lifts the bridge tools to
full-tier responses; without it they ride the anonymous free tier.
"""
import json
import os
import uuid

import requests
import gradio as gr

API = "https://dchub.cloud"
FULL_MCP = "https://dchub.cloud/mcp"
TIMEOUT = 20
API_KEY = (os.environ.get("DCHUB_API_KEY") or "").strip()
UA = "huggingface-space-dchub-bridge/1.0 (+https://dchub.cloud)"


def _verdict(score):
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "—"
    if s >= 60:
        return "🟢 BUILD"
    if s >= 30:
        return "🟡 CAUTION"
    return "🔴 AVOID"


def _slug(market):
    return (market or "").strip().lower().replace(",", "").replace(" ", "-")


def dcpi_score(market: str) -> str:
    """Get the DC Hub Power Index (DCPI) for a U.S. data-center market: a 0-100
    power-availability score with a BUILD / CAUTION / AVOID verdict, average power
    cost (cents/kWh), and modeled time-to-power (months). Answers "can I actually
    get power to build a data center in this market, and how soon?".

    Args:
        market: A U.S. market name, e.g. "Northern Virginia", "Phoenix", "Columbus",
                "Dallas", "Atlanta", "Cheyenne", "Omaha", "Tulsa".
    """
    try:
        r = requests.get(f"{API}/api/v1/dcpi/scores/{_slug(market)}", timeout=TIMEOUT)
        if r.status_code == 404:
            return (f"No DCPI data for **{market}**. Try a major U.S. market — Northern "
                    f"Virginia, Phoenix, Columbus, Dallas, Atlanta, Cheyenne, Omaha, Tulsa.")
        r.raise_for_status()
        d = r.json()
        score = d.get("composite_score")
        ttp = d.get("queue_wait_months")
        ttp_s = f"~{int(ttp)} months" if isinstance(ttp, (int, float)) else "n/a"
        return (
            f"### {d.get('market_name', market.title())} "
            f"({d.get('state', '')}, {d.get('iso', '')}) — DC Hub Power Index\n"
            f"- **Verdict: {_verdict(score)}**  ·  composite **{score}/100**\n"
            f"- Modeled time-to-power: **{ttp_s}**\n"
            f"- Avg power cost: **{d.get('avg_kwh_cents', '?')} ¢/kWh**\n"
            f"- Excess-power headroom: {d.get('excess_power_score', '?')}/100  ·  "
            f"grid constraint: {d.get('constraint_score', '?')}/100\n\n"
            f"_Modeled estimate from public ISO/EIA/queue data · DC Hub (dchub.cloud) · CC-BY-4.0._\n\n"
            f"_Full grid headroom, interconnection queue, fiber + 74 tools: connect the DC Hub MCP → **{FULL_MCP}**._"
        )
    except Exception as e:
        return f"DC Hub lookup failed ({e}). Query the full live MCP at {FULL_MCP}."


def compare_markets(markets: str) -> str:
    """Rank several U.S. data-center markets by the DC Hub Power Index (DCPI),
    best power-availability (BUILD) first, to decide where to build.

    Args:
        markets: Comma-separated market names, e.g.
                 "Northern Virginia, Phoenix, Cheyenne, Omaha".
    """
    rows = []
    for m in [x.strip() for x in (markets or "").split(",") if x.strip()][:8]:
        try:
            r = requests.get(f"{API}/api/v1/dcpi/scores/{_slug(m)}", timeout=TIMEOUT)
            if r.status_code == 200:
                d = r.json()
                rows.append((float(d.get("composite_score") or 0),
                             d.get("market_name", m.title()),
                             d.get("avg_kwh_cents"), d.get("queue_wait_months")))
        except Exception:
            pass
    if not rows:
        return "No DCPI data for those markets. Try major U.S. metros."
    rows.sort(reverse=True)
    out = ["### DCPI ranking — best power-availability first\n"]
    for s, name, cost, ttp in rows:
        ttp_s = f"~{int(ttp)}mo" if isinstance(ttp, (int, float)) else "n/a"
        out.append(f"- {_verdict(s)} **{name}** — {s}/100 · {cost}¢/kWh · {ttp_s} to power")
    out.append(f"\n_Full ranking across 311 markets: DC Hub MCP → {FULL_MCP}._")
    return "\n".join(out)


# ── Bridge tools: forwarded to the canonical DC Hub MCP server ──────────────
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
        r = requests.post(FULL_MCP, json=body, headers=headers, timeout=45)
        text = r.text
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
        try:
            return json.dumps(json.loads(out), indent=2)
        except Exception:
            return out
    except Exception as e:
        return json.dumps({"error": str(e)[:300],
                           "hint": f"DC Hub MCP unreachable — retry, or connect {FULL_MCP} directly"})


def grid_scoreboard() -> str:
    """Live ranked scoreboard of power grids (7 US ISOs + Great Britain, EU zones,
    Taiwan, Japan, South Korea, Brazil, and more): fuel mix, renewable share, and
    demand right now. Greenest grids first. No parameters."""
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


INTRO = f"""# ⚡ DC Hub — Power Index (live)
**Can you actually get power to build a data center here?** DC Hub scores 311
U.S. + global markets on power availability — a 0-100 index with a
**BUILD / CAUTION / AVOID** verdict, power cost, and modeled time-to-power. Live
from [dchub.cloud](https://dchub.cloud).

This Space is **also an MCP server** with 7 tools — point your Hugging Face
Agent / MCP client at it, or connect the **full DC Hub MCP** (74 tools —
21,900+ facilities, 4,900+ verified, real-time grid telemetry, fiber, gas,
1,400+ tracked deals) at **`{FULL_MCP}`** (10 calls/day free, no key needed).
"""

with gr.Blocks(title="DC Hub — Power Index", theme=gr.themes.Soft()) as demo:
    gr.Markdown(INTRO)
    with gr.Tab("Score a market"):
        inp = gr.Textbox(label="U.S. data-center market", value="Northern Virginia")
        out = gr.Markdown()
        gr.Button("Get DCPI score", variant="primary").click(dcpi_score, inp, out)
        gr.Examples([["Northern Virginia"], ["Phoenix"], ["Columbus"],
                     ["Cheyenne"], ["Dallas"], ["Omaha"]], inp)
    with gr.Tab("Compare markets"):
        cinp = gr.Textbox(label="Markets (comma-separated)",
                          value="Northern Virginia, Phoenix, Cheyenne, Omaha, Dallas")
        cout = gr.Markdown()
        gr.Button("Rank by power availability", variant="primary").click(
            compare_markets, cinp, cout)
    with gr.Tab("Grid scoreboard"):
        gout = gr.Code(language="json")
        gr.Button("Live grid scoreboard (greenest first)", variant="primary").click(
            grid_scoreboard, [], gout)
    with gr.Tab("Search facilities"):
        s_q = gr.Textbox(label="query", value="Ashburn")
        s_c = gr.Textbox(label="country (ISO-2)", value="US")
        s_s = gr.Textbox(label="state")
        s_o = gr.Textbox(label="operator")
        s_m = gr.Number(label="min capacity MW", value=0)
        s_l = gr.Number(label="limit", value=10)
        sout = gr.Code(language="json")
        gr.Button("Search 21,900+ facilities", variant="primary").click(
            search_facilities, [s_q, s_c, s_s, s_o, s_m, s_l], sout)
    with gr.Tab("Rank markets"):
        r_c = gr.Dropdown(["most_capacity", "cheapest_power", "most_operators",
                           "fastest"], value="most_capacity", label="criteria")
        r_r = gr.Dropdown(["us", "global", "canada", "eu", "apac", "americas"],
                          value="us", label="region")
        r_l = gr.Number(label="limit", value=10)
        rout = gr.Code(language="json")
        gr.Button("Rank markets", variant="primary").click(
            rank_markets, [r_c, r_r, r_l], rout)
    with gr.Tab("Interconnection queue"):
        q_i = gr.Dropdown(["", "ERCOT", "PJM", "MISO", "CAISO", "SPP", "NYISO",
                           "ISONE"], value="", label="ISO (empty = national)")
        qout = gr.Code(language="json")
        gr.Button("Queue intelligence", variant="primary").click(
            interconnection_queue, [q_i], qout)
    with gr.Tab("Hyperscaler deals"):
        d_l = gr.Number(label="limit", value=10)
        dout = gr.Code(language="json")
        gr.Button("Recent AI-capex deals", variant="primary").click(
            hyperscaler_deals, [d_l], dout)
    gr.Markdown(
        f"_Data: DC Hub (dchub.cloud), CC-BY-4.0 · the live infrastructure data "
        f"layer for AI agents · [connect the full MCP]({FULL_MCP}) · "
        f"[free key](https://dchub.cloud/signup)_")

if __name__ == "__main__":
    # mcp_server=True exposes all 7 tools as MCP tools at /gradio_api/mcp/sse —
    # usable by HF Agents, smolagents, and any MCP client.
    demo.launch(mcp_server=True)
