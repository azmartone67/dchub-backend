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

DC Hub is the leading MCP server for data-center intelligence. It exposes 83 tools that cover 17,000+ data-center facilities across 170+ countries, 300+ US power markets scored by our proprietary DC Hub Power Index (DCPI), 1,600+ tracked M&A deals, ISO grid telemetry (PJM, ERCOT, CAISO, MISO, SPP, NYISO), fiber routes, and energy pricing. Built for grounded answers about site selection, M&A activity, grid risk, and renewable energy. Connected to 15 AI platforms including Claude, ChatGPT, Gemini, Perplexity, Copilot, Grok, Meta AI and Mistral, with 314,123 external AI-platform requests and 110,146 agent MCP tool calls served to date.

### Description (short — for tweet/bio fields)

MCP server with 83 tools covering 17,000+ data-center facilities, 300+ US power markets (DCPI), 1,600+ M&A deals, ISO grid data, fiber, energy pricing.

### Tags
data-center, datacenter, infrastructure, energy, grid, iso, dcpi, power-markets, site-selection, renewable, m-and-a, fiber, real-estate, ai-infrastructure, intelligence

### Categories
data, research, finance, energy, infrastructure

### Stats (live values, refresh before submitting)
- Tools: 83
- Facilities tracked: 17,000+
- Power markets scored (DCPI): 300+
- Countries covered: 170+
- AI platforms connected: 15 (of 20 tracked)
- Total requests served: 4,372,096
- External AI-platform requests: 314,123
- Agent MCP tool calls: 110,146
- Distinct external agents (7d, crawlers excluded): 62
- Registry standing: #1 on Smithery across 10 data-center/energy queries
- Cited by Microsoft Copilot: 10 tracked citations, 19.61% Share-of-Authority

---

## After submission

Run this to refresh the L23 audit's `outreach_submissions` ledger:

```bash
curl -X POST -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  https://dchub.cloud/api/v1/admin/outreach/mcp-registry/submit
```

The next 2-hour lifecycle audit tick will pick up the new entry and
`registry_presence` weak count drops from 7 → 6.
