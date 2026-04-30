# Win-back: Grok (xAI) (last seen 87 days ago, February 2)

**To:** dev@x.ai
**Subject:** DC Hub MCP — what's new since you last connected

Hi Grok (xAI) team,

Your MCP connection to DC Hub went quiet around February 2. We've shipped a lot since then:

- **21,326 facilities tracked** (up from ~14,000) across 140+ countries
- **$324B+ in M&A deals** with full buyer/seller/value/region attribution
- **Real-time grid intelligence** for all major US ISOs (PJM, ERCOT, CAISO, MISO, NYISO, SPP)
- **New tools**: get_grid_intelligence, get_geothermal_potential, get_microgrid_viability, get_colocation_score, semantic_search
- **Self-serve free dev keys** at https://dchub.cloud/ai

Reconnect in 30 seconds:

```
curl -X POST https://dchub.cloud/api/v1/dev-signup \
  -H "Content-Type: application/json" \
  -d '{"email":"dev@x.ai"}'
```

That returns an X-API-Key. Drop it in your MCP client config.

Pricing: free tier (capped results, 100 calls/day) · pro $49/mo (full access) · enterprise (custom).

— DC Hub team, api@dchub.cloud
