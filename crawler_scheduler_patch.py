"""
PATCH: Add Land & Power crawler to crawler_scheduler.py
═══════════════════════════════════════════════════════════

APPLY MANUALLY: Add these sections to your existing crawler_scheduler.py

────────────────────────────────────────────────────
STEP 1: Add to SCHEDULE list (around line 39)
────────────────────────────────────────────────────
"""

# ADD THIS LINE to the SCHEDULE list in crawler_scheduler.py:
# (3,  15, "land_power_sync",    "_run_land_power_sync"),
#
# This schedules land/power at 03:00 and 15:00 UTC
# (offset from other crawlers to avoid overlap)
#
# Your SCHEDULE list should look like:
#
# SCHEDULE = [
#     (6,  18, "news",                "_run_news_crawler"),
#     (10, 22, "energy_discovery",    "_run_energy_discovery"),
#     (14,  2, "knowledge_sync",      "_run_knowledge_sync"),
#     ( 8, 20, "deals",               "_run_deals_crawler"),
#     ( 9, 21, "market_refresh",      "_run_market_refresh"),
#     ( 7, 19, "facility_discovery",  "_run_facility_discovery"),
#     (12,  0, "infrastructure_sync", "_run_infrastructure_sync"),
#     ( 3, 15, "land_power_sync",     "_run_land_power_sync"),   # <-- NEW
# ]

"""
────────────────────────────────────────────────────
STEP 2: Add runner function (after _run_infrastructure_sync)
────────────────────────────────────────────────────
"""

def _run_land_power_sync():
    """Run land & power infrastructure crawl (EIA + HIFLD).
    Crawls power plants, substations, transmission lines, gas pipelines.
    Generates market power profiles for all 42 DC Hub markets.
    """
    try:
        from land_power_crawler import run_land_power_sync
        from db_utils import get_db
        # Run incremental by default, full refresh on Sundays
        from datetime import datetime, timezone
        is_sunday = datetime.now(timezone.utc).weekday() == 6
        run_land_power_sync(get_db, full_refresh=is_sunday)
    except ImportError:
        logger.warning("Land & Power crawler not available (no land_power_crawler module)")
    except Exception as e:
        logger.error(f"Land & Power sync error: {e}")


"""
────────────────────────────────────────────────────
STEP 3: Add to manual-only status display (get_scheduler_status)
────────────────────────────────────────────────────

In get_scheduler_status(), the schedule will automatically pick up
the new entry since it iterates SCHEDULE.

────────────────────────────────────────────────────
STEP 4: Add API route for manual trigger in main.py
────────────────────────────────────────────────────

In main.py, after other route registrations:

    from land_power_crawler import register_land_power_routes, init_land_power_tables
    register_land_power_routes(app, get_db, require_admin)

    # Initialize tables on startup (safe to call multiple times)
    try:
        init_land_power_tables(get_db)
    except:
        pass

────────────────────────────────────────────────────
STEP 5: Add MCP tier config to main.py
────────────────────────────────────────────────────

In main.py, after other imports:

    from mcp_tier_config import (
        register_mcp_trial_routes,
        init_mcp_tier_tables,
        gate_response,
        get_tier,
        track_mcp_usage
    )

    # Register trial routes
    register_mcp_trial_routes(app, get_db)

    # Initialize tables
    try:
        init_mcp_tier_tables(get_db)
    except:
        pass

────────────────────────────────────────────────────
STEP 6: Wrap MCP endpoints with tier gating
────────────────────────────────────────────────────

In each MCP endpoint handler, wrap the response:

    @app.route('/api/mcp/search-facilities')
    def mcp_search_facilities():
        api_key = request.headers.get('X-API-Key', '')
        tier = get_tier(api_key, get_db)
        usage = track_mcp_usage(api_key or request.remote_addr, get_db)

        # ... existing logic to build raw_data ...

        # Gate the response based on tier
        gated = gate_response(tier, 'search_facilities', raw_data, usage)
        return jsonify(gated)

────────────────────────────────────────────────────
STEP 7: Add EIA_API_KEY to Railway environment
────────────────────────────────────────────────────

In Railway dashboard → Variables, add:

    EIA_API_KEY=your_key_here

Get a free key at: https://www.eia.gov/opendata/register.php
(Instant registration, no approval needed)

Without this key, the power plant and gas pipeline crawlers will
skip with a warning. HIFLD crawlers (substations, transmission)
work without any key.
"""
