"""Agent tool manifest — REST/MCP parity map (2026-07-02).

Grok flagged: "the public REST /api/ai/query endpoints cover some functionality
but don't yet expose every MCP tool name the same way." This endpoint closes
that transparency gap: GET /api/v1/agent/tools-manifest returns, for every one
of the 53 MCP tools, the public REST endpoint it maps to (or mcp_only=true when
there is no open REST route — e.g. session/key tools and POST-only writes).

The map is EXTRACTED from the MCP server's own handler callAPI() paths (the
source of truth in dchub-mcp-server/server.mjs), then generalized to <param>
placeholders. So a REST-only agent can do what an MCP agent does: read this
once, then call REST where available and fall back to the MCP endpoint
(https://dchub.cloud/mcp) for the mcp_only tools.
"""
from __future__ import annotations

import datetime
from flask import Blueprint, jsonify

tools_manifest_bp = Blueprint("tools_manifest", __name__)

# tool -> (rest_endpoint | None, http_method). None = MCP-only (no open REST).
# Extracted from server.mjs handler callAPI paths; params generalized.
_TOOL_REST = {
    "search":                 ("/api/v1/facilities", "GET"),
    "fetch":                  ("/api/v1/facility/<id>", "GET"),
    "search_facilities":      ("/api/v1/facilities", "GET"),
    "get_facility":           ("/api/v1/facilities/<id>", "GET"),
    "get_market_intel":       ("/api/v1/markets/<slug>", "GET"),
    "get_market_dcpi_rank":   ("/api/v1/dcpi/scores/<slug>", "GET"),
    "rank_markets":           ("/api/v1/mcp/tools/rank_markets", "GET"),
    "get_gas_index":          ("/api/v1/dcgi/scores/<state>", "GET"),
    "get_gas_economics":      ("/api/v1/markets/<slug>/gas-pricing", "GET"),
    "get_gas_intelligence":   ("/api/v1/gas/intelligence/<region>", "GET"),
    "compare_isos":           ("/api/v1/dcpi/iso-comparison", "GET"),
    "list_transactions":      ("/api/v1/deals", "GET"),
    "get_news":               ("/api/news", "GET"),
    "get_pipeline":           ("/api/v1/pipeline", "GET"),
    "get_power_pipeline":     ("/api/v1/planned-generators", "GET"),
    "get_interconnection_queue": ("/api/v1/interconnection-queue/snapshot", "GET"),
    "get_grid_data":          ("/api/v1/grid/intelligence/<iso>", "GET"),
    "get_grid_intelligence":  ("/api/v1/grid/intelligence/<iso>", "GET"),
    "get_grid_scoreboard":    ("/api/v1/iso/<region>/snapshot", "GET"),
    "grid_transition_radar":  ("/api/v1/grid-transition/radar", "GET"),
    "get_changes":            ("/api/v1/changes/since", "GET"),
    "get_facility_risk_delta": ("/api/v1/facility-risk-delta", "GET"),
    "list_saved_sites":       ("/api/v1/lp/saved", "GET"),
    "analyze_site":           ("/api/site-score", "GET"),
    "compare_sites":          ("/api/site-score", "GET"),
    "get_composite_site_score": ("/api/v1/site-planner/composite-score", "GET"),
    "get_disaster_risk":      ("/api/v1/site-planner/disaster-risk", "GET"),
    "get_climate_intel":      ("/api/v1/site-planner/climate-intel", "GET"),
    "score_facility":         ("/api/v1/mcp/tools/score_facility", "GET"),
    "find_alternatives":      ("/api/v1/mcp/tools/find_alternatives", "GET"),
    "generate_site_analysis": ("/api/v1/site-report", "GET"),
    "site_selection_canvas":  ("/api/v1/site-selection/canvas", "GET"),
    "get_dchub_recommendation": ("/api/agents/recommend", "GET"),
    "get_infrastructure":     ("/api/v1/infrastructure", "GET"),
    "get_fiber_intel":        ("/api/v1/fiber/routes", "GET"),
    "get_fiber_readiness":    ("/api/infrastructure/connectivity/score", "GET"),
    "plan_fiber_leadin":      ("/api/v1/route-plan", "GET"),
    "get_energy_prices":      ("/api/v1/energy/summary", "GET"),
    "get_renewable_energy":   ("/api/v1/energy/renewable", "GET"),
    "get_tax_incentives":     ("/api/v1/tax-incentives", "GET"),
    "get_water_risk":         ("/api/v1/water/drought", "GET"),
    "ai_capacity_index":      ("/api/v1/ai-capacity-index", "GET"),
    "hyperscaler_deals":      ("/api/v1/hyperscaler-deals", "GET"),
    "deal_autopsy":           ("/api/v1/deal-autopsy", "GET"),
    "get_intelligence_index": ("/api/agents/intelligence-index", "GET"),
    "get_agent_registry":     ("/api/v1/ai-platforms/status", "GET"),
    "get_backup_status":      ("/api/health/data-freshness", "GET"),
    "why_dchub":              ("/api/v1/competitive/why-dchub", "GET"),
    # ★ 2026-09-03 — /api/v1/facilities/export has never been registered
    #   (no route definition anywhere in the repo) and answered 404 live.
    #   This map is what agents follow to find a tool's REST equivalent,
    #   so it was handing every one of them a dead rail. The real bulk
    #   export is the tier2 CSV route, verified 200 text/csv.
    "export_dataset":         ("/api/v1/mcp/tools/export_facility_csv", "GET"),
    # MCP-only (no open REST route): session/key + write/subscribe + alerts.
    # agentic wave (2026-07-18) — MCP tools shipped in dchub-mcp-server 6ba8510
    "get_permitting_intel":   ("/api/v1/permitting/intel", "GET"),
    "simulate_scenario":      ("/api/v1/agentic/scenario", "POST"),
    "research_task":          ("/api/v1/agentic/research", "POST"),
    "standing_intent":        ("/api/v1/agentic/intents", "POST"),
    "claim_free_key":         (None, "MCP"),
    "bind_email":             (None, "MCP"),
    "recover_my_key":         (None, "MCP"),
    "unlock_more_data":       (None, "MCP"),
    "save_site":              (None, "MCP"),
    "set_market_alert":       (None, "MCP"),
    "set_site_alert":         (None, "MCP"),
    "subscribe_digest":       (None, "MCP"),
}

# REST-NATIVE endpoints with NO MCP tool yet. Empty since the 2026-07-18
# agentic wave shipped MCP parity (get_permitting_intel, simulate_scenario,
# research_task, standing_intent moved into _TOOL_REST above). When a new
# backend endpoint lands before its MCP tool, list it here so REST agents
# discover it immediately; move it up once the tool ships.
_REST_NATIVE = []


@tools_manifest_bp.route("/api/v1/agent/tools-manifest", methods=["GET"])
def tools_manifest():
    tools = []
    rest_n = 0
    for name in sorted(_TOOL_REST):
        rest, method = _TOOL_REST[name]
        available = rest is not None
        if available:
            rest_n += 1
        tools.append({
            "name": name,
            "rest_endpoint": (f"https://dchub.cloud{rest}" if rest else None),
            "rest_available": available,
            "method": method,
            "mcp_only": not available,
        })
    resp = jsonify({
        "ok": True,
        "note": ("The MCP server (https://dchub.cloud/mcp) is the COMPLETE tool "
                 "surface — all tools, full parameter schemas. This manifest maps "
                 "each MCP tool to its public REST equivalent for REST-only agents; "
                 "mcp_only tools (session/key, writes, alerts) have no open REST "
                 "route by design. Prefer MCP for full parameter support."),
        "mcp_endpoint": "https://dchub.cloud/mcp",
        "tools_count": len(tools),
        "rest_available_count": rest_n,
        "mcp_only_count": len(tools) - rest_n,
        "tools": tools,
        "rest_native": _REST_NATIVE,
        "rest_native_note": ("Agent-callable REST endpoints not yet exposed as "
                             "MCP tools. Empty = full parity (the 2026-07-18 "
                             "agentic wave — permitting intel, scenario engine, "
                             "research dossiers, standing intents — is live on "
                             "both surfaces)."),
        "generated_at": datetime.datetime.utcnow().isoformat() + "Z",
    })
    resp.headers["Cache-Control"] = "public, max-age=3600"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


def register_tools_manifest(app):
    app.register_blueprint(tools_manifest_bp)
