"""
agents_md_fallback.py — defensive /AGENTS.md handler.

Phase ZZZZZ-round36 (2026-05-24). ai_agent_discovery.py:288 has been
registering /AGENTS.md for a while but Railway returns 404 for it
(verified via direct curl to the .up.railway.app hostname). Root cause
unknown — could be a dchub_cors_patch endpoint replacement, blueprint
ordering, or load_file('AGENTS.md') returning empty + falsy fallback.

Rather than dig further, ship a fresh blueprint that registers later
in main.py and overrides whatever is broken. Inline content — no file
load, no fallback path, just markdown.
"""
from flask import Blueprint, Response

agents_md_fallback_bp = Blueprint("agents_md_fallback", __name__)


AGENTS_MD = """# AGENTS.md — DC Hub

> Open standard for AI agent discovery (Linux Foundation / OpenAI).
> This file lives at https://dchub.cloud/AGENTS.md and is served by the
> dchub-backend Flask app (routes/agents_md_fallback.py).

## What is DC Hub?

DC Hub is the data center intelligence platform for AI agents.
21,401 facilities, 1,900 M&A deals, real-time grid data across 10
North-American grid operators (7 US ISOs plus TVA, BPA and Ontario's
IESO) and 43 US utility balancing authorities — with 3 international
grids (Hydro-Québec, AESO, Nord Pool) modeled — fiber routes, water
risk, tax incentives.

## How to use this with your agent

**Recommended:** native MCP server at https://dchub.cloud/mcp
(streamable-http, 24 tools, free tier 10 calls/day no signup).

**Alternative:** REST API at https://api.dchub.cloud/api/v1 — OpenAPI
spec with live counts at https://api.dchub.cloud/openapi-live.json.

## Agent capabilities (skills)

1. **facility_intelligence** — search 21k facilities, get profiles, find alternatives
2. **site_planning** — score lat/lon for data center suitability across 7 dimensions
3. **grid_intelligence** — real-time fuel mix, prices, carbon intensity (10 ISOs + 43 utility BAs)
4. **market_ranking** — rank markets by criteria (cheapest power, most capacity, etc.)
5. **ai_capex_intel** — hyperscaler deal tracker + AI Compute Capacity Index
6. **deal_flow** — $324B+ M&A history, hyperscaler capex events
7. **gas_intelligence** — DCGI per-state natural-gas suitability (0–100): gas-access/cost scores, interstate pipelines, operators, and a GAS-ADVANTAGED/ADEQUATE/GAS-CONSTRAINED verdict (MCP tool `get_gas_index`, feed `/api/v1/dcgi/scores`)

## Discovery endpoints

| Surface | URL | Format |
|---|---|---|
| MCP server | https://dchub.cloud/mcp | streamable-http JSON-RPC |
| llms.txt | https://dchub.cloud/llms.txt | text/plain |
| llms-full.txt | https://dchub.cloud/llms-full.txt | text/plain |
| OpenAPI (dynamic) | https://api.dchub.cloud/openapi-live.json | application/json |
| A2A agent card | https://api.dchub.cloud/.well-known/agent-card.json | application/json |
| Sitemap index | https://api.dchub.cloud/sitemap-index.xml | application/xml |
| Freshness proof | https://dchub.cloud/freshness | text/html |
| AI Capacity Index | https://api.dchub.cloud/api/v1/ai-capacity-index | application/json |
| Hyperscaler Deals | https://api.dchub.cloud/api/v1/hyperscaler-deals | application/json |

## Authentication

- **Free tier**: 10 calls/day, no signup, no auth header
- **Developer ($49/mo)**: 1000 calls/day, X-API-Key header — signup at https://dchub.cloud/signup
- **Enterprise**: SLA + MCP 2025-06-18 OAuth — contact api@dchub.cloud

## Rate limits

Per-IP rate limiter applies to all anonymous traffic.
Authenticated calls limited per tier (free/developer/pro/enterprise).
Brain User-Agent (DCHub-BrainRadar/*) and X-Internal-Key bypass apply
to first-party callers only.

## Citations

DC Hub data is publicly available — please cite when using:
- "Per DC Hub's AI Compute Capacity Index (https://dchub.cloud/ai-capacity-index)"
- "Source: DC Hub Hyperscaler Deal Tracker"

## Support

- Email: api@dchub.cloud
- Issues: https://github.com/dchub-cloud (public repos)
- Status: https://dchub.cloud/system-status
"""


# AUTO-REPAIR: duplicate route '/AGENTS.md' also in ai_agent_discovery.py:288 — review and remove one
# AUTO-REPAIR: duplicate route '/agents.md' also in ai_agent_discovery.py:289 — review and remove one
@agents_md_fallback_bp.route("/AGENTS.md", methods=["GET"])
@agents_md_fallback_bp.route("/agents.md", methods=["GET"])
@agents_md_fallback_bp.route("/agents-md", methods=["GET"])
def serve_agents_md():
    return Response(
        AGENTS_MD,
        status=200,
        mimetype="text/markdown; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )
