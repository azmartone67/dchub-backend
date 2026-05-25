# Shadowed Routes Inventory

_Generated: 2026-05-25T13:52:09.642801Z_  
_Total routes: 1909_  
_Shadowed routes: **11**_

A "shadowed route" is a URL path registered in two or more places.
Flask uses the FIRST registration; the others are dead code that
creates ambiguity and can mask bugs (Phase 20 lost a week to one).

## Inventory

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

### `/favicon.ico` (GET)

Registered in 2 place(s):
- `favicon`
- `favicon_quieter.favicon`

### `/integrations/tools.json` (GET)

Registered in 2 place(s):
- `serve_tools_manifest`
- `integrations_tools.integrations_tools_short`

### `/markets/<slug>` (GET)

Registered in 2 place(s):
- `market_deep_dive.market_short_html`
- `seo_pages.market_page`

### `/research` (GET)

Registered in 2 place(s):
- `research_page`
- `open_data.research_landing`

### `/robots.txt` (GET)

Registered in 2 place(s):
- `serve_robots_txt`
- `robots_seo.robots_txt`

### `/status` (GET)

Registered in 2 place(s):
- `site_audit.status_html`
- `status_page.http_status_page`

### `/upgrade` (GET)

Registered in 2 place(s):
- `pair_code.upgrade_redirect`
- `stripe_direct_upgrade.upgrade_redirect`

### `/vs` (GET)

Registered in 2 place(s):
- `quick_redirects.vs_index_redirect`
- `bs_translator.vs_page`
