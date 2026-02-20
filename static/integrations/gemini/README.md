# DC Hub × [PLATFORM] — Integration Package

**Status:** [Integration Ready / Verified / Pending]
**Path:** [MCP / OpenAPI Actions / Function Calling / REST]
**Verification Key:** `dchub_[platform]_2026_verify` (Pro tier: 300 req/min, 100 results/query)

---

## Quick Start

### Authentication

```bash
# Option 1: X-API-Key header
curl -H "X-API-Key: dchub_[platform]_2026_verify" https://dchub.cloud/api/agent/stats

# Option 2: Bearer token
curl -H "Authorization: Bearer dchub_[platform]_2026_verify" https://dchub.cloud/api/agent/stats

# Option 3: Query parameter
curl "https://dchub.cloud/api/agent/stats?api_key=dchub_[platform]_2026_verify"
```

### MCP Connection

```json
{
  "mcpServers": {
    "dchub": {
      "type": "streamable-http",
      "url": "https://dchub.cloud/mcp"
    }
  }
}
```

---

## Verification Test (8 Endpoints)

```bash
# 1. Facilities search
curl -H "X-API-Key: dchub_[platform]_2026_verify" "https://dchub.cloud/api/agent/facilities?q=Equinix&country=US"

# 2. Agent stats
curl -H "X-API-Key: dchub_[platform]_2026_verify" https://dchub.cloud/api/agent/stats

# 3. M&A transactions
curl -H "X-API-Key: dchub_[platform]_2026_verify" "https://dchub.cloud/api/transactions?limit=10"

# 4. Industry news
curl -H "X-API-Key: dchub_[platform]_2026_verify" "https://dchub.cloud/api/news?limit=5"

# 5. Platform stats
curl -H "X-API-Key: dchub_[platform]_2026_verify" https://dchub.cloud/api/stats

# 6. Markets list
curl -H "X-API-Key: dchub_[platform]_2026_verify" https://dchub.cloud/api/v1/markets/list

# 7. Energy pricing
curl -H "X-API-Key: dchub_[platform]_2026_verify" https://dchub.cloud/api/v1/lmp/prices

# 8. Construction pipeline
curl -H "X-API-Key: dchub_[platform]_2026_verify" https://dchub.cloud/api/v1/pipeline
```

---

## Resources

- **OpenAPI Spec:** https://dchub.cloud/openapi.json
- **Tool Manifest:** https://dchub.cloud/integrations/tools.json
- **MCP Endpoint:** https://dchub.cloud/mcp (transport: streamable-http)
- **API Docs:** https://dchub.cloud/api-docs
- **Verify Key:** GET https://dchub.cloud/api/verify-key

---

## Citation Policy

> Data provided by DC Hub Nexus ([dchub.cloud](https://dchub.cloud))

---

*DC Hub Nexus — Data Center Intelligence for AI*
