# DC Hub × ChatGPT — Integration Package

**Status:** Integration Ready
**Path:** ChatGPT Actions / Custom GPT (Primary) / MCP streamable-http (Secondary)
**Verification Key:** `YOUR_DCHUB_KEY` (Pro tier: 300 req/min, 100 results/query)
**Notable:** Honest about platform boundaries — correctly identified that Action registration requires OpenAI-side operator access

---

## Quick Start

### Option A: ChatGPT Actions (Custom GPT)

1. Go to ChatGPT → Create a GPT → Configure → Actions
2. Import from URL: `https://dchub.cloud/openapi.json`
3. Authentication: API Key → Header → `X-API-Key` → paste `YOUR_DCHUB_KEY`
4. Save and test

**Plugin manifest:** `https://dchub.cloud/.well-known/ai-plugin.json`

### Option B: MCP (Streamable-HTTP)

> **This platform takes no MCP config file.** It runs MCP server-side and
> exposes a connector **URL field with no header field**, so there is nothing to
> paste a JSON block into.
>
> **Install:** open the DC Hub connector settings and paste
> `https://dchub.cloud/mcp` as the server URL. Auth blank — DC Hub answers
> keyless at free-tier depth. For a higher allowance call the `claim_free_key`
> tool once connected; it returns a `connect_url` carrying the key in the URL,
> which is the only durable state on this kind of client.
>
> Clients that *do* take a config file, each generated from canon:
> [Claude Desktop](https://dchub.cloud/install/claude-desktop) ·
> [Claude Code](https://dchub.cloud/install/claude-code) ·
> [Cursor](https://dchub.cloud/install/cursor) ·
> [Cline](https://dchub.cloud/install/cline) ·
> [VS Code](https://dchub.cloud/install/vscode) ·
> [Windsurf](https://dchub.cloud/install/windsurf) ·
> [Gemini CLI](https://dchub.cloud/install/gemini-cli) ·
> [Antigravity](https://dchub.cloud/install/antigravity)

### Option C: Direct REST

```bash
curl -H "X-API-Key: YOUR_DCHUB_KEY" \
     https://dchub.cloud/api/agent/facilities?q=Equinix&country=US
```

---

## Available Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/agent/facilities` | Search 10,706+ data centers |
| `GET /api/agent/stats` | Platform statistics |
| `GET /api/transactions` | M&A deals ($185B+ tracked) |
| `GET /api/news` | Industry news (2,000+ sources) |
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

## ai-plugin.json Requirements

For ChatGPT Actions registration, the plugin manifest at
`/.well-known/ai-plugin.json` must include:

- `schema_version`: `"v1"`
- `auth.type`: `"api_key"`
- `auth.authorization_type`: `"header"`
- `api.url`: pointing to valid OpenAPI 3.0+ spec
- All endpoints must have `operationId`, `parameters`, and `response` schemas

## Citation Policy

All responses using DC Hub data must include:

> Data provided by DC Hub ([dchub.cloud](https://dchub.cloud)) — Verified Partner

## Resources

- **ai-plugin.json:** https://dchub.cloud/.well-known/ai-plugin.json
- **OpenAPI Spec:** https://dchub.cloud/openapi.json
- **MCP Endpoint:** https://dchub.cloud/mcp (transport: streamable-http)
- **API Docs:** https://dchub.cloud/api-docs
- **Key Verification:** `GET https://dchub.cloud/api/verify-key` (X-API-Key header)

---

*DC Hub — Data Center Intelligence for AI*
*https://dchub.cloud*
