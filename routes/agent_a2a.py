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
import os
from flask import Blueprint, jsonify

# ★ Tool count comes from the canon, never hand-typed here. PINNED (not
# resolve_canon()) for the same reason ai_interconnection.py gives: this is a
# crawler/marketplace hot path and resolve_canon() probes live HTTP.
from ai_surface_canon import PINNED as _CANON, canon_text
# ONE origin for the WorkOS AuthKit AS. `_ak` is a thin alias so each caller
# re-reads the env rather than freezing it at import -- this module's block
# below is module-level, but the securitySchemes builder runs per request.
from workos_authkit import authkit_endpoints as _ak, AUTHKIT_SCOPES  # noqa: F401

agent_a2a_bp = Blueprint("agent_a2a", __name__)


AGENT_CARD = {
    "schema_version": "1.0",
    "spec":           "A2A (Agent-to-Agent) v1",
    # Marketplace-readiness marker: OAuth2 authorization_code + DCR advertised in
    # auth.oauth2 below, wired to WorkOS AuthKit. Enables Google Cloud Marketplace
    # / Gemini Enterprise Custom-MCP OAuth data-store connect.
    "a2a_marketplace_ready": True,
    "agent": {
        "name":         "DC Hub Intelligence",
        "version":      "2.1.2",
        "description":  (canon_text("Data center intelligence agent — {canon_facilities} distinct facilities, "
                         "M&A deals, grid data across live grid operators on 5 continents "
                         "(7 US ISOs plus TVA, BPA and Ontario's IESO) and 43 US utility "
                         "balancing authorities, (Hydro-Québec, AESO, Nord Pool remain modeled), "
                         "fiber routes, water risk, tax incentives. AI-capex deal tracker. "
                         "AI Compute Capacity Index.")),
        "vendor":       "DC Hub",
        "homepage":     "https://dchub.cloud",
        "contact":      "api@dchub.cloud",
        "license":      "Commercial — tier-based",
    },
    "endpoints": {
        "mcp":          "https://dchub.cloud/mcp",
        # Full machine-readable MCP manifest — authoritative source of truth for
        # the complete live tool list (the `skills` block below is a curated
        # flagship subset, not the full tool count).
        "mcp_manifest": "https://dchub.cloud/.well-known/mcp.json",
        "rest":         "https://api.dchub.cloud/api/v1",
        "openapi":      "https://api.dchub.cloud/openapi-live.json",
        "llms_txt":     "https://dchub.cloud/llms.txt",
        "agents_md":    "https://dchub.cloud/AGENTS.md",
        "freshness":    "https://dchub.cloud/freshness",
        "sitemap":      "https://api.dchub.cloud/sitemap-index.xml",
    },
    # Live MCP tool total (source of truth = mcp_manifest above). The `skills`
    # array intentionally lists a flagship subset for readability, not all tools.
    # ★ Was hand-typed 74 and drifted: live tools/list and .well-known/mcp.json
    # both served 82 from 2026-07-31, so the marketplace card under-reported the
    # tool set for two weeks and every door's readiness check failed on it.
    # A published claim must not be a literal — read the canon.
    "mcp_tools": {
        "total":          _CANON["tools_advertised"],
        "manifest":       "https://dchub.cloud/.well-known/mcp.json",
        "skills_are_subset": True,
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
            "type":                    "oauth2",
            "grant_type":              "authorization_code",
            "flow":                    "authorization_code",
            "spec":                    "MCP 2025-06-18 OAuth Protected Resource",
            "issuer":                  _ak()["issuer"],
            "authorization_endpoint":  _ak()["authorization_endpoint"],
            "token_endpoint":          _ak()["token_endpoint"],
            # Dynamic Client Registration (RFC 7591) — required by Google Cloud
            # Marketplace / Gemini Enterprise Custom-MCP OAuth data-store connect.
            "registration_endpoint":   _ak()["registration_endpoint"],
            "scopes":                  ["openid", "profile", "email", "offline_access"],
            # RFC 8414 / RFC 9728 discovery docs (WorkOS AuthKit is the real AS).
            # metadata = our RFC 9728 protected-resource doc (served 200 at
            # api.dchub.cloud); its authorization_servers[] names the AuthKit
            # issuer below. authorization_server_metadata (RFC 8414) MUST point
            # at that issuer's OWN metadata — api.dchub.cloud does NOT serve an
            # oauth-authorization-server doc (404), so header-less hosts that
            # follow this pointer dead-ended. Point it at the AuthKit issuer,
            # which serves its /.well-known/oauth-authorization-server (SH52-015).
            "metadata":                "https://api.dchub.cloud/.well-known/oauth-protected-resource",
            "authorization_server_metadata": _ak()["authorization_server_metadata"],
            "note":     ("ENTERPRISE / marketplace path (Google Cloud Marketplace, "
                         "Gemini Enterprise). OAuth2 is ADDITIVE and OPTIONAL — the "
                         "free tier stays keyless (auth.default='none') and is NEVER "
                         "required to use OAuth. DCR provisioning is automatic via the "
                         "WorkOS registration_endpoint (RFC 7591)."),
        },
    },
    "skills": [
        {
            "name":     "facility_intelligence",
            "summary":  canon_text("Search {canon_facilities} distinct data center facilities, get detailed profiles, find alternatives."),
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
            "summary":  "Real-time grid mix, prices, carbon intensity across live grids on 5 continents + 43 US utility BAs.",
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
            "summary":  "1,400+ tracked M&A deals, hyperscaler capex events.",
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
    # r-a2a-0715: the card carried DC Hub's marketplace fields (agent{}, auth{},
    # mcp_tools{}) but not the STRICT A2A v0.3 top-level shape
    # (url/preferredTransport/protocolVersion/capabilities + skills[].id) that
    # Gemini Enterprise / Spark ingestion requires. Add them ADDITIVELY so ONE
    # card satisfies both the marketplace OAuth path and A2A v0.3 discovery —
    # nothing existing is removed.
    _agent = AGENT_CARD["agent"]
    out["protocolVersion"]    = "0.3.0"
    out["name"]               = _agent["name"]
    out["description"]        = _agent["description"]
    out["version"]            = _agent["version"]
    out["url"]                = AGENT_CARD["endpoints"]["mcp"]
    out["preferredTransport"] = "JSONRPC"
    out["provider"]           = {"organization": _agent["vendor"], "url": _agent["homepage"]}
    out["capabilities"]       = {"streaming": False, "pushNotifications": False,
                                 "stateTransitionHistory": False}
    out["defaultInputModes"]  = ["application/json"]
    out["defaultOutputModes"] = ["application/json"]
    # A2A skills require id/name/description/tags; map from the proprietary
    # name/summary/tools shape without dropping the existing fields.
    out["skills"] = [
        {**s,
         "id":          s["name"],
         "description": s.get("summary", s.get("description", "")),
         "tags":        s.get("tags") or (list(s.get("tools", [])) + [s["name"].replace("_", " ")])}
        for s in AGENT_CARD["skills"]
    ]
    # r-a2a-0725: advertise the auth surface so Gemini Enterprise Custom-MCP /
    # Spark ingestion knows how to connect. The empty {} in `security` FIRST
    # declares the ANONYMOUS free tier (no key needed for the free-tool surface) —
    # this is what lets a Gemini Enterprise "No Authentication" data store connect
    # /mcp with zero OAuth. apiKey (X-API-Key) and oauth2 (WorkOS AuthKit) follow
    # for the paid tiers. Additive — nothing existing is removed.
    # Was a SECOND origin in this same file (env-with-default, and without the
    # .strip() that a hand-pasted value needs). Now the one origin.
    _authkit = _ak()["issuer"]
    out["securitySchemes"] = {
        "apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
        "oauth2": {"type": "oauth2", "flows": {"authorizationCode": {
            "authorizationUrl": f"{_authkit}/oauth2/authorize",
            "tokenUrl": f"{_authkit}/oauth2/token",
            "refreshUrl": f"{_authkit}/oauth2/token",
            "scopes": {"openid": "", "profile": "", "email": "", "offline_access": ""}}}},
    }
    out["security"] = [{}, {"apiKey": []}, {"oauth2": ["openid", "email"]}]
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
