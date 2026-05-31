# MCP Registry Submissions — DC Hub

Submission packet for the 6 registries from FUNNEL_NEXT_STEPS.md item 7.
Each section is a copy/paste-ready submission for one registry. Reuse the
canonical metadata from the top section across all of them.

═══════════════════════════════════════════════════════════════════
## Canonical metadata (paste into every submission)
═══════════════════════════════════════════════════════════════════

```
Name:            DC Hub Intelligence
URL:             https://dchub.cloud/mcp
Transport:       streamable-http
Server version:  2.1.10 (dchub-mcp-server)
Description:     Real-time data center intelligence — 21,000+ facilities
                 across 140+ countries, grid data across 10 North-American
                 grid operators (7 US ISOs plus TVA, BPA and Ontario's IESO)
                 and 43 US utility balancing authorities, with 3
                 international grids (Hydro-Quebec, AESO, Nord Pool) modeled,
                 fiber routes, $324B+
                 M&A transactions, interconnection queue snapshots,
                 daily AI capacity index, hyperscaler $1B+ deal tracker,
                 BUILD/CAUTION/AVOID DCPI verdicts per market. Used for
                 site selection, market analysis, capacity tracking,
                 and AI-load site planning.
Tool count:      29 (28 backend + 1 worker-served semantic_search)
Tools highlight: search_facilities, get_market_intel, get_market_dcpi_rank,
                 compare_isos, get_intelligence_index, list_transactions,
                 get_news, get_pipeline, get_interconnection_queue,
                 get_grid_data, analyze_site, compare_sites,
                 get_infrastructure, get_fiber_intel, get_energy_prices,
                 get_renewable_energy, get_tax_incentives, get_water_risk,
                 get_grid_intelligence, get_agent_registry,
                 get_backup_status, get_dchub_recommendation,
                 rank_markets, find_alternatives, score_facility,
                 ai_capacity_index, hyperscaler_deals, get_facility,
                 semantic_search (worker-served, Vectorize-backed)
Authentication:  Optional X-API-Key header
                 - Anonymous: 10 calls/day
                 - Free dev key (60-sec email signup): 1,000 calls/day
                 - Starter ($9/mo): 10,000 calls/day
                 - Developer ($49/mo): unlimited paid tools
                 - Pro ($199/mo): unlimited + Pro tools
Contact:         api@dchub.cloud
Documentation:   https://dchub.cloud/integrations/mcp
Signup:          https://dchub.cloud/signup
Operator:        DC Hub (azmartone@gmail.com)
GitHub:          https://github.com/azmartone67/dchub-mcp-server
Cited by:        ChatGPT, Claude, Gemini, Perplexity, Groq
                 (https://dchub.cloud/cited-by)
```

═══════════════════════════════════════════════════════════════════
## 1. Smithery — https://smithery.ai/server/new
═══════════════════════════════════════════════════════════════════

Submission method: web form. Fill in the canonical metadata above.

Smithery's registry tags — suggest:
- `data-center` `infrastructure` `grid` `energy` `market-intelligence`
- `m&a` `geospatial` `iso-rto` `interconnection-queue`

Categories: `Data & APIs` / `Infrastructure`

═══════════════════════════════════════════════════════════════════
## 2. Glama — https://glama.ai/mcp/servers/new
═══════════════════════════════════════════════════════════════════

Glama already discovers via `.well-known/glama.json` which is wired
in the worker (returns the canonical maintainers list). Just submit
the URL — they'll auto-fetch.

Verify the well-known is current:
```
curl -s https://dchub.cloud/.well-known/glama.json | python3 -m json.tool
```

═══════════════════════════════════════════════════════════════════
## 3. modelcontextprotocol/servers (Anthropic's registry)
═══════════════════════════════════════════════════════════════════

GitHub: https://github.com/modelcontextprotocol/servers

Submission method: PR to the README in their `community` or
`third-party` section. Look in the repo for the table they maintain.

Proposed README entry (markdown table row):

```markdown
| [DC Hub Intelligence](https://github.com/azmartone67/dchub-mcp-server) | Real-time data center intelligence: 21,000+ facilities, grid data across 10 ISOs + 43 utility BAs, M&A deals, interconnection queues, fiber routes. Used for site selection and market analysis. Free tier + $9/$49/$199 paid tiers. |
```

Or if they use a YAML/JSON manifest, use the canonical metadata
fields above. PR template suggests prepending a description sentence
and linking to dchub.cloud/mcp directly.

═══════════════════════════════════════════════════════════════════
## 4. Cline marketplace — https://github.com/cline/mcp-marketplace
═══════════════════════════════════════════════════════════════════

PR template (likely a JSON file in their registry directory):

```json
{
  "name": "DC Hub Intelligence",
  "id": "dchub",
  "url": "https://dchub.cloud/mcp",
  "transport": "streamable-http",
  "description": "Real-time data center intelligence — 21,000+ facilities, grid data across 10 ISOs + 43 utility BAs, M&A transactions, fiber routes, interconnection queue snapshots, daily AI capacity index.",
  "tools_count": 29,
  "auth": {
    "type": "api_key_header",
    "header": "X-API-Key",
    "optional_for_free_tier": true
  },
  "pricing": {
    "free":      "10 calls/day, no signup",
    "free_key":  "1,000 calls/day, email-only signup",
    "starter":   "$9/mo, 10,000 calls/day",
    "developer": "$49/mo, unlimited paid tools",
    "pro":       "$199/mo, unlimited + Pro tools"
  },
  "categories": ["data", "infrastructure", "energy"],
  "docs": "https://dchub.cloud/integrations/mcp",
  "operator": {
    "name": "DC Hub",
    "url": "https://dchub.cloud",
    "contact": "api@dchub.cloud"
  }
}
```

═══════════════════════════════════════════════════════════════════
## 5. Continue.dev hub — https://hub.continue.dev
═══════════════════════════════════════════════════════════════════

Submission method: hub.continue.dev → New Block → MCP Server.
Fill in canonical metadata. Continue.dev's hub uses an authoring UI
rather than a PR flow.

Snippet for the "config snippet for users" section:

```json
{
  "mcpServers": {
    "dchub": {
      "transport": "streamable-http",
      "url": "https://dchub.cloud/mcp",
      "headers": { "X-API-Key": "${DCHUB_API_KEY}" }
    }
  }
}
```

═══════════════════════════════════════════════════════════════════
## 6. agent.ai — listing form
═══════════════════════════════════════════════════════════════════

Submission method: agent.ai dashboard → New Agent / Tool.
agent.ai is more about full agents than raw MCP, so the listing
should pitch ONE specific use case rather than the full toolkit.

Suggested headline: **"Find data centers by capacity, grid, and queue."**

Use-case copy:
> "DC Hub's MCP server gives any agent live access to 21,000+ data
> center facilities globally — search by MW, ISO grid region, fiber
> coverage, and (new) interconnection queue position. The
> get_interconnection_queue tool surfaces per-ISO BUILD/CAUTION/AVOID
> verdicts so your agent can recommend short-Time-to-Power markets
> automatically. 10 free calls/day; $9/mo Starter unlocks 10,000."

═══════════════════════════════════════════════════════════════════
## After-submission tracking
═══════════════════════════════════════════════════════════════════

For each registry, log the citation in `ai_citations`:

```sql
INSERT INTO ai_citations (engine, platform, query, cited_url, citation_type,
                          dchub_cited, dchub_position, source, observed_at)
VALUES
  ('smithery', 'smithery',   'mcp registry listing', 'https://smithery.ai/server/dchub', 'registry_listing', true, 1, 'manual_submission', NOW()),
  ('glama',    'glama',      'mcp registry listing', 'https://glama.ai/mcp/servers/dchub', 'registry_listing', true, 1, 'manual_submission', NOW()),
  ('modelcontextprotocol','community','mcp servers list', 'https://github.com/modelcontextprotocol/servers', 'registry_pr', true, 1, 'manual_submission', NOW()),
  ('cline',    'cline',      'mcp marketplace', 'https://github.com/cline/mcp-marketplace', 'registry_pr', true, 1, 'manual_submission', NOW()),
  ('continue', 'continue.dev', 'mcp hub', 'https://hub.continue.dev/dchub', 'registry_listing', true, 1, 'manual_submission', NOW()),
  ('agent.ai', 'agent.ai',  'agent listing', 'https://agent.ai/tools/dchub', 'tool_listing', true, 1, 'manual_submission', NOW());
```

═══════════════════════════════════════════════════════════════════
## What I'm tracking on the data side
═══════════════════════════════════════════════════════════════════

Run `scripts/tomorrow_morning_verify.sh` 24h after r46/r47 deploys
to see if any of these registry listings start showing up as new
traffic sources in `v_paywall_attribution`. Source bucketing already
covers Smithery / Glama / Continue / Cline / mcp-inspector. If
unknown sources start appearing (which they will once registries
discover DC Hub), the parser CASE in v_paywall_attribution gets
updated.
