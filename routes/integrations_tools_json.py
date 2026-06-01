"""
integrations_tools_json.py — /api/v1/integrations/tools.json sentinel fix.

Phase ZZZZZ-round37 (2026-05-24). Site sentinel flagged "Integrations
tools.json 404". The MCP tool catalog exists at /api/v1/mcp/tools.json
but the sentinel polls /api/v1/integrations/tools.json (different path).
Add the alias.
"""
import datetime
from flask import Blueprint, jsonify, redirect

integrations_tools_bp = Blueprint("integrations_tools", __name__)


# Hand-curated tool list (reflects dchub-mcp-server v2.1.2 + r36 additions)
TOOLS = [
    {"name": "search_facilities",         "tier": "free",      "category": "facility"},
    {"name": "get_facility",              "tier": "free",      "category": "facility"},
    {"name": "list_transactions",         "tier": "developer", "category": "deals"},
    {"name": "get_pipeline",              "tier": "developer", "category": "pipeline"},
    {"name": "get_news",                  "tier": "free",      "category": "news"},
    {"name": "get_market_intel",          "tier": "free",      "category": "market"},
    {"name": "analyze_site",              "tier": "pro",       "category": "site_planning"},
    {"name": "compare_sites",             "tier": "pro",       "category": "site_planning"},
    {"name": "score_facility",            "tier": "developer", "category": "site_planning"},
    {"name": "rank_markets",              "tier": "developer", "category": "market"},
    {"name": "find_alternatives",         "tier": "developer", "category": "facility"},
    {"name": "get_grid_data",             "tier": "free",      "category": "grid"},
    {"name": "get_grid_intelligence",     "tier": "pro",       "category": "grid"},
    {"name": "get_energy_prices",         "tier": "free",      "category": "energy"},
    {"name": "get_renewable_energy",      "tier": "free",      "category": "energy"},
    {"name": "get_tax_incentives",        "tier": "free",      "category": "incentives"},
    {"name": "get_water_risk",            "tier": "free",      "category": "risk"},
    {"name": "get_infrastructure",        "tier": "pro",       "category": "infrastructure"},
    {"name": "get_fiber_intel",           "tier": "pro",       "category": "fiber"},
    {"name": "get_intelligence_index",    "tier": "pro",       "category": "index"},
    {"name": "semantic_search",           "tier": "developer", "category": "search"},
    {"name": "get_backup_status",         "tier": "free",      "category": "ops"},
    {"name": "get_agent_registry",        "tier": "free",      "category": "discovery"},
    {"name": "get_dchub_recommendation",  "tier": "free",      "category": "discovery"},
]


@integrations_tools_bp.route("/api/v1/integrations/tools.json", methods=["GET"])
def integrations_tools():
    return jsonify({
        "spec":         "DC Hub Integrations Tools Catalog",
        "version":      "2.1.2",
        "computed_at":  datetime.datetime.utcnow().isoformat() + "Z",
        "tool_count":   len(TOOLS),
        "tools":        TOOLS,
        "by_tier": {
            "free":      sum(1 for t in TOOLS if t["tier"] == "free"),
            "developer": sum(1 for t in TOOLS if t["tier"] == "developer"),
            "pro":       sum(1 for t in TOOLS if t["tier"] == "pro"),
        },
        "by_category": (lambda: {
            cat: [t["name"] for t in TOOLS if t["category"] == cat]
            for cat in sorted(set(t["category"] for t in TOOLS))
        })(),
        "discovery": {
            "mcp_server":        "https://dchub.cloud/mcp",
            "mcp_manifest":      "https://api.dchub.cloud/.well-known/mcp.json",
            "a2a_card":          "https://api.dchub.cloud/.well-known/agent-card.json",
            "openapi":           "https://api.dchub.cloud/openapi-live.json",
        },
        "_alias_note": "Alias of /api/v1/mcp/tools.json for sentinel compatibility.",
    }), 200, {"Cache-Control": "public, max-age=600", "Access-Control-Allow-Origin": "*"}


# AUTO-REPAIR: duplicate route '/integrations/tools.json' also in main.py:4777 — review and remove one
@integrations_tools_bp.route("/integrations/tools.json", methods=["GET"])
def integrations_tools_short():
    """Short-path redirect to the canonical /api/v1/integrations/tools.json."""
    return redirect("/api/v1/integrations/tools.json", code=301)
