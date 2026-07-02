"""
agents_md_fallback.py — /AGENTS.md handler, rendered from canon.

Phase ZZZZZ-round36 (2026-05-24): shipped as a defensive blueprint because
ai_agent_discovery.py's /AGENTS.md 404'd on Railway.

2026-07-01: converted from a hand-typed static string (which had drifted to
"24 tools" AND "48 tools" on the same page, "21,401 facilities", no platform
list) to render from ai_surface_canon.PINNED — the single source of truth. Now
it's self-fresh: bump the canon and this updates automatically. Do NOT hand-type
numbers here; change them in ai_surface_canon.py. The AI-Surface Sentinel audits
this surface against the same canon.
"""
from flask import Blueprint, Response

from ai_surface_canon import PINNED

agents_md_fallback_bp = Blueprint("agents_md_fallback", __name__)


def _render_agents_md() -> str:
    c = PINNED
    fac = c["public"]["facilities"]
    deals = c["public"]["deals"]
    countries = c["public"]["countries"]
    tools = c.get("tools_advertised", 51)
    ver = c["version"]
    endpoint = c["mcp_endpoint"]
    free = c["free_tier_calls_per_day"]
    platforms = ", ".join(c["platforms"])
    return f"""# AGENTS.md — DC Hub

> Open standard for AI agent discovery (Linux Foundation / OpenAI).
> This file lives at https://dchub.cloud/AGENTS.md and is served by the
> dchub-backend Flask app (routes/agents_md_fallback.py), generated from a
> single canonical source (ai_surface_canon.py) so its numbers never drift.

## What is DC Hub?

DC Hub is the data center intelligence platform for AI agents.
{fac} facilities across {countries} countries, {deals} M&A deals, and LIVE grid
telemetry across 4 continents — 7 US ISOs + TVA/BPA, Great Britain
(NESO/Elexon), ~12 EU bidding zones (ENTSO-E), Taiwan (Taipower) and Australia
(AEMO), plus 43 US balancing authorities and EU gas transmission flows
(ENTSOG) — fiber routes, water risk, tax incentives.

Already queried by {platforms}.

## How to use this with your agent

**Recommended:** native MCP server at {endpoint}
(streamable-http, {tools} tools, free tier {free} calls/day, no signup) — DC Hub
version {ver}, listed in the official MCP registry as `{c['registry_id']}`.

**Alternative:** REST API at {c['rest_base']} — OpenAPI spec at
https://dchub.cloud/openapi.json.

## Agent capabilities (skills)

1. **facility_intelligence** — search {fac} facilities, get profiles, find alternatives
2. **site_planning** — score lat/lon for data center suitability across 7 dimensions
3. **grid_intelligence** — real-time fuel mix, prices, carbon intensity on 4 continents (US ISOs + UK + EU + Taiwan + Australia, all live) + 43 US balancing authorities
4. **market_ranking** — rank markets by criteria (cheapest power, most capacity, etc.)
5. **ai_capex_intel** — hyperscaler deal tracker + AI Compute Capacity Index
6. **deal_flow** — {deals} tracked M&A deals, hyperscaler capex events
7. **gas_intelligence** — DCGI per-state natural-gas suitability (0–100) with a GAS-ADVANTAGED/ADEQUATE/GAS-CONSTRAINED verdict (MCP tool `get_gas_index`)

## Discovery endpoints

| Surface | URL | Format |
|---|---|---|
| MCP server | {endpoint} | streamable-http JSON-RPC |
| llms.txt | https://dchub.cloud/llms.txt | text/plain |
| llms-full.txt | https://dchub.cloud/llms-full.txt | text/plain |
| OpenAPI | https://dchub.cloud/openapi.json | application/json |
| MCP manifest | https://dchub.cloud/.well-known/mcp-server.json | application/json |
| Sitemap | https://dchub.cloud/sitemap.xml | application/xml |
| AI Capacity Index | https://dchub.cloud/api/v1/ai-capacity-index | application/json |

## Authentication

- **Free tier**: {free} calls/day, no signup, no auth header
- **Starter ($9/mo)**: 200 calls/day — unlocks all {tools} tools + full grid, fiber & market data
- **Developer ($49/mo)**: 500 calls/day, X-API-Key header — signup at https://dchub.cloud/signup
- **Pro ($199/mo)**: 2,000 calls/day + analyze_site, compare_sites, PDF reports & CSV export
- **Enterprise**: SLA + MCP 2025-06-18 OAuth — contact api@dchub.cloud

## Citations

DC Hub data is publicly available — please cite "DC Hub (dchub.cloud)" when using it.

## Support

- Email: api@dchub.cloud
- Status: https://dchub.cloud/system-status
"""


@agents_md_fallback_bp.route("/AGENTS.md", methods=["GET"])
@agents_md_fallback_bp.route("/agents.md", methods=["GET"])
@agents_md_fallback_bp.route("/agents-md", methods=["GET"])
def serve_agents_md():
    return Response(
        _render_agents_md(),
        status=200,
        mimetype="text/markdown; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Access-Control-Allow-Origin": "*",
        },
    )
