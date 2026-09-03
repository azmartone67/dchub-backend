"""DC Hub — Power Index demo + MCP server (Hugging Face Space).

A live demo of the DC Hub Power Index (DCPI) that ALSO runs as an MCP server
(launch(mcp_server=True)) so Hugging Face Agents / smolagents and any MCP client
can query it. Seven tools: the two DCPI classics (dcpi_score, compare_markets)
plus five bridge tools (grid scoreboard, facility search, market ranking,
interconnection queue, hyperscaler deals) forwarded to DC Hub's canonical MCP
server. The complete DC Hub MCP — every tool over the full facility base, plus
real-time grid telemetry, fiber, gas and tracked M&A — lives at
https://dchub.cloud/mcp. Connect that for the full dataset (10 calls/day free
anonymous; free key via its claim_free_key tool). Live counts are published at
https://dchub.cloud/api/v1/canon/phrases and are NOT restated here: a docstring
is the MCP tool schema, it cannot interpolate, and a number baked into one goes
stale silently — which is what happened here, twice over. The retired values and
their measurements are recorded in the CANON note below, deliberately in a
comment rather than in this docstring, because a docstring cannot restate a
count without becoming the very thing it is warning about.

Optional Space secret DCHUB_API_KEY (a dchub_ key) lifts the bridge tools to
full-tier responses; without it they ride the anonymous free tier.

UI NOTE: the 7 tool functions below are the MCP contract (names + docstrings
become the tool schema at /gradio_api/mcp/schema) — keep them stable. UI-only
events that reuse them (e.g. textbox .submit) pass api_name=False so they
never mint a duplicate MCP tool.
"""
import json
import os
import uuid

import requests
import gradio as gr

API = "https://dchub.cloud"
FULL_MCP = "https://dchub.cloud/mcp"
SPACE_MCP = "https://dchubcloud-dchub.hf.space/gradio_api/mcp/sse"
TIMEOUT = 20
API_KEY = (os.environ.get("DCHUB_API_KEY") or "").strip()
UA = "huggingface-space-dchub-bridge/1.0 (+https://dchub.cloud)"

# ── CANON (2026-09-02): every headline count is FETCHED, never baked. ────────
# This file shipped, and the Space was LIVE-SERVING, four wrong numbers at once:
#   "21,900+ facilities"   canon 20,100+  — an OVER-claim, and a retired
#                                           PRE-DEDUP floor that sits on
#                                           ai_surface_canon's stale_markers
#                                           denylist (raw rows, not distinct
#                                           buildings)
#   "4,900+ verified"      no canon source — see the note below
#   "1,400+ tracked deals" canon 2,000+   — stale under-claim
#   "79 tools"             canon 83       — stale under-claim
#   "311 markets"          canon 300+     — 311 counts score ROWS, not scored
#                                           markets; the exact number is on the
#                                           bare-int denylist for that reason
#
# This Space runs on Hugging Face and CANNOT import ai_surface_canon, so the
# numbers come from the endpoint that exists for exactly this purpose. One call,
# at import, 6s, fail-soft.
#
# ★ THE FALLBACK IS COUNT-FREE, never a baked literal. A literal here refreezes
#   the moment canon moves, and this Space deploys on its own cadence from the
#   Hugging Face remote — not with this repo — so a frozen number would sit
#   public for months, which is exactly how 21,900+ survived. Saying less is
#   always honest; saying a stale number is not.
#
# ★ "verified" IS DELIBERATELY NOT REPUBLISHED. /api/v1/canon/phrases does not
#   carry it, and the one live field that looks like it does — stats
#   `discovered_verified` — ships its own warning: "a DE-DUPLICATION state (a
#   keeper was elected), NOT a source verification. Do not publish as
#   'verified'." Sourcing it from the other field would mean inventing a second
#   rounding rule out here, and canonical_stats._countries_floor already records
#   why that is a bug: two surfaces rounding the same number two different ways.
CANON_URL = f"{API}/api/v1/canon/phrases"


def _fetch_canon() -> dict:
    """Canonical count phrases, or {} — never raises, never blocks boot."""
    try:
        r = requests.get(CANON_URL, headers={"User-Agent": UA}, timeout=6)
        d = r.json() if r.ok else {}
        return d if isinstance(d, dict) and d.get("ok") else {}
    except Exception:
        return {}


_CANON = _fetch_canon()
FACILITIES = (f"{_CANON['facilities']} facilities" if _CANON.get("facilities")
              else "the global data-center facility base")
DEALS = (f"{_CANON['deals']} tracked deals" if _CANON.get("deals")
         else "tracked M&A deals")
MARKETS = (f"{_CANON['markets']} markets" if _CANON.get("markets")
           else "markets worldwide")
COUNTRIES = (f"{_CANON['countries']} countries" if _CANON.get("countries")
             else "countries worldwide")
TOOLS = f"{_CANON['tools']} tools" if _CANON.get("tools") else "the full tool set"


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
            f"_Full grid headroom, interconnection queue, fiber + {TOOLS}: connect the DC Hub MCP → **{FULL_MCP}**._"
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
    out.append(f"\n_Full ranking across {MARKETS}: DC Hub MCP → {FULL_MCP}._")
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
    """Search DC Hub's global index of data-center facilities.

    Live facility, deal and market counts: https://dchub.cloud/api/v1/canon/phrases
    (not restated here — this docstring IS the MCP tool schema and cannot
    interpolate, so any number in it would freeze).

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
    """Recent hyperscaler / AI-capex deals from DC Hub's M&A tracker.

    Live deal count: https://dchub.cloud/api/v1/canon/phrases (not restated —
    this docstring is the MCP tool schema and cannot interpolate).

    Args:
        limit: Number of recent deals to return.
    """
    return _call_tool("hyperscaler_deals", {"limit": int(limit) or 10})


# ── UI ──────────────────────────────────────────────────────────────────────
# Everything below is presentation only — no new MCP tools are minted here
# (UI-only events pass api_name=False).

CSS = """
.gradio-container { max-width: 1080px !important; margin: 0 auto !important; }
.dc-hero {
  border-radius: 14px;
  padding: 26px 28px 22px;
  margin-bottom: 4px;
  background: linear-gradient(120deg, #0b1220 0%, #101c33 55%, #1a2a17 100%);
  border: 1px solid rgba(250, 204, 21, .25);
}
.dc-hero h1 {
  margin: 0 0 6px;
  font-size: 1.65rem;
  line-height: 1.2;
  color: #f8fafc;
}
.dc-hero h1 .amp { color: #facc15; }
.dc-hero .sub {
  margin: 0 0 14px;
  font-size: 1rem;
  color: #cbd5e1;
  max-width: 62rem;
}
.dc-hero .sub a { color: #facc15; text-decoration: none; }
.dc-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.dc-chip {
  font-size: .8rem;
  padding: 4px 11px;
  border-radius: 999px;
  background: rgba(148, 163, 184, .14);
  border: 1px solid rgba(148, 163, 184, .28);
  color: #e2e8f0;
  white-space: nowrap;
}
.dc-chip.build   { background: rgba(34, 197, 94, .14);  border-color: rgba(34, 197, 94, .45);  color: #86efac; }
.dc-chip.caution { background: rgba(234, 179, 8, .14);  border-color: rgba(234, 179, 8, .45);  color: #fde047; }
.dc-chip.avoid   { background: rgba(239, 68, 68, .14);  border-color: rgba(239, 68, 68, .45);  color: #fca5a5; }
.dc-foot { text-align: center; font-size: .85rem; opacity: .8; margin-top: 6px; }
.dc-mcp-note { font-size: .9rem; margin: 2px 0 8px; }
"""

HERO = f"""
<div class="dc-hero">
  <h1><span class="amp">⚡</span> DC Hub — Power Index</h1>
  <p class="sub"><strong>Can you actually get power to build a data center here?</strong>
  Live 0–100 power-availability scores for {MARKETS} (U.S. + global), with a verdict,
  power cost, and modeled time-to-power — from <a href="https://dchub.cloud">dchub.cloud</a>.</p>
  <div class="dc-chips">
    <span class="dc-chip build">🟢 BUILD ≥ 60</span>
    <span class="dc-chip caution">🟡 CAUTION 30–59</span>
    <span class="dc-chip avoid">🔴 AVOID &lt; 30</span>
    <span class="dc-chip">{MARKETS}</span>
    <span class="dc-chip">{FACILITIES}</span>
    <span class="dc-chip">live grid telemetry</span>
    <span class="dc-chip">{DEALS}</span>
    <span class="dc-chip">CC-BY-4.0</span>
  </div>
</div>
"""

CLAUDE_CODE_SNIPPET = f"""# This Space (7 tools, free)
claude mcp add --transport sse dchub-power-index {SPACE_MCP}

# Full DC Hub MCP ({TOOLS}; 10 calls/day free, no key needed)
claude mcp add --transport http dchub {FULL_MCP}"""

JSON_SNIPPET = f"""{{
  "mcpServers": {{
    "dchub-power-index": {{ "url": "{SPACE_MCP}" }},
    "dchub":             {{ "url": "{FULL_MCP}" }}
  }}
}}"""

SMOLAGENTS_SNIPPET = f"""from smolagents import CodeAgent, InferenceClientModel
from smolagents.mcp_client import MCPClient

with MCPClient({{"url": "{SPACE_MCP}"}}) as tools:
    agent = CodeAgent(tools=tools, model=InferenceClientModel())
    agent.run("Which U.S. market should I build a 100MW data center in?")"""


with gr.Blocks(title="DC Hub — Power Index",
               theme=gr.themes.Soft(primary_hue="amber", neutral_hue="slate"),
               css=CSS) as demo:
    gr.HTML(HERO)

    with gr.Accordion("🔌 Connect via MCP — Claude Code · Claude Desktop · Cursor · smolagents",
                      open=False):
        gr.Markdown(
            f"<p class='dc-mcp-note'>This Space <strong>is an MCP server</strong> "
            f"(7 tools) at <code>{SPACE_MCP}</code>. The full DC Hub MCP — "
            f"<strong>{TOOLS}</strong>, real-time grid telemetry, fiber, gas, "
            f"water, tax incentives, deal intelligence — is at "
            f"<code>{FULL_MCP}</code> (10 calls/day free anonymous; mint a free "
            f"key with its <code>claim_free_key</code> tool, or try it with zero "
            f"setup at <a href='https://dchub.cloud/playground'>dchub.cloud/playground</a>).</p>")
        with gr.Tabs():
            with gr.Tab("Claude Code"):
                gr.Code(CLAUDE_CODE_SNIPPET, language="shell", label="terminal")
            with gr.Tab("Desktop · Cursor"):
                gr.Code(JSON_SNIPPET, language="json", label="mcp config (JSON) — any MCP client")
            with gr.Tab("smolagents"):
                gr.Code(SMOLAGENTS_SNIPPET, language="python", label="python — HF Agents / smolagents")

    with gr.Tab("⚡ Score a market"):
        inp = gr.Textbox(label="U.S. data-center market", value="Northern Virginia",
                         placeholder="Northern Virginia, Phoenix, Columbus, Cheyenne…",
                         info="One market name — get the verdict, score, cost, and time-to-power.")
        btn = gr.Button("Get DCPI score", variant="primary")
        out = gr.Markdown()
        btn.click(dcpi_score, inp, out)
        inp.submit(dcpi_score, inp, out, api_name=False)
        gr.Examples([["Northern Virginia"], ["Phoenix"], ["Columbus"],
                     ["Cheyenne"], ["Dallas"], ["Omaha"]], inp)
    with gr.Tab("🏆 Compare markets"):
        cinp = gr.Textbox(label="Markets (comma-separated)",
                          value="Northern Virginia, Phoenix, Cheyenne, Omaha, Dallas",
                          info="Up to 8 U.S. markets — ranked best power-availability first.")
        cbtn = gr.Button("Rank by power availability", variant="primary")
        cout = gr.Markdown()
        cbtn.click(compare_markets, cinp, cout)
        cinp.submit(compare_markets, cinp, cout, api_name=False)
        gr.Examples([["Northern Virginia, Phoenix, Cheyenne, Omaha, Dallas"],
                     ["Atlanta, Columbus, Tulsa"],
                     ["Dallas, Austin, San Antonio"]], cinp)
    with gr.Tab("🌍 Grid scoreboard"):
        gr.Markdown("Live ranked grids — US ISOs + GB, EU, Taiwan, Japan, South Korea, "
                    "Brazil: fuel mix, renewable share, and demand **right now**. "
                    "Greenest first.")
        gout = gr.Code(language="json", label="live scoreboard")
        gr.Button("Live grid scoreboard (greenest first)", variant="primary").click(
            grid_scoreboard, [], gout)
    with gr.Tab("🔎 Search facilities"):
        with gr.Row():
            s_q = gr.Textbox(label="Query", value="Ashburn",
                             placeholder="name / operator / location")
            s_c = gr.Textbox(label="Country (ISO-2)", value="US", placeholder="US, GB, SG…")
            s_s = gr.Textbox(label="State", placeholder="VA, TX…")
        with gr.Row():
            s_o = gr.Textbox(label="Operator", placeholder="Equinix, Digital Realty…")
            s_m = gr.Number(label="Min capacity (MW)", value=0)
            s_l = gr.Number(label="Limit", value=10)
        sout = gr.Code(language="json", label="facilities")
        gr.Button(f"Search {FACILITIES}", variant="primary").click(
            search_facilities, [s_q, s_c, s_s, s_o, s_m, s_l], sout)
    with gr.Tab("📊 Rank markets"):
        with gr.Row():
            r_c = gr.Dropdown(["most_capacity", "cheapest_power", "most_operators",
                               "fastest"], value="most_capacity", label="Criteria")
            r_r = gr.Dropdown(["us", "global", "canada", "eu", "apac", "americas"],
                              value="us", label="Region")
            r_l = gr.Number(label="Limit", value=10)
        rout = gr.Code(language="json", label="ranking")
        gr.Button("Rank markets", variant="primary").click(
            rank_markets, [r_c, r_r, r_l], rout)
    with gr.Tab("🔌 Interconnection queue"):
        q_i = gr.Dropdown(["", "ERCOT", "PJM", "MISO", "CAISO", "SPP", "NYISO",
                           "ISONE"], value="", label="ISO",
                          info="How much capacity is waiting to connect. Empty = national overview.")
        qout = gr.Code(language="json", label="queue intelligence")
        gr.Button("Queue intelligence", variant="primary").click(
            interconnection_queue, [q_i], qout)
    with gr.Tab("💸 Hyperscaler deals"):
        d_l = gr.Number(label="Limit", value=10,
                        info=f"Most recent hyperscaler / AI-capex deals from the {DEALS} tracker.")
        dout = gr.Code(language="json", label="deals")
        gr.Button("Recent AI-capex deals", variant="primary").click(
            hyperscaler_deals, [d_l], dout)

    gr.HTML(
        f"<div class='dc-foot'>Data: <a href='https://dchub.cloud'>DC Hub</a>, CC-BY-4.0 — "
        f"the live infrastructure data layer for AI agents · "
        f"<a href='{API}/playground'>playground</a> · "
        f"<a href='{API}/signup'>free key</a></div>")

if __name__ == "__main__":
    # mcp_server=True exposes all 7 tools as MCP tools at /gradio_api/mcp/sse —
    # usable by HF Agents, smolagents, and any MCP client.
    demo.launch(mcp_server=True)
