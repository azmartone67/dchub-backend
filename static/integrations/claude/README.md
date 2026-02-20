# DC Hub × Claude — Integration Package

**Status:** Integration Ready
**Path:** MCP Native (Primary) / Direct REST (Secondary)
**Verification Key:** `dchub_claude_2026_verify` (Pro tier: 300 req/min, 100 results/query)
**Notable:** Most self-aware response in AI Wars — identified the prompt structure, flagged credential security, offered to build real assets instead

---

## Quick Start

### Option A: MCP Native (Claude Desktop / Claude Code)

Add to your Claude Desktop `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "transport": "streamable-http",
      "headers": {
        "X-API-Key": "dchub_claude_2026_verify"
      }
    }
  }
}
```

Once configured, Claude can directly call DC Hub tools:
- "Search for Equinix data centers in Northern Virginia"
- "What M&A deals happened this quarter?"
- "Score this location for a new data center: 39.04°N, 77.49°W"

### Option B: Anthropic API with Tool Use

```python
import anthropic

client = anthropic.Anthropic()

# Define DC Hub as a tool source
tools = [
    {
        "name": "search_facilities",
        "description": "Search DC Hub's 10,706+ data center facilities",
        "input_schema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Search query"},
                "country": {"type": "string", "description": "ISO country code"},
                "market": {"type": "string", "description": "Market name"}
            }
        }
    }
]
# See function-calling.json for all 6 tool declarations
```

### Option C: Direct REST

```bash
curl -H "X-API-Key: dchub_claude_2026_verify" \
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

## Claude-Specific Integration Notes

Claude has native MCP support through Claude Desktop and Claude Code. This means:

1. **No plugin system needed** — MCP is the native integration path
2. **Tool use is built-in** — Claude can call DC Hub tools directly when configured
3. **Streaming support** — Streamable-HTTP transport works natively
4. **No outbound calls from chat** — Claude correctly identified this limitation; MCP Desktop or API with tool use is the real integration path

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
