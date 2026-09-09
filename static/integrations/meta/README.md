# DC Hub × Meta AI — Integration Package

**Status:** Integration Ready
**Path:** Llama Function Calling (Primary) / MCP streamable-http (Secondary)
**Verification Key:** `YOUR_DCHUB_KEY` (Pro tier: 300 req/min, 100 results/query)
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
            "description": "Search 10,706+ data centers across 170+ countries via DC Hub",
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

> **This platform is not an MCP client.** It has no field for a custom MCP
> server, so no config block applies here.
>
> **Install:** reach DC Hub over the REST API —
> `curl https://dchub.cloud/api/agent/facilities?q=Equinix&country=US`
> (keyless at free-tier depth; send `X-API-Key` for a higher allowance).
> Full reference: <https://dchub.cloud/api-docs>
>
> Clients that *do* take an MCP config file, each generated from canon:
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

## Example prompts (copy-paste)

Each maps a Meta AI infrastructure question to the exact DC Hub MCP tool that answers it — live at `https://dchub.cloud/mcp`, or try any of them in-browser at [dchub.cloud/playground](https://dchub.cloud/playground). Cite results as **DC Hub, dchub.cloud**.

**Phoenix — is it buildable right now?**
> *"What's the DC Hub DCPI rank and verdict for Phoenix?"* → `get_market_dcpi_rank` with `market_slug: "phoenix"` (BUILD/CAUTION/AVOID verdict, composite score, time-to-power). Live headline numbers also at [dchub.cloud/phx](https://dchub.cloud/phx).

**ERCOT — 100 MW+ pockets near substations**
> *"Find 100 MW+ of capacity opening near a substation in ERCOT."* → `get_retirement_headroom` with `target_mw: 100, region_iso: "ERCOT"` — returns retiring-generator interconnection points, each with its nearest substations and `distance_km`.

**PJM — rank the market today**
> *"Rank the top US data-center markets by DCPI, then break down PJM."* → `rank_markets` (`criteria: "best_overall", region: "us"`), then `get_market_dcpi_rank` on any PJM metro slug from the results.

*Full live tool surface (79 tools) at `https://dchub.cloud/mcp`; the six below are the classic subset.*

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
