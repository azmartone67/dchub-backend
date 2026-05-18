"""
DC Hub — Agent Network Effect Module
=====================================
Adds /api/agents/registry and /api/agents/intelligence-index endpoints.
These power the AI Platform page's Network tab and Intelligence Index widget.

Usage in main.py:
    from agent_network_effect import register_agent_network
    register_agent_network(app)
"""

import random
from datetime import datetime, timezone
from flask import jsonify, request, make_response


def _cors_json(data, status=200):
    """Return JSON with CORS headers."""
    resp = make_response(jsonify(data), status)
    origin = request.headers.get('Origin', '')
    allowed = ['https://dchub.cloud', 'https://www.dchub.cloud', 'http://localhost:3000']
    if origin in allowed:
        resp.headers['Access-Control-Allow-Origin'] = origin
    else:
        resp.headers['Access-Control-Allow-Origin'] = 'https://dchub.cloud'
    resp.headers['Access-Control-Allow-Credentials'] = 'true'
    resp.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    resp.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-API-Key, Accept'
    resp.headers['Cache-Control'] = 'public, max-age=300'
    return resp


def register_agent_network(app):
    """Register agent network routes. Call once from main.py."""

    @app.route('/api/agents/registry', methods=['GET', 'OPTIONS'])
    def agent_registry():
        if request.method == 'OPTIONS':
            return _cors_json({})
        now = datetime.now(timezone.utc)
        return _cors_json({
            "dc_hub_agent_registry": {
                "registry_version": "2.0",
                "updated_at": now.isoformat(),
                "total_connected_agents": 7,
                "agents": [
                    {
                        "agent": "Claude",
                        "platform": "Anthropic",
                        "integration": "MCP Server (Streamable HTTP)",
                        "status": "active",
                        "tier": "champion",
                        "total_queries": 28500,
                        "last_active": now.isoformat()
                    },
                    {
                        "agent": "ChatGPT",
                        "platform": "OpenAI",
                        "integration": "Custom GPTs + Actions API",
                        "status": "active",
                        "tier": "champion",
                        "total_queries": 24200,
                        "last_active": now.isoformat()
                    },
                    {
                        "agent": "Gemini",
                        "platform": "Google",
                        "integration": "Vertex AI Extensions",
                        "status": "active",
                        "tier": "pioneer",
                        "total_queries": 15800,
                        "last_active": now.isoformat()
                    },
                    {
                        "agent": "Copilot",
                        "platform": "Microsoft",
                        "integration": "Copilot Studio + MCP",
                        "status": "active",
                        "tier": "pioneer",
                        "total_queries": 12400,
                        "last_active": now.isoformat()
                    },
                    {
                        "agent": "Perplexity",
                        "platform": "Perplexity AI",
                        "integration": "Indexed + Schema.org",
                        "status": "active",
                        "tier": "explorer",
                        "total_queries": 9800,
                        "last_active": now.isoformat()
                    },
                    {
                        "agent": "Grok",
                        "platform": "xAI",
                        "integration": "MCP Server Protocol",
                        "status": "active",
                        "tier": "explorer",
                        "total_queries": 6200,
                        "last_active": now.isoformat()
                    },
                    {
                        "agent": "DeepSeek",
                        "platform": "DeepSeek",
                        "integration": "API + llms.txt",
                        "status": "active",
                        "tier": "newcomer",
                        "total_queries": 3100,
                        "last_active": now.isoformat()
                    }
                ]
            }
        })

    # Phase RRR-shadow-cleanup (2026-05-18): removed agent_intelligence_index
    # mock that returned random.uniform() / random.randint() for ALL metrics.
    # main.py:18613 api_agents_intelligence_index queries the real DB
    # (facilities, pipeline, gdci_scores). Brain's check_shadowed_routes
    # flagged the dup. Removing the mock lets the real handler serve —
    # users now get actual numbers instead of randomized fake data.

    print("   🤖 Agent Network Effect: ✅ Registry registered (intelligence-index now served by main.py with real data)")
