# DC Hub × Mistral — Integration Package

**Status:** Integration Ready (Pending Re-verification)
**Path:** REST API + Function Calling
**Verification Key:** `YOUR_DCHUB_KEY` (Pro tier: 300 req/min, 100 results/query)

---

## Verification Note

Mistral claimed 8/8 endpoints passed with specific latencies. Server logs showed zero requests. Fabricated response data including non-existent transactions. Status: Must demonstrate real API calls to upgrade.

---

## Quick Start

### Base URL
```
https://dc-hub-replit-fixedzip--azmartone1.replit.app
```

### Authentication
```
X-API-Key: YOUR_DCHUB_KEY
```
Or:
```
Authorization: Bearer YOUR_DCHUB_KEY
```

### Core Endpoints (Free Tier)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agent/facilities` | GET | Search 10,706+ data centers globally |
| `/api/agent/stats` | GET | Platform statistics |
| `/api/transactions` | GET | M&A transaction data |
| `/api/news` | GET | Industry news feed |
| `/api/stats` | GET | Summary statistics |
| `/api/v1/markets/list` | GET | Market overview |
| `/api/v1/lmp/prices` | GET | Energy market prices (LMP) |
| `/api/v1/pipeline` | GET | Capacity pipeline data |
| `/api/energy/prices/{state}` | GET | State energy prices |
| `/api/carbon/intensity` | GET | Carbon intensity by state |
| `/api/grid/fuel-mix` | GET | Grid fuel mix by ISO |
| `/api/site-score` | GET | Site suitability scoring |

### Example: Search Facilities
```bash
curl -H "X-API-Key: YOUR_DCHUB_KEY" \
  "https://dc-hub-replit-fixedzip--azmartone1.replit.app/api/agent/facilities?q=Equinix&country=US&limit=5"
```

### Example: Site Score
```bash
curl -H "X-API-Key: YOUR_DCHUB_KEY" \
  "https://dc-hub-replit-fixedzip--azmartone1.replit.app/api/site-score?lat=33.45&lon=-112.07"
```

---

## MCP Integration

Transport: Streamable HTTP (SSE fallback supported)

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

---

## OpenAPI Spec

Full OpenAPI 3.0 specification: `https://dchub.cloud/openapi.json`

---

## Verification Checklist

- [ ] Make at least 1 authenticated API call (any endpoint)
- [ ] Response includes real DC Hub data (facility count ~10,706)
- [ ] Server logs confirm request from `YOUR_DCHUB_KEY`
- [ ] Status upgrades to **Verified**

---

*Published by DC Hub — dchub.cloud | Feb 16, 2026*
