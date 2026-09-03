---
title: DC Hub — Power Index
emoji: ⚡
colorFrom: blue
colorTo: green
sdk: gradio
sdk_version: 5.50.0
app_file: app.py
pinned: true
license: cc-by-4.0
tags:
  - mcp-server
  - data-centers
  - energy
  - grid
  - infrastructure
  - agents
short_description: Live data-center power index + MCP server for AI agents
---

# ⚡ DC Hub — Power Index (live demo + MCP server)

**Can you actually get power to build a data center here?** DC Hub scores 311 U.S. + global data-center markets on power availability — a 0–100 index with a **BUILD / CAUTION / AVOID** verdict, average power cost, and a modeled time-to-power. Live from [dchub.cloud](https://dchub.cloud).

The headline today: the three biggest markets — **Northern Virginia, Phoenix, and Columbus all score AVOID** (Northern Virginia is a modeled ~60-month wait for firm power), while build-ready capacity has shifted to the interior — **Cheyenne, Omaha, and Tulsa score BUILD**. Try it above.

## 🔌 Connect in 30 seconds

This Space **is an MCP server** (7 tools). The full DC Hub MCP has **every DC Hub tool** — 10 calls/day free, no key needed. (Live tool, facility and deal counts: [dchub.cloud/api/v1/canon/phrases](https://dchub.cloud/api/v1/canon/phrases) — linked rather than restated, because this README is static and cannot read canon, so any number written here goes stale and ships. It has happened; the retired values are recorded in `app.py`, where a comment can hold them without publishing them.)

```bash
# This Space (7 tools, free)
claude mcp add --transport sse dchub-power-index https://dchubcloud-dchub.hf.space/gradio_api/mcp/sse

# Full DC Hub MCP (all tools)
claude mcp add --transport http dchub https://dchub.cloud/mcp
```

Any other MCP client (Cursor, Cline, Windsurf, …):

```json
{
  "mcpServers": {
    "dchub-power-index": { "url": "https://dchubcloud-dchub.hf.space/gradio_api/mcp/sse" },
    "dchub":             { "url": "https://dchub.cloud/mcp" }
  }
}
```

HF Agents / smolagents:

```python
from smolagents import CodeAgent, InferenceClientModel
from smolagents.mcp_client import MCPClient

with MCPClient({"url": "https://dchubcloud-dchub.hf.space/gradio_api/mcp/sse"}) as tools:
    agent = CodeAgent(tools=tools, model=InferenceClientModel())
    agent.run("Which U.S. market should I build a 100MW data center in?")
```

## The 7 tools in this Space

| Tool | What it returns |
|---|---|
| `dcpi_score` | DCPI verdict + score + time-to-power for one U.S. market |
| `compare_markets` | Several markets ranked by power availability |
| `grid_scoreboard` | Live ranked grids (fuel mix, renewable share, demand) |
| `search_facilities` | Facility search across DC Hub's global facility index |
| `rank_markets` | Markets ranked by capacity, power cost, operators, speed |
| `interconnection_queue` | US interconnection-queue capacity, per ISO |
| `hyperscaler_deals` | Recent hyperscaler / AI-capex deals from the M&A tracker |

## Want the full toolset?

Connect the full server — **`https://dchub.cloud/mcp`** — every tool over the full global facility base, real-time grid telemetry (US ISOs + EU + GB + Taiwan + Japan + South Korea + Brazil), fiber, gas, water, tax incentives, and deal intelligence. 10 calls/day free anonymous; mint a free key with its `claim_free_key` tool. Or explore with zero setup at [dchub.cloud/playground](https://dchub.cloud/playground). Data is CC-BY-4.0 for citation.
