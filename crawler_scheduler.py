#!/usr/bin/env python3
# DCHUB-CRAWLER-BACKOFF-2026-04-28
# Reduced deals-crawler frequency from hourly to nightly + global semaphore
# so concurrent crawlers cannot starve the gunicorn worker pool.
import threading as _dchub_threading
_DCHUB_CRAWLER_LOCK = _dchub_threading.BoundedSemaphore(1)

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
HARD_TIMEOUT_SECONDS = 30 * 60       # 15 min max per crawler run
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
    ( 3, 15, "kmz_discovery",       "_run_kmz_discovery"),
    ( 5,  5, "intl_infra_ingest",   "_run_intl_infra_ingest"),
    # r-auto-outreach (2026-06-03): autopilot growth. Once-per-day cap
    # via same-hour pairing to avoid double-sends. Built on the 24h
    # per-target Resend guard in ai_lab_outreach._perform_resend_send.
    (16, 16, "lost_conversion",     "_run_lost_conversion_outreach"),
    (17, 17, "ai_lab_outreach",     "_run_ai_lab_auto_outreach"),
    # r-auto-interconnect (2026-06-04): scan novel UAs + pending buckets
    # once daily, write findings + email digest. Endpoint is admin-gated
    # AND idempotent (UNIQUE index on user_agent for pending/approved
    # findings — re-runs skip already-promoted UAs). Slot 11/11 chosen
    # because it's the largest empty hour in the schedule (2h gap from
    # 9 market_refresh → 12 infrastructure_sync, no overlap with
    # 16/17 outreach pair). Same-hour same-day means at most one run
    # per day even with the both-slots scheduler config.
    (11, 11, "auto_interconnect",   "_run_auto_interconnect"),
    # Powered Land Gas Pricing (2026-06-04, Phase 1 spine):
    # Refresh Henry Hub + basis + delivered tariffs + derive $/MWh per
    # marquee DCPI market. Slot 04:00 UTC chosen because it's an empty hour
    # (no overlap with 12/0 infra-sync, 14/2 knowledge-sync, 16/17 outreach
    # pair, or 11/11 auto_interconnect) AND gives EIA time to publish the
    # overnight settle. Same-hour-same-day cap → one run per day. The
    # underlying endpoint /api/v1/markets/gas-pricing/cron has a 90s soft
    # deadline so it cannot overrun the next slot.
    ( 4,  4, "gas_pricing_refresh", "_run_gas_pricing_refresh"),
    # Brain Layer-15 Tool Calibration Drift (2026-06-04):
    # Daily test of every registered tool against known-correct (input,
    # expected range) comp sets. Built after the Site Valuation Engine
    # v1.0 calibration miss ($2M/MW shipped against $150-800K industry
    # comp). 02:30 UTC slot chosen because it's empty (between 0/12
    # infra-sync and 3/15 kmz-discovery) and gives any nightly upstream
    # refresh time to land. Same-hour-same-day cap → one run/day.
    ( 2,  2, "tool_calibration",   "_run_tool_calibration_check"),
    # Daily R2 refresh — POST /refresh on the heroic-reprieve daily
    # FastAPI so today's PNGs land in R2 (2026-06-05). R2 has been
    # empty for 7+ days because extractor_cron.py's daily_refresh_if_needed()
    # silently skipped when DAILY_SVC_URL/REFRESH_SECRET weren't set on
    # that worker. This is a SECOND, redundant trigger from the main
    # backend so a single missing env var on one worker doesn't break
    # /daily. Slot 1 UTC: empty hour, fires BEFORE LinkedIn/X post slots,
    # gives R2 cache + global CF edge time to warm before first morning
    # social scrape. Same-hour-same-day cap → one run/day.
    ( 1,  1, "daily_r2_refresh",   "_run_daily_r2_refresh"),
    # LinkedIn native engagement + impressions sync (r72, 2026-06-05):
    # Fetches likes/comments via /rest/socialActions AND impressions/clicks/
    # shares/unique_impressions via /rest/organizationalEntityShareStatistics
    # for posts in the last 21 days, populating linkedin_posts.{likes, comments,
    # impressions, clicks, shares, unique_impressions}. Feeds the media
    # measure→learn loop with the FULL signal set — passive high-reach posts
    # (zero comments but big impressions) finally become visible to the
    # generator. Slot 13/13 UTC: empty hour, same-day cap via same-hour pair.
    # GitHub Actions cron (linkedin-engagement-sync.yml @ 14:40) ALSO fires
    # daily — this is a redundant second trigger so a Railway-only deploy
    # without GH secrets still measures engagement. Endpoint is admin-gated +
    # fail-soft when the LinkedIn token lacks r_organization_social.
    (13, 13, "linkedin_engagement_sync", "_run_linkedin_engagement_sync"),
    # DC Hub Media accelerator (2026-06-05): twice-daily scan for LinkedIn
    # posts that outperformed the 30d impressions baseline by 2x+ within
    # 6h of publish — auto-enqueues identical Twitter + Bluesky cross-posts
    # (status='approved'; existing publisher loops respect the 2/day cap)
    # and flags the social_media_posts row (accelerate=TRUE, viral_score).
    # The SCHEDULE harness is hour-based with two slots per entry; we pick
    # 15 UTC + 23 UTC. 15:00 fires ~2h after linkedin_engagement_sync
    # (13:00) so today's impressions data is fresh; 23:00 catches
    # afternoon-post breakthroughs without colliding with 22:00
    # energy_discovery. The qualifying window is 6h post-publish, so cron
    # cadence and detection window are aligned. Same-hour-same-day cap →
    # one run per slot per day. Both slots are empty hours (no overlap
    # with 14/2 knowledge-sync, 18/19 news/facilities, 20/21 deals/market).
    (15, 23, "accelerator_scan",   "_run_accelerator_scan"),
    # MCP-presence crawl (2026-06-05): twice-daily sweep of the ~15 MCP
    # listing sites DC Hub appears on (Smithery, MCPHive, LobeHub, Glama,
    # YellowMCP, mcp.so, PulseMCP, etc.). For each, fetch the listing
    # page, scrape published tool count + uptime, compare to our live
    # /api/v1/mcp/tools.json count, write a brain_findings row when drift
    # is detected. Rate-limited at 1s/request × max 15 requests, so a run
    # finishes in well under 60s — small enough to share an hour with
    # the news crawler (which holds the lock first; the presence crawler
    # picks up on the next-minute tick after news finishes, or on the
    # next-day re-fire if news overruns). Slot 6/18 mirrors the user's
    # spec; if collision proves chronic, move to an empty hour later.
    ( 6, 18, "mcp_presence_crawl",   "_run_mcp_presence_crawl"),
    # MCP-registry discoverer (2026-06-05): weekly Google-SERP scan for
    # NEW MCP listing sites DC Hub isn't on yet. Self-gates to Mondays
    # inside the runner so this only fires once per week (the SCHEDULE
    # harness is hour-based, not weekly). Slot 7/7 same-hour pair caps
    # to one run per UTC day; weekday gate caps to one run per week.
    # Defensive cap of 15 HTTP requests per run, never raises.
    ( 7,  7, "mcp_registry_discover", "_run_mcp_registry_discover"),
    # MCP-presence auto-fix (2026-06-05): once per day at slot 19/19 UTC
    # (an empty hour, one hour after the 18:00 crawl). Sweeps every
    # listing WHERE drift_detected=true and runs the registry-appropriate
    # auto-submitter (real POST for MCPHive/Cursor.directory, manifest-
    # refresh for Smithery/LobeHub/Glama/PulseMCP, human-loop for the
    # captcha/login-wall registries). Defaults to LIVE (dry_run=False)
    # inside the runner — the dispatcher's per-registry 1/hour rate-
    # limit token + the human-loop fallback are the safety net.
    (19, 19, "mcp_presence_auto_fix", "_run_mcp_presence_auto_fix"),
    # Customer Feedback Forum triage (2026-06-06): twice-daily brain
    # triage of new /feedback submissions. Classifies LOW/MEDIUM/HIGH/
    # SPAM, writes brain_recommendation back, and (LOW-class only +
    # 2-cycle diff-hash gate + 5/day cap + kill switch
    # FEEDBACK_AUTO_APPLY_DISABLE) marks rows in_progress so the
    # brain_pr_opener cron can ship them. HIGH-class rows email the
    # user via Resend with signed-token approve/reject buttons. Slot
    # 8/20 UTC shares the hour with deals (8/20) but those callers run
    # via separate per-name locks; collision is acceptable because
    # triage is bounded (<=20 rows, ~25s/call worst case, ~8 min total
    # ceiling). Slot chosen because the spec asks for 08:00 / 20:00 UTC.
    ( 8, 20, "feedback_triage",     "_run_feedback_triage"),
    # Market Brief pre-warm (2026-06-06): build the 9-section market_brief
    # fan-out for each of the 5 seed markets (northern-virginia, dallas,
    # phoenix, atlanta, chicago) so the first visitor doesn't pay the
    # cold per-section DB cost. The HTML endpoint has a 6h edge cache
    # (CF s-maxage=21600); the warm runs PRIME the CF cache via loopback
    # GETs. Slot 5/17 chosen per spec. Same-hour-same-day cap → one run/slot.
    # Short, defensive runner: bounded to 5 slugs × ~250ms each = ~1.3s.
    ( 5, 17, "market_brief_warm",   "_run_market_brief_warm"),
    # Per-State Brief pre-warm (2026-06-06): build the per-state roll-up for
    # each of the 8 seed states (texas, california, virginia, georgia, ohio,
    # oregon, illinois, arizona). 1s sleep between states. Slot 6/18 chosen
    # to stagger from market_brief_warm (5/17) so the DB pool doesn't see
    # both warms hit at once.
    ( 6, 18, "state_brief_warm",    "_run_state_brief_warm"),
    # Operator Brief pre-warm (2026-06-06): build the 9-section operator
    # brief for each of the 10 seed operators (aligned, qts, digital-realty,
    # equinix, vantage, cyrusone, cologix, core-scientific, airtrunk,
    # iron-mountain) so the first investor/broker visitor doesn't pay
    # the cold per-section DB cost. 6h edge cache (CF s-maxage=21600)
    # primed via loopback GETs. Slot 7/19 chosen to stagger from
    # market_brief_warm (5/17) and state_brief_warm (6/18) — DB pool
    # doesn't see three warms back-to-back. ~10 operators × 1s sleep = ~10s.
    ( 7, 19, "operator_brief_warm", "_run_operator_brief_warm"),
    # DCPI Verdict-Shift auto-press (2026-06-06): once-daily scan for
    # markets whose DCPI verdict TODAY differs from the verdict 7 days
    # ago. Qualifying shifts (BUILD or AVOID involved on at least one
    # side; sub-tier CAUTION↔CAUTION is skipped) get an Anthropic-Claude-
    # written 50-80-word LinkedIn blurb + a link to the canonical Market
    # Brief, enqueued into social_media_posts (status='approved') for
    # the existing LinkedIn publisher loop to ship. Cap MAX_POSTS_PER_RUN=3
    # so a wide rescore never floods. De-dup via
    # UNIQUE(market_slug, shift_to, DATE(posted_at)) on
    # market_verdict_post_log; soft 48h cap per slug stacks on top.
    # Slot 16/16 per spec — same hour as lost_conversion, but each
    # crawler has its own per-name last_run guard so they're independent
    # (see feedback_triage 8/20 sharing with deals for prior art).
    (16, 16, "verdict_shift_post",  "_run_verdict_shift_post"),
    # Real-time Watchlist + Verdict-Shift Alerts (2026-06-06): twice-
    # daily realtime sweep so PRO+ watchers see verdict shifts within
    # ~12h. Slot 10/22 UTC chosen because it brackets the daily DCPI
    # snapshot writer (~04:00 UTC) — by 10:00 today's shifts are
    # detectable. The dispatcher pulls shifts from
    # market_verdict_shifts._detect_shifts (one source of truth) and
    # fans out via email (Resend) + optional browser push (pywebpush+
    # VAPID, no-op if env vars missing). FREE watchers are QUEUED
    # (status='pending') for the Monday digest, not emailed in realtime.
    # Same-hour same-day cap via the per-name last_run guard. Both
    # slots are already shared with energy_discovery; per-name locks
    # keep them independent (prior art: verdict_shift_post 16/16 sharing
    # with lost_conversion).
    (10, 22, "watchlist_realtime",      "_run_watchlist_realtime"),
    # Weekly digest for FREE watchers (2026-06-06). Slot 14/14 UTC.
    # Self-gates to Mondays inside the runner because the SCHEDULE
    # harness is hour-based, not weekly — same pattern as
    # mcp_registry_discover (Mondays only). Bundles every PENDING alert
    # per watcher into one digest email and marks status='sent'.
    (14, 14, "watchlist_weekly_digest", "_run_watchlist_weekly_digest"),
    # Hyperscaler Brief pre-warm (2026-06-06): pre-build the 9-section
    # hyperscaler_brief fan-out for each of the 10 seed hyperscalers
    # (aws/azure/google-cloud/meta/apple/oracle/tiktok-bytedance/tencent/
    # alibaba/softbank) so the first visitor doesn't pay the cold per-
    # section DB cost. The HTML endpoint has a 6h edge cache
    # (CF s-maxage=21600); the warm runs PRIME the CF cache via loopback
    # GETs. Slot 8/20 per spec — shares hour with deals + feedback_triage
    # but each has its own per-name last_run guard (prior art: feedback_
    # triage 8/20 sharing with deals). Bounded: 10 slugs × ~250ms each +
    # 9 × 1s sleep = ~12s per slot.
    ( 8, 20, "hyperscaler_brief_warm", "_run_hyperscaler_brief_warm"),
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
    """Run news sync once. Uses news_aggregator (proven working path)
    with hourly sync to announcements for Brain extractors."""
    try:
        from news_aggregator import run_aggregator
        result = run_aggregator()
        logger.info(f"📰 News aggregator: {result}")
    except Exception as e:
        logger.warning(f"news_aggregator failed, falling back to auto_sync: {e}")
        try:
            from auto_sync import NewsSyncer
            ns = NewsSyncer(interval_seconds=0)
            ns.sync()
        except Exception as e2:
            logger.error(f"News crawler fully failed: {e2}")
    # Mirror fresh news rows → announcements (Brain reads from announcements)
    try:
        import psycopg2, os
        db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL', '')
        if db_url:
            conn = psycopg2.connect(db_url, connect_timeout=10)
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO announcements (id, title, summary, source_url, source,
                                              published_date, discovered_at, category, url)
                    SELECT 'news_' || id::text, title, COALESCE(description, ''), url, source,
                           COALESCE(published_date::text, created_at::text),
                           created_at::text, COALESCE(category, 'industry'), url
                    FROM news
                    WHERE created_at > NOW() - INTERVAL '48 hours'
                    ON CONFLICT (id) DO NOTHING
                """)
                inserted = cur.rowcount
            conn.commit()
            conn.close()
            logger.info(f"📋 Synced {inserted} news rows to announcements")
    except Exception as e:
        logger.warning(f"news→announcements mirror failed: {e}")


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
                'https://dchub-api-production.up.railway.app/api/deals/refresh',
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
            'https://dchub-api-production.up.railway.app/api/market-report/generate',
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
            'https://dchub-api-production.up.railway.app/api/v1/ai-wars/auto-battle',
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
            'https://dchub-api-production.up.railway.app/api/ai-ecosystem/run',
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
    # Phase FF+7-fix4 (2026-05-19): added finally blocks so conn closes
    # on every exception path, preventing pool exhaustion in this daemon.
    conn = None
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
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass

    # STEP 6: Refresh GDCI scores from facility data
    conn = None
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
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass

    # STEP 7: Refresh market_intelligence with current facility counts
    conn = None
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
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass

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

    # Legacy auto_pilot extractors retired — deal extraction migrated to
    # Energy Auto-Discovery v3.0 upstream. Setting deal_extractor=None keeps the
    # guarded block at line ~516 a no-op without generating log noise every crawl.
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
    # Phase FF+7-fix4 (2026-05-19): try/finally to ensure conn closes.
    conn = None
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
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass

    if _stop_event.is_set():
        return

    # STEP 4: Sync new facilities to capacity_pipeline
    conn = None
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
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass

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


def _run_kmz_discovery():
    """
    KMZ Auto-Discovery v4.0 — discovers fiber routes, state broadband layers,
    and ArcGIS sources. Runs twice daily at 03:00 and 15:00 UTC.
    """
    try:
        from kmz_auto_discovery import _kmz_instance
        if _kmz_instance is None:
            logger.warning("KMZ Discovery: _kmz_instance not initialized — skipping")
            return
        logger.info("📡 KMZ Discovery: starting scheduled cycle (v4.0)")
        results = _kmz_instance.run_discovery_cycle()
        new_routes = results.get('total_new_routes', 0)
        new_km     = round(results.get('total_new_km', 0, 0) or 0, 1)
        duration   = results.get('cycle_duration_seconds', 0)
        logger.info(f"📡 KMZ Discovery: ✅ complete — {new_routes} new routes, {new_km} km, {duration}s")
    except Exception as e:
        logger.error(f"📡 KMZ Discovery: ❌ error — {e}", exc_info=True)


def _run_intl_infra_ingest():
    """Pull REAL international substations (OpenStreetMap Overpass) into the
    substations table so non-US DCPI metros gain grid-presence coverage on the
    map. Runs server-side from env DATABASE_URL — no admin key, no HTTP poll
    (replaces the manual /api/v1/admin/ingest-intl-infra trigger whose in-memory
    task status doesn't survive the 2-replica load balancer). OSM has no MVA, so
    this is map PRESENCE only — it does NOT change DCPI headroom scores.
    Idempotent (ON CONFLICT DO NOTHING)."""
    try:
        import intl_infra_ingest
        rc = intl_infra_ingest.main([])
        logger.info("🌍 Intl infra ingest (OSM Overpass): done rc=%s", rc)
    except Exception as e:
        logger.error("🌍 Intl infra ingest: ❌ error — %s", e, exc_info=True)


def _run_ai_lab_auto_outreach():
    """Fire /api/v1/admin/ai-lab-outreach/auto-send (loopback). The endpoint
    sends the latest status='draft' row per target_slug if (a) target_email
    is set AND (b) no draft for that slug was sent in the last 24h.
    Polite default: limit=3 to avoid blasting all 9 in one tick.
    No-ops cleanly if DCHUB_RESEND_API_KEY or DCHUB_ADMIN_KEY are missing."""
    import requests as _rq
    key = (os.environ.get("DCHUB_ADMIN_KEY")
           or os.environ.get("DCHUB_ADMIN_API_KEY") or "")
    if not key:
        logger.warning("📧 ai_lab_outreach autopilot: skipped — DCHUB_ADMIN_KEY not set")
        return
    base = os.environ.get("DCHUB_INTERNAL_API", "http://127.0.0.1:8080")
    try:
        r = _rq.post(
            f"{base}/api/v1/admin/ai-lab-outreach/auto-send?limit=3",
            headers={"X-Admin-Key": key, "User-Agent": "dchub-cron-autoreach/1.0"},
            timeout=60,
        )
        d = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}) or {}
        logger.info("📧 ai_lab_outreach autopilot: sent=%s skipped=%s candidates=%s",
                       d.get("sent"), d.get("skipped"), d.get("candidates"))
    except Exception as e:
        logger.error("📧 ai_lab_outreach autopilot: error — %s", e)


def _run_lost_conversion_outreach():
    """Fire /api/v1/admin/lost-conversion/send for the lost-conversion pool.
    Now wired to Resend (was Office 365 SMTP that 503'd silently). Caps at
    limit=10 to stay polite. Only sends to candidates with min_signals >= 2."""
    import requests as _rq
    key = (os.environ.get("DCHUB_ADMIN_KEY")
           or os.environ.get("DCHUB_INTERNAL_KEY")
           or os.environ.get("DCHUB_ADMIN_API_KEY") or "")
    if not key:
        logger.warning("📧 lost_conversion autopilot: skipped — DCHUB_ADMIN_KEY not set")
        return
    base = os.environ.get("DCHUB_INTERNAL_API", "http://127.0.0.1:8080")
    try:
        r = _rq.post(
            f"{base}/api/v1/admin/lost-conversion/send"
            f"?confirm=true&limit=10&min_signals=2&days=30",
            headers={"X-Internal-Key": key,
                       "X-Admin-Key": key,
                       "User-Agent": "dchub-cron-autoreach/1.0"},
            timeout=120,
        )
        d = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}) or {}
        logger.info("📧 lost_conversion autopilot: attempted=%s sent=%s failed=%s",
                       d.get("attempted"), d.get("sent"), d.get("failed"))
    except Exception as e:
        logger.error("📧 lost_conversion autopilot: error — %s", e)


def _run_auto_interconnect():
    """Fire /api/v1/admin/auto-interconnect/run (loopback). Scans the last 14d
    of ai_requests + mcp_connections for novel UAs (>=5 hits AND >=2 distinct IPs)
    that the live detect_platform() buckets as mcp_generic/direct/unknown_ai, plus
    ai_cumulative rows for known-but-unseeded platforms (groq/cohere/huggingface/
    mistral/etc.) with no _PARTNERS entry. Auto-promotes in 3 reversible steps
    (discovered_platforms insert, ai_cumulative name backfill, _PARTNERS_AUTO
    stub) and emails a single admin digest. Does NOT mint dev keys, does NOT
    send outbound to discovered domains.

    Runner UA is dchub-cron-autointerconnect/1.0 which the endpoint's
    _INTERNAL_UA_MARKERS check filters out, so the runner can't promote
    itself by hitting its own loopback."""
    import requests as _rq
    key = (os.environ.get("DCHUB_ADMIN_KEY")
           or os.environ.get("DCHUB_INTERNAL_KEY")
           or os.environ.get("DCHUB_ADMIN_API_KEY") or "")
    if not key:
        logger.warning("🔌 auto_interconnect: skipped — DCHUB_ADMIN_KEY not set")
        return
    base = os.environ.get("DCHUB_INTERNAL_API", "http://127.0.0.1:8080")
    try:
        r = _rq.post(
            f"{base}/api/v1/admin/auto-interconnect/run",
            headers={"X-Internal-Key": key, "X-Admin-Key": key,
                       "User-Agent": "dchub-cron-autointerconnect/1.0"},
            timeout=90,
        )
        d = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}) or {}
        logger.info("🔌 auto_interconnect: scanned=%s novel=%s promoted=%s findings_written=%s email_sent=%s",
                       d.get("scanned"), len(d.get("novel_uas", [])),
                       len(d.get("promoted", [])), d.get("findings_written"),
                       d.get("email_sent"))
    except Exception as e:
        logger.error("🔌 auto_interconnect: error — %s", e)


def _run_daily_r2_refresh():
    """POST /refresh on the heroic-reprieve daily FastAPI so today's PNGs
    (9 variants × 3 themes × 3 sizes) get rendered + uploaded to R2.

    Background (2026-06-05): R2 was empty for all dates from 2026-05-29
    onward. The intended trigger was extractor_cron.py's
    daily_refresh_if_needed() which runs on the separate extractor
    Railway worker (railway-extractor.toml). That cron silently skips
    when DAILY_SVC_URL or REFRESH_SECRET env vars aren't set on the
    extractor service — and one of them wasn't. This is a SECOND
    trigger from the main backend so a single missing env var doesn't
    leave /daily serving the Railway live-render path forever (slower
    + redundant compute).

    Env vars (read from MAIN backend, not extractor):
      DAILY_SVC_URL    e.g. https://dchub-backend-production-f7dd.up.railway.app
                       (falls back to that exact URL if unset)
      REFRESH_SECRET   shared secret with the daily service
                       (skip with a HIGH-VISIBILITY warning if unset)

    Resilient: never raises — logs failure and exits so the next slot
    can still run.
    """
    import requests as _rq
    base = (os.environ.get("DAILY_SVC_URL", "").strip()
            or "https://dchub-backend-production-f7dd.up.railway.app").rstrip("/")
    secret = (os.environ.get("REFRESH_SECRET") or
              os.environ.get("DAILY_REFRESH_SECRET") or "").strip()
    if not secret:
        logger.warning(
            "🖼️  daily_r2_refresh: REFRESH_SECRET not set on main backend — "
            "skipping. R2 will continue serving stale dates. Set "
            "REFRESH_SECRET in Railway resourceful-essence to match the "
            "value on heroic-reprieve (daily FastAPI service)."
        )
        return
    try:
        r = _rq.post(
            f"{base}/refresh",
            headers={
                "X-Refresh-Secret": secret,
                "User-Agent": "dchub-cron-r2refresh/1.0",
            },
            timeout=30,
        )
        # The /refresh endpoint returns {"queued": true} immediately and
        # the actual render runs as a FastAPI BackgroundTask — so the
        # 200 OK just means the job got queued, not that R2 was written.
        if r.status_code == 200:
            logger.info("🖼️  daily_r2_refresh: queued OK on %s (200)", base)
        elif r.status_code == 401:
            logger.error(
                "🖼️  daily_r2_refresh: 401 bad secret on %s — REFRESH_SECRET "
                "on main backend does NOT match the value on heroic-reprieve. "
                "Rotate to the same value in both Railway services.", base)
        else:
            logger.error("🖼️  daily_r2_refresh: HTTP %d on %s — %s",
                         r.status_code, base, r.text[:200])
    except Exception as e:
        logger.error("🖼️  daily_r2_refresh: POST failed to %s — %s", base, e)


def _run_gas_pricing_refresh():
    """Nightly refresh of market_gas_pricing — fires /api/v1/markets/gas-pricing/cron.
    Recomputes Henry Hub spot + regional basis + delivered industrial / electric
    tariffs per Phase-1 marquee DCPI market and upserts the heat-rate-derived
    $/MWh scenarios. Idempotent (UNIQUE primary key on market_slug). Endpoint
    has a 90s soft deadline so it cannot overrun the next crawler slot.

    If DCHUB_ADMIN_KEY is unset, falls back to the in-process
    routes.powered_land_gas.run_gas_pricing_refresh() helper so the cron still
    works in dev (no HTTP roundtrip required)."""
    import requests as _rq
    key = (os.environ.get("DCHUB_ADMIN_KEY")
           or os.environ.get("DCHUB_INTERNAL_KEY")
           or os.environ.get("DCHUB_ADMIN_API_KEY") or "")
    if not key:
        # Dev / no-key fallback: call the in-process helper directly.
        try:
            from routes.powered_land_gas import run_gas_pricing_refresh
            d = run_gas_pricing_refresh() or {}
            logger.info("⛽ gas_pricing_refresh (in-proc): refreshed=%s failed=%s",
                        d.get("refreshed"), d.get("failed"))
        except Exception as e:
            logger.error("⛽ gas_pricing_refresh: in-proc fallback error — %s", e)
        return
    base = os.environ.get("DCHUB_INTERNAL_API", "http://127.0.0.1:8080")
    try:
        r = _rq.post(
            f"{base}/api/v1/markets/gas-pricing/cron",
            headers={"X-Admin-Key": key,
                     "User-Agent": "dchub-cron-gaspricing/1.0"},
            timeout=120,
        )
        d = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}) or {}
        logger.info("⛽ gas_pricing_refresh: refreshed=%s failed=%s duration=%ss eia=%s",
                    d.get("refreshed"), d.get("failed"),
                    d.get("duration_seconds"), d.get("eia_key_present"))
    except Exception as e:
        logger.error("⛽ gas_pricing_refresh: error — %s", e)


def _run_tool_calibration_check():
    """Brain Layer-15: daily tool-output drift check vs known-correct
    (input → expected range) comp sets. Files brain_findings on >2x
    deviation. Built after the Site Valuation Engine v1.0 calibration
    miss ($2M/MW shipped against $150-800K industry comp)."""
    try:
        from routes.brain_layer15_tool_calibration import (
            run_tool_calibration_check as _l15_run,
        )
        result = _l15_run()
        logger.info(
            "🎯 Tool calibration drift check: %d tools, %d findings filed",
            result.get("tools_checked", 0), result.get("findings_filed", 0),
        )
        for name, summary in (result.get("summary") or {}).items():
            logger.info(
                "   %s: %d/%d passed (pass_rate=%.2f)",
                name, summary.get("passed", 0),
                summary.get("tests_run", 0), summary.get("pass_rate", 0),
            )
    except Exception as e:
        logger.error("🎯 Tool calibration check error: %s", e, exc_info=True)


def _run_linkedin_engagement_sync():
    """r72 (2026-06-05): pull native LinkedIn engagement + impressions onto
    recent posts so the media measure→learn loop sees BOTH halves of the
    signal (likes/comments via socialActions AND impressions/clicks/shares
    via organizationalEntityShareStatistics).

    FAIL-SOFT: if the token lacks r_organization_social, fetch_* returns
    `reason: scope_or_auth_403` and we just log + continue — no exception
    propagates. Same-day cap is provided by SCHEDULE's same-hour pair
    (13,13), so at most one run per UTC day."""
    try:
        from linkedin_poster import (
            fetch_linkedin_engagement as _fetch_eng,
            fetch_linkedin_impressions as _fetch_imp,
        )
        eng = _fetch_eng(days=21) or {}
        imp = _fetch_imp(days=21) or {}
        logger.info(
            "📈 LinkedIn engagement sync: eng(checked=%d updated=%d reason=%s) "
            "imp(checked=%d updated=%d reason=%s)",
            eng.get("checked", 0), eng.get("updated", 0), eng.get("reason"),
            imp.get("checked", 0), imp.get("updated", 0), imp.get("reason"),
        )
    except Exception as e:
        logger.error("📈 LinkedIn engagement sync error: %s", e, exc_info=True)


def _run_accelerator_scan():
    """DC Hub Media accelerator (2026-06-05): scan LinkedIn posts for
    breakouts vs the 30d impressions baseline. Any post ≥2.0x baseline
    AND <6h old AND not yet accelerated gets:
      (a) social_media_posts row flagged: accelerate=TRUE, viral_score=R
      (b) identical Twitter + Bluesky cross-posts enqueued
          (status='approved' — the existing publisher loops respect
          the 2/day per-platform cap, so we cannot spam).
    Calls the loopback admin endpoint so the same auth gate applies to
    cron and to manual triggers."""
    import requests as _rq
    key = (os.environ.get("DCHUB_ADMIN_KEY")
           or os.environ.get("DCHUB_INTERNAL_KEY")
           or os.environ.get("DCHUB_ADMIN_API_KEY") or "")
    if not key:
        logger.warning("📣 accelerator_scan: skipped — DCHUB_ADMIN_KEY not set")
        return
    base = os.environ.get("DCHUB_INTERNAL_API", "http://127.0.0.1:8080")
    try:
        r = _rq.post(
            f"{base}/api/v1/admin/media/accelerator/scan",
            headers={"X-Admin-Key": key,
                     "User-Agent": "dchub-cron-accelerator/1.0"},
            timeout=60,
        )
        d = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}) or {}
        logger.info(
            "📣 accelerator_scan: scanned=%s accelerated=%s baseline=%s errors=%s",
            d.get("scanned"), d.get("accelerated"),
            d.get("baseline"), len(d.get("errors", []) or []),
        )
    except Exception as e:
        logger.error("📣 accelerator_scan: error — %s", e)


def _run_verdict_shift_post():
    """DCPI Verdict-Shift auto-press (2026-06-06): once-daily scan for
    markets whose DCPI verdict TODAY differs from the verdict 7 days
    ago. Qualifying shifts (BUILD or AVOID on at least one side) get
    an Anthropic-Claude-written 50-80-word LinkedIn blurb + a link to
    the canonical Market Brief, enqueued into social_media_posts so
    the existing LinkedIn publisher loop ships it under its 2/day cap.

    Cron call → X-Internal-Cron → the endpoint runs LIVE (the in-process
    _is_cron_call() check inside the route flips dry_run off when the
    cron secret is present). Caps at 3 posts/run; de-dups via
    UNIQUE(market_slug, shift_to, DATE) on market_verdict_post_log
    + a soft 48h cap per slug. Never raises."""
    import requests as _rq
    key = (os.environ.get("DCHUB_ADMIN_KEY")
           or os.environ.get("DCHUB_INTERNAL_KEY")
           or os.environ.get("DCHUB_ADMIN_API_KEY") or "")
    cron_secret = os.environ.get("DCHUB_CRON_SECRET", "")
    if not key and not cron_secret:
        logger.warning("📣 verdict_shift_post: skipped — "
                       "no DCHUB_ADMIN_KEY or DCHUB_CRON_SECRET set")
        return
    base = os.environ.get("DCHUB_INTERNAL_API", "http://127.0.0.1:8080")
    headers = {"User-Agent": "dchub-cron-verdict-shifts/1.0"}
    if key:
        headers["X-Admin-Key"] = key
    if cron_secret:
        headers["X-Internal-Cron"] = cron_secret
    try:
        # Force live via query param too — belt-and-suspenders alongside
        # the X-Internal-Cron header (the route accepts either).
        r = _rq.post(
            f"{base}/api/v1/admin/verdict-shifts/scan?live=1",
            headers=headers,
            timeout=60,
        )
        d = (r.json() if r.headers.get("content-type", "").startswith("application/json") else {}) or {}
        logger.info(
            "📣 verdict_shift_post: scanned=%s qualifying=%s posted=%s "
            "would_post=%s errors=%s",
            d.get("scanned"), d.get("qualifying"),
            d.get("posted"),  d.get("would_post"),
            len(d.get("errors", []) or []),
        )
    except Exception as e:
        logger.error("📣 verdict_shift_post: error — %s", e)


def _run_watchlist_realtime():
    """Real-time Watchlist + Verdict-Shift Alerts (2026-06-06): twice-
    daily fan-out for PRO+ watchers. Pulls today's verdict shifts from
    market_verdict_shifts._detect_shifts (one source of truth) and
    emails / browser-pushes every PRO+ watcher on a shifted market.
    FREE watchers are QUEUED (status='pending') for the Monday digest.

    Calls the dispatcher MODULE directly (no HTTP loopback) — fan-out
    is short (bounded by MAX_REALTIME_ALERTS_PER_RUN=500). The dispatcher
    is defensive everywhere (per-watcher try/except, missing Resend/
    VAPID degrades silently, malformed rows are skipped) so this runner
    can never crash the scheduler thread."""
    try:
        from routes.watchlist_dispatcher import dispatch_watchlist_alerts as _disp
        result = _disp(mode="realtime") or {}
        logger.info(
            "🔔 watchlist_realtime: shifts=%s watchers=%s sent=%s queued=%s "
            "skipped=%s push_sent=%s push_failed=%s errors=%s",
            result.get("shifts_seen"), result.get("watchers_total"),
            result.get("alerts_sent"), result.get("alerts_queued"),
            result.get("alerts_skipped"), result.get("push_sent"),
            result.get("push_failed"),
            len(result.get("errors", []) or []),
        )
    except Exception as e:
        logger.error("🔔 watchlist_realtime error: %s", e, exc_info=True)


def _run_watchlist_weekly_digest():
    """Weekly digest for FREE watchers (2026-06-06). Self-gates to
    Mondays (weekday()==0) — the SCHEDULE harness is hour-based, not
    weekly, so we cap here. Bundles every PENDING alert per watcher
    into one digest email and marks status='sent'. Defensive — never
    raises (mirror of mcp_registry_discover's weekday gate pattern)."""
    try:
        now = datetime.now(timezone.utc)
        if now.weekday() != 0:  # 0 == Monday
            logger.info(
                "🔔 watchlist_weekly_digest: skipped — weekly gate "
                "(today=%s, runs Mondays only)",
                now.strftime("%A"),
            )
            return
        from routes.watchlist_dispatcher import dispatch_watchlist_alerts as _disp
        result = _disp(mode="digest") or {}
        logger.info(
            "🔔 watchlist_weekly_digest: shifts=%s watchers=%s sent=%s "
            "queued=%s skipped=%s errors=%s",
            result.get("shifts_seen"), result.get("watchers_total"),
            result.get("alerts_sent"), result.get("alerts_queued"),
            result.get("alerts_skipped"),
            len(result.get("errors", []) or []),
        )
    except Exception as e:
        logger.error("🔔 watchlist_weekly_digest error: %s", e, exc_info=True)


def _run_mcp_presence_crawl():
    """MCP-presence crawl (2026-06-05): for each known MCP listing site
    DC Hub appears on (Smithery, MCPHive, LobeHub, Glama, YellowMCP,
    mcp.so, PulseMCP, etc.), fetch the listing page, scrape published
    tool count + uptime, compare to our live tool count, write drift
    findings to brain_findings. Defensive — never raises. Calls the
    module directly (no HTTP loopback) because the crawler is short
    enough to fit inside the scheduler's run-guard window."""
    try:
        from routes.mcp_presence_crawler import crawl_mcp_presence as _crawl
        result = _crawl() or {}
        logger.info(
            "📡 mcp_presence_crawl: checked=%s drift=%s stale=%s errors=%s actual_tools=%s",
            result.get("checked"), result.get("drift_found"),
            result.get("stale_found"), result.get("errors"),
            result.get("our_actual_tool_count"),
        )
    except Exception as e:
        logger.error("📡 mcp_presence_crawl error: %s", e, exc_info=True)


def _run_mcp_registry_discover():
    """MCP-registry discoverer (2026-06-05): weekly Google-SERP scan for
    NEW MCP listing sites DC Hub isn't on yet. WEEKDAY GATE: only fires
    on Mondays (weekday()==0) — the SCHEDULE harness is hour-based, not
    weekly, so we self-cap here. Files brain_findings rows of type
    'mcp_registry_discovered' for the brain to triage. Defensive —
    never raises."""
    try:
        now = datetime.now(timezone.utc)
        if now.weekday() != 0:  # 0 = Monday
            logger.info(
                "📡 mcp_registry_discover: skipped — weekly gate (today=%s, runs Mondays only)",
                now.strftime("%A"),
            )
            return
        from routes.mcp_presence_crawler import (
            discover_new_mcp_registries as _disc,
        )
        result = _disc() or {}
        logger.info(
            "📡 mcp_registry_discover: queries=%s candidates=%s new=%s submit_pages=%s errors=%s",
            result.get("queries_run"), result.get("candidates_seen"),
            result.get("new_domains"), result.get("submit_pages"),
            result.get("errors"),
        )
    except Exception as e:
        logger.error("📡 mcp_registry_discover error: %s", e, exc_info=True)


def _run_market_brief_warm():
    """Market Brief pre-warm (2026-06-06): pre-build the 9-section brief
    for each of the 5 seed markets so the first real visitor doesn't pay
    the cold fan-out cost. Also issues a loopback GET against the public
    HTML endpoint so the CF edge cache populates.

    Defensive — never raises. Bounded (5 slugs × short queries). Two
    layers:
      1) prewarm_seed_markets() builds the in-process brief (covers any
         backend that doesn't sit behind CF — direct hits to Railway).
      2) loopback GET to /markets/<slug>/brief so the CF s-maxage=21600
         cache populates ahead of organic traffic.
    """
    try:
        from routes.market_brief import prewarm_seed_markets, SEED_MARKETS
        result = prewarm_seed_markets() or {}
        logger.info(
            "🧾 market_brief_warm: warmed=%s errors=%s",
            result.get("warmed"), result.get("errors"),
        )
    except Exception as e:
        logger.error("🧾 market_brief_warm prewarm error: %s", e)
        SEED_MARKETS = ("northern-virginia", "dallas", "phoenix",
                        "atlanta", "chicago")
    # CF edge-cache prime (best-effort).
    try:
        import requests as _rq
        base = os.environ.get("DCHUB_PUBLIC_BASE", "https://dchub.cloud")
        primed = 0
        for slug in SEED_MARKETS:
            try:
                r = _rq.get(f"{base}/markets/{slug}/brief",
                            headers={"User-Agent": "dchub-cron-market-brief-warm/1.0"},
                            timeout=12)
                if r.status_code == 200:
                    primed += 1
            except Exception:
                pass
        logger.info("🧾 market_brief_warm: cf_primed=%s/%s", primed, len(SEED_MARKETS))
    except Exception as e:
        logger.error("🧾 market_brief_warm CF prime error: %s", e)


def _run_state_brief_warm():
    """State Brief pre-warm (2026-06-06): build the per-state aggregate
    brief for each of the 8 seed states (texas, california, virginia,
    georgia, ohio, oregon, illinois, arizona) so the first real visitor
    doesn't pay the cold fan-out cost. Also issues a loopback GET to
    prime the CF edge cache. Defensive — never raises. Bounded
    (8 states × ~600ms each + 1s sleeps = ~13s)."""
    try:
        from routes.state_brief import prewarm_seed_states, SEED_STATES
        result = prewarm_seed_states() or {}
        logger.info(
            "🗺️ state_brief_warm: warmed=%s errors=%s",
            result.get("warmed"), result.get("errors"),
        )
    except Exception as e:
        logger.error("🗺️ state_brief_warm prewarm error: %s", e)
        SEED_STATES = ("texas", "california", "virginia", "georgia",
                       "ohio", "oregon", "illinois", "arizona")
    # CF edge-cache prime (best-effort).
    try:
        import requests as _rq
        base = os.environ.get("DCHUB_PUBLIC_BASE", "https://dchub.cloud")
        primed = 0
        for slug in SEED_STATES:
            try:
                r = _rq.get(f"{base}/states/{slug}/brief",
                            headers={"User-Agent": "dchub-cron-state-brief-warm/1.0"},
                            timeout=12)
                if r.status_code == 200:
                    primed += 1
            except Exception:
                pass
        logger.info("🗺️ state_brief_warm: cf_primed=%s/%s",
                    primed, len(SEED_STATES))
    except Exception as e:
        logger.error("🗺️ state_brief_warm CF prime error: %s", e)


def _run_operator_brief_warm():
    """Operator Brief pre-warm (2026-06-06): build the 9-section operator
    brief for each of the 10 seed operators (aligned, qts, digital-realty,
    equinix, vantage, cyrusone, cologix, core-scientific, airtrunk,
    iron-mountain) so the first investor/broker visitor doesn't pay the
    cold per-section DB cost. Issues loopback GETs to prime the CF edge
    cache. Defensive — never raises. Bounded (10 ops × ~700ms + 1s
    sleeps = ~17s wall time)."""
    try:
        from routes.operator_brief import (prewarm_seed_operators,
                                            SEED_OPERATORS)
        result = prewarm_seed_operators() or {}
        logger.info(
            "🏢 operator_brief_warm: warmed=%s errors=%s",
            result.get("warmed"), result.get("errors"),
        )
    except Exception as e:
        logger.error("🏢 operator_brief_warm prewarm error: %s", e)
        SEED_OPERATORS = ("aligned", "qts", "digital-realty", "equinix",
                          "vantage", "cyrusone", "cologix",
                          "core-scientific", "airtrunk", "iron-mountain")
    # CF edge-cache prime (best-effort).
    try:
        import requests as _rq
        base = os.environ.get("DCHUB_PUBLIC_BASE", "https://dchub.cloud")
        primed = 0
        for slug in SEED_OPERATORS:
            try:
                r = _rq.get(f"{base}/operators/{slug}/brief",
                            headers={"User-Agent": "dchub-cron-operator-brief-warm/1.0"},
                            timeout=12)
                if r.status_code == 200:
                    primed += 1
            except Exception:
                pass
        logger.info("🏢 operator_brief_warm: cf_primed=%s/%s",
                    primed, len(SEED_OPERATORS))
    except Exception as e:
        logger.error("🏢 operator_brief_warm CF prime error: %s", e)


def _run_hyperscaler_brief_warm():
    """Hyperscaler Brief pre-warm (2026-06-06): build the 9-section
    hyperscaler brief for each of the 10 seed hyperscalers (aws, azure,
    google-cloud, meta, apple, oracle, tiktok-bytedance, tencent,
    alibaba, softbank) so the first M&A-banker visitor doesn't pay the
    cold per-section DB cost. Issues loopback GETs to prime the CF edge
    cache. Defensive — never raises. Bounded (10 hs × ~250ms + 9 × 1s
    sleeps = ~12s wall time)."""
    try:
        from routes.hyperscaler_brief import (prewarm_seed_hyperscalers,
                                               SEED_HYPERSCALERS)
        result = prewarm_seed_hyperscalers() or {}
        logger.info(
            "🛰️ hyperscaler_brief_warm: warmed=%s errors=%s",
            result.get("warmed"), result.get("errors"),
        )
    except Exception as e:
        logger.error("🛰️ hyperscaler_brief_warm prewarm error: %s", e)
        SEED_HYPERSCALERS = ("aws", "azure", "google-cloud", "meta",
                             "apple", "oracle", "tiktok-bytedance",
                             "tencent", "alibaba", "softbank")
    # CF edge-cache prime (best-effort).
    try:
        import requests as _rq
        base = os.environ.get("DCHUB_PUBLIC_BASE", "https://dchub.cloud")
        primed = 0
        for slug in SEED_HYPERSCALERS:
            try:
                r = _rq.get(f"{base}/hyperscalers/{slug}/brief",
                            headers={"User-Agent": "dchub-cron-hyperscaler-brief-warm/1.0"},
                            timeout=12)
                if r.status_code == 200:
                    primed += 1
            except Exception:
                pass
        logger.info("🛰️ hyperscaler_brief_warm: cf_primed=%s/%s",
                    primed, len(SEED_HYPERSCALERS))
    except Exception as e:
        logger.error("🛰️ hyperscaler_brief_warm CF prime error: %s", e)


def _run_mcp_presence_auto_fix():
    """MCP-presence auto-fix (2026-06-05): sweep every listing WHERE
    drift_detected=true and run the registry-appropriate submitter.
    Defaults to LIVE (dry_run=False) — the per-registry 1/hour rate-
    limit token in the submitter is the safety net. Defensive; never
    raises."""
    try:
        from routes.mcp_presence_crawler import (
            auto_fix_all_drifted as _fix,
        )
        result = _fix(dry_run=False) or {}
        logger.info(
            "📡 mcp_presence_auto_fix: checked=%s submitted=%s rate_limited=%s errors=%s",
            result.get("checked"), result.get("submitted"),
            result.get("rate_limited"), result.get("errors"),
        )
    except Exception as e:
        logger.error("📡 mcp_presence_auto_fix error: %s", e, exc_info=True)


def _run_feedback_triage():
    """Customer Feedback Forum triage (2026-06-06): pull status='new'
    + 'triaged' rows from feedback_submissions, classify via Claude,
    write brain_recommendation back, and (LOW class only + 2-cycle
    diff-hash gate + 5/day cap + kill switch
    FEEDBACK_AUTO_APPLY_DISABLE) mark eligible rows in_progress so
    the brain_pr_opener cron ships them. HIGH-class rows email the
    user via Resend with signed-token approve/reject buttons.
    Defensive; never raises. Slot 8/20 UTC."""
    try:
        from routes.feedback_triage import triage_pending_submissions
        result = triage_pending_submissions(limit=20) or {}
        cls = result.get("classified", {}) or {}
        logger.info(
            "📬 feedback_triage: checked=%s LOW=%s MED=%s HIGH=%s SPAM=%s "
            "applied=%s kill=%s errors=%s",
            result.get("checked"),
            cls.get("LOW", 0), cls.get("MEDIUM", 0),
            cls.get("HIGH", 0), cls.get("SPAM", 0),
            result.get("auto_applied", 0),
            result.get("kill_switch"),
            len(result.get("errors", []) or []),
        )
    except Exception as e:
        logger.error("📬 feedback_triage error: %s", e, exc_info=True)


_RUNNERS = {
    "market_refresh":      _run_market_refresh,
    "news":                _run_news_crawler,
    "api_discovery":       _run_api_discovery,
    "energy_discovery":    _run_energy_discovery,
    "knowledge_sync":      _run_knowledge_sync,
    "deals":               _run_deals_crawler,
    "facility_discovery":  _run_facility_discovery,
    "infrastructure_sync": _run_infrastructure_sync,
    "kmz_discovery":       _run_kmz_discovery,
    "intl_infra_ingest":   _run_intl_infra_ingest,
    "ai_lab_outreach":     _run_ai_lab_auto_outreach,
    "lost_conversion":     _run_lost_conversion_outreach,
    "auto_interconnect":   _run_auto_interconnect,
    "gas_pricing_refresh": _run_gas_pricing_refresh,
    "tool_calibration":    _run_tool_calibration_check,
    "linkedin_engagement_sync": _run_linkedin_engagement_sync,
    "accelerator_scan":    _run_accelerator_scan,
    "mcp_presence_crawl":  _run_mcp_presence_crawl,
    "mcp_registry_discover": _run_mcp_registry_discover,
    "mcp_presence_auto_fix": _run_mcp_presence_auto_fix,
    "market_brief_warm":   _run_market_brief_warm,
    "state_brief_warm":    _run_state_brief_warm,
    "operator_brief_warm": _run_operator_brief_warm,
    "feedback_triage":     _run_feedback_triage,
    "verdict_shift_post":  _run_verdict_shift_post,
    "watchlist_realtime":       _run_watchlist_realtime,
    "watchlist_weekly_digest":  _run_watchlist_weekly_digest,
    "hyperscaler_brief_warm":   _run_hyperscaler_brief_warm,
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
