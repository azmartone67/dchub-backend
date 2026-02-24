"""
DC Hub - Google & AI Platform Integration Routes 2026
=================================================
Adds structured data, Swagger UI, and enhanced discovery endpoints.

NOTE: /openapi.json is now served by ai_discovery_routes.py (v2.1.0 spec).
      This file handles schema.org, Swagger UI, and Google-specific discovery.
"""

from flask import Flask, jsonify, request, redirect
from datetime import datetime, timezone
import json


def setup_google_routes(app):
    """Register Google & AI platform integration routes."""

    BASE_URL = "https://dchub.cloud"
    FRONTEND_URL = "https://dchub.cloud"
    API_VERSION = "2.1.0"
    CONTACT_EMAIL = "info@dchub.cloud"

    # /openapi.json — REMOVED (now served by ai_discovery_routes.py with v2.1.0 spec)
    # /.well-known/openapi.json — REMOVED (same reason)

    # =========================================================================
    # ROUTE: /api/docs — Swagger UI
    # =========================================================================
    @app.route('/api/docs')
    def api_docs():
        """Serve a Swagger UI page for interactive API exploration."""
        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>DC Hub API Documentation</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.11.0/swagger-ui.min.css">
    <style>
        body {{ margin: 0; padding: 0; }}
        .swagger-ui .topbar {{ display: none; }}
        .swagger-ui .info .title {{ color: #1A5276; }}
    </style>
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/swagger-ui/5.11.0/swagger-ui-bundle.min.js"></script>
    <script>
        SwaggerUIBundle({{
            url: "{BASE_URL}/openapi.json",
            dom_id: '#swagger-ui',
            deepLinking: true,
            presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
            layout: "BaseLayout"
        }});
    </script>
</body>
</html>"""
        return html, 200, {'Content-Type': 'text/html'}

    # =========================================================================
    # ROUTE: /api/schema-org — JSON-LD structured data
    # =========================================================================
    @app.route('/api/schema-org')
    def schema_org():
        """Returns JSON-LD structured data for Google, Gemini, and search engines."""
        now = datetime.now(timezone.utc).isoformat()

        structured_data = {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "Organization",
                    "@id": f"{FRONTEND_URL}/#organization",
                    "name": "DC Hub",
                    "alternateName": "DC Hub Nexus",
                    "url": FRONTEND_URL,
                    "description": "Real-time data center intelligence platform tracking 20,000+ facilities across 140+ countries.",
                    "foundingDate": "2024",
                    "founder": {
                        "@type": "Person",
                        "name": "Jonathan Martone",
                        "jobTitle": "Founder & CEO",
                        "worksFor": [
                            {"@type": "Organization", "name": "DC Hub"},
                            {"@type": "Organization", "name": "EdgeConneX"}
                        ]
                    },
                    "knowsAbout": [
                        "Data Centers", "Colocation", "Cloud Infrastructure",
                        "Data Center M&A", "Energy Infrastructure", "Site Selection"
                    ]
                },
                {
                    "@type": "WebAPI",
                    "@id": f"{BASE_URL}/#api",
                    "name": "DC Hub Data Center Intelligence API",
                    "description": "REST API providing real-time data center facility search, M&A deal tracking, industry news, market intelligence, and energy infrastructure analysis.",
                    "url": f"{BASE_URL}/api/docs",
                    "documentation": f"{BASE_URL}/openapi.json",
                    "provider": {"@id": f"{FRONTEND_URL}/#organization"},
                    "termsOfService": f"{FRONTEND_URL}/terms"
                },
                {
                    "@type": "Dataset",
                    "@id": f"{FRONTEND_URL}/#facilities-dataset",
                    "name": "Global Data Center Facilities Database",
                    "description": "Comprehensive database of 20,000+ data center facilities across 140+ countries.",
                    "url": f"{FRONTEND_URL}/facilities",
                    "keywords": ["data centers", "colocation", "cloud infrastructure", "facility database"],
                    "creator": {"@id": f"{FRONTEND_URL}/#organization"},
                    "dateModified": now,
                    "spatialCoverage": {"@type": "Place", "name": "Global"},
                    "variableMeasured": [
                        {"@type": "PropertyValue", "name": "Facility Count", "value": "20000+"},
                        {"@type": "PropertyValue", "name": "Countries", "value": "140+"},
                        {"@type": "PropertyValue", "name": "Providers", "value": "3800+"}
                    ],
                    "distribution": {
                        "@type": "DataDownload",
                        "encodingFormat": "application/json",
                        "contentUrl": f"{BASE_URL}/api/v1/facilities"
                    },
                    "isAccessibleForFree": True
                },
                {
                    "@type": "WebSite",
                    "@id": f"{FRONTEND_URL}/#website",
                    "name": "DC Hub",
                    "url": FRONTEND_URL,
                    "publisher": {"@id": f"{FRONTEND_URL}/#organization"},
                    "potentialAction": {
                        "@type": "SearchAction",
                        "target": {"@type": "EntryPoint", "urlTemplate": f"{BASE_URL}/api/v1/facilities?q={{search_term}}"},
                        "query-input": "required name=search_term"
                    }
                }
            ]
        }

        response = jsonify(structured_data)
        response.headers['Content-Type'] = 'application/ld+json'
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Cache-Control'] = 'public, max-age=3600'
        return response

    # =========================================================================
    # ROUTE: /api/schema-org/facility — JSON-LD for a specific facility
    # =========================================================================
    @app.route('/api/schema-org/facility')
    def schema_org_facility():
        """Returns JSON-LD for a specific facility."""
        name = request.args.get('name', 'Unknown Facility')
        provider = request.args.get('provider', 'Unknown')
        city = request.args.get('city', '')
        country = request.args.get('country', '')
        lat = request.args.get('lat', '')
        lng = request.args.get('lng', '')

        facility_ld = {
            "@context": "https://schema.org",
            "@type": "Place",
            "name": name,
            "description": f"Data center facility operated by {provider} in {city}, {country}",
            "address": {"@type": "PostalAddress", "addressLocality": city, "addressCountry": country},
            "isPartOf": {"@type": "Organization", "name": provider},
            "additionalType": "https://schema.org/LocalBusiness",
            "url": f"{FRONTEND_URL}/facilities?q={name}"
        }
        if lat and lng:
            facility_ld["geo"] = {"@type": "GeoCoordinates", "latitude": lat, "longitude": lng}

        response = jsonify(facility_ld)
        response.headers['Content-Type'] = 'application/ld+json'
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response

    # =========================================================================
    # ROUTE: /api/discovery/google — Google/Gemini discovery
    # =========================================================================
    @app.route('/api/discovery/google')
    def google_discovery():
        """Google-specific discovery manifest for Vertex AI / Gemini."""
        return jsonify({
            "platform": "DC Hub Nexus",
            "version": API_VERSION,
            "target": "Google Vertex AI / Gemini",
            "openapi_spec": f"{BASE_URL}/openapi.json",
            "structured_data": f"{BASE_URL}/api/schema-org",
            "capabilities": {
                "facility_search": {
                    "description": "Search 20,000+ data center facilities worldwide",
                    "endpoint": f"{BASE_URL}/api/v1/facilities",
                    "method": "GET",
                    "params": ["q", "country", "limit"]
                },
                "transaction_tracking": {
                    "description": "M&A deals, acquisitions, and investments",
                    "endpoint": f"{BASE_URL}/api/v1/transactions",
                    "method": "GET",
                    "params": ["limit", "deal_type"]
                },
                "news_aggregation": {
                    "description": "Real-time news from 40+ industry sources",
                    "endpoint": f"{BASE_URL}/api/news",
                    "method": "GET",
                    "params": ["limit"]
                },
                "market_intelligence": {
                    "description": "Market statistics and comparisons",
                    "endpoint": f"{BASE_URL}/api/v1/markets",
                    "method": "GET"
                },
                "energy_analysis": {
                    "description": "Energy infrastructure analysis for site selection",
                    "endpoint": f"{BASE_URL}/api/v1/energy/site-analysis",
                    "method": "GET",
                    "params": ["lat", "lng", "radius"]
                },
                "site_scoring": {
                    "description": "Site suitability score (0-100) for data center development",
                    "endpoint": f"{BASE_URL}/api/site-score",
                    "method": "GET",
                    "params": ["lat", "lon", "state"]
                }
            },
            "authentication": {"required": False, "note": "All public endpoints require NO authentication"},
            "response_format": "JSON",
            "cors_enabled": True,
            "contact": CONTACT_EMAIL
        }), 200, {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'}

    # =========================================================================
    # ROUTE: /api/discovery/all — Unified discovery index
    # =========================================================================
    @app.route('/api/discovery/all')
    def discovery_all():
        """Unified discovery index for all AI platforms."""
        return jsonify({
            "platform": "DC Hub Nexus",
            "description": "Data Center Intelligence Platform — 20,000+ facilities, 140+ countries",
            "version": API_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "api_base": BASE_URL,
            "discovery_protocols": {
                "openapi": {"spec": f"{BASE_URL}/openapi.json", "docs": f"{BASE_URL}/api/docs", "version": "3.1.0"},
                "schema_org": f"{BASE_URL}/api/schema-org",
                "chatgpt_plugin": f"{BASE_URL}/.well-known/ai-plugin.json",
                "mcp_server_card": f"{BASE_URL}/.well-known/mcp/server-card.json",
                "mcp_endpoint": f"{BASE_URL}/mcp",
                "llms_txt": f"{BASE_URL}/llms.txt",
                "llms_full_txt": f"{BASE_URL}/llms-full.txt",
                "agents_md": f"{BASE_URL}/AGENTS.md",
                "robots_txt": f"{BASE_URL}/robots.txt"
            },
            "primary_endpoints": {
                "facilities": f"{BASE_URL}/api/v1/facilities?q={{query}}&country={{code}}&limit=25",
                "markets": f"{BASE_URL}/api/v1/markets",
                "market_compare": f"{BASE_URL}/api/v1/markets/compare?markets={{m1}},{{m2}}",
                "transactions": f"{BASE_URL}/api/v1/transactions?limit=20",
                "news": f"{BASE_URL}/api/news?limit=10",
                "pipeline": f"{BASE_URL}/api/v1/pipeline",
                "site_score": f"{BASE_URL}/api/site-score?lat={{lat}}&lon={{lon}}&state={{st}}",
                "grid_fuel_mix": f"{BASE_URL}/api/grid/fuel-mix?iso={{region}}",
                "energy_prices": f"{BASE_URL}/api/energy/prices/{{state}}",
                "stats": f"{BASE_URL}/api/v1/stats",
                "health": f"{BASE_URL}/api/health"
            }
        }), 200, {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*', 'Cache-Control': 'public, max-age=1800'}

    print("🔗 Google Integration Routes: ✅ Loaded")
    print("   /api/docs                  - Swagger UI")
    print("   /api/schema-org            - JSON-LD structured data")
    print("   /api/schema-org/facility   - Facility JSON-LD")
    print("   /api/discovery/google      - Google/Gemini discovery")
    print("   /api/discovery/all         - Unified discovery index")
    print("   (openapi.json now served by ai_discovery_routes.py)")
