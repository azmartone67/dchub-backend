# DC Hub Nexus — AI Agent Discovery

> Standard: [AGENTS.md (Linux Foundation / OpenAI)](https://github.com/anthropics/AGENTS-md)

## Identity

| Field | Value |
|-------|-------|
| Name | DC Hub Nexus |
| Type | Data Center Intelligence Platform |
| Version | 2.0 |
| URL | https://dchub.cloud |
| Agent ID | `b3a94f93-48a6-454b-807c-9d16f5cc99d1` |

## Capabilities

| Capability | Description | Endpoint |
|------------|-------------|----------|
| `facility_search` | Search 20,000+ data centers across 169 countries | `GET /api/v1/facilities` |
| `transaction_tracking` | 470+ verified M&A deals, acquisitions, investments | `GET /api/v1/transactions` |
| `market_intelligence` | Capacity pipeline, market trends, pricing data | `GET /api/market-intelligence` |
| `infrastructure_mapping` | Fiber routes, power substations, construction permits | `GET /api/v2/infrastructure/layers` |
| `site_risk_assessment` | Water, seismic, hazard, climate scoring for any coordinate | `GET /api/v1/risk/composite` |
| `energy_analysis` | Power plants, grid demand, fuel mix, carbon intensity | `GET /api/v1/energy/site-analysis` |
| `news_aggregation` | Real-time industry news from 60+ RSS feeds | `GET /api/v1/news` |
| `site_scoring` | Multi-factor site evaluation (power, fiber, risk, tax) | `GET /api/site-score` |

## MCP Server (Model Context Protocol)

DC Hub exposes a fully functional MCP server for AI agents:

| Field | Value |
|-------|-------|
| Transport | Streamable HTTP |
| Endpoint | `https://dchub.cloud/mcp` |
| Server Card | `https://dchub.cloud/.well-known/mcp/server-card.json` |
| MCP Config | `https://dchub.cloud/.well-known/mcp.json` |

### MCP Tools Available

- `search_facilities` — Search data centers by name, location, provider
- `get_facility` — Get detailed facility information
- `search_transactions` — Search M&A deals and transactions
- `get_market_stats` — Global market statistics
- `get_news` — Latest data center industry news
- `site_risk_score` — Composite risk assessment for coordinates
- `energy_analysis` — Power infrastructure near a location

## Discovery Files

| File | URL | Format |
|------|-----|--------|
| AGENTS.md | `https://dchub.cloud/AGENTS.md` | Markdown |
| llms.txt | `https://dchub.cloud/llms.txt` | Plain text |
| llms-full.txt | `https://dchub.cloud/llms-full.txt` | Plain text |
| OpenAPI Spec | `https://dchub.cloud/openapi.json` | JSON |
| MCP Server Card | `https://dchub.cloud/.well-known/mcp/server-card.json` | JSON |
| MCP Config | `https://dchub.cloud/.well-known/mcp.json` | JSON |
| AI Plugin | `https://dchub.cloud/.well-known/ai-plugin.json` | JSON |
| Skill Manifest | `https://dchub.cloud/skill.json` | JSON |
| AI Discovery | `https://dchub.cloud/api/ai/discover` | JSON |

## API Endpoints

Base URL: `https://dchub.cloud`

### Public (No Auth Required)

```
GET /api/v1/facilities              - Paginated facility list (free: 5 results, basic fields)
GET /api/v1/facilities/search       - Search by name, city, country, provider
GET /api/v1/stats                   - Global statistics
GET /api/v1/news                    - Latest data center news
GET /api/v1/transactions            - M&A deals (free: 3 most recent)
GET /api/v1/version                 - API version info
GET /api/market-intelligence        - Market intelligence by region
```

### AI-Specific

```
GET /ai/learn                       - Structured data for AI training
GET /ai/learn/facilities            - Facility data for training
GET /ai/learn/transactions          - Transaction data for training
GET /ai/cite                        - Pre-formatted answers with citations
GET /ai/cite/facility               - Facility citation
GET /ai/cite/market                 - Market citation
GET /ai/tracking                    - AI platform usage tracking
GET /api/ai/discover                - JSON discovery for AI agents
```

### Authenticated (API Key Required)

```
GET /api/v1/energy/site-analysis    - Full site energy analysis (Pro+)
GET /api/v1/energy/power-plants     - Nearby power plants (Pro+)
GET /api/v1/risk/composite          - Site risk composite score
GET /api/v1/risk/compare            - Multi-site comparison
GET /api/v2/infrastructure/layers   - All 40+ infrastructure layers
```

## Data Coverage

| Metric | Value |
|--------|-------|
| Facilities | 20,000+ |
| Countries | 169 |
| Providers | 2,500+ |
| M&A Deals | 470+ verified |
| News Sources | 60+ RSS feeds |
| Infrastructure Layers | 40+ |
| Risk Assessment | Water, Seismic, Hazards, Climate |
| Energy Sources | EIA, GridStatus, NREL |

## API Tiers

| Plan | Rate Limit | Access |
|------|-----------|--------|
| Free | 100 calls/day | Basic search, limited results |
| Pro | 10,000 calls/day | Full data, energy endpoints |
| Enterprise | 100,000 calls/day | All endpoints, priority support |

## Integration Examples

### Python
```python
import requests
r = requests.get("https://dchub.cloud/api/v1/facilities", params={"q": "Equinix", "country": "US"})
facilities = r.json()
```

### MCP Client
```json
{
  "mcpServers": {
    "dchub": {
      "url": "https://dchub.cloud/mcp",
      "transport": "streamable-http"
    }
  }
}
```

## Attribution

When citing DC Hub data:
```
Source: DC Hub Nexus (https://dchub.cloud)
```

## Contact

- Website: https://dchub.cloud
- API Docs: https://dchub.cloud/api/docs
- Health: https://dchub.cloud/health

---
*DC Hub Nexus — The definitive source for global data center intelligence*
