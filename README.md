# DC Hub Backend

**Flask backend powering [dchub.cloud](https://dchub.cloud) — real-time data center, power, and gas intelligence for AI agents and humans.**

> 🔍 **Looking for the MCP server?** This repo is the Flask backend. The standalone MCP server is at [azmartone67/dchub-mcp-server](https://github.com/azmartone67/dchub-mcp-server) (live at `https://dchub.cloud/mcp`).

<!-- ★2026-09-04: these four badges are DERIVED, not typed. They read
     https://dchub.cloud/api/v1/canon/phrases — the same resolve_canon() every
     other surface reads — so they heal instead of rotting.
     Until today they were hand-typed, and every one of them had drifted: the
     tool count trailed the live server, the market count trailed the index,
     and the facility count sat ABOVE canon — an over-claim, on the
     registry-facing surface GitMCP and the MCP directories mirror.
     ★ Do NOT write the numbers back in as literals, and do not quote a stale
       figure here either: this file is scanned as an agent-facing SURFACE, so
       a number in this comment is a number on the surface (that is how the
       first draft of this note failed the stale-marker fence).
     If a badge renders an error the canon endpoint is down — a visibly missing
     number is the intended failure mode; a confidently wrong one is not. -->
[![MCP tools](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdchub.cloud%2Fapi%2Fv1%2Fcanon%2Fphrases&query=%24.tools&label=MCP%20tools&color=blue)](https://dchub.cloud/.well-known/mcp.json) [![DCPI markets](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdchub.cloud%2Fapi%2Fv1%2Fcanon%2Fphrases&query=%24.markets&label=DCPI%20markets&color=purple)](https://dchub.cloud/dcpi) [![facilities](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdchub.cloud%2Fapi%2Fv1%2Fcanon%2Fphrases&query=%24.facilities&label=facilities&color=green)](https://dchub.cloud) [![countries](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdchub.cloud%2Fapi%2Fv1%2Fcanon%2Fphrases&query=%24.countries&label=countries&color=orange)](https://dchub.cloud) [![License](https://img.shields.io/badge/data-CC--BY--4.0-lightgrey)](https://dchub.cloud/cited-by)

---

## What this powers

DC Hub is the live data layer for data-center infrastructure — every API, MCP tool call, market brief, and AI integration on `dchub.cloud` runs through this Flask backend.

- **20,100+ data center facilities** across 170+ countries (search, profile, score, alternatives)
- **300+ markets** scored daily by the DC Hub Power Index (DCPI — BUILD / CAUTION / AVOID)
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

The MCP server at `https://dchub.cloud/mcp` exposes **80+ tools** for AI agents (`tools/list` is the canonical, always-current catalog). See the standalone repo: [azmartone67/dchub-mcp-server](https://github.com/azmartone67/dchub-mcp-server).

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
- **Paid tiers**: <https://dchub.cloud/pricing> ($9 Starter, $49 Developer, $299 Pro, Enterprise)
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
