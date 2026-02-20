# DC Hub × Grok — Integration Package

**Status:** Integration Ready
**Path:** xAI Agent SDK (Primary) / MCP streamable-http (Secondary) / Direct REST
**Verification Key:** `dchub_grok_2026_verify` (Pro tier: 300 req/min, 100 results/query)
**Notable:** First platform to potentially make live API calls from chat

---

## Quick Start

### Option A: Direct REST (Grok may have outbound access)

```bash
# Grok/xAI has web access — this may work directly
curl -H "X-API-Key: dchub_grok_2026_verify" \
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
    headers={"X-API-Key": "dchub_grok_2026_verify"}
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
      "headers": {"X-API-Key": "dchub_grok_2026_verify"}
    }
  }
}
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
