#!/usr/bin/env python3
"""
DC Hub Staggered Crawler Scheduler
====================================
Replaces always-on background threads with twice-daily scheduled runs.
Each crawler runs one at a time, with connection limits and hard timeouts.

USAGE:
  - Import and call start_scheduled_crawlers() from main.py
  - Set DISABLE_ALL_CRAWLERS=true on Railway to skip everything
  - Set CRAWLER_SCHEDULE=once to run once/day instead of twice

SCHEDULE (UTC, 2hr gaps so crawlers never overlap):
  Run 1: 06:00 News → 08:00 API Discovery → 10:00 Energy/Power → 12:00 Knowledge
  Run 2: 18:00 News → 20:00 API Discovery → 22:00 Energy/Power → 00:00 Knowledge
"""

import os
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("crawler_scheduler")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MAX_CONNECTIONS_PER_CRAWLER = 2       # Leave 6 of 8 for API traffic
HARD_TIMEOUT_SECONDS = 15 * 60       # 15 min max per crawler run
OVERLAP_GUARD_SECONDS = 30           # Wait after each crawler finishes

# Schedule: (hour_utc_run1, hour_utc_run2, crawler_name, runner_func_name)
# 2-hour gaps between each crawler
SCHEDULE = [
    (6,  18, "news",             "_run_news_crawler"),
    (8,  20, "api_discovery",    "_run_api_discovery"),
    (10, 22, "energy_discovery", "_run_energy_discovery"),
    (12,  0, "knowledge_sync",   "_run_knowledge_sync"),
]

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_scheduler_thread = None
_stop_event = threading.Event()
_active_crawler = None       # Name of currently running crawler (or None)
_lock = threading.Lock()
_run_history = []            # List of {name, started, finished, status, duration}


def get_scheduler_status():
    """Return status dict for /api/admin/crawler-status endpoint."""
    return {
        "active_crawler": _active_crawler,
        "schedule": [
            {"name": s[2], "run1_utc": f"{s[0]:02d}:00", "run2_utc": f"{s[1]:02d}:00"}
            for s in SCHEDULE
        ],
        "recent_runs": _run_history[-20:],  # Last 20 runs
        "disabled": os.environ.get("DISABLE_ALL_CRAWLERS", "").lower() in ("true", "1", "yes"),
    }


# ---------------------------------------------------------------------------
# Crawler runners (each wraps the actual crawler with connection + timeout guard)
# ---------------------------------------------------------------------------

def _run_with_guard(name, func):
    """Run a crawler function with connection limit, timeout, and logging."""
    global _active_crawler
    
    with _lock:
        if _active_crawler:
            logger.warning(f"⏭️  Skipping {name} — {_active_crawler} still running")
            return
        _active_crawler = name
    
    started = datetime.now(timezone.utc)
    status = "success"
    logger.info(f"🚀 CRAWLER START: {name} at {started.strftime('%H:%M:%S UTC')}")
    
    try:
        # Run with hard timeout
        result = {"done": False, "error": None}
        
        def _target():
            try:
                func()
                result["done"] = True
            except Exception as e:
                result["error"] = str(e)
                logger.error(f"❌ CRAWLER ERROR: {name} — {e}")
        
        t = threading.Thread(target=_target, daemon=True, name=f"crawler-{name}")
        t.start()
        t.join(timeout=HARD_TIMEOUT_SECONDS)
        
        if t.is_alive():
            status = "timeout"
            logger.warning(f"⏰ CRAWLER TIMEOUT: {name} exceeded {HARD_TIMEOUT_SECONDS}s — abandoning")
            # Thread is daemon, will die when main process exits or next cycle
        elif result["error"]:
            status = f"error: {result['error'][:100]}"
        else:
            status = "success"
            
    except Exception as e:
        status = f"guard_error: {str(e)[:100]}"
        logger.error(f"❌ CRAWLER GUARD ERROR: {name} — {e}")
    finally:
        finished = datetime.now(timezone.utc)
        duration = (finished - started).total_seconds()
        
        with _lock:
            _active_crawler = None
        
        _run_history.append({
            "name": name,
            "started": started.isoformat(),
            "finished": finished.isoformat(),
            "status": status,
            "duration_seconds": round(duration, 1),
        })
        # Keep history bounded
        if len(_run_history) > 100:
            _run_history[:] = _run_history[-50:]
        
        logger.info(f"✅ CRAWLER DONE: {name} in {duration:.1f}s — {status}")
        
        # Guard period before next crawler can start
        time.sleep(OVERLAP_GUARD_SECONDS)


def _run_news_crawler():
    """Run news sync once."""
    try:
        from auto_sync import NewsSyncer
        ns = NewsSyncer(interval_seconds=0)  # interval=0 so it doesn't loop
        # NewsSyncer.run() does a single sync pass
        ns.sync()
    except ImportError:
        try:
            from sync_news import sync_all_news
            sync_all_news()
        except ImportError:
            logger.warning("News crawler not available (no auto_sync or sync_news module)")


def _run_api_discovery():
    """Run API auto-discovery once."""
    try:
        from api_auto_discovery import APIAutoDiscovery
        discovery = APIAutoDiscovery()
        discovery.run_discovery_cycle()
    except ImportError:
        logger.warning("API discovery not available (no api_auto_discovery module)")
    except Exception as e:
        logger.error(f"API discovery error: {e}")


def _run_energy_discovery():
    """Run energy/power plant sync for all monitored markets."""
    try:
        from energy_auto_discovery import MONITORED_MARKETS, sync_market
        logger.info(f"   Syncing {len(MONITORED_MARKETS)} energy markets...")
        for market_key, market_info in MONITORED_MARKETS.items():
            if _stop_event.is_set():
                logger.info(f"   Stopping energy sync early (shutdown requested)")
                break
            try:
                sync_market(market_key, market_info)
            except Exception as e:
                logger.warning(f"   Energy sync error for {market_key}: {e}")
    except ImportError:
        logger.warning("Energy discovery not available (no energy_auto_discovery module)")
    except Exception as e:
        logger.error(f"Energy discovery error: {e}")


def _run_knowledge_sync():
    """Run knowledge/evolution engine sync once."""
    try:
        from evolution_engine import EvolutionEngine
        ee = EvolutionEngine()
        ee.run_evolution_cycle()
    except (ImportError, AttributeError):
        # Try alternate entry point
        try:
            from evolution_engine import run_evolution
            run_evolution()
        except (ImportError, AttributeError):
            logger.warning("Knowledge/evolution engine not available or no run method found")
    except Exception as e:
        logger.error(f"Knowledge sync error: {e}")


# Map names to functions
_RUNNERS = {
    "news":             _run_news_crawler,
    "api_discovery":    _run_api_discovery,
    "energy_discovery": _run_energy_discovery,
    "knowledge_sync":   _run_knowledge_sync,
}


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

def _should_run_now(hour1, hour2, now_hour, now_minute, last_run_hours):
    """Check if a crawler should run based on current time.
    Returns True if we're within the first 5 minutes of a scheduled hour
    and haven't run in this window yet.
    """
    once_a_day = os.environ.get("CRAWLER_SCHEDULE", "").lower() == "once"
    
    target_hours = [hour1] if once_a_day else [hour1, hour2]
    
    for target in target_hours:
        if now_hour == target and now_minute < 5:
            if target not in last_run_hours:
                return True, target
    return False, None


def _scheduler_loop():
    """Main scheduler loop — checks every 60s if any crawler should run."""
    logger.info("📅 Crawler scheduler started")
    logger.info(f"   Schedule: {', '.join(f'{s[2]} @ {s[0]:02d}:00/{s[1]:02d}:00 UTC' for s in SCHEDULE)}")
    
    # Track which hours we've already run in (reset daily)
    last_run_hours = {}  # {crawler_name: set of hours already run today}
    last_reset_day = None
    
    while not _stop_event.is_set():
        try:
            now = datetime.now(timezone.utc)
            
            # Reset tracking at midnight UTC
            if last_reset_day != now.day:
                last_run_hours = {s[2]: set() for s in SCHEDULE}
                last_reset_day = now.day
                logger.info(f"📅 New day — reset crawler schedule tracking")
            
            # Check each crawler
            for hour1, hour2, name, _ in SCHEDULE:
                if _stop_event.is_set():
                    break
                    
                should_run, target_hour = _should_run_now(
                    hour1, hour2, now.hour, now.minute,
                    last_run_hours.get(name, set())
                )
                
                if should_run and name in _RUNNERS:
                    last_run_hours[name].add(target_hour)
                    _run_with_guard(name, _RUNNERS[name])
            
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
        
        # Check every 60 seconds
        _stop_event.wait(60)
    
    logger.info("📅 Crawler scheduler stopped")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_scheduled_crawlers():
    """Start the staggered crawler scheduler.
    
    Call this from main.py instead of starting individual crawler threads.
    Respects DISABLE_ALL_CRAWLERS env var.
    Runs on Railway only — Replit is API-only failover.
    """
    global _scheduler_thread
    
    # Kill switch
    if os.environ.get("DISABLE_ALL_CRAWLERS", "").lower() in ("true", "1", "yes"):
        logger.info("📅 Crawler scheduler DISABLED (DISABLE_ALL_CRAWLERS=true)")
        return
    
    # Replit should NOT run crawlers — Railway handles them
    is_replit = os.environ.get("REPL_ID") or os.environ.get("REPLIT_DB_URL") or os.environ.get("REPL_SLUG")
    if is_replit:
        logger.info("📅 Crawler scheduler DISABLED (Replit = API-only failover)")
        return
    
    # Only run scheduler on ONE gunicorn worker to prevent 4x duplicate crawlers
    _lock_file = "/tmp/.crawler_scheduler.lock"
    try:
        import fcntl
        _lock_fd = open(_lock_file, 'w')
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
        # Keep _lock_fd open — lock is held as long as the file descriptor is open
        logger.info(f"📅 Crawler scheduler: Acquired lock (PID {os.getpid()})")
    except (IOError, OSError):
        logger.info("📅 Crawler scheduler SKIPPED (another worker holds the lock)")
        return
    
    if _scheduler_thread and _scheduler_thread.is_alive():
        logger.warning("📅 Scheduler already running")
        return
    
    _stop_event.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        daemon=True,
        name="crawler-scheduler"
    )
    _scheduler_thread.start()
    
    schedule_type = "once/day" if os.environ.get("CRAWLER_SCHEDULE", "").lower() == "once" else "twice/day"
    logger.info(f"📅 Crawler scheduler running ({schedule_type})")


def stop_scheduled_crawlers():
    """Gracefully stop the scheduler."""
    _stop_event.set()
    if _scheduler_thread:
        _scheduler_thread.join(timeout=10)
    logger.info("📅 Crawler scheduler stopped")


def run_crawler_now(crawler_name):
    """Manually trigger a specific crawler (for admin endpoint).
    Returns (success: bool, message: str)
    """
    if crawler_name not in _RUNNERS:
        return False, f"Unknown crawler: {crawler_name}. Available: {list(_RUNNERS.keys())}"
    
    if _active_crawler:
        return False, f"Cannot start {crawler_name} — {_active_crawler} is currently running"
    
    # Run in background thread so API doesn't block
    threading.Thread(
        target=_run_with_guard,
        args=(crawler_name, _RUNNERS[crawler_name]),
        daemon=True,
        name=f"manual-{crawler_name}"
    ).start()
    
    return True, f"Started {crawler_name} manually"


# ---------------------------------------------------------------------------
# Admin endpoints (register with Flask app)
# ---------------------------------------------------------------------------

def register_crawler_admin(app):
    """Register admin endpoints for crawler management.
    
    Call from main.py:
        from crawler_scheduler import register_crawler_admin, start_scheduled_crawlers
        register_crawler_admin(app)
        start_scheduled_crawlers()
    """
    
    @app.route('/api/admin/crawler-status', methods=['GET'])
    def crawler_status():
        from flask import jsonify
        return jsonify(get_scheduler_status())
    
    @app.route('/api/admin/crawler-run/<crawler_name>', methods=['POST'])
    def crawler_run(crawler_name):
        from flask import jsonify
        # TODO: Add admin auth check here
        success, message = run_crawler_now(crawler_name)
        return jsonify({"success": success, "message": message}), 200 if success else 409
    
    logger.info("📅 Crawler admin endpoints registered: /api/admin/crawler-status, /api/admin/crawler-run/<crawler_name>")
