# Shadowed Routes Inventory

_Generated: 2026-08-24T12:35:46.778131Z_  
_Total routes: 3437_  
_Shadowed routes: **15**_

A "shadowed route" is a URL path registered in two or more places.
Flask uses the FIRST registration; the others are dead code that
creates ambiguity and can mask bugs (Phase 20 lost a week to one).

## Inventory

### `/api/v1/brain/ask` (GET, POST)

Registered in 2 place(s):
- `brain_layer9.ask`
- `brain_qa.brain_ask`

### `/api/v1/dcpi/ask` (GET, POST)

Registered in 2 place(s):
- `dcpi.dcpi_ask`
- `dcpi_ask.ask`

### `/api/v1/dcpi/history` (GET)

Registered in 2 place(s):
- `dcpi_temporal.dcpi_history`
- `dcpi.api_history`

### `/api/v1/dcpi/lite-recompute` (POST)

Registered in 2 place(s):
- `dcpi.lite_recompute`
- `_v216_dcpi_lite_recompute`

### `/api/v1/reports/monthly` (GET)

Registered in 2 place(s):
- `monthly_trend.monthly_json_current`
- `comprehensive_report.monthly_json`

### `/api/v1/webhooks/resend` (POST)

Registered in 2 place(s):
- `resend_webhook.resend_webhook`
- `email_engagement.resend_webhook`

### `/integrations/tools.json` (GET)

Registered in 2 place(s):
- `serve_tools_manifest`
- `integrations_tools.integrations_tools_short`

### `/markets/<slug>` (GET)

Registered in 2 place(s):
- `market_deep_dive.market_short_html`
- `seo_pages.market_page`

### `/methodology` (GET)

Registered in 2 place(s):
- `methodology_pages.methodology_index`
- `redirects_404_killer.redir_methodology`

### `/reports/monthly` (GET)

Registered in 2 place(s):
- `monthly_trend.monthly_html_current`
- `comprehensive_report.monthly_html`

### `/research` (GET)

Registered in 2 place(s):
- `research_page`
- `redirects_404_killer.redir_research`

### `/robots.txt` (GET)

Registered in 2 place(s):
- `serve_robots_txt`
- `robots_seo.robots_txt`

### `/status` (GET)

Registered in 2 place(s):
- `site_audit.status_html`
- `status_page.http_status_page`

### `/team` (GET)

Registered in 2 place(s):
- `team_landing.team`
- `redirects_404_killer.redir_team`

### `/upgrade` (GET)

Registered in 2 place(s):
- `pair_code.upgrade_redirect`
- `stripe_direct_upgrade.upgrade_redirect`
