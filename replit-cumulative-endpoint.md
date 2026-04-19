"""
REPLIT TASK: Add /api/v1/ai-tracking/cumulative endpoint
=====================================================

The frontend AI dashboard needs all-time cumulative data from the ai_cumulative table.
Currently the /api/v1/ai-tracking/stats only reads from ai_usage_tracking (which has ~247 rows).
The ai_cumulative table in Neon has 67,000+ requests — the REAL totals.

ADD this new endpoint. Do NOT modify any existing endpoints.

Endpoint: GET /api/v1/ai-tracking/cumulative

What it should do:
1. Query Neon PostgreSQL: SELECT * FROM ai_cumulative ORDER BY total_requests DESC
2. Return JSON array of all rows

Expected response format:
[
  {"platform": "direct", "total_requests": 65549, "first_seen": "2026-02-10T06:22:25", "last_seen": "2026-02-16T09:21:21"},
  {"platform": "chatgpt", "total_requests": 35, "first_seen": "2026-02-02T01:05:15", "last_seen": "2026-02-15T23:58:11"},
  ...
]

Also update /api/v1/ai-tracking/stats to include:
- total_all_time: SUM of total_requests from ai_cumulative table
- today_count: COUNT from ai_usage_tracking WHERE date(timestamp) = CURRENT_DATE
  (or from ai_requests WHERE date(created_at) = CURRENT_DATE, whichever table has today's data)

CORS: Both endpoints need CORS headers for https://dchub.cloud

Do NOT modify or delete any existing data or endpoints.
Do NOT change the ai_cumulative table structure.
Just ADD the new endpoint and UPDATE the stats endpoint response.
"""
