"""
DC Hub — crawler_scheduler.py KMZ Integration Patch
=====================================================
Add this code to your existing crawler_scheduler.py to schedule the
KMZ auto-discovery engine alongside your existing crawlers.

HOW TO APPLY:
  1. Open dchub-backend/crawler_scheduler.py
  2. Find your CRAWLER_SCHEDULE dict (where news, energy_discovery, etc. live)
  3. Add the KMZ entry from STEP 1 below
  4. Add the run_kmz_discovery function from STEP 2 below
  5. Add the dispatcher case from STEP 3 below

After applying, Railway logs will show:
  📅 Schedule: ..., kmz_discovery @ 03:00/15:00 UTC, ...
"""

# =============================================================================
# STEP 1 — Add to your CRAWLER_SCHEDULE dict
# =============================================================================
# Find: CRAWLER_SCHEDULE = { ... }
# Add this entry:

KMZ_SCHEDULE_ENTRY = {
    'kmz_discovery': {
        'times':       ['03:00', '15:00'],   # UTC — runs at 3am and 3pm
        'description': 'KMZ fiber + infrastructure auto-discovery (v4.0)',
        'max_per_day': 2,
        'enabled':     True,
    }
}

# =============================================================================
# STEP 2 — Add this function near your other crawler runner functions
# =============================================================================

def run_kmz_discovery(app=None):
    """
    Trigger a full KMZ auto-discovery cycle.
    Discovers fiber routes, state broadband layers, and ArcGIS sources.
    Compatible with the existing crawler_scheduler dispatch pattern.
    """
    import logging
    logger = logging.getLogger('crawler_scheduler')

    try:
        from kmz_auto_discovery import _kmz_instance

        if _kmz_instance is None:
            logger.warning("KMZ: _kmz_instance not initialized — skipping")
            return {'success': False, 'reason': 'not_initialized'}

        logger.info("📡 KMZ Discovery: starting scheduled cycle")
        results = _kmz_instance.run_discovery_cycle()

        new_routes = results.get('total_new_routes', 0)
        new_km     = round(results.get('total_new_km', 0), 1)
        duration   = results.get('cycle_duration_seconds', 0)

        logger.info(
            f"📡 KMZ Discovery: ✅ complete — "
            f"{new_routes} new routes, {new_km} km, {duration}s"
        )
        return {
            'success':     True,
            'new_routes':  new_routes,
            'new_km':      new_km,
            'duration_s':  duration,
            'details':     results,
        }

    except Exception as e:
        logger.error(f"📡 KMZ Discovery: ❌ error — {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


# =============================================================================
# STEP 3 — Add this case to your crawler dispatcher (run_crawler function)
# =============================================================================
# Find your run_crawler (or equivalent) function that dispatches by name.
# It likely looks like:
#
#   def run_crawler(name, app=None):
#       if name == 'news':
#           return run_news_crawler(app)
#       elif name == 'energy_discovery':
#           return run_energy_discovery(app)
#       ...
#
# Add this elif:

KMZ_DISPATCHER_CASE = """
    elif name == 'kmz_discovery':
        return run_kmz_discovery(app)
"""

# =============================================================================
# STEP 4 — (Optional) Admin endpoint already exists — test it after deploy:
# =============================================================================
#
#   POST https://dchub-backend-production.up.railway.app/api/admin/crawler-run/kmz_discovery
#   Authorization: Bearer <your-admin-jwt>
#
# Expected response:
#   {
#     "success": true,
#     "crawler": "kmz_discovery",
#     "result": {
#       "new_routes": 42,
#       "new_km": 1823.4,
#       "duration_s": 38.2,
#       ...
#     }
#   }
#
# Health check (no auth needed):
#   GET https://dchub-backend-production.up.railway.app/api/kmz/health

# =============================================================================
# STEP 5 — Full schedule after patch (for reference)
# =============================================================================
FULL_SCHEDULE_AFTER_PATCH = {
    'news':              {'times': ['06:00', '18:00'], 'description': 'News crawler'},
    'energy_discovery':  {'times': ['10:00', '22:00'], 'description': 'Energy market discovery'},
    'knowledge_sync':    {'times': ['14:00', '02:00'], 'description': 'Knowledge base sync'},
    'deals':             {'times': ['08:00', '20:00'], 'description': 'Deals & transactions'},
    'market_refresh':    {'times': ['09:00', '21:00'], 'description': 'Market data refresh'},
    'facility_discovery':{'times': ['07:00', '19:00'], 'description': 'Facility discovery'},
    'infrastructure_sync':{'times':['12:00', '00:00'], 'description': 'Infrastructure sync'},
    'kmz_discovery':     {'times': ['03:00', '15:00'], 'description': 'KMZ fiber auto-discovery v4.0'},
    # manual-only (unchanged):
    # 'api_discovery': {...}
}
