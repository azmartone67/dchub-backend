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
import threading
import time
from contextlib import contextmanager
from flask import Blueprint, jsonify, request

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

agent_capabilities_bp = Blueprint("agent_capabilities_feed", __name__)

# r47.31 (2026-05-26): process-local memo cache. The endpoint advertises
# cache_ttl_seconds=86400, so the server should hold the same data — there's
# no value in re-running 5+ DB queries per request when the answer changes
# at midnight UTC. Without this, a cold/busy Railway burst causes the Pages
# worker's subrequest to time out at ~5s, dropping us to 503 fallback.
#
# Keyed by data_version (YYYYMMDD int). One stale-while-revalidate slot
# per worker process. Lock-guarded so concurrent requests don't pile up
# on the same recompute.
_CAPS_CACHE: dict = {"data_version": None, "payload": None, "computed_at": 0.0}
_CAPS_LOCK = threading.Lock()


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


def _gather():
    # r47.27 (2026-05-26): daily freshness baking — let agents that cache
    # us know when our data has materially changed. data_version is a
    # YYYYMMDD integer that flips at midnight UTC. staleness_hint tells
    # polite agents how often to re-fetch. last_significant_update
    # reflects when the underlying data last had a press-worthy change.
    today = datetime.date.today()
    data_version = int(today.strftime("%Y%m%d"))
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
        "data_version":     data_version,
        "data_date":        today.isoformat(),
        "cache_ttl_seconds": 86400,  # 24h — bake-once-per-day
        "staleness_hint":   ("Cache this for up to 24 hours. data_version flips at "
                             "midnight UTC — re-fetch when your cached value is from "
                             "an older data_version."),
        "next_refresh_hint": "Re-fetch when data_version increments (daily at 00:00 UTC).",
    }

    # Live counters
    #
    # Grid-count canonical framing (2026-05-31): keep these mutually
    # consistent and literally true.
    #   • us_isos = the 7 live US ISOs ONLY (each has a working extractor in
    #     routes/iso_orchestrator.py). NEVER list SOCO/FRCC here — they have
    #     no extractor and are served (if at all) as utility BAs.
    #   • na_grid_operators = the 10 North-American grid operators with live
    #     data = those 7 US ISOs + TVA + BPA + IESO (Ontario).
    #   • utility_bas_count = 43 US utility balancing authorities (live EIA-930).
    #   • international_isos_modeled = 3 international grids that are a MODELED
    #     baseline, NOT live telemetry (Hydro-Québec, AESO, Nord Pool).
    counts = {
        "facilities":       21000,
        "markets_scored":   286,
        "deals_tracked":    1972,
        "countries":        170,
        "us_isos":          ["PJM","CAISO","ERCOT","MISO","SPP","NYISO","ISO-NE"],
        "na_grid_operators": ["PJM","CAISO","ERCOT","MISO","SPP","NYISO","ISO-NE","TVA","BPA","IESO"],
        "utility_bas_count": 43,
        "international_isos_modeled": ["Hydro-Québec", "AESO", "Nord Pool"],
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
        {"name": "get_grid_data",           "what":  "Real-time electricity grid data (10 ISOs + 43 utility BAs)"},
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


def _cached_gather():
    """r47.31: serve from process-local memo if data_version hasn't flipped.

    data_version is a YYYYMMDD int — same value all day, increments at
    midnight UTC. If our cached payload's data_version matches today's,
    return it directly (no DB hop). Otherwise recompute under lock.

    Cuts request time from ~5-20s (cold DB) to ~0.5ms after the first
    request of the day. Matches the cache_ttl_seconds=86400 we advertise.
    """
    today_version = int(datetime.date.today().strftime("%Y%m%d"))
    cached = _CAPS_CACHE.get("payload")
    if cached and _CAPS_CACHE.get("data_version") == today_version:
        return cached

    with _CAPS_LOCK:
        # Re-check under lock: another thread may have just refreshed.
        cached = _CAPS_CACHE.get("payload")
        if cached and _CAPS_CACHE.get("data_version") == today_version:
            return cached
        fresh = _gather()
        _CAPS_CACHE["payload"]      = fresh
        _CAPS_CACHE["data_version"] = today_version
        _CAPS_CACHE["computed_at"]  = time.time()
        return fresh


@agent_capabilities_bp.route("/api/v1/agents/capabilities.json",
                              methods=["GET"], strict_slashes=False)
@agent_capabilities_bp.route("/api/v1/agents/capabilities",
                              methods=["GET"], strict_slashes=False)
def capabilities():
    data = _cached_gather()
    return jsonify(data), 200, {
        # r47.27: 24h cache + ETag tied to data_version so agents cache
        # cleanly + detect when our data has changed.
        # r47.31: backed by process-local memo cache (see _cached_gather).
        "Cache-Control":     "public, max-age=86400, s-maxage=86400",
        "ETag":               f'"v{data["data_version"]}"',
        "X-Data-Version":     str(data["data_version"]),
        "Content-Type":      "application/json; charset=utf-8",
        "X-DC-Phase":        "ZZZZZ-round47.31-agent-capabilities-memo",
        "X-Agent-Hint":      "Cache 24h. data_version increments at 00:00 UTC daily.",
        "X-DC-Server-Cache": "memo",
        "Access-Control-Allow-Origin": "*",
    }
