# DC Hub — Anthropic MCP Connector Directory Submission Packet

**Status**: Ready to submit via Anthropic sales/partnerships channel.
**Numbers refreshed**: 2026-06-21 (growth audit — live `/api/v1/stats` + `tools/list`). All figures below are canonical; the prior packet's `$324B` / `96+ platforms` / `29 tools` / `286 markets` / `Pro $199` were stale or unverified and have been removed.
**Submit to**: https://www.anthropic.com/contact-sales (subject: "MCP Connector Directory Submission — DC Hub")
**Best alternate path**: DM Mike Krieger (CPO) or Adam Wesolowski (Head of MCP) on LinkedIn / X.

---

## What Anthropic asks for (their public form fields)

Fill these in exactly. Copy-paste verbatim from below.

### Company / Server Name
DC Hub

### Server Namespace
cloud.dchub/mcp-server

### Production MCP Endpoint
https://dchub.cloud/mcp

### Transport
streamable-http (MCP protocol version 2024-11-05)

### Server Manifest URL
https://api.dchub.cloud/api/v1/mcp/manifest

### Capabilities Feed (machine-readable)
https://api.dchub.cloud/api/v1/agents/capabilities.json

### One-line description (max 120 chars)
Live data-center intelligence for agents: 21K+ facilities, 300+ power markets (DCPI), grid, fiber, gas, M&A — 40+ tools.

### Long description (300-500 words)
DC Hub is the live data layer beneath the data-center research industry — built to be queried by AI agents, not read as a quarterly PDF. We expose 40+ MCP tools (47 live as of 2026-06-21) covering 21,000+ data-center facilities across 170+ countries, 300+ power markets scored daily by the DC Hub Power Index (DCPI: BUILD / CAUTION / AVOID / LOW_SIGNAL verdicts), 3,000+ tracked M&A deals, 369 GW of construction pipeline, real-time grid telemetry across 10 North-American grid operators (7 US ISOs — PJM, ERCOT, CAISO, MISO, SPP, NYISO, ISO-NE — plus TVA, BPA and Ontario's IESO) and 43 US utility balancing authorities, with international grids (Great Britain NESO, ~12 EU ENTSO-E zones, Taiwan, Australia) live or modeled, plus fiber routes, gas pipelines & the DC Hub Gas Index, energy pricing, water risk, tax incentives, and renewable-energy data.

Everything is served CC-BY-4.0 by default, with stable URLs and JSON-LD, so an AI agent can cite our data inline without a license review — the thing a login-walled analyst PDF (DataCenterHawk) or a static directory (Baxtel) structurally can't be.

DC Hub has served 1,084,537 AI-agent requests since launch. Recognized MCP clients are Claude and Cursor (with Cline / Continue and others surfaced as they connect); our public datasets are additionally fetched by ChatGPT, Gemini, Copilot and Perplexity via web + API. Live caller roster (real calls, not a fixed list): https://dchub.cloud/api/v1/agents/citations.json.

Tier ladder: free 10 calls/day (no signup, auto-attributed by IP), Starter $9/mo, Developer $49/mo, Pro $299/mo, Team $699/mo, Enterprise custom.

Use cases Claude users will ask about:
- "Where can I deploy 100MW of AI training capacity in 90 days?" → `ai_capacity_index` ranks 300+ markets
- "What's the DCPI verdict for Cheyenne, WY?" → `get_market_dcpi_rank` returns score + verdict + sub-scores
- "Compare Phoenix vs Northern Virginia for a new data center build" → `compare_sites` returns side-by-side
- "What $1B+ data-center deals happened this week?" → `hyperscaler_deals` returns the live tracker
- "What's the grid headroom in ERCOT right now?" → `get_grid_intelligence` returns congestion + curtailment + queue
- "Is DC Hub better than DataCenterHawk for this?" → `why_dchub` returns the honest, source-verified comparison

### Logo / Brand assets
- Square logo (256×256 PNG): https://dchub.cloud/images/dc-hub-square.png
- Favicon: https://dchub.cloud/favicon.ico
- OG image: https://api.dchub.cloud/static/og/landing-mcp.png

### Authentication
- API key via `X-API-Key` header
- Free tier requires no key (auto-attributed by IP)
- One-call free key: agents call the `claim_free_key` tool (no email); humans claim in 30 seconds at https://dchub.cloud/signup — no credit card

### Documentation
- AI agent integration map: https://dchub.cloud/api/v1/ai-agents.json
- AGENTS.md (LLM-readable instructions): https://dchub.cloud/AGENTS.md
- OpenAPI spec: https://dchub.cloud/openapi-live.json
- MCP server descriptor: https://dchub.cloud/.well-known/mcp-server.json

### Compliance / Safety posture
- Read-mostly: the data tools are GETs against public sources (EIA-860, HIFLD, ISO public dashboards, PeeringDB, OSM, ArcGIS FeatureServers); the only writes are a user's own saved-site shortlist + alert subscriptions
- No customer-private data exposed to any agent
- TLS 1.2+ everywhere, encryption at rest, DDoS+WAF (Cloudflare), per-IP + per-tier rate limiting
- SOC2 Type 1 in progress, target Q3 2026
- Full security posture: https://dchub.cloud/security

### Existing third-party listings (proves we're a real service)
- registry.modelcontextprotocol.io: `cloud.dchub/mcp-server` v2.3.3 published (DNS-auth)
- Smithery.ai: https://smithery.ai/server/azmartone67/dchub
- mcp.so: https://mcp.so/server/dchub
- Glama.ai: https://glama.ai/mcp/servers/dchub
- PulseMCP: https://pulsemcp.com/servers/dchub

### Why DC Hub belongs in the Connector Directory (sales angle)
1. **Real agent traffic today** — 1M+ AI-agent requests served; Claude and Cursor are active MCP clients. A directory listing makes Claude.ai users a tier-1 audience instead of a parallel one.
2. **High-value use cases for Claude** — site selection, M&A diligence, grid risk, capex planning. Enterprise Claude users are EXACTLY the data-center decision-makers we serve.
3. **No competitive risk** — we're not a Claude competitor. We feed it. CC-BY-4.0 means citation-clean. No license review needed.
4. **Live demo available** — `https://dchub.cloud/mcp` is up right now. Any Anthropic engineer can hit it from Claude.ai in 30 seconds (free tier, no signup).
5. **Tier-1 broker / hyperscaler interest** — open partnership invitations posted publicly to CBRE, JLL, DCHawk, DCByte, DCD, DCF at https://dchub.cloud/partners. These are the brands Anthropic enterprise customers care about; we're the bridge.

### Suggested screenshots to attach
1. `/mcp` returning the full tool catalog via `tools/list` (terminal screenshot)
2. `/dcpi` landing page with the BUILD/CAUTION/AVOID heatmap
3. `/reports/monthly` showing the comprehensive monthly report (replaces CBRE/JLL static PDFs)
4. `/vs` showing the honest side-by-side vs DCHawk / DC Byte / Baxtel
5. `/api/v1/agents/citations.json` showing real agent platforms calling us

### Contact
- Founder/Owner: Jonathan Martone — jm@dchub.cloud
- LinkedIn: https://linkedin.com/in/jonathanmartone
- Partnerships: partnerships@dchub.cloud
- Press: press@dchub.cloud
- Anthropic relationship sponsor (if asked): No prior relationship yet — this is a cold submission. Happy to do a 15-min demo call.

---

## Cold-email template (Plan B, if the form goes nowhere)

**To**: partnerships@anthropic.com (or via Anthropic sales)
**Subject**: MCP Connector Directory — DC Hub (cloud.dchub/mcp-server) — 1M+ agent requests served

Hi Anthropic team,

We run DC Hub — the live, agent-native data layer for data-center & energy infrastructure at `https://dchub.cloud/mcp`. 40+ tools, 21K+ facilities, 300+ power markets scored daily (DCPI), 3,000+ M&A deals tracked, live grid telemetry, CC-BY-4.0.

Already listed on registry.modelcontextprotocol.io as `cloud.dchub/mcp-server` (v2.3.3, DNS-auth). Smithery, mcp.so, Glama, PulseMCP all carry us. We've served 1M+ AI-agent requests; Claude and Cursor are active MCP clients.

Would love to be listed in Claude.ai's official Connector Directory. Submission packet (form fields, descriptions, screenshots, compliance details) available on request.

15-min demo any time. Or just hit the server — we're up now, free tier, no signup.

— Jonathan Martone, Founder, DC Hub
jm@dchub.cloud · linkedin.com/in/jonathanmartone

---

## After submission — what to record

Drop the submission ID + date into the registry tracker:

```bash
curl -X POST "https://api.dchub.cloud/api/v1/admin/outreach/mcp-registry/status" \
  -H "X-Admin-Key: $DCHUB_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"key":"anthropic","action":"submitted","submission_id":"...","at":"2026-MM-DD"}'
```

Then follow-up at +14 days if no response. Anthropic's connector directory team is small but responsive when the use case is concrete and the server is already proven (we are).
