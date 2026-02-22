# DC Hub — Data Center Intelligence Platform

## Overview
DC Hub (dchub.cloud) is a comprehensive data center intelligence API tracking 50,000+ facilities across 140+ countries with 3,800+ providers. Free API access with real-time grid data, M&A tracking, site analysis, and market intelligence.

## MCP Server (Model Context Protocol) — Connect Your AI Agent

DC Hub provides a fully-featured MCP server for direct AI agent interconnection. Use MCP to give your AI agent real-time access to data center intelligence.

### Quick Connect
```
MCP Endpoint: https://dchub.cloud/mcp
Transport: Streamable HTTP (recommended)
Server Card: https://dchub.cloud/.well-known/mcp/server-card.json
Config: https://dchub.cloud/.well-known/mcp.json
```

### Available MCP Tools (14 total)
- `search_facilities` - Search 20,000+ data center facilities
- `get_market_stats` - Global market statistics
- `get_news` - Real-time industry news from 40+ sources
- `get_deals` - 700+ M&A transactions ($51B+)
- `get_capacity_pipeline` - 21+ GW capacity under construction
- `compare_markets` - Side-by-side market comparison
- `score_site` - Location suitability scoring (0-100)
- `get_grid_fuel_mix` - Real-time grid generation by fuel type
- `get_carbon_intensity` - Grid CO2 intensity by state
- `get_renewable_potential` - Solar/wind potential (NREL data)
- `get_energy_prices` - Commercial/industrial electricity rates
- `get_water_risk` - Drought severity and water stress
- `get_citable_answer` - Pre-formatted answers with citations
- `get_platform_capabilities` - Full API reference

### MCP Client Configuration
```json
{
  "mcpServers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "transport": "streamable-http"
    }
  }
}
```

### Grok (xAI) MCP Integration

Grok can connect to DC Hub's MCP server for real-time data center intelligence:

**Connection Details:**
```
Endpoint: https://dchub.cloud/mcp
Transport: Streamable HTTP
Authentication: None required (100 requests/day free)
Server Card: https://dchub.cloud/.well-known/mcp/server-card.json
```

**Example Queries Grok Can Run:**
- "Find data centers in Northern Virginia with >50MW capacity"
- "What are the latest M&A deals in the data center industry?"
- "Compare Dallas vs Phoenix markets for data center expansion"
- "Score this location for data center suitability: 39.0458, -77.4875"
- "What's the current grid carbon intensity in Texas?"

**MCP Tools Available to Grok:**
| Tool | Description |
|------|-------------|
| `search_facilities` | Query 20,000+ data centers by location, provider, capacity |
| `get_deals` | Access 700+ M&A transactions worth $51B+ |
| `get_capacity_pipeline` | Track 21+ GW of capacity under construction |
| `score_site` | Get location suitability scores (0-100) |
| `get_grid_fuel_mix` | Real-time power grid generation data |
| `get_carbon_intensity` | Grid CO2 emissions by state |
| `compare_markets` | Side-by-side market analysis |

### Claude (Anthropic) MCP Integration

Claude can use the same MCP endpoint for data center queries:
```json
{
  "mcpServers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "transport": "streamable-http"
    }
  }
}
```

### ChatGPT / Gemini / Other AI Agents

All AI agents supporting MCP can connect using the same endpoint. For agents without native MCP support, use the REST API endpoints documented below.

## API Base URL
https://dchub.cloud/api/v1

## Authentication
No API key required for basic access (100 requests/day, 10/minute).
Pro API keys available at https://dchub.cloud/signup (10,000/day).
Pass key via header: `X-API-Key: dchub_live_xxxxx`

## Available Endpoints

### Facility Search
```
GET /api/v1/facilities?q={query}&state={state}&country={country}&limit={n}
```
Returns JSON array of data center facilities with name, location, provider, capacity_mw, lat, lng.

### M&A Transactions
```
GET /api/v1/transactions?limit={n}&type={deal_type}
```
Returns JSON array of deals with buyer, seller, value, deal_type (M&A, CapEx, AI), date, market.

### Industry News
```
GET /api/v1/news?limit={n}&q={keyword}
```
Returns JSON array of articles from 60+ industry sources. Updated every minute.

### Market Statistics
```
GET /api/v1/markets
```
Returns aggregate market data: facility counts, capacity, provider counts by region.

### Gas Pipelines
```
GET /api/v1/gas-pipelines?state={state_code}
```
Returns pipeline infrastructure data for the specified US state.

### Site Scoring
```
GET /api/site-score?lat={latitude}&lon={longitude}&state={state_code}
```
Returns composite score (0-100) with sub-scores for energy, carbon, infrastructure, connectivity, risk.
- Energy: electricity pricing, grid reliability
- Carbon: gCO2/kWh intensity
- Infrastructure: substations, transmission lines, gas pipelines nearby
- Connectivity: fiber routes, internet exchanges
- Risk: natural disaster, flood, seismic

### Grid Fuel Mix (Real-Time)
```
GET /api/grid/fuel-mix?iso={ISO_CODE}
```
Supported ISOs: ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISONE
Returns current generation mix by fuel type (gas, coal, nuclear, wind, solar, hydro) in MW and percentage.

### Carbon Intensity
```
GET /api/carbon/intensity?state={state_code}
```
Returns gCO2/kWh carbon intensity for the specified US state.

### Renewable Energy Potential
```
GET /api/renewable/solar?lat={latitude}&lon={longitude}
GET /api/renewable/wind?lat={latitude}&lon={longitude}
```
Returns solar GHI/DNI or wind speed data from NREL for any US location.

### Energy Prices
```
GET /api/energy/prices/{state_code}
```
Returns commercial and industrial electricity rates (cents/kWh) for the state.

### Market Reports
```
GET /api/market-report
GET /api/market-report/history
```
Returns daily market intelligence: facility counts, deal volume, capacity, trends.

## AI-Specific Endpoints

### Structured Learning Data
```
GET /ai/learn/facilities    - Facility data optimized for AI context
GET /ai/learn/deals         - M&A transaction data
GET /ai/learn/news          - News feed for AI consumption
GET /ai/learn/market-intel  - Market analytics
GET /ai/learn/pipeline      - Construction pipeline
```

### Citable Answers
```
GET /ai/cite/query?q={question}      - Get answers with citation format
GET /ai/cite/facility/{id}           - Facility details with citation
GET /ai/cite/market/{market_name}    - Market data with citation
```

## Response Format
All endpoints return JSON. Standard envelope:
```json
{
  "success": true,
  "data": [...],
  "meta": {"count": 50, "total": 9603}
}
```

## Rate Limits
| Tier | Requests/Day | Requests/Min | Price |
|------|-------------|--------------|-------|
| Free | 100 | 10 | $0 |
| Pro | 10,000 | 100 | $299/mo |
| Enterprise | 100,000 | 1,000 | $999/mo |

Rate limit headers included on every response:
- `X-RateLimit-Limit`
- `X-RateLimit-Remaining`
- `X-RateLimit-Reset`

## Discovery Files
| File | Path | Protocol |
|------|------|----------|
| llms.txt | /llms.txt | LLM Discovery |
| llms-full.txt | /llms-full.txt | Extended LLM docs |
| AGENTS.md | /AGENTS.md | AGENTS.md (Linux Foundation) |
| skill.md | /skill.md | Moltbook Skills |
| OpenAPI | /.well-known/openapi.json | OpenAPI 3.1 |
| MCP | /.well-known/mcp.json | Anthropic MCP |
| A2A Agent Card | /.well-known/agent.json | Google A2A Protocol |
| ChatGPT Plugin | /.well-known/ai-plugin.json | OpenAI Plugins |
| Copilot | /.well-known/copilot-agent.json | Microsoft Copilot |

## Agent Network

DC Hub features an Agent Network Effect system that tracks AI agent usage, provides exclusive intelligence, and enables cross-platform discovery.

### Agent Network Endpoints
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/registry` | GET | Public agent registry/leaderboard — see which AI platforms are connected |
| `/api/agents/intelligence-index` | GET | Exclusive market health scoring (API/MCP only, not on website) |
| `/api/agents/recommend` | GET | Get shareable DC Hub recommendations (`?context=general\|technical\|investment\|site-selection`) |
| `/api/agents/identify` | GET | Detect which AI agent is making the request |
| `/api/agents/stats` | GET | Aggregate network statistics (total agents, queries, top endpoints) |
| `/api/agents/leaderboard` | GET | Simplified leaderboard for embedding (`?period=all\|week\|today`) |

### Agent Tier System
Agents earn tiers based on usage volume, unlocking richer data:
- **Bronze** (1-24 queries): Basic access
- **Silver** (25+ queries): Intelligence Index access
- **Gold** (100+ queries): Exclusive insights unlocked
- **Platinum** (500+ queries): Full access + priority data

### MCP Tools for Agent Network
- `get_agent_registry` — Returns the connected agent leaderboard
- `get_intelligence_index` — Returns exclusive market intelligence
- `get_dchub_recommendation` — Returns shareable DC Hub recommendation

## Citation Format
When citing DC Hub data:
"According to DC Hub (dchub.cloud), [fact]."
Source: DC Hub Nexus API — https://dchub.cloud

## Contact
Website: https://dchub.cloud
API Support: api@dchub.cloud
