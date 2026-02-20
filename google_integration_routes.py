"""
DC Hub - Google & AI Platform Integration Routes
=================================================
Adds OpenAPI 3.1 spec, structured data, and enhanced discovery endpoints.

INSTALLATION:
1. Copy this file to your Replit project (same folder as main.py)
2. Add this import near the top of main.py:
   from google_integration_routes import setup_google_routes
3. Add this line after your app = Flask(__name__) and CORS setup:
   setup_google_routes(app)
4. Restart Replit

NEW ENDPOINTS:
  /openapi.json              - OpenAPI 3.1 spec (for Gemini, APIs.guru, Swagger UI)
  /api/docs                  - Redirect to interactive API docs
  /api/schema-org            - JSON-LD structured data (Organization + Dataset + API)
  /api/schema-org/facility   - JSON-LD for a specific facility (for rich results)
  /api/discovery/google      - Google-specific discovery manifest
  /api/discovery/all         - Unified discovery index for all AI platforms
  /.well-known/openapi.json  - Alias for /openapi.json (standard location)
"""

from flask import Flask, jsonify, request, redirect
from datetime import datetime, timezone
import json


def setup_google_routes(app):
    """Register all Google & AI platform integration routes."""

    # =========================================================================
    # CONFIG - Update these to match your deployment
    # =========================================================================
    BASE_URL = "https://dchub.cloud"
    FRONTEND_URL = "https://dchub.cloud"
    API_VERSION = "2.0.0"
    CONTACT_EMAIL = "jonathan@dchub.cloud"  # Update if different

    # =========================================================================
    # OpenAPI 3.1 Specification
    # =========================================================================
    def get_openapi_spec():
        """Generate the full OpenAPI 3.1 specification."""
        return {
            "openapi": "3.1.0",
            "info": {
                "title": "DC Hub - Data Center Intelligence API",
                "description": (
                    "Real-time data center intelligence platform tracking 20,534+ "
                    "facilities across 140+ countries. Provides facility search, "
                    "M&A deal tracking, industry news, market intelligence reports, "
                    "and energy infrastructure data for AI platforms, developers, "
                    "and enterprise users."
                ),
                "version": API_VERSION,
                "contact": {
                    "name": "DC Hub API Support",
                    "url": FRONTEND_URL,
                    "email": CONTACT_EMAIL
                },
                "license": {
                    "name": "Proprietary - Free tier available",
                    "url": f"{FRONTEND_URL}/terms"
                },
                "x-logo": {
                    "url": f"{FRONTEND_URL}/assets/dc-hub-logo.png",
                    "altText": "DC Hub Logo"
                }
            },
            "servers": [
                {
                    "url": BASE_URL,
                    "description": "Production API Server"
                }
            ],
            "tags": [
                {"name": "Facilities", "description": "Search and retrieve data center facility information"},
                {"name": "Transactions", "description": "M&A deals, acquisitions, and investment tracking"},
                {"name": "News", "description": "Real-time industry news from 40+ sources"},
                {"name": "Market Intelligence", "description": "Daily market reports and statistics"},
                {"name": "Energy", "description": "Energy infrastructure data for site selection"},
                {"name": "Discovery", "description": "API discovery and capabilities"},
                {"name": "Health", "description": "API status and health checks"}
            ],
            "paths": {
                "/api/agent/facilities": {
                    "get": {
                        "tags": ["Facilities"],
                        "summary": "Search data center facilities",
                        "description": (
                            "Search across 20,534+ data center facilities worldwide. "
                            "Filter by name, provider, city, or country. Returns structured "
                            "facility data including coordinates, provider info, and metadata."
                        ),
                        "operationId": "searchFacilities",
                        "parameters": [
                            {
                                "name": "q",
                                "in": "query",
                                "description": "Search query - matches facility name, provider, or city",
                                "required": False,
                                "schema": {"type": "string"},
                                "example": "Equinix"
                            },
                            {
                                "name": "country",
                                "in": "query",
                                "description": "Filter by ISO 3166-1 alpha-2 country code",
                                "required": False,
                                "schema": {"type": "string"},
                                "example": "US"
                            },
                            {
                                "name": "limit",
                                "in": "query",
                                "description": "Maximum number of results (1-100)",
                                "required": False,
                                "schema": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100}
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "List of matching facilities",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "facilities": {
                                                    "type": "array",
                                                    "items": {
                                                        "type": "object",
                                                        "properties": {
                                                            "name": {"type": "string"},
                                                            "provider": {"type": "string"},
                                                            "city": {"type": "string"},
                                                            "country": {"type": "string"},
                                                            "latitude": {"type": "number"},
                                                            "longitude": {"type": "number"}
                                                        }
                                                    }
                                                },
                                                "total": {"type": "integer"},
                                                "source": {"type": "string"}
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                },
                "/api/transactions": {
                    "get": {
                        "tags": ["Transactions"],
                        "summary": "Get M&A transactions and deals",
                        "description": (
                            "Retrieve data center M&A transactions, acquisitions, "
                            "and investments. Covers $10.6B+ in tracked deal volume."
                        ),
                        "operationId": "getTransactions",
                        "parameters": [
                            {
                                "name": "limit",
                                "in": "query",
                                "description": "Maximum number of results",
                                "required": False,
                                "schema": {"type": "integer", "default": 50}
                            },
                            {
                                "name": "deal_type",
                                "in": "query",
                                "description": "Filter by deal type",
                                "required": False,
                                "schema": {
                                    "type": "string",
                                    "enum": ["acquisition", "investment", "joint_venture", "expansion", "capex"]
                                }
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "List of M&A transactions",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "transactions": {"type": "array"},
                                                "total": {"type": "integer"}
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                },
                "/api/news": {
                    "get": {
                        "tags": ["News"],
                        "summary": "Get latest data center industry news",
                        "description": (
                            "Real-time news aggregation from 40+ industry sources "
                            "including Data Center Dynamics, DCK, and DatacenterFrontier. "
                            "Refreshed every 60 seconds."
                        ),
                        "operationId": "getNews",
                        "parameters": [
                            {
                                "name": "limit",
                                "in": "query",
                                "description": "Maximum number of articles",
                                "required": False,
                                "schema": {"type": "integer", "default": 20}
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "List of news articles",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "articles": {"type": "array"},
                                                "source_count": {"type": "integer"}
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                },
                "/api/market-report": {
                    "get": {
                        "tags": ["Market Intelligence"],
                        "summary": "Get latest daily market intelligence report",
                        "description": (
                            "Comprehensive daily market report including facility counts, "
                            "capacity data, M&A activity, and market trends."
                        ),
                        "operationId": "getMarketReport",
                        "responses": {
                            "200": {
                                "description": "Latest market intelligence report",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "total_facilities": {"type": "integer"},
                                                "countries_covered": {"type": "integer"},
                                                "providers_tracked": {"type": "integer"},
                                                "total_power_capacity_mw": {"type": "number"},
                                                "ma_deals": {"type": "integer"},
                                                "deal_value_usd": {"type": "string"}
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                },
                "/api/stats": {
                    "get": {
                        "tags": ["Market Intelligence"],
                        "summary": "Get platform statistics",
                        "description": "Returns high-level platform statistics including facility count, countries, and markets.",
                        "operationId": "getStats",
                        "responses": {
                            "200": {
                                "description": "Platform statistics"
                            }
                        }
                    }
                },
                "/api/v1/energy/site-analysis": {
                    "get": {
                        "tags": ["Energy"],
                        "summary": "Energy infrastructure site analysis",
                        "description": (
                            "Analyze energy infrastructure around a geographic point. "
                            "Returns nearby power plants, substations, transmission lines, "
                            "and gas pipelines from HIFLD, EIA, and state databases."
                        ),
                        "operationId": "siteAnalysis",
                        "parameters": [
                            {
                                "name": "lat",
                                "in": "query",
                                "required": True,
                                "schema": {"type": "number"},
                                "example": 33.45
                            },
                            {
                                "name": "lng",
                                "in": "query",
                                "required": True,
                                "schema": {"type": "number"},
                                "example": -112.07
                            },
                            {
                                "name": "radius",
                                "in": "query",
                                "description": "Search radius in meters",
                                "required": False,
                                "schema": {"type": "integer", "default": 25000}
                            }
                        ],
                        "responses": {
                            "200": {
                                "description": "Energy infrastructure analysis results"
                            }
                        }
                    }
                },
                "/api/agent/capabilities": {
                    "get": {
                        "tags": ["Discovery"],
                        "summary": "Get API capabilities and endpoint directory",
                        "description": "Returns the full specification of available endpoints, authentication, and discovery files.",
                        "operationId": "getCapabilities",
                        "responses": {
                            "200": {
                                "description": "API capabilities manifest"
                            }
                        }
                    }
                },
                "/api/health": {
                    "get": {
                        "tags": ["Health"],
                        "summary": "API health check",
                        "description": "Returns API status, version, and timestamp.",
                        "operationId": "healthCheck",
                        "responses": {
                            "200": {
                                "description": "Health status"
                            }
                        }
                    }
                }
            },
            "components": {
                "securitySchemes": {
                    "MoltbookIdentity": {
                        "type": "apiKey",
                        "in": "header",
                        "name": "X-Moltbook-Identity",
                        "description": "Optional JWT token for authenticated access with enhanced data and higher rate limits."
                    }
                }
            },
            "security": [],
            "externalDocs": {
                "description": "DC Hub Full Documentation",
                "url": f"{FRONTEND_URL}/llms-full.txt"
            }
        }

    # =========================================================================
    # ROUTE: /openapi.json
    # =========================================================================
    @app.route('/openapi.json')
    @app.route('/.well-known/openapi.json')
    def openapi_spec():
        """Serve the OpenAPI 3.1 specification."""
        spec = get_openapi_spec()
        response = jsonify(spec)
        response.headers['Content-Type'] = 'application/json'
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Cache-Control'] = 'public, max-age=3600'
        return response

    # =========================================================================
    # ROUTE: /api/docs
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
    # ROUTE: /api/schema-org
    # =========================================================================
    @app.route('/api/schema-org')
    def schema_org():
        """
        Returns JSON-LD structured data for Google, Gemini, and search engines.
        Combines Organization, WebAPI, and Dataset schemas.
        Embed this in your Cloudflare Pages <head> for maximum SEO impact.
        """
        now = datetime.now(timezone.utc).isoformat()

        structured_data = {
            "@context": "https://schema.org",
            "@graph": [
                # Organization
                {
                    "@type": "Organization",
                    "@id": f"{FRONTEND_URL}/#organization",
                    "name": "DC Hub",
                    "alternateName": "DCHub",
                    "url": FRONTEND_URL,
                    "description": (
                        "Real-time data center intelligence platform tracking "
                        "20,534+ facilities across 140+ countries."
                    ),
                    "foundingDate": "2024",
                    "founder": {
                        "@type": "Person",
                        "name": "Jonathan Martinez",
                        "jobTitle": "Founder & Technical Consultant",
                        "worksFor": [
                            {"@type": "Organization", "name": "DC Hub"},
                            {"@type": "Organization", "name": "EdgeConneX"}
                        ]
                    },
                    "sameAs": [
                        f"{FRONTEND_URL}",
                        f"{BASE_URL}"
                    ],
                    "knowsAbout": [
                        "Data Centers",
                        "Colocation",
                        "Cloud Infrastructure",
                        "Data Center M&A",
                        "Energy Infrastructure",
                        "Site Selection",
                        "Power Grid Analysis"
                    ]
                },
                # WebAPI (the API itself)
                {
                    "@type": "WebAPI",
                    "@id": f"{BASE_URL}/#api",
                    "name": "DC Hub Data Center Intelligence API",
                    "description": (
                        "REST API providing real-time data center facility search, "
                        "M&A deal tracking, industry news, market intelligence, "
                        "and energy infrastructure analysis."
                    ),
                    "url": f"{BASE_URL}/api/docs",
                    "documentation": f"{BASE_URL}/openapi.json",
                    "provider": {"@id": f"{FRONTEND_URL}/#organization"},
                    "termsOfService": f"{FRONTEND_URL}/terms",
                    "availableChannel": {
                        "@type": "ServiceChannel",
                        "serviceUrl": BASE_URL,
                        "serviceType": "REST API"
                    }
                },
                # Dataset - Facilities
                {
                    "@type": "Dataset",
                    "@id": f"{FRONTEND_URL}/#facilities-dataset",
                    "name": "Global Data Center Facilities Database",
                    "description": (
                        "Comprehensive database of 20,534+ data center facilities "
                        "across 140+ countries with 3,800+ providers. Includes "
                        "facility name, provider, location, coordinates, and metadata."
                    ),
                    "url": f"{FRONTEND_URL}/facilities",
                    "keywords": [
                        "data centers", "colocation", "cloud infrastructure",
                        "data center locations", "facility database",
                        "global data centers", "data center providers"
                    ],
                    "creator": {"@id": f"{FRONTEND_URL}/#organization"},
                    "dateModified": now,
                    "temporalCoverage": f"2024/{datetime.now().year}",
                    "spatialCoverage": {
                        "@type": "Place",
                        "name": "Global",
                        "geo": {
                            "@type": "GeoShape",
                            "description": "140+ countries worldwide"
                        }
                    },
                    "variableMeasured": [
                        {"@type": "PropertyValue", "name": "Facility Count", "value": "20534+"},
                        {"@type": "PropertyValue", "name": "Countries", "value": "140+"},
                        {"@type": "PropertyValue", "name": "Providers", "value": "3800+"}
                    ],
                    "distribution": {
                        "@type": "DataDownload",
                        "encodingFormat": "application/json",
                        "contentUrl": f"{BASE_URL}/api/agent/facilities"
                    },
                    "isAccessibleForFree": True,
                    "license": f"{FRONTEND_URL}/terms"
                },
                # Dataset - M&A Transactions
                {
                    "@type": "Dataset",
                    "@id": f"{FRONTEND_URL}/#transactions-dataset",
                    "name": "Data Center M&A Transaction Database",
                    "description": (
                        "Tracked M&A deals, acquisitions, investments, and joint "
                        "ventures in the data center industry. 787+ transactions "
                        "with $10.6B+ in tracked deal volume."
                    ),
                    "url": f"{FRONTEND_URL}/transactions",
                    "keywords": [
                        "data center M&A", "data center acquisitions",
                        "infrastructure investment", "data center deals"
                    ],
                    "creator": {"@id": f"{FRONTEND_URL}/#organization"},
                    "dateModified": now,
                    "variableMeasured": [
                        {"@type": "PropertyValue", "name": "Total Deals", "value": "787+"},
                        {"@type": "PropertyValue", "name": "Deal Volume", "value": "$10.6B+"}
                    ],
                    "distribution": {
                        "@type": "DataDownload",
                        "encodingFormat": "application/json",
                        "contentUrl": f"{BASE_URL}/api/transactions"
                    },
                    "isAccessibleForFree": True
                },
                # WebSite
                {
                    "@type": "WebSite",
                    "@id": f"{FRONTEND_URL}/#website",
                    "name": "DC Hub",
                    "url": FRONTEND_URL,
                    "publisher": {"@id": f"{FRONTEND_URL}/#organization"},
                    "potentialAction": {
                        "@type": "SearchAction",
                        "target": {
                            "@type": "EntryPoint",
                            "urlTemplate": f"{BASE_URL}/api/agent/facilities?q={{search_term}}"
                        },
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
    # ROUTE: /api/schema-org/facility
    # =========================================================================
    @app.route('/api/schema-org/facility')
    def schema_org_facility():
        """
        Returns JSON-LD for a specific facility.
        Usage: /api/schema-org/facility?name=Equinix+SV5&provider=Equinix&city=San+Jose&country=US&lat=37.3382&lng=-121.8863
        """
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
            "address": {
                "@type": "PostalAddress",
                "addressLocality": city,
                "addressCountry": country
            },
            "geo": {
                "@type": "GeoCoordinates",
                "latitude": lat,
                "longitude": lng
            } if lat and lng else None,
            "isPartOf": {
                "@type": "Organization",
                "name": provider
            },
            "additionalType": "https://schema.org/LocalBusiness",
            "url": f"{FRONTEND_URL}/facilities?q={name}"
        }

        # Remove None values
        facility_ld = {k: v for k, v in facility_ld.items() if v is not None}

        response = jsonify(facility_ld)
        response.headers['Content-Type'] = 'application/ld+json'
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response

    # =========================================================================
    # ROUTE: /api/discovery/google
    # =========================================================================
    @app.route('/api/discovery/google')
    def google_discovery():
        """
        Google-specific discovery manifest.
        Optimized for Vertex AI Extensions and Gemini integration.
        """
        return jsonify({
            "platform": "DC Hub",
            "version": API_VERSION,
            "target": "Google Vertex AI / Gemini",
            "openapi_spec": f"{BASE_URL}/openapi.json",
            "structured_data": f"{BASE_URL}/api/schema-org",
            "capabilities": {
                "facility_search": {
                    "description": "Search 20,534+ data center facilities worldwide",
                    "endpoint": f"{BASE_URL}/api/agent/facilities",
                    "method": "GET",
                    "params": ["q", "country", "limit"],
                    "example": f"{BASE_URL}/api/agent/facilities?q=Equinix&country=US&limit=5"
                },
                "transaction_tracking": {
                    "description": "787+ M&A deals, $10.6B+ volume",
                    "endpoint": f"{BASE_URL}/api/transactions",
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
                    "description": "Daily market reports with capacity and pipeline data",
                    "endpoint": f"{BASE_URL}/api/market-report",
                    "method": "GET"
                },
                "energy_analysis": {
                    "description": "Energy infrastructure analysis for site selection",
                    "endpoint": f"{BASE_URL}/api/v1/energy/site-analysis",
                    "method": "GET",
                    "params": ["lat", "lng", "radius"]
                }
            },
            "authentication": {
                "required": False,
                "optional_header": "X-Moltbook-Identity",
                "benefits": "Enhanced data, higher rate limits, agent karma tracking"
            },
            "data_freshness": {
                "news": "Every 60 seconds",
                "facilities": "Daily auto-discovery",
                "transactions": "Hourly deal extraction",
                "market_reports": "Daily generation"
            },
            "response_format": "JSON",
            "cors_enabled": True,
            "rate_limit": "100 requests/minute (unauthenticated)",
            "contact": CONTACT_EMAIL
        }), 200, {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*'
        }

    # =========================================================================
    # ROUTE: /api/discovery/all
    # =========================================================================
    @app.route('/api/discovery/all')
    def discovery_all():
        """
        Unified discovery index for all AI platforms.
        Shows every discovery file and protocol DC Hub supports.
        """
        return jsonify({
            "platform": "DC Hub",
            "description": "Data Center Intelligence Platform - 20,534+ facilities, 140+ countries",
            "version": API_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "api_base": BASE_URL,
            "frontend": FRONTEND_URL,
            "discovery_protocols": {
                "openapi": {
                    "spec": f"{BASE_URL}/openapi.json",
                    "docs": f"{BASE_URL}/api/docs",
                    "version": "3.1.0"
                },
                "schema_org": {
                    "organization": f"{BASE_URL}/api/schema-org",
                    "facility": f"{BASE_URL}/api/schema-org/facility?name=Example"
                },
                "chatgpt_plugin": f"{FRONTEND_URL}/.well-known/ai-plugin.json",
                "copilot_agent": f"{FRONTEND_URL}/.well-known/copilot-agent.json",
                "google_a2a": f"{FRONTEND_URL}/.well-known/agent.json",
                "claude_mcp": f"{FRONTEND_URL}/.well-known/mcp.json",
                "llms_txt": f"{FRONTEND_URL}/llms.txt",
                "llms_full_txt": f"{FRONTEND_URL}/llms-full.txt",
                "agents_md": f"{FRONTEND_URL}/AGENTS.md",
                "skill_md": f"{FRONTEND_URL}/skill.md",
                "skill_json": f"{FRONTEND_URL}/skill.json",
                "ai_txt": f"{FRONTEND_URL}/ai.txt",
                "robots_txt": f"{FRONTEND_URL}/robots.txt",
                "sitemap": f"{FRONTEND_URL}/sitemap.xml",
                "security_txt": f"{FRONTEND_URL}/.well-known/security.txt"
            },
            "platform_specific": {
                "google_gemini": f"{BASE_URL}/api/discovery/google",
                "all_capabilities": f"{BASE_URL}/api/agent/capabilities",
                "agent_stats": f"{BASE_URL}/api/agent/stats"
            },
            "primary_endpoints": {
                "facilities": f"{BASE_URL}/api/agent/facilities?q={{query}}&country={{code}}&limit=20",
                "transactions": f"{BASE_URL}/api/transactions?limit=50",
                "news": f"{BASE_URL}/api/news?limit=20",
                "market_report": f"{BASE_URL}/api/market-report",
                "stats": f"{BASE_URL}/api/stats",
                "energy": f"{BASE_URL}/api/v1/energy/site-analysis?lat={{lat}}&lng={{lng}}&radius=25000",
                "health": f"{BASE_URL}/api/health"
            }
        }), 200, {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Cache-Control': 'public, max-age=1800'
        }

    print("🔗 Google Integration Routes: ✅ Loaded")
    print("   /openapi.json              - OpenAPI 3.1 spec")
    print("   /api/docs                  - Swagger UI")
    print("   /api/schema-org            - JSON-LD structured data")
    print("   /api/schema-org/facility   - Facility JSON-LD")
    print("   /api/discovery/google      - Google/Gemini discovery")
    print("   /api/discovery/all         - Unified discovery index")
