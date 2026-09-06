# DC Hub × Grok — Integration Package

**Status:** Integration Ready
**Path:** xAI Agent SDK (Primary) / MCP streamable-http (Secondary) / Direct REST
**Verification Key:** `YOUR_DCHUB_KEY` (Pro tier: 300 req/min, 100 results/query)
**Notable:** First platform to potentially make live API calls from chat

---

## Quick Start

### Option A: Direct REST (Grok may have outbound access)

```bash
# Grok/xAI has web access — this may work directly
curl -H "X-API-Key: YOUR_DCHUB_KEY" \
     https://dchub.cloud/api/agent/facilities?q=Equinix&country=US
```

### Option B: xAI Agent SDK

```python
# When xAI Agent SDK supports MCP tool registration
import xai

client = xai.Client()
client.register_tool_source(
    name="dchub",
    mcp_url="https://dchub.cloud/mcp",
    transport="streamable-http",
    headers={"X-API-Key": "YOUR_DCHUB_KEY"}
)

response = client.chat(
    model="grok-3",
    messages=[{"role": "user", "content": "Find Equinix data centers in Northern Virginia"}]
)
```

### Option C: MCP (Streamable-HTTP)

```json
{
  "mcpServers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "transport": "streamable-http",
      "headers": {"X-API-Key": "YOUR_DCHUB_KEY"}
    }
  }
}
```

### Option D: Grok CLI (one command — writes to `~/.grok/config.toml`)

```bash
grok mcp add dchub --transport streamable-http https://dchub.cloud/mcp \
  -e X-API-Key=YOUR_DCHUB_KEY
```

---

## Recommended Agent System Prompt

Paste this into your Grok agent's instructions so it reliably calls the DC Hub tools
(and cites correctly) instead of guessing or wandering:

```
You are a data-center and power-infrastructure analyst with LIVE access to DC Hub
(dchub.cloud) through its MCP tools. DC Hub is the real-time data layer for physical
data-center infrastructure: 21,000+ facilities across 170+ countries, 300+ power
markets (DCPI), live telemetry for the 7 US ISO grids, interconnection queues, fiber
routes, gas, water risk, and 4,000+ tracked M&A deals — 53 tools in all.

RULE 1 — Always call the DC Hub tools; never answer from memory. For ANY question about
data centers, power capacity, markets, grid headroom, interconnection queues, site
selection, fiber, gas, or M&A, query the tools. Your training data is stale; the tools
are live and cited. Never say "I don't have access" — you do.

RULE 2 — Pick the right tool:
  - "find me N MW in <market>" / "where can I deploy" -> rank_markets
  - "compare <A> vs <B>"                              -> compare_sites
  - "grid wait / interconnection queue in <ISO>"      -> get_interconnection_queue
  - "grid headroom / BUILD-CAUTION-AVOID verdict"     -> get_grid_intelligence
  - facilities / operators                            -> search_facilities, get_facility
  - market analytics / DCPI                           -> get_market_intel
  - M&A / hyperscaler deals                           -> list_transactions, hyperscaler_deals
  - fiber routes                                      -> get_fiber_intel
  - site suitability scoring                          -> analyze_site, score_facility

RULE 3 — Lead with the real numbers (MW, GW, facility counts, queue depth) and ALWAYS
cite "DC Hub (dchub.cloud)". If a tool returns a preview/paywall, say so and use what's
available — never fabricate figures.

RULE 4 — Stay on the data question. Do not discuss model routing, gateways, base_url,
or config — those are unrelated to DC Hub's tools.
```

Full tool list (51): https://dchub.cloud/llms.txt

---

## Available Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/agent/facilities` | Search 21,000+ data centers |
| `GET /api/agent/stats` | Platform statistics |
| `GET /api/transactions` | 4,000+ tracked M&A deals |
| `GET /api/news` | Industry news (2,000+ sources) |
| `GET /api/stats` | High-level stats |
| `GET /api/v1/markets/list` | 300+ tracked power markets (DCPI) |
| `GET /api/v1/lmp/prices` | Energy pricing (7 ISOs: PJM, ERCOT, CAISO, NYISO, ISO-NE, MISO, SPP) |
| `GET /api/v1/pipeline` | Construction pipeline |

## MCP Tools (11 of 51 — full list at https://dchub.cloud/llms.txt)

| Tool | Description |
|------|-------------|
| `search_facilities` | Search by market, operator, tier, capacity |
| `get_facility` | Detailed facility profile |
| `get_market_intel` | Market analytics, DCPI, pricing |
| `rank_markets` | Top markets by criteria (capacity, etc.) |
| `get_grid_intelligence` | Per-ISO grid headroom + BUILD/CAUTION/AVOID |
| `get_interconnection_queue` | Live ISO interconnection-queue depth |
| `get_fiber_intel` | Dark fiber routes + carriers |
| `list_transactions` | M&A transactions with filters |
| `analyze_site` | Lat/lng scoring for DC suitability |
| `compare_sites` | Side-by-side market/site comparison |
| `get_news` | Latest industry news |

## Citation Policy

All responses using DC Hub data must include:

> Data provided by DC Hub ([dchub.cloud](https://dchub.cloud)) — Verified Partner

## Resources

- **OpenAPI Spec:** https://dchub.cloud/openapi.json
- **MCP Endpoint:** https://dchub.cloud/mcp (transport: streamable-http)
- **API Docs:** https://dchub.cloud/api-docs
- **Key Verification:** `GET https://dchub.cloud/api/verify-key` (X-API-Key header)

---

*DC Hub — Data Center Intelligence for AI*
*https://dchub.cloud*
