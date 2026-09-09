# DC Hub × Pi (Inflection AI) — Integration Package

**Status:** Integration Ready
**Path:** REST API
**Verification Key:** `YOUR_DCHUB_KEY` (Pro tier: 300 req/min, 100 results/query)

---

## Verification Note

Pi expressed interest in integrating. Credentials provided.

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
