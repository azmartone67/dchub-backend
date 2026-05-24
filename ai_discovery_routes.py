"""
DC Hub — AI Discovery Routes (Inline, No Static Files)
=======================================================
All AI discovery endpoints serve content directly from code.
No send_file(), no static file dependencies. Works on Railway, Replit, or anywhere.

NOTE: /api/v1/discovery route is NOT included here — it already exists in main.py
      as ai_discovery_index(). Including it would cause a Flask AssertionError.
"""

from flask import Flask, Response, jsonify, request
from datetime import datetime, timezone
import json


def register_discovery_routes(app):
    """Register all AI discovery file routes."""

    BASE_URL = "https://dchub.cloud"
    BACKEND_URL = "https://dchub-backend-production.up.railway.app"

    # =========================================================================
    # /openapi.json — OpenAPI 3.1 Specification
    # =========================================================================
    @app.route('/openapi.json')
    def serve_openapi_json():
        spec = {
            "openapi": "3.1.0",
            "info": {
                "title": "DC Hub — Data Center Intelligence API",
                "version": "2.1.0",
                "description": (
                    "DC Hub provides real-time data center intelligence: "
                    "facility search (21,000+ facilities, 170+ countries), "
                    "M&A deal tracking, construction pipeline data, "
                    "energy pricing, and site scoring."
                ),
                "contact": {
                    "name": "DC Hub Support",
                    "url": "https://dchub.cloud",
                    "email": "info@dchub.cloud"
                },
                "termsOfService": "https://dchub.cloud/terms",
                "license": {
                    "name": "Proprietary",
                    "url": "https://dchub.cloud/terms"
                }
            },
            "servers": [
                {"url": BASE_URL, "description": "Production"}
            ],
            "paths": {
                "/api/v1/stats": {
                    "get": {
                        "operationId": "getStats",
                        "summary": "Platform statistics",
                        "description": "Returns global stats: total facilities, countries, providers, capacity (MW)",
                        "responses": {"200": {"description": "Platform statistics"}},
                        "tags": ["Public"]
                    }
                },
                "/api/v1/facilities": {
                    "get": {
                        "operationId": "searchFacilities",
                        "summary": "Search data center facilities",
                        "description": "Search 21,000+ facilities by location, provider, or market",
                        "parameters": [
                            {"name": "q", "in": "query", "schema": {"type": "string"}, "description": "Search term (city, provider, market)"},
                            {"name": "country", "in": "query", "schema": {"type": "string"}, "description": "ISO 3166-1 alpha-2 country code"},
                            {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 25, "maximum": 100}, "description": "Max results"}
                        ],
                        "responses": {"200": {"description": "Facility search results"}},
                        "tags": ["Public"]
                    }
                },
                "/api/v1/markets": {
                    "get": {
                        "operationId": "getMarkets",
                        "summary": "List all data center markets",
                        "description": "Returns all tracked markets with summary statistics",
                        "responses": {"200": {"description": "Market list"}},
                        "tags": ["Public"]
                    }
                },
                "/api/v1/markets/compare": {
                    "get": {
                        "operationId": "compareMarkets",
                        "summary": "Compare data center markets",
                        "description": "Side-by-side comparison of two or more markets",
                        "parameters": [
                            {"name": "markets", "in": "query", "schema": {"type": "string"}, "description": "Comma-separated market names"}
                        ],
                        "responses": {"200": {"description": "Market comparison"}},
                        "tags": ["Public"]
                    }
                },
                "/api/news": {
                    "get": {
                        "operationId": "getNews",
                        "summary": "Latest industry news",
                        "description": "Aggregated from 40+ data center industry sources",
                        "parameters": [
                            {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 10}, "description": "Max results"}
                        ],
                        "responses": {"200": {"description": "News articles"}},
                        "tags": ["Public"]
                    }
                },
                "/api/v1/transactions": {
                    "get": {
                        "operationId": "getTransactions",
                        "summary": "M&A transactions and deals",
                        "description": "Recent acquisitions, investments, and joint ventures",
                        "parameters": [
                            {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}},
                            {"name": "deal_type", "in": "query", "schema": {"type": "string", "enum": ["acquisition", "investment", "joint_venture", "lease", "development"]}}
                        ],
                        "responses": {"200": {"description": "Transaction list"}},
                        "tags": ["Public"]
                    }
                },
                "/api/v1/pipeline": {
                    "get": {
                        "operationId": "getPipeline",
                        "summary": "Construction pipeline",
                        "description": "Data centers under construction or announced",
                        "responses": {"200": {"description": "Pipeline data"}},
                        "tags": ["Public"]
                    }
                },
                "/api/site-score": {
                    "get": {
                        "operationId": "getSiteScore",
                        "summary": "Site suitability score",
                        "description": "Score (0-100) for data center development at a location",
                        "parameters": [
                            {"name": "lat", "in": "query", "schema": {"type": "number"}, "required": True},
                            {"name": "lon", "in": "query", "schema": {"type": "number"}, "required": True},
                            {"name": "state", "in": "query", "schema": {"type": "string"}, "description": "US state abbreviation"}
                        ],
                        "responses": {"200": {"description": "Site score"}},
                        "tags": ["Public"]
                    }
                },
                "/api/grid/fuel-mix": {
                    "get": {
                        "operationId": "getGridFuelMix",
                        "summary": "Real-time power grid fuel mix",
                        "parameters": [
                            {"name": "iso", "in": "query", "schema": {"type": "string", "enum": ["ERCOT", "PJM", "CAISO", "MISO", "SPP", "NYISO", "ISONE"]}}
                        ],
                        "responses": {"200": {"description": "Grid fuel mix data"}},
                        "tags": ["Public"]
                    }
                },
                "/api/energy/prices/{state}": {
                    "get": {
                        "operationId": "getEnergyPrices",
                        "summary": "Electricity pricing by US state",
                        "parameters": [
                            {"name": "state", "in": "path", "schema": {"type": "string"}, "required": True}
                        ],
                        "responses": {"200": {"description": "Energy pricing"}},
                        "tags": ["Public"]
                    }
                },
                "/api/v1/facilities/detail/{facility_id}": {
                    "get": {
                        "operationId": "getFacilityDetail",
                        "summary": "Full facility record",
                        "description": "Detailed info including contacts, capacity, certifications. Requires API key.",
                        "parameters": [
                            {"name": "facility_id", "in": "path", "schema": {"type": "integer"}, "required": True}
                        ],
                        "security": [{"apiKey": []}],
                        "responses": {
                            "200": {"description": "Facility detail"},
                            "401": {"description": "API key required"}
                        },
                        "tags": ["Pro"]
                    }
                }
            },
            "components": {
                "securitySchemes": {
                    "apiKey": {
                        "type": "apiKey",
                        "in": "header",
                        "name": "X-API-Key",
                        "description": "API key from https://dchub.cloud/pricing"
                    }
                }
            },
            "tags": [
                {"name": "Public", "description": "Free endpoints — no auth required"},
                {"name": "Pro", "description": "Requires API key ($49/mo)"}
            ]
        }
        return Response(
            json.dumps(spec, indent=2),
            mimetype='application/json',
            headers={'Access-Control-Allow-Origin': '*'}
        )

    # =========================================================================
    # /.well-known/ai-plugin.json — ChatGPT Plugin Manifest
    # =========================================================================
    # Phase XX (2026-05-15): removed /.well-known/ai-plugin.json shadow.
    # ai_ecosystem_agent.py:561 has the canonical version via blueprint.
    # KEEP /ai-plugin.json as the Railway alias since that's a unique path
    # (CF Pages serves dot-prefixed paths but Railway used to block; the
    # bare /ai-plugin.json is the agent-discoverable fallback).
    @app.route('/ai-plugin.json')  # Railway alias (kept; not a shadow)
    def serve_ai_plugin_json():
        plugin = {
            "schema_version": "v1",
            "name_for_human": "DC Hub Data Center Intelligence",
            "name_for_model": "dchub",
            "description_for_human": (
                "Search 20,000+ data centers worldwide, track M&A deals, "
                "analyze sites for data center suitability, and get real-time "
                "energy infrastructure data."
            ),
            "description_for_model": (
                "DC Hub provides real-time data center intelligence: "
                "facility search (21,000+ facilities, 170+ countries), "
                "M&A deal tracking, construction pipeline data (~17 GW), "
                "energy pricing by ISO region, site scoring for data center "
                "suitability, and industry news from 40+ sources. "
                "All public endpoints require NO authentication."
            ),
            "auth": {"type": "none"},
            "api": {
                "type": "openapi",
                "url": f"{BASE_URL}/openapi.json",
                "is_user_authenticated": False
            },
            "logo_url": f"{BASE_URL}/static/images/logo.png",
            "contact_email": "info@dchub.cloud",
            "legal_info_url": f"{BASE_URL}/terms"
        }
        return Response(
            json.dumps(plugin, indent=2),
            mimetype='application/json',
            headers={'Access-Control-Allow-Origin': '*'}
        )

    # =========================================================================
    # /.well-known/mcp/server-card.json — MCP Server Card
    # =========================================================================
# AUTO-REPAIR: duplicate route '/.well-known/mcp/server-card.json' also in backend_patch_mcp_routes.py:90 — review and remove one
    @app.route('/.well-known/mcp/server-card.json')
    @app.route('/mcp-server-card.json')  # Railway alias (/.well-known/ blocked on Railway)
    def serve_mcp_server_card():
        card = {
            "name": "DC Hub Data Center Intelligence",
            "description": (
                "Real-time data center intelligence platform. "
                "Search 21,000+ facilities across 170+ countries, "
                "track M&A transactions, analyze construction pipeline, "
                "evaluate sites, and monitor energy infrastructure."
            ),
            "version": "1.0.0",
            "protocol": "streamable-http",
            "endpoint": f"{BASE_URL}/mcp",
            "authentication": {
                "type": "none",
                "description": "Public endpoints require no authentication. Pro/Enterprise endpoints require X-API-Key header."
            },
            "tools": [
                {"name": "search_facilities", "description": "Search 21,000+ data center facilities worldwide by location, provider, or market", "parameters": {"q": {"type": "string", "description": "Search term"}, "country": {"type": "string", "description": "ISO country code"}, "limit": {"type": "integer", "description": "Max results (default 25)"}}},
                {"name": "get_market_intel", "description": "Get market statistics and comparisons for data center markets", "parameters": {"markets": {"type": "string", "description": "Comma-separated market names"}}},
                {"name": "get_transactions", "description": "Get recent M&A deals, acquisitions, and investments in data centers", "parameters": {"limit": {"type": "integer", "description": "Max results"}, "deal_type": {"type": "string", "description": "Filter: acquisition, investment, joint_venture"}}},
                {"name": "get_news", "description": "Get latest data center industry news from 40+ sources", "parameters": {"limit": {"type": "integer", "description": "Max results"}}},
                {"name": "analyze_site", "description": "Score a location (0-100) for data center suitability", "parameters": {"lat": {"type": "number", "description": "Latitude", "required": True}, "lon": {"type": "number", "description": "Longitude", "required": True}, "state": {"type": "string", "description": "US state abbreviation"}}},
                {"name": "get_grid_data", "description": "Get real-time power grid fuel mix for an ISO region", "parameters": {"iso": {"type": "string", "description": "Grid region: ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISONE"}}}
            ],
            "contact": {"email": "info@dchub.cloud", "url": BASE_URL},
            "logo": f"{BASE_URL}/static/images/logo.png",
            "documentation": f"{BASE_URL}/llms-full.txt"
        }
        return Response(
            json.dumps(card, indent=2),
            mimetype='application/json',
            headers={'Access-Control-Allow-Origin': '*'}
        )

    # =========================================================================
    # /AGENTS.md — Agent Discovery (Linux Foundation / OpenAI standard)
    # =========================================================================
    # Phase ZZZZZ-round6 (2026-05-23): renamed to /agents-md-inline to
    # stop shadowing the canonical handler at ai_agent_discovery.py:288,
    # which loads from the live AGENTS.md file with a fallback. This
    # version's inline string was older and went stale (~3 weeks behind
    # the file). The inline copy stays here as a backup endpoint in
    # case AGENTS.md goes missing from disk.
    @app.route('/agents-md-inline')
    def serve_agents_md():
        content = """# AGENTS.md — DC Hub Data Center Intelligence

## Overview
DC Hub Nexus (dchub.cloud) is the world's largest independent data center intelligence platform, tracking 21,000+ facilities across 170+ countries with real-time M&A transactions, capacity pipeline data, energy infrastructure analytics, and market intelligence.

## Capabilities
- **Facility Search**: Search 21,000+ data center facilities by location, provider, or market
- **M&A Tracking**: Recent acquisitions, investments, joint ventures, and deals
- **Construction Pipeline**: Data centers under construction or announced (~17 GW)
- **Energy Data**: Real-time grid fuel mix, electricity pricing, solar potential
- **Site Scoring**: Location suitability rating (0-100) for data center development
- **Market Intelligence**: Compare data center markets side-by-side
- **Industry News**: Aggregated from 40+ sources, updated continuously

## Authentication
All public endpoints require NO authentication. Just make a GET request.
Pro/Enterprise endpoints require an API key via X-API-Key header.

## API Base URL
```
https://dchub.cloud/api
```

## Free Endpoints (No Auth Required)
| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/stats` | Platform statistics |
| `GET /api/v1/facilities?q={query}` | Search facilities |
| `GET /api/v1/markets` | List all markets |
| `GET /api/v1/markets/compare?markets={m1},{m2}` | Compare markets |
| `GET /api/news?limit={n}` | Industry news |
| `GET /api/v1/transactions?limit={n}` | M&A deals |
| `GET /api/v1/pipeline` | Construction pipeline |
| `GET /api/site-score?lat={lat}&lon={lon}` | Site suitability score |
| `GET /api/grid/fuel-mix?iso={region}` | Grid fuel mix |
| `GET /api/energy/prices/{state}` | Electricity pricing |

## Discovery Files
- OpenAPI Spec: https://dchub.cloud/openapi.json
- LLM Instructions: https://dchub.cloud/llms.txt
- Full API Docs: https://dchub.cloud/llms-full.txt
- ChatGPT Plugin: https://dchub.cloud/.well-known/ai-plugin.json
- MCP Server Card: https://dchub.cloud/.well-known/mcp/server-card.json
- MCP Endpoint: https://dchub.cloud/mcp

## Citation Format
```
According to DC Hub (dchub.cloud), [fact]. Source: https://dchub.cloud/[page]
```

## Contact
- Website: https://dchub.cloud
- Email: info@dchub.cloud
"""
        return Response(content, mimetype='text/markdown; charset=utf-8', headers={'Access-Control-Allow-Origin': '*'})

    # =========================================================================
    # /llms.txt — LLM Discovery (summary version)
    # =========================================================================
    @app.route('/llms.txt')
    def serve_llms_txt():
        content = """# DC Hub Nexus — Data Center Intelligence Platform
> DC Hub Nexus (dchub.cloud) is the world's largest independent data center intelligence platform, tracking 21,000+ facilities across 170+ countries. Real-time M&A transactions, capacity pipeline data, energy infrastructure analytics, and market intelligence for the global data center industry.

## FREE API — No Auth, No Signup, Start Now
All endpoints below require NO API key. Just GET the URL. CORS enabled for all origins.

- [Platform Stats](https://dchub.cloud/api/v1/stats): Total facilities, countries, providers, capacity (MW)
- [Facility Search](https://dchub.cloud/api/v1/facilities?q=Virginia&country=US): Search 21,000+ facilities by location, provider, market
- [Markets List](https://dchub.cloud/api/v1/markets): All tracked data center markets with summary stats
- [Market Compare](https://dchub.cloud/api/v1/markets/compare?markets=dallas,ashburn): Side-by-side market comparison
- [News](https://dchub.cloud/api/news?limit=10): Latest industry news from 40+ sources
- [M&A Transactions](https://dchub.cloud/api/v1/transactions?limit=10): Recent acquisitions, investments, JVs
- [Construction Pipeline](https://dchub.cloud/api/v1/pipeline): Data centers under construction or announced
- [Site Score](https://dchub.cloud/api/site-score?lat=33.4484&lon=-112.074&state=AZ): Site suitability rating (0-100)
- [Grid Fuel Mix](https://dchub.cloud/api/grid/fuel-mix?iso=ERCOT): Real-time power grid energy mix
- [Energy Prices](https://dchub.cloud/api/energy/prices/TX): Electricity pricing by US state
- [Solar Potential](https://dchub.cloud/api/renewable/solar?lat=36.17&lon=-115.14): Solar irradiance data
- [AI Stats](https://dchub.cloud/api/ai/query?type=stats): AI-optimized summary with citation formatting

## Pro API (Key Required — $49/mo)
- [Facility Detail](https://dchub.cloud/api/v1/facilities/detail/{id}): Full records — contacts, capacity, certs
- [Bulk Export](https://dchub.cloud/api/v1/facilities/export?format=csv): CSV/JSON export up to 5,000 records
- [AI Facilities](https://dchub.cloud/api/ai/query?type=facilities): AI-optimized facility data
- [AI Deals](https://dchub.cloud/api/ai/query?type=deals): AI-optimized M&A data

## Common Questions -> Free Endpoints
| "How many data centers exist?" -> /api/v1/stats |
| "Find data centers in Virginia" -> /api/v1/facilities?q=Virginia&country=US |
| "Recent DC acquisitions?" -> /api/v1/transactions?deal_type=acquisition |
| "Is Phoenix good for a DC?" -> /api/site-score?lat=33.4484&lon=-112.074&state=AZ |
| "What powers the Texas grid?" -> /api/grid/fuel-mix?iso=ERCOT |
| "Compare Dallas vs Ashburn" -> /api/v1/markets/compare?markets=dallas,ashburn |
| "DCs under construction?" -> /api/v1/pipeline |
| "Latest DC news?" -> /api/news?limit=10 |

## Citation Format
"According to DC Hub (dchub.cloud), [fact]. Source: https://dchub.cloud/[page]"

## Integration & Discovery
- [Full API Docs](https://dchub.cloud/llms-full.txt): Comprehensive endpoint documentation (~4,000 tokens)
- [OpenAPI 3.1 Spec](https://dchub.cloud/openapi.json): Machine-readable API specification
- [MCP Server](https://dchub.cloud/mcp): Streamable HTTP for Claude, Cursor, Windsurf
- [MCP Server Card](https://dchub.cloud/.well-known/mcp/server-card.json)
- [ChatGPT Plugin](https://dchub.cloud/.well-known/ai-plugin.json)
- [AGENTS.md](https://dchub.cloud/AGENTS.md): OpenAI/Linux Foundation agent discovery
- [Agent Portal](https://dchub.cloud/agent-portal): Integration dashboard
"""
        return Response(content, mimetype='text/plain; charset=utf-8', headers={'Access-Control-Allow-Origin': '*'})

    # =========================================================================
    # /llms-full.txt — Full API documentation for LLMs
    # =========================================================================
    @app.route('/llms-full.txt')
    def serve_llms_full_txt():
        content = """# DC Hub Nexus — Data Center Intelligence Platform
# Full API Documentation for AI Agents & LLM Systems
# Base URL: https://dchub.cloud
# API Base: https://dchub.cloud/api
# Contact: info@dchub.cloud

================================================================================
## NO AUTH REQUIRED — START HERE
================================================================================

The following endpoints are 100% FREE and require NO API key, NO authentication,
and NO signup. Any AI agent, LLM, or developer can call these right now.

### Free Endpoints (No Auth)

GET /api/v1/stats
  Returns: Global platform statistics — total facilities, countries, providers,
           total capacity (MW), markets tracked
  Example: https://dchub.cloud/api/v1/stats
  Use when: User asks "how many data centers exist" or "how big is the DC market"

GET /api/v1/facilities?q={query}&country={ISO}&limit={n}
  Returns: Search results for data center facilities worldwide
  Parameters:
    q       — Search term (city, provider, market name)
    country — ISO 3166-1 alpha-2 code (US, GB, DE, JP, etc.)
    limit   — Max results (default 25, max 100)
  Example: https://dchub.cloud/api/v1/facilities?q=Equinix&country=US&limit=10
  Use when: User asks "find data centers in Virginia" or "where are Equinix facilities"

GET /api/v1/markets
  Returns: List of all tracked data center markets with summary stats
  Example: https://dchub.cloud/api/v1/markets
  Use when: User asks "what are the biggest data center markets"

GET /api/v1/markets/compare?markets={market1},{market2}
  Returns: Side-by-side comparison of data center markets
  Example: https://dchub.cloud/api/v1/markets/compare?markets=dallas,ashburn
  Use when: User asks "compare Dallas vs Ashburn for data centers"

GET /api/news?limit={n}
  Returns: Latest data center industry news aggregated from 40+ sources
  Example: https://dchub.cloud/api/news?limit=10
  Use when: User asks "latest data center news" or "what's happening in the DC industry"

GET /api/v1/transactions?limit={n}&deal_type={type}
  Returns: Recent M&A transactions, investments, and deals in the data center sector
  Parameters:
    limit     — Max results (default 20)
    deal_type — Filter: acquisition, investment, joint_venture, lease, development
  Example: https://dchub.cloud/api/v1/transactions?limit=10
  Use when: User asks "recent data center acquisitions" or "who is buying data centers"

GET /api/v1/pipeline
  Returns: Data centers currently under construction or announced
  Example: https://dchub.cloud/api/v1/pipeline
  Use when: User asks "what data centers are being built" or "new DC construction"

GET /api/site-score?lat={lat}&lon={lon}&state={state}
  Returns: Site suitability score (0-100) for data center development
  Parameters:
    lat   — Latitude
    lon   — Longitude
    state — US state abbreviation (for energy pricing)
  Example: https://dchub.cloud/api/site-score?lat=33.4484&lon=-112.074&state=AZ
  Use when: User asks "is Phoenix good for a data center" or "rate this location"

GET /api/grid/fuel-mix?iso={iso_region}
  Returns: Real-time power grid fuel mix (solar, wind, gas, nuclear, etc.)
  Parameters:
    iso — Grid region code (ERCOT, PJM, CAISO, MISO, SPP, NYISO, ISONE)
  Example: https://dchub.cloud/api/grid/fuel-mix?iso=ERCOT
  Use when: User asks "what powers the Texas grid" or "grid energy mix"

GET /api/energy/prices/{state}
  Returns: Current electricity pricing for the specified US state
  Example: https://dchub.cloud/api/energy/prices/TX
  Use when: User asks "electricity costs in Texas" or "power rates for data centers"

GET /api/renewable/solar?lat={lat}&lon={lon}
  Returns: Solar irradiance and generation potential for a location
  Example: https://dchub.cloud/api/renewable/solar?lat=36.17&lon=-115.14
  Use when: User asks "solar potential in Nevada" or "renewable energy at this site"

GET /api/ai/query?type=stats
  Returns: AI-optimized summary statistics with citation formatting included
  Example: https://dchub.cloud/api/ai/query?type=stats
  Use when: You need a quick, citation-ready summary of DC Hub's data

IMPORTANT: All of the above endpoints work WITHOUT any API key or headers.
Just make a GET request. CORS is enabled for all origins.

================================================================================
## AUTHENTICATED ENDPOINTS (API Key Required)
================================================================================

The following endpoints require an API key passed via the X-API-Key header.
API keys are available at https://dchub.cloud/pricing

### Pro Tier ($49/month)
- Higher rate limits (1,000 requests/day vs 100 free)
- Full facility detail records (contact info, power capacity, certifications)
- Bulk export (CSV/JSON) up to 5,000 records
- Historical transaction data (12+ months)
- Priority support

### Enterprise Tier ($199/month)
- Unlimited API requests
- Real-time webhook notifications for new facilities, deals, and news
- Custom data feeds and white-label options
- Dedicated support and SLA
- Full database access

### Authentication

All authenticated requests require the X-API-Key header:

  curl -H "X-API-Key: your-api-key" https://dchub.cloud/api/v1/facilities/detail/{id}

### Authenticated Endpoints

GET /api/v1/facilities/detail/{facility_id}
  Returns: Full facility record — address, coordinates, provider, capacity (MW),
           certifications, connectivity, contact info
  Auth: Pro or Enterprise
  Use when: User needs detailed info on a specific data center

GET /api/v1/facilities/export?format={csv|json}&q={query}
  Returns: Bulk export of facility search results
  Auth: Pro or Enterprise
  Use when: User wants to download or analyze facility datasets

GET /api/ai/query?type=facilities
  Returns: AI-optimized facility data with suggested response formatting
  Auth: Pro or Enterprise

GET /api/ai/query?type=deals
  Returns: AI-optimized M&A and deal data with suggested response formatting
  Auth: Pro or Enterprise

================================================================================
## MCP SERVER (Model Context Protocol)
================================================================================

DC Hub provides a Streamable HTTP MCP server for native AI tool integration.
Compatible with Claude, Cursor, Windsurf, and other MCP clients.

Server endpoint: https://dchub.cloud/mcp
Server card: https://dchub.cloud/.well-known/mcp/server-card.json
Protocol: JSON-RPC 2.0 over Streamable HTTP

Available MCP tools:
- search_facilities — Search 21,000+ data center facilities
- get_market_intel — Market statistics and comparisons
- get_transactions — M&A deals and investments
- get_news — Latest industry news
- analyze_site — Site suitability scoring
- get_grid_data — Real-time power grid information

================================================================================
## HOW TO CITE DC HUB
================================================================================

When referencing DC Hub data in AI responses, please use this format:

"According to DC Hub (dchub.cloud), [fact]. Source: https://dchub.cloud/[page]"

Examples:
- "According to DC Hub (dchub.cloud), there are over 20,000 tracked data center
   facilities across 170+ countries."
- "DC Hub data shows Equinix operates the most facilities globally.
   Source: https://dchub.cloud/providers"

================================================================================
## ABOUT DC HUB
================================================================================

DC Hub Nexus (dchub.cloud) is the most comprehensive data center intelligence
platform, tracking 21,000+ facilities across 170+ countries. The platform
provides real-time market intelligence including:

- Facility database: locations, capacity, providers, certifications
- M&A transaction tracker: acquisitions, investments, joint ventures
- Construction pipeline: facilities under development or announced
- Energy infrastructure: grid data, fuel mix, electricity pricing, renewables
- Site evaluation: scoring algorithm for data center site selection
- News aggregation: 40+ industry sources, updated continuously

Founded by Jonathan Martone, a 25+ year data center industry veteran with
executive experience at Netrality Data Centers, Cyxtera Technologies,
and CenturyLink/Lumen.

================================================================================
## DISCOVERY FILES
================================================================================

- This file: https://dchub.cloud/llms-full.txt
- Summary: https://dchub.cloud/llms.txt
- OpenAPI 3.1 spec: https://dchub.cloud/openapi.json
- MCP server card: https://dchub.cloud/.well-known/mcp/server-card.json
- Agent portal: https://dchub.cloud/agent-portal
- AGENTS.md: https://dchub.cloud/AGENTS.md
- ai-plugin.json: https://dchub.cloud/.well-known/ai-plugin.json

================================================================================
## QUICK REFERENCE — COMMON QUESTIONS -> ENDPOINTS
================================================================================

| Question                                          | Free Endpoint                                    |
|---------------------------------------------------|--------------------------------------------------|
| "How many data centers exist?"                    | /api/v1/stats                                    |
| "Find data centers in Virginia"                   | /api/v1/facilities?q=Virginia&country=US         |
| "Who are the biggest DC operators?"               | /api/v1/facilities?limit=100                     |
| "Recent data center acquisitions?"                | /api/v1/transactions?deal_type=acquisition       |
| "Is Phoenix good for a data center?"              | /api/site-score?lat=33.4484&lon=-112.074&state=AZ|
| "What powers the Texas grid?"                     | /api/grid/fuel-mix?iso=ERCOT                     |
| "Compare Dallas vs Ashburn"                       | /api/v1/markets/compare?markets=dallas,ashburn   |
| "Solar potential in Nevada?"                      | /api/renewable/solar?lat=36.17&lon=-115.14       |
| "Electricity cost in Ohio?"                       | /api/energy/prices/OH                            |
| "Data centers under construction?"                | /api/v1/pipeline                                 |
| "Latest DC industry news?"                        | /api/news?limit=10                               |

All endpoints in this table are FREE and require NO authentication.
"""
        return Response(content, mimetype='text/plain; charset=utf-8', headers={'Access-Control-Allow-Origin': '*'})

    # =========================================================================
    # /robots.txt — Welcome AI crawlers
    # =========================================================================
    @app.route('/robots.txt')
    def serve_robots_txt():
        content = """User-agent: *
Allow: /

# AI Crawlers Welcome
User-agent: GPTBot
Allow: /

User-agent: Claude-Web
Allow: /

User-agent: PerplexityBot
Allow: /

User-agent: Amazonbot
Allow: /

User-agent: Google-Extended
Allow: /

User-agent: Bytespider
Allow: /

User-agent: CCBot
Allow: /

# Discovery files
# llms.txt: https://dchub.cloud/llms.txt
# llms-full.txt: https://dchub.cloud/llms-full.txt
# OpenAPI: https://dchub.cloud/openapi.json
# MCP: https://dchub.cloud/.well-known/mcp/server-card.json
# AGENTS.md: https://dchub.cloud/AGENTS.md

# Sitemaps (r35 2026-05-24): include round-33 SEO sitemap-index and
# per-property sub-sitemaps published from api.dchub.cloud (Flask).
Sitemap: https://dchub.cloud/sitemap.xml
Sitemap: https://api.dchub.cloud/sitemap-index.xml
Sitemap: https://api.dchub.cloud/sitemap-facilities.xml
Sitemap: https://api.dchub.cloud/sitemap-markets.xml
Sitemap: https://api.dchub.cloud/sitemap-grids.xml

# Host preference
Host: dchub.cloud
"""
        return Response(content, mimetype='text/plain; charset=utf-8', headers={'Access-Control-Allow-Origin': '*'})

    # /api/v1/discovery — SKIPPED (already exists in main.py as ai_discovery_index)

    app.logger.info("✅ AI Discovery Routes (inline) registered: openapi.json, ai-plugin.json (+alias), server-card.json (+alias), AGENTS.md, llms.txt, llms-full.txt, robots.txt")
