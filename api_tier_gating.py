

heroic-reprieve

production



Agent




worker
Deployments
Variables
Metrics
Settings
Unexposed service
3.13.12python@3.13.12
us-west2
1 Replica




History



















worker
/
68e6567c
Active

Mar 18, 2026, 9:37 AM MST
Details
Build Logs
Deploy Logs
Network Flow Logs
Filter and search logs

2026-03-19 06:45:17 UTC [INFO] ▶ Running: Capacity Headroom → /api/jobs/capacity-headroom
2026-03-19 06:45:29 UTC [INFO]   ✅ Capacity Headroom completed in 11.5s (HTTP 200)
2026-03-19 06:45:29 UTC [INFO]      result: {'phoenix': {'market': 'phoenix', 'name': 'Phoenix, AZ', 'iso': 'CAISO', 'state': 'AZ', 'grid': {'installed_capacity_mw': 85000, 'current_demand_mw': 29126, 'spare_capacity_mw': 55874, 'spare_capacity_pct': 65.7, 'signal': 'green', 'demand_timestamp': '2026-03-19T06', 'data_source': 'EIA Live'}, 'gas': {'pipeline_count': 2, 'total_capacity_mdth': 9600.0, 'utilization_pct': 61.0, 'headroom_mdth': 3744.0, 'signal': 'green'}, 'power': {'local_plants': 188, 'local_capacity_mw': 46947.1, 'fuel_mix': 
2026-03-19 07:00:34 UTC [INFO] ▶ Running: Facility Discovery → /api/jobs/discovery
2026-03-19 07:03:34 UTC [ERROR]   ❌ Facility Discovery — connection failed: The read operation timed out
2026-03-19 08:00:39 UTC [INFO] ▶ Running: News/RSS Refresh → /api/jobs/news-refresh
2026-03-19 08:02:00 UTC [INFO]   ✅ News/RSS Refresh completed in 81.6s (HTTP 200)
2026-03-19 08:02:00 UTC [INFO]      new_articles: 364
2026-03-19 08:20:05 UTC [INFO] ▶ Running: Auto-Approve → /api/jobs/auto-approve
2026-03-19 08:20:13 UTC [INFO]   ✅ Auto-Approve completed in 7.9s (HTTP 200)
2026-03-19 08:20:13 UTC [INFO]      result: {"approved": 0, "batches": 2, "cache_age_seconds": 390, "cache_names": 11497, "count_after": 11800, "count_before": 11800, "duplicate_skipped": 60, "errors": 0, "flagged_review": 40, "net_new": 0, "st
2026-03-19 08:30:18 UTC [INFO] ▶ Running: Evolution Engine → /api/jobs/evolution
2026-03-19 08:30:51 UTC [INFO]   ✅ Evolution Engine completed in 33.0s (HTTP 200)
2026-03-19 08:30:51 UTC [INFO]      result: {'cycle_id': '20260319_083018', 'started_at': '2026-03-19T08:30:18.899005', 'phases': {'observe': {'items_gathered': 0, 'sources_checked': 5, 'new_sources': 0, 'facility_stats': {'total_facilities': 11800, 'operators': 4016, 'countries': 179, 'updated_last_week': 12}, 'news_stats': {'total_articles': 13062, 'today': 143, 'unique_sources': 1195}, 'api_checks': [{'name': 'PeeringDB', 'status': 'healthy', 'response_time': 0.121415}, {'name': 'OpenStreetMap/Overpass', 'status': 'unhealthy', 'respons
2026-03-19 08:30:56 UTC [INFO] ▶ Running: Infrastructure Sync → /api/jobs/infrastructure-sync
2026-03-19 08:31:49 UTC [INFO]   ✅ Infrastructure Sync completed in 52.5s (HTTP 200)
2026-03-19 08:45:54 UTC [INFO] ▶ Running: Simple Alerts → /api/jobs/simple-alerts
2026-03-19 08:45:54 UTC [INFO]   ✅ Simple Alerts completed in 0.7s (HTTP 200)
2026-03-19 08:45:54 UTC [INFO]      result: {'status': 'ok', 'processed': 0, 'matched': 0, 'message': 'No active alerts'}
2026-03-19 09:15:59 UTC [INFO] ▶ Running: Alert Emails → /api/jobs/alert-emails
2026-03-19 09:16:00 UTC [INFO]   ✅ Alert Emails completed in 0.7s (HTTP 200)
2026-03-19 09:16:00 UTC [INFO]      result: {'alerts_checked': 0, 'emails_sent': 0}
2026-03-19 09:30:05 UTC [INFO] ▶ Running: Fiber Route Sync → /api/jobs/fiber-sync
2026-03-19 09:33:29 UTC [WARNING]   ⚠️ Fiber Route Sync returned HTTP 502 in 203.8s
2026-03-19 10:00:34 UTC [INFO] ▶ Running: AI Ecosystem Agent → /api/jobs/ai-ecosystem
2026-03-19 10:00:36 UTC [INFO]   ✅ AI Ecosystem Agent completed in 1.9s (HTTP 200)
2026-03-19 10:00:36 UTC [INFO]      result: {'timestamp': '2026-03-19T10:00:34.465517', 'discoveries': 0, 'enrichments': 0, 'outreach': [{'platform': 'OpenAI ChatGPT', 'method': 'Actions/Plugins', 'timestamp': '2026-03-19T10:00:35.362838', 'status': 'manifest_ready', 'notes': 'OpenAPI spec available for ChatGPT Actions'}, {'platform': 'Google Gemini', 'method': 'Vertex AI Extensions', 'timestamp': '2026-03-19T10:00:35.362849', 'status': 'discovery_enabled', 'notes': 'Vertex AI Extension manifest generated'}, {'platform': 'Groq', 'method':
2026-03-19 10:00:41 UTC [INFO] ▶ Running: Energy Discovery → /api/jobs/energy-discovery
2026-03-19 10:00:41 UTC [WARNING]   ⚠️ Energy Discovery returned HTTP 500 in 0.6s
2026-03-19 10:45:46 UTC [INFO] ▶ Running: Simple Alerts → /api/jobs/simple-alerts
2026-03-19 10:45:47 UTC [INFO]   ✅ Simple Alerts completed in 0.7s (HTTP 200)
2026-03-19 10:45:47 UTC [INFO]      result: {'status': 'ok', 'processed': 0, 'matched': 0, 'message': 'No active alerts'}
2026-03-19 11:30:52 UTC [INFO] ▶ Running: Content Publishing → /api/jobs/content-publish
2026-03-19 11:30:55 UTC [INFO]   ✅ Content Publishing completed in 2.8s (HTTP 200)
2026-03-19 11:30:55 UTC [INFO]      results: {"seo": {"duration": 1.9833667278289795, "indexnow": {}, "search_engines": {"bing": {"code": 410, "status": "failed", "url": "https://www.bing.com/ping?sitemap=https://dchub.cloud/sitemap.xml"}, "goog
2026-03-19 12:00:00 UTC [INFO] ▶ Running: News/RSS Refresh → /api/jobs/news-refresh
2026-03-19 12:01:28 UTC [INFO]   ✅ News/RSS Refresh completed in 87.7s (HTTP 200)
2026-03-19 12:01:28 UTC [INFO]      new_articles: 353
2026-03-19 12:20:33 UTC [INFO] ▶ Running: Auto-Approve → /api/jobs/auto-approve
2026-03-19 12:20:41 UTC [INFO]   ✅ Auto-Approve completed in 8.3s (HTTP 200)
2026-03-19 12:20:41 UTC [INFO]      result: {"approved": 0, "batches": 2, "cache_age_seconds": 7, "cache_names": 11497, "count_after": 11800, "count_before": 11800, "duplicate_skipped": 60, "errors": 0, "flagged_review": 40, "net_new": 0, "stat
2026-03-19 12:30:46 UTC [INFO] ▶ Running: Market Report → /api/jobs/market-report
2026-03-19 12:30:47 UTC [INFO]   ✅ Market Report completed in 0.9s (HTTP 200)
2026-03-19 12:30:47 UTC [INFO]      result: generated
2026-03-19 12:45:52 UTC [INFO] ▶ Running: Simple Alerts → /api/jobs/simple-alerts
2026-03-19 12:45:53 UTC [INFO]   ✅ Simple Alerts completed in 0.8s (HTTP 200)
2026-03-19 12:45:53 UTC [INFO]      result: {'status': 'ok', 'processed': 0, 'matched': 0, 'message': 'No active alerts'}
2026-03-19 12:45:58 UTC [INFO] ▶ Running: Capacity Headroom → /api/jobs/capacity-headroom
2026-03-19 12:48:58 UTC [ERROR]   ❌ Capacity Headroom — connection failed: The read operation timed out
2026-03-19 13:00:03 UTC [INFO] ▶ Running: AI Outreach Agent → /api/jobs/ai-outreach
2026-03-19 13:00:48 UTC [INFO]   ✅ AI Outreach Agent completed in 45.7s (HTTP 200)
2026-03-19 13:00:48 UTC [INFO]      result: {'timestamp': '2026-03-19T13:00:08.366352+00:00', 'discovery_endpoints': [{'endpoint': '/llms.txt', 'status': 200, 'success': True}, {'endpoint': '/llms-full.txt', 'status': 200, 'success': True}, {'endpoint': '/robots.txt', 'status': 200, 'success': True}, {'endpoint': '/skill.json', 'status': 404, 'success': False}, {'endpoint': '/AGENTS.md', 'status': 200, 'success': True}], 'directories_pinged': [{'directory': 'gptstore', 'name': 'GPTStore.ai', 'target': 'https://gptstore.ai', 'action': 'hom
2026-03-19 13:15:53 UTC [INFO] ▶ Running: Alert Emails → /api/jobs/alert-emails
2026-03-19 13:15:54 UTC [INFO]   ✅ Alert Emails completed in 0.8s (HTTP 200)
2026-03-19 13:15:54 UTC [INFO]      result: {'alerts_checked': 0, 'emails_sent': 0}
2026-03-19 14:00:59 UTC [INFO] ▶ Running: Facility Discovery → /api/jobs/discovery
2026-03-19 14:03:59 UTC [ERROR]   ❌ Facility Discovery — connection failed: The read operation timed out
2026-03-19 14:30:04 UTC [INFO] ▶ Running: Infrastructure Sync → /api/jobs/infrastructure-sync
2026-03-19 14:31:02 UTC [INFO]   ✅ Infrastructure Sync completed in 58.0s (HTTP 200)
2026-03-19 14:45:07 UTC [INFO] ▶ Running: Simple Alerts → /api/jobs/simple-alerts
2026-03-19 14:45:18 UTC [WARNING]   ⚠️ Simple Alerts returned HTTP 502 in 10.0s
2026-03-19 15:00:23 UTC [INFO] ▶ Running: AI Ecosystem Agent → /api/jobs/ai-ecosystem
2026-03-19 15:00:33 UTC [WARNING]   ⚠️ AI Ecosystem Agent returned HTTP 502 in 10.1s
2026-03-19 15:30:38 UTC [INFO] ▶ Running: Fiber Route Sync → /api/jobs/fiber-sync
2026-03-19 15:30:48 UTC [WARNING]   ⚠️ Fiber Route Sync returned HTTP 502 in 10.0s
2026-03-19 16:00:53 UTC [INFO] ▶ Running: News/RSS Refresh → /api/jobs/news-refresh
2026-03-19 16:01:03 UTC [WARNING]   ⚠️ News/RSS Refresh returned HTTP 502 in 10.0s
2026-03-19 16:01:08 UTC [INFO] ▶ Running: KMZ Infrastructure Discovery → /api/kmz-discovery/run
2026-03-19 16:01:18 UTC [WARNING]   ⚠️ KMZ Infrastructure Discovery returned HTTP 502 in 10.0s
2026-03-19 16:20:23 UTC [INFO] ▶ Running: Auto-Approve → /api/jobs/auto-approve
2026-03-19 16:20:32 UTC [INFO]   ✅ Auto-Approve completed in 9.2s (HTTP 200)
2026-03-19 16:20:32 UTC [INFO]      result: {"approved": 0, "batches": 2, "cache_age_seconds": 270, "cache_names": 11497, "count_after": 11800, "count_before": 11800, "duplicate_skipped": 60, "errors": 0, "flagged_review": 40, "net_new": 0, "st
2026-03-19 16:30:37 UTC [INFO] ▶ Running: Welcome Email Drip → /api/admin/drip-check?admin_key=f4f961b15334c7b3a570681354638ed5
2026-03-19 16:30:41 UTC [INFO]   ✅ Welcome Email Drip completed in 4.3s (HTTP 200)
2026-03-19 16:30:41 UTC [INFO]      status: ok
2026-03-19 16:45:46 UTC [INFO] ▶ Running: Simple Alerts → /api/jobs/simple-alerts
2026-03-19 16:45:47 UTC [INFO]   ✅ Simple Alerts completed in 0.6s (HTTP 200)
2026-03-19 16:45:47 UTC [INFO]      result: {'status': 'ok', 'processed': 0, 'matched': 0, 'message': 'No active alerts'}
2026-03-19 17:15:52 UTC [INFO] ▶ Running: Alert Emails → /api/jobs/alert-emails
2026-03-19 17:15:53 UTC [INFO]   ✅ Alert Emails completed in 0.7s (HTTP 200)
2026-03-19 17:15:53 UTC [INFO]      result: {'alerts_checked': 0, 'emails_sent': 0}
2026-03-19 18:00:58 UTC [INFO] ▶ Running: Global Intelligence → /api/jobs/global-intelligence
2026-03-19 18:01:08 UTC [INFO]   ✅ Global Intelligence completed in 9.9s (HTTP 200)
2026-03-19 18:01:08 UTC [INFO]      result: {'international': {'total_discovered': 0, 'by_region': {'europe': 0, 'asia_pacific': 0, 'latin_america': 0, 'middle_east_africa': 0}, 'new_facilities': 0, 'sources_checked': 0}, 'pipeline': {'announcements_found': 101, 'total_mw': 26615.899999999998, 'by_operator': {'Shinsegae Group': 250.0, 'Approval': 147.0, 'Advait Greenergy': 30.0, 'Norway': 50.0, 'New Jersey': 645.0, 'Germany': 390.0, 'Canada': 600.0, 'Is Scaling Its Canadian': 16.6, 'Slough': 50.0, 'Madrid': 240.0, 'Butzbach': 40.0, 'Data 
2026-03-19 18:01:13 UTC [INFO] ▶ Running: Energy Discovery → /api/jobs/energy-discovery
2026-03-19 18:01:13 UTC [WARNING]   ⚠️ Energy Discovery returned HTTP 500 in 0.6s
2026-03-19 18:45:18 UTC [INFO] ▶ Running: Simple Alerts → /api/jobs/simple-alerts
2026-03-19 18:45:19 UTC [INFO]   ✅ Simple Alerts completed in 0.9s (HTTP 200)
2026-03-19 18:45:19 UTC [INFO]      result: {'status': 'ok', 'processed': 0, 'matched': 0, 'message': 'No active alerts'}
2026-03-19 18:45:24 UTC [INFO] ▶ Running: Capacity Headroom → /api/jobs/capacity-headroom
2026-03-19 18:45:36 UTC [INFO]   ✅ Capacity Headroom completed in 11.7s (HTTP 200)
2026-03-19 18:45:36 UTC [INFO]      result: {'phoenix': {'market': 'phoenix', 'name': 'Phoenix, AZ', 'iso': 'CAISO', 'state': 'AZ', 'grid': {'installed_capacity_mw': 85000, 'current_demand_mw': 23069, 'spare_capacity_mw': 61931, 'spare_capacity_pct': 72.9, 'signal': 'green', 'demand_timestamp': '2026-03-19T18', 'data_source': 'EIA Live'}, 'gas': {'pipeline_count': 2, 'total_capacity_mdth': 9600.0, 'utilization_pct': 61.0, 'headroom_mdth': 3744.0, 'signal': 'green'}, 'power': {'local_plants': 188, 'local_capacity_mw': 46947.1, 'fuel_mix': 
2026-03-19 19:00:41 UTC [INFO] ▶ Running: Facility Discovery → /api/jobs/discovery
2026-03-19 19:02:14 UTC [WARNING]   ⚠️ Facility Discovery returned HTTP 502 in 92.7s
2026-03-19 20:00:19 UTC [INFO] ▶ Running: News/RSS Refresh → /api/jobs/news-refresh
2026-03-19 20:01:35 UTC [INFO]   ✅ News/RSS Refresh completed in 76.8s (HTTP 200)
2026-03-19 20:01:35 UTC [INFO]      new_articles: 352
2026-03-19 20:20:40 UTC [INFO] ▶ Running: Auto-Approve → /api/jobs/auto-approve
2026-03-19 20:20:49 UTC [INFO]   ✅ Auto-Approve completed in 9.1s (HTTP 200)
2026-03-19 20:20:49 UTC [INFO]      result: {"approved": 0, "batches": 2, "cache_age_seconds": 7, "cache_names": 11497, "count_after": 11800, "count_before": 11800, "duplicate_skipped": 60, "errors": 0, "flagged_review": 40, "net_new": 0, "stat
2026-03-19 20:30:54 UTC [INFO] ▶ Running: Evolution Engine → /api/jobs/evolution
2026-03-19 20:31:27 UTC [INFO]   ✅ Evolution Engine completed in 32.9s (HTTP 200)
2026-03-19 20:31:27 UTC [INFO]      result: {'cycle_id': '20260319_203055', 'started_at': '2026-03-19T20:30:55.080448', 'phases': {'observe': {'items_gathered': 0, 'sources_checked': 5, 'new_sources': 0, 'facility_stats': {'total_facilities': 11800, 'operators': 4016, 'countries': 179, 'updated_last_week': 12}, 'news_stats': {'total_articles': 13161, 'today': 113, 'unique_sources': 1204}, 'api_checks': [{'name': 'PeeringDB', 'status': 'healthy', 'response_time': 0.106292}, {'name': 'OpenStreetMap/Overpass', 'status': 'unhealthy', 'respons
2026-03-19 20:31:32 UTC [INFO] ▶ Running: Infrastructure Sync → /api/jobs/infrastructure-sync
2026-03-19 20:32:26 UTC [INFO]   ✅ Infrastructure Sync completed in 53.5s (HTTP 200)
2026-03-19 20:45:31 UTC [INFO] ▶ Running: Simple Alerts → /api/jobs/simple-alerts
2026-03-19 20:45:32 UTC [INFO]   ✅ Simple Alerts completed in 0.9s (HTTP 200)
2026-03-19 20:45:32 UTC [INFO]      result: {'status': 'ok', 'processed': 0, 'matched': 0, 'message': 'No active alerts'}
2026-03-19 21:00:37 UTC [INFO] ▶ Running: AI Outreach Agent → /api/jobs/ai-outreach
2026-03-19 21:01:05 UTC [INFO]      result: {'timestamp': '2026-03-19T21:00:37.192622+00:00', 'discovery_endpoints': [{'endpoint': '/llms.txt', 'status': 200, 'success': True}, {'endpoint': '/llms-full.txt', 'status': 200, 'success': True}, {'endpoint': '/robots.txt', 'status': 200, 'success': True}, {'endpoint': '/skill.json', 'status': 404, 'success': False}, {'endpoint': '/AGENTS.md', 'status': 200, 'success': True}], 'directories_pinged': [{'directory': 'gptstore', 'name': 'GPTStore.ai', 'target': 'https://gptstore.ai', 'action': 'hom
2026-03-19 21:01:05 UTC [INFO]   ✅ AI Outreach Agent completed in 28.5s (HTTP 200)
2026-03-19 21:15:10 UTC [INFO] ▶ Running: Alert Emails → /api/jobs/alert-emails
2026-03-19 21:15:11 UTC [INFO]   ✅ Alert Emails completed in 0.7s (HTTP 200)
2026-03-19 21:15:11 UTC [INFO]      result: {'alerts_checked': 0, 'emails_sent': 0}
2026-03-19 21:30:16 UTC [INFO] ▶ Running: Fiber Route Sync → /api/jobs/fiber-sync
2026-03-19 21:35:16 UTC [ERROR]   ❌ Fiber Route Sync — connection failed: The read operation timed out


New Agent

Develop, debug, deploy anything...

worker
3
