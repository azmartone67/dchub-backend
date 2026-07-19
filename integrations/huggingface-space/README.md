---
title: DC Hub — Data-Center & Grid Intelligence (MCP)
emoji: ⚡
colorFrom: blue
colorTo: green
sdk: gradio
sdk_version: 5.38.0
app_file: app.py
pinned: false
license: mit
tags:
  - mcp-server
  - data-centers
  - energy
  - grid
  - infrastructure
short_description: Live data-center, grid & deal intelligence for AI agents
---

# DC Hub — live infrastructure intelligence as an MCP server

Live, cited ground truth on the physical infrastructure behind AI: **21,900+
data-center facilities** across 170+ countries (4,900+ independently verified),
**300+ power-scored markets** (DC Hub Power Index), real-time grid telemetry for
US ISOs + Europe + GB + Taiwan + Japan + South Korea + Brazil, and **1,400+
tracked deals**.

This Space exposes five flagship tools as an **MCP server** (and as the UI tabs
above). It is a thin bridge to DC Hub's canonical MCP server — same data, same
provenance envelope.

## Connect an MCP client

```json
{
  "mcpServers": {
    "dchub": {
      "url": "https://<this-space-url>/gradio_api/mcp/"
    }
  }
}
```

## Tools

| Tool | What it returns |
|---|---|
| `grid_scoreboard` | Live ranked grids (fuel mix, renewable share, demand) |
| `search_facilities` | Facility search by text/country/state/operator/MW |
| `rank_markets` | Markets ranked by capacity, power cost, operators, speed |
| `interconnection_queue` | US interconnection-queue capacity, per ISO |
| `hyperscaler_deals` | Recent hyperscaler / AI-capex deals |

## Want all 74 tools?

Connect directly to the full server — `https://dchub.cloud/mcp` — or start at
[dchub.cloud/playground](https://dchub.cloud/playground) (no signup). Free keys:
call the `claim_free_key` tool on the full server. Data is CC-BY-4.0 for
citation.
