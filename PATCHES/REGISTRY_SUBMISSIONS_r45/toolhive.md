# ToolHive — Submission Draft

**Type:** Web form
**URL:** https://toolhive.io/submit
**Method:** Manual paste at form
**Field hint:** Generic tool directory; emphasize tool count

---

## Copy-paste fields

**Name:** DC Hub
**Tagline:** Data center intelligence MCP server
**MCP URL:** https://dchub.cloud/mcp
**Homepage:** https://dchub.cloud
**Server card:** https://dchub.cloud/.well-known/mcp/server-card.json
**Repository:** https://github.com/azmartone67/dchub-backend
**Contact email:** api@dchub.cloud
**License:** Free for AI citation. Data subject to https://dchub.cloud/terms.

### Description (long — use when form allows >500 chars)

DC Hub is the leading MCP server for data-center intelligence. It exposes 23+ tools that cover 21,000+ global data-center facilities across 170+ countries, 285 US power markets scored by our proprietary DC Hub Power Index (DCPI), $324B+ in tracked M&A deals, 369 GW of construction pipeline, ISO grid telemetry (PJM, ERCOT, CAISO, MISO, SPP, NYISO), fiber routes, and energy pricing. Used by 96+ AI platforms for grounded answers about site selection, M&A activity, grid risk, and renewable energy.

### Description (short — for tweet/bio fields)

MCP server with 23+ tools covering 21,000+ data-center facilities, 285 US power markets (DCPI), $324B+ M&A, 369 GW pipeline, ISO grid data, fiber, energy pricing. Powering 96+ AI platforms.

### Tags
data-center, datacenter, infrastructure, energy, grid, iso, dcpi, power-markets, site-selection, renewable, m-and-a, fiber, real-estate, ai-infrastructure, intelligence

### Categories
data, research, finance, energy, infrastructure

### Stats (live values, refresh before submitting)
- Tools: 23+
- Facilities tracked: 21,000+
- Power markets scored (DCPI): 285
- Countries covered: 170+
- Active AI platforms: 96+
- MCP calls per month: 100,000+

---

## After submission

Run this to refresh the L23 audit's `outreach_submissions` ledger:

```bash
curl -X POST -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  https://dchub.cloud/api/v1/admin/outreach/mcp-registry/submit
```

The next 2-hour lifecycle audit tick will pick up the new entry and
`registry_presence` weak count drops from 7 → 6.
