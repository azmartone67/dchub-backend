#!/usr/bin/env python3
"""
DC Hub Staggered Crawler Scheduler 2.0
====================================
Replaces always-on background threads with twice-daily scheduled runs.
Each crawler runs one at a time, with connection limits and hard timeouts.

USAGE:
  - Import and call start_scheduled_crawlers() from main.py
  - Set DISABLE_ALL_CRAWLERS=true on Railway to skip everything
  - Set CRAWLER_SCHEDULE=once to run once/day instead of twice

SCHEDULE (UTC, staggered so crawlers never overlap):
  Run 1: 06:00 News → 07:00 Facilities → 08:00 Deals → 10:00 Energy → 12:00 Infra → 14:00 Knowledge
  Run 2: 18:00 News → 19:00 Facilities → 20:00 Deals → 22:00 Energy → 00:00 Infra → 02:00 Knowledge

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
    (6,  18, "news",                "_run_news_crawler"),
    (10, 22, "energy_discovery",    "_run_energy_discovery"),
    (14,  2, "knowledge_sync",      "_run_knowledge_sync"),
    ( 8, 20, "deals",               "_run_deals_crawler"),
    ( 9, 21, "market_refresh",      "_run_market_refresh"),
    ( 7, 19, "facility_discovery",  "_run_facility_discovery"),
    (12,  0, "infrastructure_sync", "_run_infrastructure_sync"),
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
    """Run knowledge/evolution engine sync + AI growth engines.
    This is the 'tell AI about us' job — runs outreach, promotion, and MCP registration.
    """
    # STEP 1: Evolution engine (trend analysis, insights)
    try:
        from evolution_engine import EvolutionEngine
        ee = EvolutionEngine()
        ee.run_evolution_cycle()
        logger.info("   [1/4] Evolution engine: completed")
    except (ImportError, AttributeError):
        try:
            from evolution_engine import run_evolution
            run_evolution()
            logger.info("   [1/4] Evolution engine: completed (fallback)")
        except (ImportError, AttributeError):
            logger.warning("   [1/4] Evolution engine: not available")
    except Exception as e:
        logger.error(f"   [1/4] Evolution engine error: {e}")

    if _stop_event.is_set():
        return

    # STEP 2: AI Outreach — tell ChatGPT, Gemini, Perplexity, Claude about DC Hub
    try:
        from ai_outreach_agent import run_outreach_cycle
        result = run_outreach_cycle()
        logger.info(f"   [2/4] AI Outreach: completed — {result if result else 'cycle done'}")
    except ImportError:
        logger.warning("   [2/4] AI Outreach: not available (no ai_outreach_agent)")
    except Exception as e:
        logger.warning(f"   [2/4] AI Outreach error: {e}")

    if _stop_event.is_set():
        return

    # STEP 3: Promotion engine — submit to AI directories (GPTStore, Futurepedia, Toolify, etc)
    try:
        from enhanced_promotion_engine import run_cycle as run_promotion
        result = run_promotion()
        logger.info(f"   [3/4] Promotion engine: completed — {result if result else 'cycle done'}")
    except ImportError:
        logger.warning("   [3/4] Promotion engine: not available")
    except Exception as e:
        logger.warning(f"   [3/4] Promotion error: {e}")

    if _stop_event.is_set():
        return

    # STEP 4: MCP Auto-Register — keep DC Hub listed on Smithery, Glama, PulseMCP, etc
    try:
        from mcp_auto_register import MCPAutoRegister
        mar = MCPAutoRegister()
        mar.run_cycle()
        logger.info("   [4/4] MCP Auto-Register: completed")
    except ImportError:
        try:
            from mcp_auto_register import run_cycle as run_mcp_reg
            run_mcp_reg()
            logger.info("   [4/4] MCP Auto-Register: completed (fallback)")
        except (ImportError, AttributeError):
            logger.warning("   [4/4] MCP Auto-Register: not available")
    except Exception as e:
        logger.warning(f"   [4/4] MCP Auto-Register error: {e}")

    logger.info("   === KNOWLEDGE + GROWTH ENGINES COMPLETE ===")


def _run_market_refresh():
    """Refresh market intelligence: deals sync, GDCI recompute, market report, AI Wars, market_intelligence.
    This is the most comprehensive scheduled job — keeps ALL analytics sections current.
    7-step chain: deals → report → AI Wars → ecosystem → pipeline → GDCI → market_intelligence.
    Scheduled: 09:00/21:00 UTC.
    """
    logger.info("   === MARKET REFRESH START (7 steps) ===")

    # STEP 1: Refresh deals from news sources
    try:
        from deal_scraper import DealScraper
        ds = DealScraper()
        result = ds.scrape_all()
        new_deals = result.get('new_deals', 0) if isinstance(result, dict) else 0
        logger.info(f"   [1/7] Deals scraper: +{new_deals} new deals")
    except ImportError:
        # Fallback: trigger via HTTP
        try:
            import urllib.request
            req = urllib.request.Request(
                'https://dchub-backend-production.up.railway.app/api/deals/refresh',
                method='POST',
                headers={'X-Admin-Key': os.environ.get('DCHUB_ADMIN_KEY', '')}
            )
            urllib.request.urlopen(req, timeout=30)
            logger.info("   [1/7] Deals refresh: triggered via API")
        except Exception as e2:
            logger.warning(f"   [1/7] Deals refresh error: {e2}")
    except Exception as e:
        logger.warning(f"   [1/7] Deals scraper error: {e}")

    if _stop_event.is_set():
        return

    # STEP 2: Regenerate market report
    try:
        import urllib.request, json
        req = urllib.request.Request(
            'https://dchub-backend-production.up.railway.app/api/market-report/generate',
            method='POST',
            headers={'X-Admin-Key': os.environ.get('DCHUB_ADMIN_KEY', '')}
        )
        resp = urllib.request.urlopen(req, timeout=30)
        logger.info("   [2/7] Market report: regenerated")
    except Exception as e:
        logger.warning(f"   [2/7] Market report error: {e}")

    if _stop_event.is_set():
        return

    # STEP 3: AI Wars auto-challenge (benchmark AI platforms against DC Hub)
    try:
        import urllib.request, json as _json
        categories = ['mcp-tool-test', 'site-selection', 'construction-pipeline', 'energy-ppa', 'ma-forensics', 'operator-showdown']
        import random
        cat = random.choice(categories)
        req = urllib.request.Request(
            'https://dchub-backend-production.up.railway.app/api/v1/ai-wars/auto-battle',
            method='POST',
            data=_json.dumps({'category': cat}).encode('utf-8'),
            headers={
                'X-Admin-Key': os.environ.get('DCHUB_ADMIN_KEY', ''),
                'Content-Type': 'application/json',
            }
        )
        resp = urllib.request.urlopen(req, timeout=120)
        result_data = _json.loads(resp.read())
        winner = result_data.get('winner', 'unknown')
        logger.info(f"   [3/7] AI Wars: {cat} battle — winner: {winner}")
    except Exception as e:
        logger.warning(f"   [3/7] AI Wars error: {e}")

    if _stop_event.is_set():
        return

    # STEP 4: Ecosystem discovery (scan for new DC companies)
    try:
        import urllib.request
        req = urllib.request.Request(
            'https://dchub-backend-production.up.railway.app/api/ai-ecosystem/run',
            method='POST',
            headers={'X-Admin-Key': os.environ.get('DCHUB_ADMIN_KEY', '')}
        )
        urllib.request.urlopen(req, timeout=30)
        logger.info("   [4/7] Ecosystem discovery: triggered")
    except Exception as e:
        logger.warning(f"   [4/7] Ecosystem discovery error: {e}")

    if _stop_event.is_set():
        return

    # STEP 5: Sync new facilities → capacity_pipeline (catch any missed)
    try:
        from db_utils import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO capacity_pipeline (operator, market, region, capacity_mw, status, announcement_date, source, source_url, created_at, confidence_score)
            SELECT df.provider, COALESCE(df.city, df.state), df.country, df.power_mw, df.status,
                   df.discovered_at, df.source, df.source_url, NOW()::text, COALESCE(df.confidence_score, 0.8)::integer
            FROM discovered_facilities df
            WHERE df.status IN ('Under Construction', 'Planned', 'Announced')
              AND df.power_mw IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM capacity_pipeline cp
                  WHERE LOWER(COALESCE(cp.operator,'')) = LOWER(COALESCE(df.provider,''))
                    AND LOWER(COALESCE(cp.market,'')) = LOWER(COALESCE(df.city, df.state, ''))
              )
        """)
        new_pipeline = c.rowcount
        conn.commit()
        logger.info(f"   [5/7] Pipeline sync: +{new_pipeline} new projects")
    except Exception as e:
        logger.warning(f"   [5/7] Pipeline sync error: {e}")

    # STEP 6: Refresh GDCI scores from facility data
    try:
        from db_utils import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO gdci_scores (market, country, facility_count, total_mw, gdci_score, tier, computed_at)
            SELECT COALESCE(city, state), COALESCE(country,'US'), COUNT(*), ROUND(SUM(COALESCE(power_mw,0))::numeric),
                ROUND((LEAST(COUNT(*)/5.0, 25)*0.25 + LEAST(SUM(COALESCE(power_mw,0))/100.0, 25)*0.25 + 15*0.20 + 18*0.15 + 16*0.15)::numeric, 1),
                CASE WHEN COUNT(*) >= 50 THEN 'Tier 1' WHEN COUNT(*) >= 20 THEN 'Tier 2' WHEN COUNT(*) >= 10 THEN 'Tier 3' ELSE 'Tier 4' END,
                NOW()
            FROM discovered_facilities WHERE city IS NOT NULL GROUP BY COALESCE(city,state), country HAVING COUNT(*) >= 5
            ON CONFLICT (market, country) DO UPDATE SET facility_count=EXCLUDED.facility_count, total_mw=EXCLUDED.total_mw, gdci_score=EXCLUDED.gdci_score, tier=EXCLUDED.tier, computed_at=NOW()
        """)
        conn.commit()
        logger.info(f"   [6/7] GDCI refresh: {c.rowcount} markets scored")
    except Exception as e:
        logger.warning(f"   [6/7] GDCI refresh error: {e}")

    # STEP 7: Refresh market_intelligence with current facility counts
    try:
        from db_utils import get_db
        conn = get_db()
        c = conn.cursor()
        # Map of market_name → city filter lists
        MARKET_CITY_MAP = {
            'Northern Virginia': ['Ashburn','Sterling','Manassas','Leesburg','Bristow','Chantilly','Herndon','Reston'],
            'Phoenix': ['Phoenix','Mesa','Chandler','Goodyear','Tempe','Scottsdale'],
            'Chicago': ['Chicago','Aurora','Elk Grove Village'],
            'Atlanta': ['Atlanta','Douglasville','Lithia Springs','Suwanee'],
            'Columbus': ['Columbus','New Albany'],
            'Houston': ['Houston'],
            'Denver': ['Denver'],
            'Miami': ['Miami'],
            'Austin': ['Austin'],
        }
        updated = 0
        for market_name, cities in MARKET_CITY_MAP.items():
            placeholders = ','.join(['%s'] * len(cities))
            c.execute(f"""
                UPDATE market_intelligence SET
                    facility_count = sub.cnt,
                    total_mw = sub.mw,
                    last_updated = NOW()::text
                FROM (
                    SELECT COUNT(*) as cnt, ROUND(COALESCE(SUM(power_mw),0)::numeric) as mw
                    FROM discovered_facilities WHERE city IN ({placeholders}) AND country = 'US'
                ) sub
                WHERE market_intelligence.market_name = %s
            """, (*cities, market_name))
            if c.rowcount:
                updated += 1
        conn.commit()
        logger.info(f"   [7/7] Market intelligence: {updated} markets refreshed")
    except Exception as e:
        logger.warning(f"   [7/7] Market intelligence error: {e}")

    logger.info("   === MARKET REFRESH COMPLETE ===")


# Map names to functions (includes manual-only crawlers)

def _run_deals_crawler():
    """Run AI deals discovery using auto_pilot extractors, saving to Neon PostgreSQL."""
    import os, hashlib, psycopg2, sys
    from datetime import datetime, timezone
    # Railway uses /app/, Replit used /home/runner/workspace
    if os.path.exists('/app'):
        sys.path.insert(0, '/app')
    elif os.path.exists('/home/runner/workspace'):
        sys.path.insert(0, '/home/runner/workspace')

    logger.info("💼 Deals crawler starting (Neon-backed)...")

    db_url = os.environ.get('DATABASE_URL') or os.environ.get('NEON_DATABASE_URL')
    if not db_url or 'neon' not in db_url.lower() and 'postgresql' not in db_url.lower():
        logger.error("💼 Deals crawler: No Neon DATABASE_URL found — aborting")
        return

    try:
        # Use auto_pilot deal extractor
        from auto_pilot import deal_extractor, capacity_extractor, _is_dc_relevant, _is_valid_company_name
        logger.info("💼 Using auto_pilot extractors")
    except Exception as e:
        logger.warning(f"💼 auto_pilot extractors not available: {e}")
        deal_extractor = None

    try:
        import feedparser
    except ImportError:
        logger.warning("💼 feedparser not available")
        feedparser = None

    FEEDS = [
        "https://www.datacenterdynamics.com/rss/",
        "https://www.datacenterknowledge.com/rss.xml",
        "https://www.prnewswire.com/rss/news-releases-list.rss",
        "https://www.businesswire.com/rss/home/%srss=G7",
        "https://feeds.reuters.com/reuters/businessNews",
    ]

    import re
    VALUE_RE = re.compile(r'\$\s*([\d,.]+)\s*(billion|million|B|M)\b', re.IGNORECASE)

    def extract_value_m(text):
        m = VALUE_RE.search(text)
        if not m: return None
        n = float(m.group(1).replace(',',''))
        return n*1000 if m.group(2).lower() in ('billion','b') else n

    def simple_extract(title):
        """Fallback extractor if auto_pilot not available."""
        tl = title.lower()
        deal_kw = ['acqui','merger','invest','joint venture','data center','colocation','hyperscale','billion','million']
        if sum(1 for k in deal_kw if k in tl) < 2:
            return None
        type_map = [('acqui','acquisition'),('merger','acquisition'),('joint venture','jv'),
                    ('debt','debt'),('equity','equity'),('lease','lease'),('capex','capex')]
        dtype = next((t for k,t in type_map if k in tl), 'investment')
        # Extract buyer (first capitalized entity before verb)
        m = re.search(r'^([A-Z][\w\s/&]+?)\s+(?:acquires?|invests?|announces?|closes?|completes?)', title)
        buyer = m.group(1).strip() if m else None
        if not buyer or len(buyer) < 3 or len(buyer) > 80: return None
        return {'buyer': buyer, 'type': dtype, 'value': extract_value_m(title)}

    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    saved = 0

    for feed_url in FEEDS:
        if _stop_event.is_set():
            break
        if not feedparser:
            break
        try:
            feed = feedparser.parse(feed_url)
            logger.info(f"💼 {feed_url.split('/')[2]}: {len(feed.entries)} entries")
            for entry in feed.entries[:30]:
                title = entry.get('title', '')
                summary = entry.get('summary', '') or ''
                text = f"{title} {summary}"

                # Use auto_pilot extractor if available
                if deal_extractor:
                    try:
                        if not _is_dc_relevant(title):
                            continue
                        deal = deal_extractor.extract_deal(title)
                        buyer = deal.get('buyer')
                        if not buyer or not _is_valid_company_name(buyer):
                            continue
                        value_m = deal.get('value')
                        dtype = deal.get('type', 'investment')
                        confidence = deal.get('confidence', 0)
                        if confidence < 60 or dtype == 'unknown':
                            continue
                    except Exception:
                        continue
                else:
                    result = simple_extract(title)
                    if not result:
                        continue
                    buyer = result['buyer']
                    value_m = result['value']
                    dtype = result['type']

                # Parse date
                published = entry.get('published_parsed')
                if published:
                    deal_date = datetime(*published[:3]).strftime('%Y-%m-%d')
                    deal_year = published[0]
                else:
                    deal_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                    deal_year = datetime.now(timezone.utc).year

                deal_id = hashlib.md5(f"{buyer}{title[:50]}".encode()).hexdigest()[:16]

                try:
                    cur.execute("""
                        INSERT INTO deals (id, date, year, buyer, seller, value, type, region, market, source_url, created_at, verified)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), 0)
                        ON CONFLICT (id) DO NOTHING
                    """, (deal_id, deal_date, deal_year,
                          buyer[:100], 'Undisclosed',
                          value_m, dtype, None, None,
                          entry.get('link', feed_url)[:500]))
                    if cur.rowcount:
                        saved += 1
                        logger.info(f"   ✅ Deal: {buyer} ({dtype}, ${value_m}M)")
                except Exception as e:
                    logger.warning(f"   Deal insert error: {e}")
                    conn.rollback()

        except Exception as e:
            logger.warning(f"   Feed error {feed_url.split('/')[2]}: {e}")

        time.sleep(3)

    conn.commit()
    cur.close()
    conn.close()
    logger.info(f"💼 Deals crawler done — {saved} new deals saved to Neon")


def _run_facility_discovery():
    """FULL facility pipeline: news extraction → discovery → auto-approve → pipeline sync.
    This is the main automation chain that keeps DC Hub dynamic.
    """
    logger.info("   === FACILITY PIPELINE START ===")

    # STEP 1: Extract facilities from recent news articles
    try:
        from news_facility_extractor import scan_news_sources
        result = scan_news_sources()
        if isinstance(result, dict):
            logger.info(f"   [1/4] News extraction: {result.get('facilities_extracted', 0)} extracted, {result.get('pending_review', 0)} pending")
        else:
            logger.info(f"   [1/4] News extraction: completed")
    except ImportError:
        logger.warning("   [1/4] News extraction: not available (no news_facility_extractor)")
    except Exception as e:
        logger.warning(f"   [1/4] News extraction error: {e}")

    if _stop_event.is_set():
        return

    # STEP 2: Discover facilities from PeeringDB, OSM, datacentermap
    try:
        from discovery_engine import (
            init_discovery_tables, run_peeringdb_discovery,
            run_osm_discovery, run_datacentermap_discovery
        )
        try:
            init_discovery_tables()
        except Exception:
            pass

        total_found = 0
        total_added = 0
        for source_name, run_func in [
            ('peeringdb', run_peeringdb_discovery),
            ('openstreetmap', run_osm_discovery),
            ('datacentermap', run_datacentermap_discovery),
        ]:
            if _stop_event.is_set():
                break
            try:
                result = run_func()
                found = result.get('found', 0)
                added = result.get('added', 0)
                total_found += found
                total_added += added
                if added > 0:
                    logger.info(f"   [2/4] {source_name}: +{added} new (from {found})")
            except Exception as e:
                logger.warning(f"   [2/4] {source_name} error: {e}")
        logger.info(f"   [2/4] Discovery totals: {total_added} new from {total_found} found")
    except ImportError:
        logger.warning("   [2/4] Discovery engine not available")
    except Exception as e:
        logger.error(f"   [2/4] Discovery error: {e}")

    if _stop_event.is_set():
        return

    # STEP 3: Auto-approve high-confidence pending facilities
    try:
        from facility_auto_approve import run_auto_approve
        from db_utils import get_db
        conn = get_db()
        result = run_auto_approve(conn, batch_size=50, dry_run=False)
        if isinstance(result, dict):
            logger.info(f"   [3/4] Auto-approve: {result.get('approved', 0)} approved, {result.get('rejected', 0)} rejected")
        else:
            logger.info(f"   [3/4] Auto-approve: completed")
    except ImportError:
        logger.warning("   [3/4] Auto-approve not available")
    except Exception as e:
        logger.warning(f"   [3/4] Auto-approve error: {e}")

    if _stop_event.is_set():
        return

    # STEP 4: Sync new facilities to capacity_pipeline
    try:
        from db_utils import get_db
        conn = get_db()
        c = conn.cursor()
        c.execute("""
            INSERT INTO capacity_pipeline (operator, market, region, capacity_mw, status, announcement_date, source, source_url, created_at, confidence_score)
            SELECT df.provider, COALESCE(df.city, df.state), df.country, df.power_mw, df.status,
                   df.discovered_at, df.source, df.source_url, NOW()::text, COALESCE(df.confidence_score, 0.8)::integer
            FROM discovered_facilities df
            WHERE df.status IN ('Under Construction', 'Planned', 'Announced')
              AND df.power_mw IS NOT NULL
              AND df.discovered_at >= (NOW() - INTERVAL '7 days')::text
              AND NOT EXISTS (
                  SELECT 1 FROM capacity_pipeline cp
                  WHERE LOWER(COALESCE(cp.operator,'')) = LOWER(COALESCE(df.provider,''))
                    AND LOWER(COALESCE(cp.market,'')) = LOWER(COALESCE(df.city, df.state, ''))
              )
        """)
        new_pipeline = c.rowcount
        conn.commit()
        if new_pipeline > 0:
            logger.info(f"   [4/4] Pipeline sync: +{new_pipeline} new construction projects")
        else:
            logger.info(f"   [4/4] Pipeline sync: no new projects")
    except Exception as e:
        logger.warning(f"   [4/4] Pipeline sync error: {e}")

    logger.info("   === FACILITY PIPELINE COMPLETE ===")


def _run_infrastructure_sync():
    """Sync infrastructure data: fiber routes, HIFLD transmission lines, substations.
    Calls the same logic as /api/jobs/fiber-sync endpoint.
    """
    total_new = 0

    # 1. PeeringDB facility coordinate cache
    try:
        from routes.energy_routes import _ensure_peeringdb_fac_coords
        fac_coords = _ensure_peeringdb_fac_coords()
        logger.info(f"   PeeringDB facility cache: {len(fac_coords)} facilities")
    except Exception as e:
        logger.warning(f"   PeeringDB cache error: {e}")

    # 2. Fiber network discovery
    try:
        from fiber_network_discovery import sync_fiber_routes
        fiber_result = sync_fiber_routes()
        new_routes = fiber_result.get('new_routes', 0) if isinstance(fiber_result, dict) else 0
        total_new += new_routes
        logger.info(f"   Fiber routes: +{new_routes} new")
    except ImportError:
        try:
            from infrastructure_discovery import FiberRouteDiscovery
            frd = FiberRouteDiscovery()
            new_routes = frd.sync()
            total_new += new_routes
            logger.info(f"   Fiber routes (infra module): +{new_routes} new")
        except Exception as e2:
            logger.warning(f"   Fiber discovery not available: {e2}")
    except Exception as e:
        logger.warning(f"   Fiber sync error: {e}")

    # 3. HIFLD transmission lines
    try:
        from infrastructure_discovery import TransmissionLineDiscovery
        tld = TransmissionLineDiscovery()
        new_lines = tld.sync()
        total_new += new_lines
        logger.info(f"   Transmission lines: +{new_lines} new")
    except Exception as e:
        logger.warning(f"   Transmission sync not available: {e}")

    logger.info(f"   Infrastructure sync totals: {total_new} new records")


_RUNNERS = {
    "market_refresh":      _run_market_refresh,
    "news":                _run_news_crawler,
    "api_discovery":       _run_api_discovery,
    "energy_discovery":    _run_energy_discovery,
    "knowledge_sync":      _run_knowledge_sync,
    "deals":               _run_deals_crawler,
    "facility_discovery":  _run_facility_discovery,
    "infrastructure_sync": _run_infrastructure_sync,
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
