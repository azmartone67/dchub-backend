# Shadowed Routes Inventory

_Generated: 2026-05-11T14:25:58.278066Z_  
_Total routes: 1189_  
_Shadowed routes: **25**_

A "shadowed route" is a URL path registered in two or more places.
Flask uses the FIRST registration; the others are dead code that
creates ambiguity and can mask bugs (Phase 20 lost a week to one).

## Inventory

### `/.well-known/ai-plugin.json` (GET)

Registered in 2 place(s):
- `serve_ai_plugin_json`
- `ai_ecosystem.openai_plugin_manifest`

### `/api/agents/intelligence-index` (GET)

Registered in 2 place(s):
- `agent_intelligence_index`
- `api_agents_intelligence_index`

### `/api/crawlers/stats` (GET)

Registered in 2 place(s):
- `google_meta.crawler_stats`
- `crawler_stats`

### `/api/discovery/run` (POST)

Registered in 2 place(s):
- `api_discovery.run_api_discovery`
- `discovery.discovery_run`

### `/api/discovery/status` (GET)

Registered in 2 place(s):
- `api_discovery.api_discovery_status`
- `discovery.discovery_status`

### `/api/energy-discovery/status` (GET)

Registered in 2 place(s):
- `energy_discovery_status`
- `energy_discovery.energy_discovery_status`

### `/api/founding-members` (GET)

Registered in 2 place(s):
- `public_endpoints.founding_members_status`
- `founding_members_status`

### `/api/jobs/auto-approve` (POST)

Registered in 2 place(s):
- `jobs.job_auto_approve`
- `job_auto_approve`

### `/api/jobs/infrastructure-sync` (POST)

Registered in 2 place(s):
- `job_infrastructure_sync`
- `jobs.job_infrastructure_sync`

### `/api/market-intelligence` (GET)

Registered in 2 place(s):
- `market_intelligence_neon.get_market_intelligence`
- `get_market_intelligence`

### `/api/v1/dcpi/lite-recompute` (POST)

Registered in 2 place(s):
- `dcpi.lite_recompute`
- `_v216_dcpi_lite_recompute`

### `/api/v1/map` (GET)

Registered in 2 place(s):
- `api_v1_map`
- `public_endpoints.public_map_view`

### `/api/v1/mcp/track` (POST)

Registered in 2 place(s):
- `mcp_bp.track_tool_call`
- `phase9g_mcp_track_override`

### `/api/v2/alerts` (GET)

Registered in 2 place(s):
- `alerts_v2.list_alerts`
- `get_user_alerts`

### `/api/v2/alerts` (POST)

Registered in 2 place(s):
- `alerts_v2.create_alert`
- `create_alert`

### `/api/v2/alerts/<int:alert_id>` (DELETE)

Registered in 2 place(s):
- `alerts_v2.delete_alert`
- `delete_alert`

### `/api/v2/keys` (GET)

Registered in 2 place(s):
- `monetization.list_api_keys`
- `list_api_keys`

### `/api/v2/keys` (POST)

Registered in 2 place(s):
- `monetization.create_api_key`
- `create_api_key`

### `/api/v2/keys/<int:key_id>` (DELETE)

Registered in 2 place(s):
- `monetization.revoke_api_key`
- `revoke_api_key`

### `/api/v2/plans` (GET)

Registered in 2 place(s):
- `monetization.list_plans`
- `get_plans`

### `/api/v2/risk/active-fires` (GET)

Registered in 2 place(s):
- `fire_data.get_active_fires`
- `active_fires`

### `/api/v2/usage` (GET)

Registered in 2 place(s):
- `monetization.get_usage_stats`
- `get_api_usage`

### `/dashboard` (GET)

Registered in 2 place(s):
- `serve_dashboard`
- `land_power_page`

### `/research` (GET)

Registered in 2 place(s):
- `research_page`
- `open_data.research_landing`

### `/sitemap.xml` (GET)

Registered in 2 place(s):
- `grid_public.sitemap`
- `serve_sitemap_xml`
