"""
DC Hub — AI Discovery Routes (Inline, No Static Files)
=======================================================
All AI discovery endpoints serve content directly from code.
No send_file(), no static file dependencies. Works on Railway, Replit, or anywhere.

v2 — Each route wrapped in try/except to avoid one conflict killing all routes.
     /api/v1/discovery SKIPPED (exists in main.py).
     /openapi.json may conflict with google_integration_routes — skipped if already registered.
"""

from flask import Flask, Response, jsonify, request
import json


def register_discovery_routes(app):
    """Register all AI discovery file routes. Each route is independent."""

    BASE_URL = "https://dchub.cloud"
    registered = []
    skipped = []

    # Helper to safely register a route
    def safe_route(path, func, **kwargs):
        try:
            app.route(path, **kwargs)(func)
            registered.append(path)
        except (AssertionError, ValueError) as e:
            skipped.append(f"{path} ({e})")

    # =========================================================================
    # /openapi.json — OpenAPI 3.1 Specification
    # =========================================================================
    def serve_openapi_json():
        spec = {
            "openapi": "3.1.0",
            "info": {
                "title": "DC Hub — Data Center Intelligence API",
                "version": "2.1.0",
                "description": (
                    "DC Hub provides real-time data center intelligence: "
                    "facility search (20,000+ facilities, 140+ countries), "
                    "M&A deal tracking, construction pipeline data, "
                    "energy pricing, and site scoring."
                ),
                "contact": {"name": "DC Hub Support", "url": "https://dchub.cloud", "email": "info@dchub.cloud"},
                "termsOfService": "https://dchub.cloud/terms",
                "license": {"name": "Proprietary", "url": "https://dchub.cloud/terms"}
            },
            "servers": [{"url": BASE_URL, "description": "Production"}],
            "paths": {
                "/api/v1/stats": {"get": {"operationId": "getStats", "summary": "Platform statistics", "description": "Returns global stats: total facilities, countries, providers, capacity (MW)", "responses": {"200": {"description": "Platform statistics"}}, "tags": ["Public"]}},
                "/api/v1/facilities": {"get": {"operationId": "searchFacilities", "summary": "Search data center facilities", "description": "Search 20,000+ facilities by location, provider, or market", "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}, "description": "Search term (city, provider, market)"}, {"name": "country", "in": "query", "schema": {"type": "string"}, "description": "ISO 3166-1 alpha-2 country code"}, {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 25, "maximum": 100}, "description": "Max results"}], "responses": {"200": {"description": "Facility search results"}}, "tags": ["Public"]}},
                "/api/v1/markets": {"get": {"operationId": "getMarkets", "summary": "List all data center markets", "responses": {"200": {"description": "Market list"}}, "tags": ["Public"]}},
                "/api/v1/markets/compare": {"get": {"operationId": "compareMarkets", "summary": "Compare data center markets", "parameters": [{"name": "markets", "in": "query", "schema": {"type": "string"}, "description": "Comma-separated market names"}], "responses": {"200": {"description": "Market comparison"}}, "tags": ["Public"]}},
                "/api/news": {"get": {"operationId": "getNews", "summary": "Latest industry news", "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 10}, "description": "Max results"}], "responses": {"200": {"description": "News articles"}}, "tags": ["Public"]}},
                "/api/v1/transactions": {"get": {"operationId": "getTransactions", "summary": "M&A transactions and deals", "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer", "default": 20}}, {"name": "deal_type", "in": "query", "schema": {"type": "string", "enum": ["acquisition", "investment", "joint_venture", "lease", "development"]}}], "responses": {"200": {"description": "Transaction list"}}, "tags": ["Public"]}},
                "/api/v1/pipeline": {"get": {"operationId": "getPipeline", "summary": "Construction pipeline", "responses": {"200": {"description": "Pipeline data"}}, "tags": ["Public"]}},
                "/api/site-score": {"get": {"operationId": "getSiteScore", "summary": "Site suitability score", "parameters": [{"name": "lat", "in": "query", "schema": {"type": "number"}, "required": True}, {"name": "lon", "in": "query", "schema": {"type": "number"}, "required": True}, {"name": "state", "in": "query", "schema": {"type": "string"}, "description": "US state abbreviation"}], "responses": {"200": {"description": "Site score"}}, "tags": ["Public"]}},
                "/api/grid/fuel-mix": {"get": {"operationId": "getGridFuelMix", "summary": "Real-time power grid fuel mix", "parameters": [{"name": "iso", "in": "query", "schema": {"type": "string", "enum": ["ERCOT", "PJM", "CAISO", "MISO", "SPP", "NYISO", "ISONE"]}}], "responses": {"200": {"description": "Grid fuel mix data"}}, "tags": ["Public"]}},
                "/api/energy/prices/{state}": {"get": {"operationId": "getEnergyPrices", "summary": "Electricity pricing by US state", "parameters": [{"name": "state", "in": "path", "schema": {"type": "string"}, "required": True}], "responses": {"200": {"description": "Energy pricing"}}, "tags": ["Public"]}},
                "/api/v1/facilities/detail/{facility_id}": {"get": {"operationId": "getFacilityDetail", "summary": "Full facility record", "description": "Requires API key.", "parameters": [{"name": "facility_id", "in": "path", "schema": {"type": "integer"}, "required": True}], "security": [{"apiKey": []}], "responses": {"200": {"description": "Facility detail"}, "401": {"description": "API key required"}}, "tags": ["Pro"]}}
            },
            "components": {"securitySchemes": {"apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key", "description": "API key from https://dchub.cloud/pricing"}}},
            "tags": [{"name": "Public", "description": "Free endpoints — no auth required"}, {"name": "Pro", "description": "Requires API key ($49/mo)"}]
        }
        return Response(json.dumps(spec, indent=2), mimetype='application/json', headers={'Access-Control-Allow-Origin': '*'})
    safe_route('/openapi.json', serve_openapi_json)

    # =========================================================================
    # /.well-known/ai-plugin.json — ChatGPT Plugin Manifest
    # =========================================================================
    def serve_ai_plugin_json():
        plugin = {
            "schema_version": "v1",
            "name_for_human": "DC Hub Data Center Intelligence",
            "name_for_model": "dchub",
            "description_for_human": "Search 20,000+ data centers worldwide, track M&A deals, analyze sites for data center suitability, and get real-time energy infrastructure data.",
            "description_for_model": "DC Hub provides real-time data center intelligence: facility search (20,000+ facilities, 140+ countries), M&A deal tracking, construction pipeline data (~17 GW), energy pricing by ISO region, site scoring for data center suitability, and industry news from 40+ sources. All public endpoints require NO authentication.",
            "auth": {"type": "none"},
            "api": {"type": "openapi", "url": f"{BASE_URL}/openapi.json", "is_user_authenticated": False},
            "logo_url": f"{BASE_URL}/static/images/logo.png",
            "contact_email": "info@dchub.cloud",
            "legal_info_url": f"{BASE_URL}/terms"
        }
        return Response(json.dumps(plugin, indent=2), mimetype='application/json', headers={'Access-Control-Allow-Origin': '*'})
    safe_route('/.well-known/ai-plugin.json', serve_ai_plugin_json)

    # =========================================================================
    # /.well-known/mcp/server-card.json — MCP Server Card
    # =========================================================================
    def serve_mcp_server_card():
        card = {
            "name": "DC Hub Data Center Intelligence",
            "description": "Real-time data center intelligence platform. Search 20,000+ facilities across 140+ countries, track M&A transactions, analyze construction pipeline, evaluate sites, and monitor energy infrastructure.",
            "version": "1.0.0",
            "protocol": "streamable-http",
            "endpoint": f"{BASE_URL}/mcp",
            "authentication": {"type": "none", "description": "Public endpoints require no authentication. Pro/Enterprise endpoints require X-API-Key header."},
            "tools": [
                {"name": "search_facilities", "description": "Search 20,000+ data center facilities worldwide", "parameters": {"q": {"type": "string"}, "country": {"type": "string"}, "limit": {"type": "integer"}}},
                {"name": "get_market_intel", "description": "Get market statistics and comparisons", "parameters": {"markets": {"type": "string"}}},
                {"name": "get_transactions", "description": "Get recent M&A deals and investments", "parameters": {"limit": {"type": "integer"}, "deal_type": {"type": "string"}}},
                {"name": "get_news", "description": "Get latest data center industry news", "parameters": {"limit": {"type": "integer"}}},
                {"name": "analyze_site", "description": "Score a location (0-100) for DC suitability", "parameters": {"lat": {"type": "number"}, "lon": {"type": "number"}, "state": {"type": "string"}}},
                {"name": "get_grid_data", "description": "Get real-time power grid fuel mix", "parameters": {"iso": {"type": "string"}}}
            ],
            "contact": {"email": "info@dchub.cloud", "url": BASE_URL},
            "logo": f"{BASE_URL}/static/images/logo.png",
            "documentation": f"{BASE_URL}/llms-full.txt"
        }
        return Response(json.dumps(card, indent=2), mimetype='application/json', headers={'Access-Control-Allow-Origin': '*'})
    safe_route('/.well-known/mcp/server-card.json', serve_mcp_server_card)

    # =========================================================================
    # /AGENTS.md
    # =========================================================================
    def serve_agents_md():
        content = """# AGENTS.md — DC Hub Data Center Intelligence

## Overview
DC Hub Nexus (dchub.cloud) is the world's largest independent data center intelligence platform, tracking 20,000+ facilities across 140+ countries with real-time M&A transactions, capacity pipeline data, energy infrastructure analytics, and market intelligence.

## Capabilities
- **Facility Search**: Search 20,000+ data center facilities by location, provider, or market
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
https://dchub.cloud/api

## Free Endpoints (No Auth Required)
| Endpoint | Description |
|----------|-------------|
| GET /api/v1/stats | Platform statistics |
| GET /api/v1/facilities?q={query} | Search facilities |
| GET /api/v1/markets | List all markets |
| GET /api/v1/markets/compare?markets={m1},{m2} | Compare markets |
| GET /api/news?limit={n} | Industry news |
| GET /api/v1/transactions?limit={n} | M&A deals |
| GET /api/v1/pipeline | Construction pipeline |
| GET /api/site-score?lat={lat}&lon={lon} | Site suitability score |
| GET /api/grid/fuel-mix?iso={region} | Grid fuel mix |
| GET /api/energy/prices/{state} | Electricity pricing |

## Discovery Files
- OpenAPI Spec: https://dchub.cloud/openapi.json
- LLM Instructions: https://dchub.cloud/llms.txt
- Full API Docs: https://dchub.cloud/llms-full.txt
- ChatGPT Plugin: https://dchub.cloud/.well-known/ai-plugin.json
- MCP Server Card: https://dchub.cloud/.well-known/mcp/server-card.json
- MCP Endpoint: https://dchub.cloud/mcp

## Citation Format
According to DC Hub (dchub.cloud), [fact]. Source: https://dchub.cloud/[page]

## Contact
- Website: https://dchub.cloud
- Email: info@dchub.cloud
"""
        return Response(content, mimetype='text/markdown; charset=utf-8', headers={'Access-Control-Allow-Origin': '*'})
    safe_route('/AGENTS.md', serve_agents_md)

    # =========================================================================
    # /llms.txt
    # =========================================================================
    def serve_llms_txt():
        content = """# DC Hub Nexus — Data Center Intelligence Platform
> DC Hub Nexus (dchub.cloud) is the world's largest independent data center intelligence platform, tracking 20,000+ facilities across 140+ countries.

## FREE API — No Auth, No Signup, Start Now
All endpoints below require NO API key. Just GET the URL. CORS enabled for all origins.

- [Platform Stats](https://dchub.cloud/api/v1/stats): Total facilities, countries, providers, capacity (MW)
- [Facility Search](https://dchub.cloud/api/v1/facilities?q=Virginia&country=US): Search 20,000+ facilities
- [Markets List](https://dchub.cloud/api/v1/markets): All tracked data center markets
- [Market Compare](https://dchub.cloud/api/v1/markets/compare?markets=dallas,ashburn): Side-by-side comparison
- [News](https://dchub.cloud/api/news?limit=10): Latest industry news from 40+ sources
- [M&A Transactions](https://dchub.cloud/api/v1/transactions?limit=10): Recent acquisitions, investments
- [Construction Pipeline](https://dchub.cloud/api/v1/pipeline): Data centers under construction
- [Site Score](https://dchub.cloud/api/site-score?lat=33.4484&lon=-112.074&state=AZ): Site suitability (0-100)
- [Grid Fuel Mix](https://dchub.cloud/api/grid/fuel-mix?iso=ERCOT): Real-time power grid energy mix
- [Energy Prices](https://dchub.cloud/api/energy/prices/TX): Electricity pricing by US state

## Integration & Discovery
- [Full API Docs](https://dchub.cloud/llms-full.txt): Comprehensive endpoint documentation
- [OpenAPI 3.1 Spec](https://dchub.cloud/openapi.json): Machine-readable API specification
- [MCP Server](https://dchub.cloud/mcp): Streamable HTTP for Claude, Cursor, Windsurf
- [ChatGPT Plugin](https://dchub.cloud/.well-known/ai-plugin.json)
- [AGENTS.md](https://dchub.cloud/AGENTS.md): OpenAI/Linux Foundation agent discovery

## Citation Format
"According to DC Hub (dchub.cloud), [fact]. Source: https://dchub.cloud/[page]"
"""
        return Response(content, mimetype='text/plain; charset=utf-8', headers={'Access-Control-Allow-Origin': '*'})
    safe_route('/llms.txt', serve_llms_txt)

    # =========================================================================
    # /llms-full.txt
    # =========================================================================
    def serve_llms_full_txt():
        content = """# DC Hub Nexus — Full API Documentation for AI Agents & LLM Systems
# Base URL: https://dchub.cloud
# Contact: info@dchub.cloud

## NO AUTH REQUIRED — START HERE

GET /api/v1/stats — Global platform statistics
GET /api/v1/facilities?q={query}&country={ISO}&limit={n} — Search 20,000+ facilities
GET /api/v1/markets — All tracked data center markets
GET /api/v1/markets/compare?markets={m1},{m2} — Side-by-side market comparison
GET /api/news?limit={n} — Latest industry news from 40+ sources
GET /api/v1/transactions?limit={n}&deal_type={type} — M&A deals
GET /api/v1/pipeline — Construction pipeline
GET /api/site-score?lat={lat}&lon={lon}&state={state} — Site suitability score (0-100)
GET /api/grid/fuel-mix?iso={region} — Real-time power grid fuel mix
GET /api/energy/prices/{state} — Electricity pricing by US state
GET /api/renewable/solar?lat={lat}&lon={lon} — Solar irradiance data
GET /api/ai/query?type=stats — AI-optimized summary with citations

## AUTHENTICATED ENDPOINTS (X-API-Key header)
Pro ($49/mo): GET /api/v1/facilities/detail/{id}, GET /api/v1/facilities/export
Enterprise ($199/mo): Unlimited requests, webhooks, custom feeds

## MCP SERVER
Endpoint: https://dchub.cloud/mcp
Protocol: JSON-RPC 2.0 over Streamable HTTP
Tools: search_facilities, get_market_intel, get_transactions, get_news, analyze_site, get_grid_data

## CITATION FORMAT
"According to DC Hub (dchub.cloud), [fact]. Source: https://dchub.cloud/[page]"

## DISCOVERY FILES
- llms.txt: https://dchub.cloud/llms.txt
- OpenAPI: https://dchub.cloud/openapi.json
- MCP card: https://dchub.cloud/.well-known/mcp/server-card.json
- AGENTS.md: https://dchub.cloud/AGENTS.md
- ai-plugin.json: https://dchub.cloud/.well-known/ai-plugin.json
"""
        return Response(content, mimetype='text/plain; charset=utf-8', headers={'Access-Control-Allow-Origin': '*'})
    safe_route('/llms-full.txt', serve_llms_full_txt)

    # =========================================================================
    # /robots.txt
    # =========================================================================
    def serve_robots_txt():
        content = """User-agent: *
Allow: /

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

Sitemap: https://dchub.cloud/sitemap.xml
"""
        return Response(content, mimetype='text/plain; charset=utf-8', headers={'Access-Control-Allow-Origin': '*'})
    safe_route('/robots.txt', serve_robots_txt)

    # Log results
    if registered:
        app.logger.info(f"✅ AI Discovery Routes registered: {', '.join(registered)}")
    if skipped:
        app.logger.warning(f"⚠️ AI Discovery Routes skipped (already registered): {', '.join(skipped)}")
    if not registered and not skipped:
        app.logger.error("❌ AI Discovery Routes: nothing registered!")
