"""
agent_a2a.py — A2A (Agent-to-Agent) discovery surface.

Phase ZZZZZ-round36 (2026-05-24). Google's A2A protocol is gaining
traction as the "agent peer-handshake" standard. AGENTS.md is for
humans/LLMs; agent.json is for OTHER AGENTS to discover capabilities,
authentication, and skills.

Routes:
  GET /.well-known/agent.json       — A2A capability card
  GET /.well-known/agent-card.json  — alternate alias
"""
import datetime
from flask import Blueprint, jsonify

agent_a2a_bp = Blueprint("agent_a2a", __name__)


AGENT_CARD = {
    "schema_version": "1.0",
    "spec":           "A2A (Agent-to-Agent) v1",
    "agent": {
        "name":         "DC Hub Intelligence",
        "version":      "2.1.2",
        "description":  ("Data center intelligence agent — 21,000+ facilities, "
                         "M&A deals, 10 ISO grids (7 US + Hydro-Quebec + AESO + Nord Pool 15 zones), "
                         "fiber routes, water risk, tax incentives. AI-capex deal tracker. "
                         "AI Compute Capacity Index."),
        "vendor":       "DC Hub",
        "homepage":     "https://dchub.cloud",
        "contact":      "api@dchub.cloud",
        "license":      "Commercial — tier-based",
    },
    "endpoints": {
        "mcp":          "https://dchub.cloud/mcp",
        "rest":         "https://api.dchub.cloud/api/v1",
        "openapi":      "https://api.dchub.cloud/openapi-live.json",
        "llms_txt":     "https://dchub.cloud/llms.txt",
        "agents_md":    "https://dchub.cloud/AGENTS.md",
        "freshness":    "https://dchub.cloud/freshness",
        "sitemap":      "https://api.dchub.cloud/sitemap-index.xml",
    },
    "auth": {
        "modes":        ["none", "api_key", "oauth2"],
        "default":      "none",
        "api_key": {
            "scheme":   "header",
            "header":   "X-API-Key",
            "signup":   "https://dchub.cloud/signup",
            "free_tier": {"requests_per_day": 10, "no_signup": True},
        },
        "oauth2": {
            "spec":     "MCP 2025-06-18 OAuth Protected Resource",
            "metadata": "https://api.dchub.cloud/.well-known/oauth-protected-resource",
            "note":     "Enterprise tier — contact api@dchub.cloud for DCR provisioning.",
        },
    },
    "skills": [
        {
            "name":     "facility_intelligence",
            "summary":  "Search 21,000+ data center facilities, get detailed profiles, find alternatives.",
            "tools":    ["search_facilities", "get_facility", "find_alternatives", "semantic_search"],
            "examples": ["Find hyperscale campuses over 500MW in Virginia",
                          "Get full profile for facility #3000",
                          "Find 3 similar facilities to MSFT-ASH within 50 miles"],
        },
        {
            "name":     "site_planning",
            "summary":  "Score arbitrary lat/lon for data center suitability across 7 dimensions.",
            "tools":    ["analyze_site", "compare_sites", "score_facility"],
            "examples": ["Score a 50MW site at 38.95, -77.45",
                          "Compare Ashburn vs Reno vs Quincy"],
        },
        {
            "name":     "grid_intelligence",
            "summary":  "Real-time grid mix, prices, carbon intensity across 10 ISOs.",
            "tools":    ["get_grid_data", "get_grid_intelligence", "get_energy_prices"],
            "examples": ["Get current CAISO fuel mix",
                          "Hydro-Quebec carbon intensity right now",
                          "Nord Pool spot prices"],
        },
        {
            "name":     "market_ranking",
            "summary":  "Rank markets by criteria (cheapest power, most capacity, best overall).",
            "tools":    ["rank_markets", "get_market_intel"],
            "examples": ["Top 10 cheapest power markets in US",
                          "Where can 100MW land in 90 days?"],
        },
        {
            "name":     "ai_capex_intel",
            "summary":  "Hyperscaler AI deal tracker + AI compute capacity index.",
            "tools":    ["hyperscaler_deals", "ai_capacity_index"],
            "examples": ["Recent Stargate deal announcements",
                          "Where can 200MW of AI training land in 60 days?"],
            "note":     "New in r36 — endpoints live, MCP tool registration pending.",
        },
        {
            "name":     "deal_flow",
            "summary":  "$324B+ M&A history, hyperscaler capex events.",
            "tools":    ["list_transactions", "get_pipeline", "hyperscaler_deals"],
            "examples": ["All AWS acquisitions over $1B",
                          "Q1 2026 M&A in EMEA"],
        },
    ],
    "delegation": {
        "supports_a2a_handoff": False,
        "supports_sampling":    False,
        "note":                 "Resource subscriptions + sampling on roadmap Q3 2026.",
    },
    "rate_limits": {
        "free":       {"per_day": 10,    "per_minute": 5},
        "developer":  {"per_day": 1000,  "per_minute": 60},
        "pro":        {"per_day": 10000, "per_minute": 200},
        "enterprise": {"per_day": 100000, "per_minute": 1000},
    },
    "discovery_aliases": [
        "/.well-known/agent.json",
        "/.well-known/agent-card.json",
        "/.well-known/a2a.json",
    ],
}


def _card():
    out = dict(AGENT_CARD)
    out["computed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    return out


# NOTE: /.well-known/agent.json is owned by ai_agent_discovery.py
# (registered first via discovery_bp). Our richer A2A card lives at
# alternate aliases so it's discoverable without the shadow conflict.
@agent_a2a_bp.route("/.well-known/agent-card.json", methods=["GET"])
@agent_a2a_bp.route("/.well-known/a2a.json", methods=["GET"])
@agent_a2a_bp.route("/agent.json", methods=["GET"])
@agent_a2a_bp.route("/.well-known/dchub-agent.json", methods=["GET"])
def agent_card():
    return jsonify(_card()), 200, {
        "Cache-Control": "public, max-age=3600",
        "Access-Control-Allow-Origin": "*",
    }


@agent_a2a_bp.route("/.well-known/agent-card-health", methods=["GET"])
def health():
    return jsonify({"blueprint": "agent_a2a_bp", "skills": len(AGENT_CARD["skills"])}), 200
