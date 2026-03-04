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

SCHEDULE (UTC, 4hr gaps so crawlers never overlap):
  Run 1: 06:00 News → 10:00 Energy/Power → 14:00 Knowledge
  Run 2: 18:00 News → 22:00 Energy/Power → 02:00 Knowledge

NOTE: api_discovery is available for manual trigger only — it's too heavy
for scheduled runs (exhausts DB connection pool and crashes the app).
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
# 4-hour gaps between each crawler for safety
# api_discovery EXCLUDED — too heavy, available via manual trigger only
SCHEDULE = [
    (6,  18, "news",             "_run_news_crawler"),
    (10, 22, "energy_discovery", "_run_energy_discovery"),
    (14,  2, "knowledge_sync",   "_run_knowledge_sync"),
    ( 8, 20, "deals",            "_run_deals_crawler"),
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
        "manual_only": ["api_discovery"],
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
        ns = NewsSyncer(interval_seconds=0)
        ns.sync()
    except ImportError:
        try:
            from sync_news import sync_all_news
            sync_all_news()
        except ImportError:
            logger.warning("News crawler not available (no auto_sync or sync_news module)")


def _run_api_discovery():
    """Run API auto-discovery once.
    WARNING: This is heavy — only available via manual trigger, not scheduled.
    """
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
        try:
            from evolution_engine import run_evolution
            run_evolution()
        except (ImportError, AttributeError):
            logger.warning("Knowledge/evolution engine not available or no run method found")
    except Exception as e:
        logger.error(f"Knowledge sync error: {e}")


# Map names to functions (includes manual-only crawlers)
_RUNNERS = {
    "news":             _run_news_crawler,
    "api_discovery":    _run_api_discovery,
    "energy_discovery": _run_energy_discovery,
    "knowledge_sync":   _run_knowledge_sync,
    "deals":            _run_deals_crawler,
}


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

def _should_run_now(hour1, hour2, now_hour, now_minute, last_run_hours):
    """Check if a crawler should run based on current time."""
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
    logger.info(f"   Manual-only: api_discovery (too heavy for scheduled runs)")
    
    last_run_hours = {}
    last_reset_day = None
    
    while not _stop_event.is_set():
        try:
            now = datetime.now(timezone.utc)
            
            if last_reset_day != now.day:
                last_run_hours = {s[2]: set() for s in SCHEDULE}
                last_reset_day = now.day
                logger.info(f"📅 New day — reset crawler schedule tracking")
            
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
        
        _stop_event.wait(60)
    
    logger.info("📅 Crawler scheduler stopped")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_scheduled_crawlers():
    """Start the staggered crawler scheduler.
    Runs on Railway only — Replit is API-only failover.
    """
    global _scheduler_thread
    
    if os.environ.get("DISABLE_ALL_CRAWLERS", "").lower() in ("true", "1", "yes"):
        logger.info("📅 Crawler scheduler DISABLED (DISABLE_ALL_CRAWLERS=true)")
        return
    
    is_replit = os.environ.get("REPL_ID") or os.environ.get("REPLIT_DB_URL") or os.environ.get("REPL_SLUG")
    if is_replit:
        logger.info("📅 Crawler scheduler DISABLED (Replit = API-only failover)")
        return
    
    _lock_file = "/tmp/.crawler_scheduler.lock"
    try:
        import fcntl
        _lock_fd = open(_lock_file, 'w')
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
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
    """Manually trigger a specific crawler (for admin endpoint)."""
    if crawler_name not in _RUNNERS:
        return False, f"Unknown crawler: {crawler_name}. Available: {list(_RUNNERS.keys())}"
    
    if _active_crawler:
        return False, f"Cannot start {crawler_name} — {_active_crawler} is currently running"
    
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
    """Register admin endpoints for crawler management."""
    
    @app.route('/api/admin/crawler-status', methods=['GET'])
    def crawler_status():
        from flask import jsonify
        return jsonify(get_scheduler_status())
    
    @app.route('/api/admin/crawler-run/<crawler_name>', methods=['POST'])
    def crawler_run(crawler_name):
        from flask import jsonify
        success, message = run_crawler_now(crawler_name)
        return jsonify({"success": success, "message": message}), 200 if success else 409
    
    logger.info("📅 Crawler admin endpoints registered: /api/admin/crawler-status, /api/admin/crawler-run/<crawler_name>")


def _run_deals_crawler():
    """Run AI deals discovery and save to Neon PostgreSQL."""
    import os, re, hashlib, requests, psycopg2
    from datetime import datetime, timezone

    logger.info("💼 Deals crawler starting...")

    FEEDS = [
        "https://www.datacenterdynamics.com/rss/",
        "https://www.datacenterknowledge.com/rss.xml",
        "https://www.bisnow.com/national/news/data-center/rss",
        "https://feeds.bloomberg.com/technology/news.rss",
        "https://www.prnewswire.com/rss/news-releases-list.rss",
        "https://www.businesswire.com/rss/home/?rss=G7",
    ]

    DEAL_KEYWORDS = [
        'acqui', 'merger', 'acquisition', 'invest', 'joint venture', 'jv',
        'data center', 'datacenter', 'colocation', 'hyperscale',
        'billion', 'million', '$', 'deal', 'transaction', 'purchase',
        'partnership', 'fund', 'equity', 'debt', 'lease', 'capex'
    ]

    VALUE_RE = re.compile(r'\$\s*([\d,.]+)\s*(billion|million|B|M)\b', re.IGNORECASE)
    BUYER_RE = re.compile(
        r'([\w\s/&,]+?)\s+(?:acquires?|buys?|invests?\s+in|partners?\s+with|closes?\s+|announces?|completes?)',
        re.IGNORECASE
    )

    def extract_value_millions(text):
        m = VALUE_RE.search(text)
        if not m:
            return None
        num = float(m.group(1).replace(',', ''))
        unit = m.group(2).lower()
        return num * 1000 if unit in ('billion', 'b') else num

    def extract_buyer(text):
        m = BUYER_RE.search(text)
        if m:
            b = m.group(1).strip().rstrip(',')
            if len(b) > 3 and len(b) < 80:
                return b
        return None

    def is_deal_relevant(text):
        tl = text.lower()
        return sum(1 for kw in DEAL_KEYWORDS if kw in tl) >= 2

    try:
        import feedparser
    except ImportError:
        logger.warning("feedparser not available — skipping deals crawler")
        return

    conn = psycopg2.connect(os.environ['DATABASE_URL'])
    cur = conn.cursor()
    saved = 0

    for feed_url in FEEDS:
        if _stop_event.is_set():
            break
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:20]:
                title = entry.get('title', '')
                summary = entry.get('summary', '') or entry.get('description', '')
                text = f"{title} {summary}"

                if not is_deal_relevant(text):
                    continue

                value_m = extract_value_millions(text)
                buyer = extract_buyer(title) or extract_buyer(summary)

                if not buyer or len(buyer) < 3:
                    continue

                # Generate stable ID
                deal_id = hashlib.md5(f"{buyer}{title}".encode()).hexdigest()[:16]

                # Parse date
                published = entry.get('published_parsed')
                if published:
                    deal_date = datetime(*published[:3]).strftime('%Y-%m-%d')
                    deal_year = published[0]
                else:
                    deal_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                    deal_year = datetime.now(timezone.utc).year

                # Determine type
                tl = text.lower()
                if 'acqui' in tl or 'merger' in tl:
                    deal_type = 'acquisition'
                elif 'joint venture' in tl or ' jv ' in tl:
                    deal_type = 'jv'
                elif 'debt' in tl or 'loan' in tl or 'financ' in tl:
                    deal_type = 'debt'
                elif 'equity' in tl or 'invest' in tl:
                    deal_type = 'equity'
                elif 'lease' in tl:
                    deal_type = 'lease'
                elif 'capex' in tl or 'capital' in tl:
                    deal_type = 'capex'
                else:
                    deal_type = 'investment'

                try:
                    cur.execute("""
                        INSERT INTO deals (id, date, year, buyer, seller, value, type, region, market, source_url, created_at, verified)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), false)
                        ON CONFLICT (id) DO NOTHING
                    """, (
                        deal_id, deal_date, deal_year,
                        buyer[:100], 'Undisclosed',
                        value_m, deal_type,
                        None, None,
                        entry.get('link', feed_url)[:500]
                    ))
                    if cur.rowcount:
                        saved += 1
                except Exception as e:
                    logger.warning(f"Deal insert error: {e}")
                    conn.rollback()

        except Exception as e:
            logger.warning(f"Feed error {feed_url}: {e}")
        
        time.sleep(2)  # Rate limit between feeds

    conn.commit()
    cur.close()
    conn.close()
    logger.info(f"💼 Deals crawler done — {saved} new deals saved to Neon")
