# DC Hub × NVIDIA — Integration Package

**Status:** Integration Ready
**Path:** MCP (Primary) / NIM Inference Layer (Optional)
**Verification Key:** `dchub_nvidia_2026_verify` (Pro tier: 300 req/min, 100 results/query)

---

## Architecture

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  NVIDIA Agent /  │     │   DC Hub MCP     │     │  NIM Inference   │
│  Copilot / NIM   │────▶│   (Data Layer)   │────▶│  (Optional AI)   │
│                  │     │                  │     │                  │
│  Queries DC Hub  │     │  10,706 DCs      │     │  Summarize news  │
│  via MCP tools   │     │  $185B+ M&A      │     │  Score sites     │
│                  │     │  7 ISO energy    │     │  Predict trends  │
└──────────────────┘     └──────────────────┘     └──────────────────┘
```

**MCP** handles all data retrieval. **NIM** adds optional GPU-accelerated inference
for summarization, risk scoring, and market forecasting on top of raw DC Hub data.

---

## Quick Start

### Option A: MCP (Recommended)

```bash
# Register DC Hub as an MCP tool source
export DC_HUB_API_KEY="dchub_nvidia_2026_verify"

# Python SDK generation from OpenAPI spec
openapi-generator-cli generate \
  -i https://dchub.cloud/openapi.json \
  -g python \
  -o ./dc_hub_sdk
```

### Option B: MCP + NIM Hybrid

See `docker-compose.yml` for a containerized setup that combines
MCP data retrieval with NIM inference.

### Option C: Direct REST

```bash
curl -H "X-API-Key: dchub_nvidia_2026_verify" \
     https://dchub.cloud/api/agent/facilities?q=Equinix&country=US
```

---

## Available Endpoints

| Endpoint | Description | Example |
|----------|-------------|---------|
| `GET /api/agent/facilities` | Search 10,706+ data centers | `?q=Equinix&country=US` |
| `GET /api/agent/stats` | Facility counts, countries, providers | Returns actual platform metrics |
| `GET /api/transactions` | M&A deals ($185B+ tracked) | `?limit=10` |
| `GET /api/news` | Industry news (40+ sources, 5-min refresh) | `?limit=5` |
| `GET /api/stats` | High-level platform statistics | Facilities, providers, countries |
| `GET /api/v1/markets/list` | 35+ tracked metro markets | Vacancy, pricing, inventory |
| `GET /api/v1/lmp/prices` | Energy pricing (PJM, ERCOT, CAISO, MISO, NYISO, SPP, ISO-NE) | Real-time LMP data |
| `GET /api/v1/pipeline` | Construction pipeline (~7.8 GW) | Projects, markets, MW, developers |

**Important:** LMP endpoint uses ISO region codes (PJM, ERCOT, CAISO), not cloud-provider zone names.

## MCP Tools

| Tool | Description | NIM Enhancement |
|------|-------------|-----------------|
| `search_facilities` | Search by market, operator, tier, capacity | Rank by custom scoring model |
| `get_facility` | Detailed facility profile | Generate executive summary |
| `get_market_data` | Vacancy rates, pricing, inventory | Trend prediction |
| `search_deals` | M&A transactions with filters | Deal pattern analysis |
| `get_news` | Latest industry news | AI-powered summarization |
| `analyze_site` | Lat/lng scoring for DC suitability | Risk modeling with GPU inference |

---

## NIM Inference Examples

### Summarize Market News
```python
# 1. Retrieve raw data via MCP
news = mcp_client.get_news(limit=20)

# 2. Forward to NIM for summarization
nim_response = nim_client.generate(
    model="nvidia/llama-3.1-nemotron-70b",
    prompt=f"Summarize these data center industry headlines for an executive audience:\n{json.dumps(news)}"
)
```

### Site Risk Scoring
```python
# 1. Get base score from DC Hub
site_score = mcp_client.analyze_site(lat=39.04, lon=-77.49)

# 2. Enhance with NIM predictive model
risk_assessment = nim_client.generate(
    model="nvidia/llama-3.1-nemotron-70b",
    prompt=f"Given this DC Hub site score: {json.dumps(site_score)}, assess 5-year infrastructure risk for a 50MW hyperscale deployment."
)
```

---

## Verification

This integration is **"Integration Ready"** status.
Upgrades to **"DC Hub Verified"** once server logs capture
authenticated requests from `dchub_nvidia_2026_verify`.

### Health Check
```bash
curl -s -H "X-API-Key: dchub_nvidia_2026_verify" \
     https://dchub.cloud/api/verify-key
# Expected: {"verified": true, "platform": "NVIDIA", "tier": "pro"}
```

---

## Citation Policy

All responses using DC Hub data must include:

> Data provided by DC Hub Nexus ([dchub.cloud](https://dchub.cloud)) — NVIDIA Verified Partner

---

## Resources

- **OpenAPI Spec:** https://dchub.cloud/openapi.json
- **Tool Manifest:** https://dchub.cloud/integrations/tools.json
- **MCP Endpoint:** https://dchub.cloud/mcp (transport: streamable-http)
- **API Docs:** https://dchub.cloud/api-docs

---

*DC Hub Nexus × NVIDIA — Data Center Intelligence Powered by AI*
