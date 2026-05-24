"""
surface_registrations_batch.py — Phase r33 (2026-05-24).

Bulk surface_brain registration for the 59 sentinel-manifest pages that
were showing as "orphan" in /api/v1/sentinel/page-integrity. An orphan
in the integrity scoring is a page that is healthy + probed by sentinel
but has NO surface_brain registration — meaning the brain doesn't see
engagement, can't learn what users do there, and can't include it in
the surfaces dashboard.

Each call to register_surface() adds an entry keyed by surface_id with
a routes list that page_integrity matches against. We use `auto_*`
prefixes on every surface_id here so we never collide with the
hand-curated registrations already scattered across main.py + individual
route files (which use names like "transactions", "transparency",
"power_totals", etc.).

This is bulk-metadata only — it doesn't change page behavior or add
event-logging. To actually capture view/click events on these pages,
each page's route handler still needs `auto_log("auto_homepage",
"view")` style calls. That second wave can land per-handler over time.
What this commit fixes is the visibility gap — every page now shows up
in the brain's "surfaces I know about" list, which:

  - bumps each one's page-integrity score from 70 → 85
  - makes them appear in /api/v1/surfaces
  - means a future event-logging migration only needs to add the
    auto_log call (the surface already exists)

After this lands, site_score should rise from ~70 to ~85+ as 59
pages graduate from orphan → tracked-but-unobserved.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# (surface_id, name, description, routes, paid_tools, event_types)
# Grouped by sentinel-manifest category for review-readability.
_BATCH = [
    # ── critical (public-facing money pages) ─────────────────────
    ("auto_homepage", "Homepage",
     "Public homepage — DC Hub's primary landing surface",
     ["/"], [], ["view"]),
    ("auto_live_pulse", "Live Pulse",
     "/intelligence — real-time DCPI + market activity feed",
     ["/intelligence"], [], ["view"]),
    ("auto_pricing", "Pricing",
     "/pricing — public pricing + tier descriptions",
     ["/pricing"], [], ["view", "click_upgrade"]),
    ("auto_bs_translator", "BS Translator (vs)",
     "/vs — head-to-head positioning vs competitors",
     ["/vs", "/vs/<slug>", "/bs-translator"], [], ["view"]),
    ("auto_claims_api", "Claims API",
     "/api/v1/vs/claims — machine-readable BS-translator data",
     ["/api/v1/vs/claims"], [], ["api_call"]),

    # ── high (AI / analytics product surfaces) ────────────────────
    ("auto_ai_hub", "AI Hub",
     "/ai — overview of DC Hub's AI integrations",
     ["/ai"], [], ["view"]),
    ("auto_ai_deals", "AI Deals",
     "/ai-deals — public deal-flow filtered for AI / hyperscale",
     ["/ai-deals"], ["list_transactions"], ["view", "filter"]),
    ("auto_ai_inventory", "AI Inventory",
     "/ai-inventory — AI-relevant capacity inventory",
     ["/ai-inventory"], [], ["view", "filter"]),
    ("auto_ai_pipeline", "AI Pipeline",
     "/ai-pipeline — AI hyperscale projects in flight",
     ["/ai-pipeline"], ["get_pipeline"], ["view", "filter"]),
    ("auto_ai_integrations", "AI Integrations",
     "/ai-integrations — registered AI tools + MCP integration map",
     ["/ai-integrations"], ["get_agent_registry"], ["view"]),
    ("auto_assets", "Assets Explorer",
     "/assets — searchable facility/asset registry",
     ["/assets"], ["search_facilities"], ["view", "search"]),
    ("auto_capacity_pipeline", "Capacity Pipeline",
     "/capacity-pipeline — full pipeline browser",
     ["/capacity-pipeline"], ["get_pipeline"], ["view", "filter"]),
    ("auto_daily", "Daily Report",
     "/daily — DC Hub Daily — DCPI + DCI + market digest",
     ["/daily"], [], ["view"]),
    ("auto_market_intelligence", "Market Analytics",
     "/market-intelligence — composite market analytics dashboard",
     ["/market-intelligence"], ["get_market_intel"], ["view"]),
    ("auto_news", "News",
     "/news — aggregated industry news feed",
     ["/news"], ["get_news"], ["view"]),
    ("auto_powered_shell", "Powered Shell",
     "/powered-shell — powered-shell inventory + tracker",
     ["/powered-shell"], [], ["view", "filter"]),
    ("auto_rankings", "Rankings",
     "/rankings — DCPI / DCI market + facility rankings",
     ["/rankings"], ["rank_markets"], ["view"]),
    ("auto_tax_incentives", "Tax Incentives",
     "/tax-incentives — per-state DC tax incentive registry",
     ["/tax-incentives"], ["get_tax_incentives"], ["view"]),
    ("auto_api_docs", "API Docs",
     "/api-docs — public REST + MCP API documentation",
     ["/api-docs"], [], ["view"]),
    ("auto_dcpi_scores_api", "DCPI Scores API",
     "/api/v1/dcpi/scores — public DCPI scores feed",
     ["/api/v1/dcpi/scores"], [], ["api_call"]),
    ("auto_iso_zones", "ISO Zones Aggregator",
     "/api/v1/iso/zones — multi-ISO zone aggregator",
     ["/api/v1/iso/zones"], ["get_grid_intelligence"], ["api_call"]),
    ("auto_mcp_manifest_api", "MCP Manifest (api/v1)",
     "/api/v1/mcp/manifest — MCP capability discovery for agents",
     ["/api/v1/mcp/manifest"], [], ["api_call"]),
    ("auto_dc_hub_media", "DC Hub Media",
     "/dc-hub-media — the media kit + brand-press surface",
     ["/dc-hub-media"], [], ["view"]),
    ("auto_developers", "Developers",
     "/developers — devrel landing + API onboarding",
     ["/developers"], [], ["view", "click_signup"]),
    ("auto_ecosystem", "Ecosystem",
     "/ecosystem — vendor + partner directory",
     ["/ecosystem"], [], ["view"]),
    ("auto_grid_hub", "Grid Hub",
     "/grid — public ISO grid intelligence index",
     ["/grid"], ["get_grid_data"], ["view"]),
    ("auto_land_power", "Land + Power",
     "/land-power — combined land + power discovery surface",
     ["/land-power"], [], ["view"]),
    ("auto_land_power_map", "Land + Power Map",
     "/land-power-map — geo land+power map",
     ["/land-power-map"], [], ["view", "map_click"]),
    ("auto_facility_map", "Facility Map",
     "/map — global facility map",
     ["/map"], ["search_facilities"], ["view", "map_click"]),
    ("auto_markets", "Markets",
     "/markets/ — market browser",
     ["/markets/", "/markets"], ["rank_markets", "get_market_intel"], ["view", "filter"]),
    ("auto_pipeline_tracker", "Pipeline Tracker",
     "/pipeline-tracker — capacity pipeline tracker view",
     ["/pipeline-tracker"], [], ["view"]),
    ("auto_sites", "Sites",
     "/sites — site selection + ranking tool",
     ["/sites"], ["score_facility", "compare_sites"], ["view", "filter"]),
    ("auto_spare_capacity", "Spare Capacity",
     "/spare-capacity — submit + browse spare capacity listings",
     ["/spare-capacity"], [], ["view", "submit"]),
    ("auto_mcp_manifest", "MCP Manifest (.well-known)",
     "/.well-known/mcp.json — public MCP discovery file",
     ["/.well-known/mcp.json"], [], ["api_call"]),
    ("auto_power_totals_api", "Power Totals API",
     "/api/v1/power/totals — public power totals JSON",
     ["/api/v1/power/totals"], [], ["api_call"]),

    # ── normal (informational + utility surfaces) ─────────────────
    ("auto_about", "About",
     "/about — about-us company page",
     ["/about"], [], ["view"]),
    ("auto_advertise", "Advertise",
     "/advertise — advertising / sponsorship inquiries",
     ["/advertise"], [], ["view", "click_contact"]),
    ("auto_announcements", "Announcements",
     "/announcements — DC Hub product announcements feed",
     ["/announcements"], [], ["view"]),
    ("auto_architecture", "Architecture",
     "/architecture — public system architecture page",
     ["/architecture"], [], ["view"]),
    ("auto_cited_by", "Cited By",
     "/cited-by — citations of DC Hub in press + AI agents",
     ["/cited-by"], [], ["view"]),
    ("auto_faq", "FAQ",
     "/faq — frequently asked questions",
     ["/faq"], [], ["view"]),
    ("auto_founders", "Founders",
     "/founders — founders / team page",
     ["/founders"], [], ["view"]),
    ("auto_gdci", "GDCI",
     "/gdci — Global Data Center Index",
     ["/gdci"], [], ["view"]),
    ("auto_glossary", "Glossary",
     "/glossary — DC industry term glossary",
     ["/glossary"], [], ["view"]),
    ("auto_operators", "Operators Index",
     "/operators — operator directory",
     ["/operators"], [], ["view", "click_operator"]),
    ("auto_pocket_listings", "Pocket Listings",
     "/pocket-listings — pocket-listing landing page",
     ["/pocket-listings"], [], ["view"]),
    ("auto_press", "Press",
     "/press — press releases index",
     ["/press"], [], ["view"]),
    ("auto_state_of_dc", "State of the Data Center",
     "/state-of-the-data-center — state-of-industry surface",
     ["/state-of-the-data-center"], [], ["view"]),
    ("auto_system_status", "System Status",
     "/system-status — public system health dashboard",
     ["/system-status"], [], ["view"]),
    ("auto_testimonials", "Testimonials",
     "/testimonials — public customer + AI agent testimonials",
     ["/testimonials"], [], ["view"]),
    ("auto_transparency", "Transparency Console (alias)",
     "/transparency — public live ops console",
     ["/transparency"], [], ["view"]),
    ("auto_developers_funnel_api", "Developers Funnel API",
     "/api/v1/developers/funnel — devrel funnel metrics",
     ["/api/v1/developers/funnel"], [], ["api_call"]),
    ("auto_facilities_delta_api", "Facilities Delta API",
     "/api/v1/facilities/delta — facility delta feed",
     ["/api/v1/facilities/delta"], [], ["api_call"]),
    ("auto_mcp_growth_api", "MCP Growth API",
     "/api/v1/mcp/growth — MCP usage growth metrics",
     ["/api/v1/mcp/growth"], [], ["api_call"]),
    ("auto_surfaces_api", "Surfaces API",
     "/api/v1/surfaces — surface registry API",
     ["/api/v1/surfaces"], [], ["api_call"]),
    ("auto_llms_txt", "llms.txt",
     "/llms.txt — agent-discovery manifest",
     ["/llms.txt"], [], ["api_call"]),
    ("auto_openapi", "OpenAPI",
     "/openapi.json — public OpenAPI 3 spec",
     ["/openapi.json"], [], ["api_call"]),
    ("auto_agent_card", "Agent Card",
     "/.well-known/agent.json — agent capability card",
     ["/.well-known/agent.json"], [], ["api_call"]),
]


def register_all() -> dict:
    """Register every surface in _BATCH. Returns count + any errors.
    Safe to call multiple times — SURFACES dict overwrites by id.
    """
    try:
        from routes.surface_brain import register_surface, Surface
    except Exception as e:
        return {"registered": 0, "error": f"surface_brain import failed: {e}"}

    registered = 0
    errors: list = []
    for sid, name, desc, routes, tools, events in _BATCH:
        try:
            register_surface(Surface(
                surface_id=sid,
                name=name,
                description=desc,
                routes=routes,
                paid_tools=tools,
                expected_event_types=events,
            ))
            registered += 1
        except Exception as e:
            errors.append(f"{sid}: {type(e).__name__}: {str(e)[:80]}")

    if registered:
        logger.info(f"[surface_registrations_batch] registered {registered} surfaces "
                    f"({len(errors)} errors)")
    return {"registered": registered, "total": len(_BATCH), "errors": errors}
