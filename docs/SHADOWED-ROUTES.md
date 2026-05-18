# Shadowed Routes Inventory

_Generated: 2026-05-18T14:53:35.970433Z_  
_Total routes: 1528_  
_Shadowed routes: **10**_

A "shadowed route" is a URL path registered in two or more places.
Flask uses the FIRST registration; the others are dead code that
creates ambiguity and can mask bugs (Phase 20 lost a week to one).

## Inventory

### `/api/discovery/run` (POST)

Registered in 2 place(s):
- `api_discovery.run_api_discovery`
- `discovery.discovery_run`

### `/api/discovery/status` (GET)

Registered in 2 place(s):
- `api_discovery.api_discovery_status`
- `discovery.discovery_status`

### `/api/v1/dcpi/ask` (GET, POST)

Registered in 2 place(s):
- `dcpi.dcpi_ask`
- `dcpi_ask.ask`

### `/api/v1/dcpi/lite-recompute` (POST)

Registered in 2 place(s):
- `dcpi.lite_recompute`
- `_v216_dcpi_lite_recompute`

### `/api/v1/mcp/conversion-funnel` (GET)

Registered in 2 place(s):
- `mcp_funnel_v2.conversion_funnel`
- `_mcp_conversion_funnel`

### `/api/v1/mcp/track` (POST)

Registered in 2 place(s):
- `mcp_bp.track_tool_call`
- `phase9g_mcp_track_override`

### `/api/v1/stripe/webhook-mcp` (POST)

Registered in 2 place(s):
- `mcp_bp.stripe_webhook_mcp`
- `stripe_webhook`

### `/research` (GET)

Registered in 2 place(s):
- `research_page`
- `open_data.research_landing`

### `/sitemap.xml` (GET)

Registered in 2 place(s):
- `grid_public.sitemap`
- `serve_sitemap_xml`

### `/vs` (GET)

Registered in 2 place(s):
- `competitive_vs.vs_index`
- `bs_translator.vs_page`
