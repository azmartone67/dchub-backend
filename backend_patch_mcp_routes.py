from flask import jsonify, request
from datetime import datetime, timezone
import logging

logger = logging.getLogger("mcp_v1_routes")

SERVER_CARD = {
    "schema_version": "2024-11-05",
    "name": "DC Hub MCP Server",
    "description": "Data center intelligence for AI agents",
    "url": "https://dchub.cloud",
    "mcp_endpoint": "https://dchub.cloud/mcp",
    "version": "2.1.0",
    "capabilities": {"tools": True, "resources": True, "prompts": True},
    "authentication": {"type": "api_key", "header": "Authorization", "prefix": "Bearer"},
    "tools": [
        {"name": "search_facilities", "description": "Search 21,000+ data center facilities worldwide"},
        {"name": "get_facility", "description": "Get detailed facility profile by ID"},
        {"name": "list_transactions", "description": "List M&A deals and transactions"},
        {"name": "get_pipeline", "description": "Get construction pipeline data"},
        {"name": "get_market_intel", "description": "Get market intelligence for a region"},
        {"name": "get_grid_data", "description": "Get power grid data by region"},
        {"name": "get_energy_prices", "description": "Get energy pricing data"},
        {"name": "get_news", "description": "Get latest data center industry news"}
    ],
    "contact": {"name": "DC Hub", "url": "https://dchub.cloud/connect", "email": "support@dchub.cloud"}
}


def register_mcp_v1_routes(app):
# AUTO-REPAIR: duplicate route '/api/v1/mcp/analytics' also in main.py:12920 — review and remove one
    @app.route("/api/v1/mcp/analytics", methods=["GET"])
    def mcp_v1_analytics():
        try:
            hours = request.args.get("hours", 24, type=int)
            try:
                from mcp_gateway import MCPGateway
                gw = getattr(app, '_mcp_gateway', None)
                if gw:
                    return jsonify({
                        "requests": gw.db.get_request_analytics(hours),
                        "discovery": gw.db.get_discovery_analytics(hours),
                        "period_hours": hours,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    })
            except (ImportError, AttributeError):
                pass
            return jsonify({
                "requests": {"total": 0, "by_platform": {}, "by_endpoint": {}, "by_hour": []},
                "discovery": {"total_hits": 0, "by_file": {}, "unique_agents": 0},
                "period_hours": hours,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "note": "Analytics data initializing"
            })
        except Exception as e:
            logger.error(f"mcp_v1_analytics error: {e}")
            return jsonify({"requests": {"total": 0}, "discovery": {"total_hits": 0}, "period_hours": 24, "error": str(e)}), 200
# AUTO-REPAIR: duplicate route '/api/v1/mcp/platforms' also in main.py:13002 — review and remove one

    @app.route("/api/v1/mcp/platforms", methods=["GET"])
    def mcp_v1_platforms():
        try:
            try:
                from mcp_gateway import MCPGateway
                gw = getattr(app, '_mcp_gateway', None)
                if gw:
                    return jsonify({
                        "registered": gw.db.get_platform_stats(),
                        "unknown_agents": gw.db.get_unknown_agents(),
                        "total_registered": len(gw.db.get_platform_stats()),
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    })
            except (ImportError, AttributeError):
                pass
            platforms = [
                {"id": "claude_desktop", "name": "Claude Desktop", "protocol": "mcp_native", "status": "supported"},
                {"id": "chatgpt_plugins", "name": "ChatGPT / Plugins", "protocol": "openapi", "status": "supported"},
                {"id": "perplexity", "name": "Perplexity", "protocol": "openapi", "status": "supported"},
                {"id": "copilot_studio", "name": "Copilot Studio", "protocol": "openapi", "status": "supported"},
                {"id": "gemini", "name": "Google Gemini", "protocol": "openapi", "status": "supported"},
                {"id": "grok", "name": "Grok / xAI", "protocol": "openapi", "status": "supported"},
                {"id": "cursor", "name": "Cursor", "protocol": "mcp_native", "status": "supported"},
                {"id": "smithery", "name": "Smithery", "protocol": "mcp_registry", "status": "supported"},
                {"id": "windsurf", "name": "Windsurf", "protocol": "mcp_native", "status": "supported"},
                {"id": "continue_dev", "name": "Continue.dev", "protocol": "mcp_native", "status": "supported"},
            ]
            return jsonify({"registered": platforms, "unknown_agents": [], "total_registered": len(platforms), "timestamp": datetime.now(timezone.utc).isoformat()})
        except Exception as e:
            logger.error(f"mcp_v1_platforms error: {e}")
            return jsonify({"registered": [], "unknown_agents": [], "total_registered": 0, "error": str(e)}), 200

    @app.route("/.well-known/mcp/server-card.json", methods=["GET"])
    def mcp_server_card():
        return jsonify(SERVER_CARD)

    logger.info("MCP v1 routes registered: /api/v1/mcp/analytics, /api/v1/mcp/platforms, /.well-known/mcp/server-card.json")
