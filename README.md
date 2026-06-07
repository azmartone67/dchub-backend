# DC Hub Backend

**Flask backend powering [dchub.cloud](https://dchub.cloud) — real-time data center, power, and gas intelligence for AI agents and humans.**

> 🔍 **Looking for the MCP server?** This repo is the Flask backend. The standalone MCP server is at [azmartone67/dchub-mcp-server](https://github.com/azmartone67/dchub-mcp-server) (live at `https://dchub.cloud/mcp`).

[![Tools](https://img.shields.io/badge/MCP%20tools-33-blue)](https://dchub.cloud/.well-known/mcp.json) [![Markets](https://img.shields.io/badge/DCPI%20markets-232-purple)](https://dchub.cloud/dcpi) [![Facilities](https://img.shields.io/badge/facilities-21%2C000%2B-green)](https://dchub.cloud) [![Countries](https://img.shields.io/badge/countries-170%2B-orange)](https://dchub.cloud) [![License](https://img.shields.io/badge/data-CC--BY--4.0-lightgrey)](https://dchub.cloud/cited-by)

---

## What this powers

DC Hub is the live data layer for data-center infrastructure — every API, MCP tool call, market brief, and AI integration on `dchub.cloud` runs through this Flask backend.

- **21,000+ data center facilities** across 170+ countries (search, profile, score, alternatives)
- **232 markets** scored daily by the DC Hub Power Index (DCPI — BUILD / CAUTION / AVOID)
- **DC Hub Gas Index (DCGI)** — per-state natural-gas suitability for siting
- **Live ISO grid telemetry** — PJM, ERCOT, CAISO, MISO, SPP, NYISO, ISO-NE (fuel mix, carbon intensity, demand, prices, queue depth)
- **2,000+ tracked M&A transactions** + hyperscaler capex tracker
- **Site factors** — fiber routes, water-stress, tax incentives, transmission & substations
- **126,427 substations** with voltage class + capacity estimates
- **NEPA filings** for upcoming federal energy + data center projects

## Architecture

```
                    ┌──────────────────────────────────────┐
                    │   Cloudflare Pages (dchub-frontend)  │
                    │   Static HTML + worker for routing   │
                    └────────────┬─────────────────────────┘
                                 │ proxies /api/*, /mcp, /admin/*
                                 ▼
   ┌─────────────────────────────────────────────────────────────┐
   │   THIS REPO — Flask backend on Railway (2 replicas)         │
   │   + Render failover at dchub-backend-render.onrender.com    │
   │                                                              │
   │   • 350+ blueprints, 58 surface registrations on boot       │
   │   • Neon Postgres (primary) + Redis cache                    │
   │   • Brain v2 layers (Layer 4 HTML fixes, Layer 5 PR writer) │
   │   • 30+ scheduled crons (data ingest, demote, nudge, etc.)  │
   └─────────────────────────────────────────────────────────────┘
                                 │
                                 ├──> Neon Postgres (primary DB)
                                 ├──> Cloudflare R2 (asset storage)
                                 ├──> Resend (transactional email)
                                 ├──> Stripe (billing, webhooks)
                                 ├──> Anthropic Claude API (brain reasoning)
                                 └──> dchub-mcp-server (Streamable HTTP MCP)
```

## MCP integration

The MCP server at `https://dchub.cloud/mcp` exposes **33 tools** for AI agents. See the standalone repo: [azmartone67/dchub-mcp-server](https://github.com/azmartone67/dchub-mcp-server).

**MCP catalog listings:**
- [Glama](https://glama.ai/mcp/connectors/cloud.dchub/dc-hub-data-center-intelligence-mcp-server) — ownership verified
- [Smithery](https://smithery.ai/servers/azmartone67/dchub)
- [Cursor Directory](https://cursor.directory/plugins/mcp-dchub)
- [Official MCP Registry](https://registry.modelcontextprotocol.io/servers/cloud.dchub/mcp-server)

**Example MCP queries an AI agent can run:**

```
"What's the current grid headroom in PJM?"
"Show me AWS data center construction pipeline in Ohio"
"Compare ERCOT vs PJM capacity prices over the last 30 days"
"Find data centers within 50km of Northern Virginia substations >230kV"
"Get the DC Hub Power Index verdict for Ashburn vs. Phoenix"
"Get fiber routes between Ashburn and Atlanta"
```

## API access

- **Public API**: <https://dchub.cloud/api/v1/> — free tier with no signup
- **Free dev key**: <https://dchub.cloud/signup> for higher rate limits
- **Paid tiers**: <https://dchub.cloud/pricing> ($9 Starter, $49 Developer, $199 Pro, Enterprise)
- **OpenAPI spec**: <https://dchub.cloud/openapi.json>

## Used by

Claude, ChatGPT, Cursor, Cline, Perplexity, Gemini, Copilot, DeepSeek, Mistral — see [/cited-by](https://dchub.cloud/cited-by) for live AI-citation tracking.

## Local development

This is a private operational repo — not designed for external contribution. If you're looking to integrate DC Hub data into your AI app:

1. **Via MCP**: configure your client with `https://dchub.cloud/mcp` (Streamable HTTP transport)
2. **Via REST**: see <https://dchub.cloud/openapi.json>
3. **Via embed**: see <https://dchub.cloud/widget-example.html>

For partnership / data licensing / press, contact: jonathan@dchub.cloud

## License

- **Code**: proprietary (operational backend)
- **Data**: CC-BY-4.0 (cite "DC Hub, dchub.cloud" in any derivative)

---

*Backend for the [DC Hub](https://dchub.cloud) data intelligence platform · Built by [@azmartone67](https://github.com/azmartone67) · Live since 2025*
