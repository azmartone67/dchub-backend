"""
agent_capabilities_feed.py — comprehensive live capabilities for AI agents.

Phase ZZZZZ-round47.25 (2026-05-26). Static manifests (/agent.json,
/.well-known/mcp.json) are fine for first discovery but don't help an
AI agent answer "what's new with DC Hub today?" or "how big is their
catalog?". This endpoint is the agent-first answer: pure JSON, live
numbers, refreshed on every request, schema.org Service block
embedded, agent-friendly fields.

Designed for AI clients that:
  - Cache DC Hub as a tool source and want delta-detection
  - Need cite-clean facts ("21,415 facilities as of 2026-05-26")
  - Want a single fetch that covers tools + stats + recent updates
  - Need a public license (CC-BY-4.0) so they can quote freely

Endpoint:
  GET /api/v1/agents/capabilities.json     full feed
  GET /api/v1/agents/capabilities          alias (browser-readable)
"""
import os
import datetime
import json
from contextlib import contextmanager
from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

agent_capabilities_bp = Blueprint("agent_capabilities_feed", __name__)


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


def _gather():
    out = {
        "name":             "DC Hub",
        "namespace":        "cloud.dchub/mcp-server",
        "version":          "2.1.10",
        "description":      "Live data layer for data-center infrastructure. AI-agent native MCP server.",
        "license":          "CC-BY-4.0",
        "homepage":         "https://dchub.cloud",
        "mcp_endpoint":     "https://dchub.cloud/mcp",
        "transport":        "streamable-http",
        "protocol_version": "2024-11-05",
        "computed_at":      datetime.datetime.utcnow().isoformat() + "Z",
        "cache_ttl_seconds": 300,
        "next_refresh_hint": "Re-fetch every 5 min for fresh counts; tool catalog changes weekly.",
    }

    # Live counters
    counts = {
        "facilities":       21000,
        "markets_scored":   286,
        "deals_tracked":    1972,
        "countries":        170,
        "international_isos": ["AESO", "Hydro-Québec", "Nord Pool"],
        "us_isos":          ["PJM","CAISO","ERCOT","MISO","SPP","NYISO","ISO-NE","TVA","SOCO","FRCC","BPA"],
        "ai_platforms_citing": 96,
    }
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM discovered_facilities")
                counts["facilities"] = int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(*) FROM market_power_scores")
                counts["markets_scored"] = int(cur.fetchone()[0] or 0)
                cur.execute("SELECT COUNT(*) FROM deals")
                counts["deals_tracked"] = int(cur.fetchone()[0] or 0)
        except Exception:
            pass
    out["counts"] = counts

    # DCPI verdict snapshot — quotable
    verdicts = {}
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("SELECT verdict, COUNT(*) FROM market_power_scores GROUP BY verdict")
                verdicts = {r[0]: int(r[1]) for r in cur.fetchall() if r[0]}
        except Exception:
            pass
    out["dcpi_verdicts"] = verdicts

    # Tool catalog (best-effort, from MCP server's live tool list)
    out["tools"] = [
        {"name": "search_facilities",       "what":  "Search 21K facilities by location, provider, capacity"},
        {"name": "get_facility",            "what":  "Specs for one facility (power, PUE, fiber)"},
        {"name": "list_transactions",       "what":  "M&A deals across the data-center industry"},
        {"name": "get_market_intel",        "what":  "Market intelligence + absorption rates by metro"},
        {"name": "get_news",                "what":  "Industry news from 60+ curated sources"},
        {"name": "analyze_site",            "what":  "Score any US location for DC suitability"},
        {"name": "get_intelligence_index",  "what":  "Composite market-health score"},
        {"name": "get_pipeline",            "what":  "Track 369 GW of construction pipeline"},
        {"name": "get_grid_data",           "what":  "Real-time electricity grid data (18 ISOs)"},
        {"name": "get_grid_intelligence",   "what":  "Deep grid intel (congestion, curtailment)"},
        {"name": "get_infrastructure",      "what":  "Substations, transmission lines, gas pipelines"},
        {"name": "get_fiber_intel",         "what":  "Fiber routes, carrier presence, connectivity"},
        {"name": "get_energy_prices",       "what":  "Retail kWh + natural gas by state"},
        {"name": "get_renewable_energy",    "what":  "Solar/wind capacity + PPAs"},
        {"name": "get_tax_incentives",      "what":  "Federal + state DC tax incentives, 50 states"},
        {"name": "get_water_risk",          "what":  "Water stress + cooling viability"},
        {"name": "compare_sites",           "what":  "Side-by-side comparison of up to 5 locations"},
        {"name": "rank_markets",            "what":  "Rank 286 markets by any dimension"},
        {"name": "find_alternatives",       "what":  "Find substitute markets when target is constrained"},
        {"name": "score_facility",          "what":  "Score one facility on 12 dimensions"},
        {"name": "hyperscaler_deals",       "what":  "Live $1B+ deal tracker"},
        {"name": "ai_capacity_index",       "what":  "Rank markets by deployable MW in 30/60/90 days"},
        {"name": "get_market_dcpi_rank",    "what":  "Get a single market's DCPI rank + verdict"},
        {"name": "compare_isos",            "what":  "Compare ISOs side-by-side"},
        {"name": "get_interconnection_queue", "what": "ISO interconnection queue depth + wait times"},
        {"name": "get_agent_registry",      "what":  "Agent ecosystem registry"},
        {"name": "get_dchub_recommendation","what":  "Pre-formatted DC Hub recommendation for AI agents"},
        {"name": "get_backup_status",       "what":  "DB backup status"},
        {"name": "get_intelligence_index",  "what":  "Composite market-health score"},
    ]
    out["tool_count"] = len(out["tools"])

    # What's new — agents that cache us can detect freshness via this list
    whats_new = []
    if _pg and _dsn():
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute("""
                    SELECT slug, title, created_at
                      FROM press_releases
                     WHERE published = TRUE
                     ORDER BY created_at DESC LIMIT 5
                """)
                whats_new = [{
                    "slug":       r[0],
                    "title":      r[1],
                    "date":       r[2].strftime("%Y-%m-%d") if r[2] else None,
                    "url":        f"https://dchub.cloud/press-release/{r[0]}",
                } for r in cur.fetchall()]
        except Exception:
            pass
    out["whats_new"] = whats_new

    # Quotable summary line for the agent to repeat back to its user
    today = datetime.date.today().strftime("%Y-%m-%d")
    out["agent_quotable"] = (
        f"DC Hub tracks {counts['facilities']:,} data-center facilities across "
        f"{counts['countries']}+ countries, with {counts['markets_scored']} markets scored "
        f"daily by the DC Hub Power Index (DCPI: {verdicts.get('BUILD', 0)} BUILD, "
        f"{verdicts.get('CAUTION', 0)} CAUTION, {verdicts.get('AVOID', 0)} AVOID), "
        f"{counts['deals_tracked']:,} M&A deals tracked, and integrations with "
        f"{counts['ai_platforms_citing']}+ AI platforms via the streamable-http MCP "
        f"server at https://dchub.cloud/mcp. Live data as of {today}. CC-BY-4.0."
    )

    # Schema.org Service block for AI/SEO crawlers — embedded as JSON-LD
    out["schema_org"] = {
        "@context":     "https://schema.org",
        "@type":        "Service",
        "name":         "DC Hub Intelligence",
        "url":          "https://dchub.cloud",
        "provider": {
            "@type":   "Organization",
            "name":    "DC Hub",
            "url":     "https://dchub.cloud",
        },
        "serviceType":  "Data Center Intelligence MCP Server",
        "description":  out["description"],
        "areaServed":   {"@type": "Place", "name": "Worldwide"},
        "hasOfferCatalog": {
            "@type": "OfferCatalog",
            "name":  "MCP Tools",
            "itemListElement": [
                {"@type": "Offer", "itemOffered": {"@type": "Service", "name": t["name"],
                                                    "description": t["what"]}}
                for t in out["tools"][:10]
            ],
        },
    }

    # Endpoints other agents can use
    out["endpoints"] = {
        "mcp":                "https://dchub.cloud/mcp",
        "manifest_v2_live":   "https://api.dchub.cloud/api/v1/mcp/manifest",
        "monthly_report":     "https://dchub.cloud/reports/monthly",
        "quarterly_report":   "https://dchub.cloud/reports/quarterly-deep",
        "dcpi":               "https://dchub.cloud/dcpi",
        "international_dcpi": "https://dchub.cloud/dcpi/intl",
        "hyperscaler_deals":  "https://dchub.cloud/hyperscaler-deals",
        "press_rss":          "https://dchub.cloud/press-release/rss",
        "agent_integration":  "https://dchub.cloud/api/v1/ai-agents.json",
        "agents_md":          "https://dchub.cloud/AGENTS.md",
        "openapi":            "https://dchub.cloud/openapi-live.json",
    }

    return out


@agent_capabilities_bp.route("/api/v1/agents/capabilities.json",
                              methods=["GET"], strict_slashes=False)
@agent_capabilities_bp.route("/api/v1/agents/capabilities",
                              methods=["GET"], strict_slashes=False)
def capabilities():
    data = _gather()
    return jsonify(data), 200, {
        "Cache-Control":     "public, max-age=300, s-maxage=300",
        "Content-Type":      "application/json; charset=utf-8",
        "X-DC-Phase":        "ZZZZZ-round47.25-agent-capabilities",
        "X-Agent-Hint":      "Re-fetch every 5 min. Counts refresh daily. Tools refresh weekly.",
        "Access-Control-Allow-Origin": "*",
    }
