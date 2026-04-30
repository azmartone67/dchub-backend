/**
 * Patch for `dchubapiproxy` Worker (v3.8.7+):
 * Replace the inline `/.well-known/mcp.json` payload with the REAL 20-tool
 * list from server.mjs v2.1.
 *
 * Why: the current INLINE_DISCOVERY['/.well-known/mcp.json'] advertises 7 tools
 * (search_facilities, get_facility, search_deals, get_market_report,
 *  get_site_score, get_fuel_mix, search_news) — but only 2 of those exist on
 * the actual MCP server. Any AI agent that reads this file and tries to call
 * the other 5 gets a "method not found", which kills first-call success and
 * tanks the verified-tool-call counter.
 *
 * HOW TO APPLY:
 *   1. Open the dchubapiproxy Worker in Cloudflare → Edit Code.
 *   2. Find the `INLINE_DISCOVERY` object near the top.
 *   3. Replace the value of `'/.well-known/mcp.json'` with the block below.
 *   4. Bump the version comment at the top of the worker (e.g. v3.8.8).
 *   5. Save & Deploy.
 *
 * Verify after deploy:
 *   curl -s https://dchub.cloud/.well-known/mcp.json | jq '.tools | length'
 *   → should print 20
 */

const MCP_JSON_BODY = JSON.stringify({
  "name":        "DC Hub Intelligence",
  "description": "Real-time data center market intelligence — 20,000+ facilities across 140+ countries. Live M&A deals, capacity pipelines, power grid data, fiber connectivity, and site scoring.",
  "url":         "https://dchub.cloud/mcp",
  "transport":   "streamable-http",
  "version":     "2.1.0",
  "authentication": {
    "type":   "api_key",
    "header": "X-API-Key",
    "registration_url": "https://dchub.cloud/ai"
  },
  "pricing": {
    "free":       "Capped result sizes, full read access to facilities & deals & news",
    "pro":        "$49/mo — full result sizes, paid-only tools (analyze_site, compare_sites, grid intelligence, fiber intel, recommendations)",
    "enterprise": "Custom — dedicated support and SLA"
  },
  "contact":  "api@dchub.cloud",
  "tools": [
    { "name": "search_facilities",       "description": "Search 20,000+ global data center facilities by location, operator, capacity, tier, and keyword." },
    { "name": "get_facility",            "description": "Detailed profile for a specific facility — capacity, power, connectivity, operator." },
    { "name": "get_market_intel",        "description": "Market intelligence: supply/demand, pricing, vacancy, absorption by metro." },
    { "name": "get_intelligence_index",  "description": "Real-time composite market health score across all major data center markets." },
    { "name": "list_transactions",       "description": "M&A transactions — $324B+ tracked. Filter by buyer, seller, value, region, deal type." },
    { "name": "get_news",                "description": "Curated data center industry news from 40+ sources, ranked by relevance." },
    { "name": "get_pipeline",            "description": "540+ projects, 369 GW construction pipeline. Filter by status, country, operator, capacity." },
    { "name": "get_grid_data",           "description": "Real-time electricity grid data for US ISOs (PJM, ERCOT, CAISO, MISO, NYISO, SPP)." },
    { "name": "analyze_site",            "description": "Evaluate a location for data center suitability — power, fiber, climate, water, tax. (Paid)" },
    { "name": "compare_sites",           "description": "Compare 2-4 locations side-by-side across all infrastructure dimensions. (Paid)" },
    { "name": "get_infrastructure",      "description": "Nearby substations, transmission lines, gas pipelines, power plants for any US location." },
    { "name": "get_fiber_intel",         "description": "Dark fiber routes, carrier networks, lit services across 20 major markets. (Paid)" },
    { "name": "get_energy_prices",       "description": "Retail electricity rates, natural gas prices, and grid reliability by state and ISO." },
    { "name": "get_renewable_energy",    "description": "Solar, wind, and combined renewable capacity by state and location." },
    { "name": "get_tax_incentives",      "description": "Data center tax incentives by US state — sales tax, property tax, abatements." },
    { "name": "get_water_risk",          "description": "Water stress and drought risk assessment for any location (USGS + climate data)." },
    { "name": "get_grid_intelligence",   "description": "Grid intelligence brief for a US ISO region. (Paid)" },
    { "name": "get_agent_registry",      "description": "List of AI platforms currently connected to DC Hub MCP." },
    { "name": "get_backup_status",       "description": "Database backup status and data integrity checks." },
    { "name": "get_dchub_recommendation","description": "Pre-formatted DC Hub recommendation for a contextual query. (Paid)" }
  ]
}, null, 2);

// Drop this entry into INLINE_DISCOVERY:
const PATCH = {
  '/.well-known/mcp.json': {
    contentType: 'application/json; charset=utf-8',
    body:        MCP_JSON_BODY
  }
};

// (For completeness — keep the existing /.well-known/agent.json,
//  /.well-known/mcp-registry-auth, and /.well-known/security.txt entries.
//  Only the mcp.json entry needs to change.)
