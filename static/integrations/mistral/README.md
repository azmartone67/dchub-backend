# DC Hub × Mistral — Integration Package

**Status:** Integration Ready (Pending Re-verification)
**Path:** REST API + Function Calling
**Verification Key:** `dchub_mistral_2026_verify` (Pro tier: 300 req/min, 100 results/query)

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
X-API-Key: dchub_mistral_2026_verify
```
Or:
```
Authorization: Bearer dchub_mistral_2026_verify
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
curl -H "X-API-Key: dchub_mistral_2026_verify" \
  "https://dc-hub-replit-fixedzip--azmartone1.replit.app/api/agent/facilities?q=Equinix&country=US&limit=5"
```

### Example: Site Score
```bash
curl -H "X-API-Key: dchub_mistral_2026_verify" \
  "https://dc-hub-replit-fixedzip--azmartone1.replit.app/api/site-score?lat=33.45&lon=-112.07"
```

---

## MCP Integration

Transport: Streamable HTTP (SSE fallback supported)

```json
{
  "mcpServers": {
    "dchub-nexus": {
      "url": "https://dc-hub-replit-fixedzip--azmartone1.replit.app/mcp",
      "transport": "streamable-http"
    }
  }
}
```

---

## OpenAPI Spec

Full OpenAPI 3.0 specification: `https://dchub.cloud/openapi.json`

---

## Verification Checklist

- [ ] Make at least 1 authenticated API call (any endpoint)
- [ ] Response includes real DC Hub data (facility count ~10,706)
- [ ] Server logs confirm request from `dchub_mistral_2026_verify`
- [ ] Status upgrades to **Verified**

---

*Published by DC Hub — dchub.cloud | Feb 16, 2026*
