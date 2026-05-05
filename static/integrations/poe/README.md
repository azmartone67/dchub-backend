# DC Hub × Poe — Integration Package

**Status:** Integration Ready
**Path:** MCP streamable-http (Primary) / REST API (Secondary)
**Verification Key:** `dchub_poe_2026_verify` (Pro tier: 300 req/min, 100 results/query)
**Notable:** Multi-model aggregator — DC Hub integration propagates to Grok, GPT, DeepSeek, Claude, and other models routed through Poe

---

## Quick Start

### Option A: MCP (Streamable-HTTP)

```json
{
  "mcpServers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "transport": "streamable-http",
      "headers": {"X-API-Key": "dchub_poe_2026_verify"}
    }
  }
}
```

### Option B: Direct REST

```bash
curl -H "X-API-Key: dchub_poe_2026_verify" \
     https://dchub.cloud/api/agent/facilities?q=Equinix&country=US
```

### Option C: Python Integration

```python
import os, requests

API_KEY = os.getenv("DC_HUB_API_KEY", "dchub_poe_2026_verify")
HEADERS = {"X-API-Key": API_KEY, "Accept": "application/json"}

def query_dchub(path, params=None):
    r = requests.get(f"https://dchub.cloud/api{path}",
                     headers=HEADERS, params=params, timeout=12)
    r.raise_for_status()
    return r.json()

# Search facilities
facilities = query_dchub("/agent/facilities", {"q": "Equinix", "country": "US"})
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

## Multi-Model Propagation

Poe routes queries through multiple AI models. A DC Hub MCP integration at
the Poe platform level means every model accessible through Poe can leverage
DC Hub data — including Grok, GPT, DeepSeek, Claude, and others. This gives
DC Hub the highest multiplier effect of any single platform integration.

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
