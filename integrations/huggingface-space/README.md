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

## This Space is also an MCP server — 7 tools

It runs with `launch(mcp_server=True)`, so its tools are callable by **Hugging Face Agents, smolagents, and any MCP client** at `…/gradio_api/mcp/sse`.

| Tool | What it returns |
|---|---|
| `dcpi_score` | DCPI verdict + score + time-to-power for one U.S. market |
| `compare_markets` | Several markets ranked by power availability |
| `grid_scoreboard` | Live ranked grids (fuel mix, renewable share, demand) |
| `search_facilities` | Facility search across 21,900+ facilities (4,900+ verified) |
| `rank_markets` | Markets ranked by capacity, power cost, operators, speed |
| `interconnection_queue` | US interconnection-queue capacity, per ISO |
| `hyperscaler_deals` | Recent hyperscaler / AI-capex deals (1,400+ tracked) |

## Want all 74 tools?

Connect the full server — **`https://dchub.cloud/mcp`** — 74 tools over 21,900+ facilities in 170+ countries, real-time grid telemetry (US ISOs + EU + GB + Taiwan + Japan + South Korea + Brazil), fiber, gas, water, tax incentives, and deal intelligence. 10 calls/day free anonymous; mint a free key with its `claim_free_key` tool. Or explore with zero setup at [dchub.cloud/playground](https://dchub.cloud/playground). Data is CC-BY-4.0 for citation.
