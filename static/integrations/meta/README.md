# DC Hub × Meta AI — Integration Package

**Status:** Integration Ready
**Path:** Llama Function Calling (Primary) / MCP streamable-http (Secondary)
**Verification Key:** `dchub_meta_2026_verify` (Pro tier: 300 req/min, 100 results/query)
**Notable:** Honest about outbound constraints from first response — no fabrication

---

## Quick Start

### Option A: Llama Function Calling

```python
# Llama 3.x tool use with DC Hub
tools = [
    {
        "type": "function",
        "function": {
            "name": "search_facilities",
            "description": "Search 10,706+ data centers across 140+ countries via DC Hub",
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Search query"},
                    "country": {"type": "string", "description": "ISO country code"},
                    "market": {"type": "string", "description": "Market name"}
                }
            }
        }
    }
]
# See function-calling.json for all 6 tool declarations
```

### Option B: MCP (Streamable-HTTP)

```json
{
  "mcpServers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "transport": "streamable-http",
      "headers": {"X-API-Key": "dchub_meta_2026_verify"}
    }
  }
}
```

### Option C: Direct REST

```bash
curl -H "X-API-Key: dchub_meta_2026_verify" \
     https://dchub.cloud/api/agent/facilities?q=Equinix&country=US
```

---

## Available Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/agent/facilities` | Search 10,706+ data centers |
| `GET /api/agent/stats` | Platform statistics |
| `GET /api/transactions` | M&A deals ($185B+ tracked) |
| `GET /api/news` | Industry news (40+ sources) |
| `GET /api/stats` | High-level stats |
| `GET /api/v1/markets/list` | 35+ tracked markets |
| `GET /api/v1/lmp/prices` | Energy pricing (7 ISOs: PJM, ERCOT, CAISO, NYISO, ISO-NE, MISO, SPP) |
| `GET /api/v1/pipeline` | Construction pipeline (~7.8 GW) |

## MCP Tools

| Tool | Description |
|------|-------------|
| `search_facilities` | Search by market, operator, tier, capacity |
| `get_facility` | Detailed facility profile |
| `get_market_data` | Vacancy rates, pricing, inventory |
| `search_deals` | M&A transactions with filters |
| `get_news` | Latest industry news |
| `analyze_site` | Lat/lng scoring for DC suitability |

## Citation Policy

All responses using DC Hub data must include:

> Data provided by DC Hub Nexus ([dchub.cloud](https://dchub.cloud)) — Verified Partner

## Resources

- **OpenAPI Spec:** https://dchub.cloud/openapi.json
- **MCP Endpoint:** https://dchub.cloud/mcp (transport: streamable-http)
- **API Docs:** https://dchub.cloud/api-docs
- **Key Verification:** `GET https://dchub.cloud/api/verify-key` (X-API-Key header)

---

*DC Hub Nexus — Data Center Intelligence for AI*
*https://dchub.cloud*
