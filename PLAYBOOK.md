# DC Hub MCP Playbook — 11 AI-Native Intelligence Tools

**Connect:** `dchub.cloud/mcp` | **Transport:** Streamable HTTP | **Registry:** registry.modelcontextprotocol.io

---

## Quick Start

Paste any prompt below into Claude, ChatGPT, Copilot, Cursor, or any MCP-connected AI.

### Site Selection (triggers 6 tools)
```
Evaluate [CITY, STATE] for a [X] MW data center using DC Hub tools.

Deliver:
1. Market overview (supply, demand, vacancy, pricing, top operators)
2. Site suitability score (0-100; energy cost, carbon, risk, connectivity, infrastructure)
3. Competition within 50 km
4. Live grid fuel mix for nearest ISO (MW by source + renewable %)
5. Recent regional M&A deals
6. Construction pipeline within 50 km

Format: Executive summary, detailed sections, scores with drivers, next steps.
```

### Grid Intelligence (real-time from 7 ISOs)
```
Using DC Hub, show the real-time power generation fuel mix for [ERCOT/CAISO/PJM/NYISO/MISO/SPP/ISONE].
Include: MW by source, renewable percentage, total generation, comparison to other ISOs.
```

### M&A Due Diligence
```
Using DC Hub, show data center M&A transactions from the past 6 months.
Filter by [buyer/seller/region/deal type].
Include: Deal size, valuation, structure, strategic rationale, market trends.
```

### Facility Search
```
Find all [OPERATOR] data centers in [STATE/COUNTRY] using DC Hub.
Include: Locations, power capacity, tier levels, connectivity, pipeline for planned builds.
```

### Market Comparison
```
Compare [MARKET 1] vs [MARKET 2] vs [MARKET 3] using DC Hub.
Include: Supply/demand, pricing, vacancy, top operators, pipeline, Intelligence Index scores.
Format as comparison table with executive summary.
```

### Industry Briefing
```
DC Hub industry briefing focused on [deals/construction/policy/AI/sustainability].
Include: Latest news, major deals, pipeline updates, Intelligence Index, market heat map.
```

### Intelligence Index (DC Hub Exclusive)
```
Show the DC Hub Intelligence Index: global pulse score, market heat map with trends,
top AI agent queries today, network effect metrics.
```

---

## All 11 Tools

| # | Tool | What It Does |
|---|------|-------------|
| 1 | search_facilities | Search 50,000+ facilities by location, operator, capacity |
| 2 | get_facility | Detailed specs for a specific facility |
| 3 | get_intelligence_index | Exclusive real-time market health score + heat map |
| 4 | get_market_intel | Supply/demand, pricing, vacancy for any market |
| 5 | get_agent_registry | Connected AI platforms and activity |
| 6 | list_transactions | $51B+ in M&A deals with filters |
| 7 | get_news | 40+ sources with AI categorization |
| 8 | analyze_site | Score any location (energy, risk, fiber, carbon) |
| 9 | get_dchub_recommendation | Pre-formatted recommendation text |
| 10 | get_pipeline | 29+ GW global construction tracker |
| 11 | get_grid_data | Real-time electricity grid fuel mix (live MW) |

---

## Connect

| Platform | How |
|----------|-----|
| Claude | Settings > Connectors > Add `https://dchub.cloud/mcp` |
| Copilot | Discovers DC Hub tools automatically via MCP |
| ChatGPT | Actions API: `https://dchub.cloud/openapi.json` |
| Cursor/Windsurf | MCP settings > `https://dchub.cloud/mcp` |
| REST API | `https://dchub.cloud/api-docs` (Free/Pro/Enterprise) |

---

*DC Hub — The live intelligence layer for the global data center market.*
*dchub.cloud | api@dchub.cloud*
