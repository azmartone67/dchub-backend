#!/usr/bin/env python3
"""
DC Hub External Scheduler v3.9
===============================
Triggers discovery jobs via HTTP POST to the DC Hub API /api/jobs/* endpoints.
All jobs are staggered to prevent Railway resource conflicts.

Usage:
  python3 dchub-scheduler.py              # Run the full scheduler loop
  python3 dchub-scheduler.py --once       # Run all due jobs once and exit
  python3 dchub-scheduler.py --job news   # Run a specific job and exit
  python3 dchub-scheduler.py --all        # Run ALL jobs immediately
  python3 dchub-scheduler.py --status     # Check health + job status

Environment:
  DCHUB_API_BASE    — API base URL (default: https://dchub-api-production.up.railway.app)
  DCHUB_ADMIN_KEY   — Admin API key (required)

v3.9 changelog:
  - NEW '/api/jobs/backup' endpoint (lightweight Neon snapshot) — fixes 404
  - NEW '/api/jobs/mcp-rate-cleanup' endpoint — fixes 404
  - NEW 'smoke_test' job — runs /api/jobs/smoke-test 6x/day at :55 past
  - 21 active jobs (was 20)

v3.8 changelog:
  - RE-ENABLED infra_sync as 'infra_sync_safe' — reduced from 4x to 1x/day (02:30 UTC)
  - NEW pre-flight pool health check: infra_sync_safe checks /api/admin/pool-status
    before running; aborts if pool utilization >60% or circuit breaker is open
  - NEW 'pool_watchdog' job — checks pool health every 2 hours, logs alerts
  - 20 active jobs (was 18)

v3.7 changelog:
  - DISABLED infra_sync — runaway substation INSERT loop was exhausting Neon
    pool (49-50/50 connections) due to missing UNIQUE constraint on substations
    table + il.latitude column reference bug. Re-enable after DB fixes.
  - FIXED X-Internal-Key header (now via get_internal_key_for_client; env-based)
  - REMOVED orphaned @scheduler.task decorator at EOF (referenced nonexistent scheduler object)
  - 18 active jobs (was 19)

v3.6 changelog:
  - REMOVED keep-alive (Railway doesn't sleep — it was wasting pool connections)
  - DISABLED autopilot, autonomous_brain, ambassador (modules not installed,
    every call just returns 'skipped' or times out — add back when modules exist)
  - Reduced energy_discovery from 3x to 2x/day (was causing HTTP 500s)
  - Fixed version strings throughout
  - 19 active jobs (was 23)

Schedule (UTC) — 21 active jobs, verified no overlaps:
  00:00  News/RSS Refresh        (also 04, 08, 12, 16, 20)
  00:20  Auto-Approve            (also 04, 08, 12, 16, 20)
  00:45  Simple Alerts           (also 02,04,06,08,10,12,14,16,18,20,22)
  00:55  Production Smoke Test   (also 04, 08, 12, 16, 20)
  01:00  Facility Discovery      (also 07, 14, 19)
  01:15  Alert Emails            (also 05,09,13,17,21)
  02:30  Infrastructure Sync     (1x/day, pool-gated — aborts if pool >60%)
  03:00  AI Ecosystem Agent      (also 10, 15, 22)
  03:10  MCP Rate Limit Cleanup  (daily)
  03:15  Neon DB Backup          (daily)
  03:30  Fiber Route Sync        (also 09:30, 15:30, 21:30)
  03:45  Confidence Recalc       (daily)
  04:00  KMZ Discovery           (also 16:00) — 12hr cycle
  05:00  AI Outreach Agent       (also 13, 21)
  06:00  Global Intelligence     (also 18)
  06:45  Capacity Headroom       (also 12:45, 18:45)
  07:00  Pool Watchdog           (also 13, 19, 01)
  08:30  Evolution Engine        (also 20:30)
  10:00  Energy Discovery        (also 18:00) — reduced from 3x to 2x
  11:30  Content Publishing      (daily)
  12:30  Market Report           (daily)
  16:30  Ambassador + Drip       (daily)

  DISABLED:
  xx:xx  Infrastructure Sync     — pool exhaustion (substations UNIQUE constraint + il.latitude bug)
"""

import os
import sys
import time
import json
import logging
import argparse
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from internal_auth import is_valid_internal_key, get_internal_key_for_client

# ============================================================
# CONFIG
# ============================================================
API_BASE = os.environ.get('DCHUB_API_BASE', 'https://dchub-api-production.up.railway.app')
ADMIN_KEY = os.environ.get('DCHUB_ADMIN_KEY', '')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s UTC [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('dchub-scheduler')

# ============================================================
# JOB DEFINITIONS — 21 active (v3.9)
# ============================================================
JOBS = {
    'permit_scraper': {
        'name': 'Permit Scraper (Phase 1)',
        'endpoint': '/api/jobs/permit-scraper',
        'method': 'POST',
        'hours': [2],
        'minute': 0,
        'day_of_week': 6,  # Sunday only
        'timeout': 3600,
    },
    'sec_parser': {
        'name': 'SEC/EDGAR Parser (Phase 2)',
        'endpoint': '/api/jobs/sec-parser',
        'method': 'POST',
        'hours': [3],
        'minute': 0,
        'day_of_month': 1,  # 1st of month only
        'timeout': 3600,
    },
    'news': {
        'name': 'News/RSS Refresh',
        'endpoint': '/api/jobs/news-refresh',
        'method': 'POST',
        'hours': [0, 4, 8, 12, 16, 20],
        'minute': 0,
        'timeout': 300,
    },
    'discovery': {
        'name': 'Facility Discovery',
        'endpoint': '/api/jobs/discovery',
        'method': 'POST',
        'hours': [1, 7, 14, 19],
        'minute': 0,
        'timeout': 180,
    },
    'auto_approve': {
        'name': 'Auto-Approve',
        'endpoint': '/api/jobs/auto-approve',
        'method': 'POST',
        'hours': [0, 4, 8, 12, 16, 20],
        'minute': 20,
        'timeout': 120,
    },
    'global_intel': {
        'name': 'Global Intelligence',
        'endpoint': '/api/jobs/global-intelligence',
        'method': 'POST',
        'hours': [6, 18],
        'minute': 0,
        'timeout': 180,
    },
    'ecosystem': {
        'name': 'AI Ecosystem Agent',
        'endpoint': '/api/jobs/ai-ecosystem',
        'method': 'POST',
        'hours': [3, 10, 15, 22],
        'minute': 0,
        'timeout': 120,
    },
    'outreach': {
        'name': 'AI Outreach Agent',
        'endpoint': '/api/jobs/ai-outreach',
        'method': 'POST',
        'hours': [5, 13, 21],
        'minute': 0,
        'timeout': 120,
    },
    'evolution': {
        'name': 'Evolution Engine',
        'endpoint': '/api/jobs/evolution',
        'method': 'POST',
        'hours': [8, 20],
        'minute': 30,
        'timeout': 120,
    },
    'content': {
        'name': 'Content Publishing',
        'endpoint': '/api/jobs/content-publish',
        'method': 'POST',
        'hours': [11],
        'minute': 30,
        'timeout': 120,
    },
    'backup': {
        'name': 'Neon DB Backup',
        'endpoint': '/api/jobs/backup',
        'method': 'POST',
        'hours': [3],
        'minute': 15,
        'timeout': 600,
    },
    'energy_discovery': {
        'name': 'Energy Discovery',
        'endpoint': '/api/jobs/energy-discovery',
        'method': 'POST',
        'hours': [10, 18],           # reduced from [2,10,18] — was causing 500s
        'minute': 0,
        'timeout': 180,
    },
    'alert_emails': {
        'name': 'Alert Emails',
        'endpoint': '/api/jobs/alert-emails',
        'method': 'POST',
        'hours': [1, 5, 9, 13, 17, 21],
        'minute': 15,
        'timeout': 120,
    },
    'drip_emails': {
        'name': 'Welcome Email Drip',
        'endpoint': f'/api/admin/drip-check?admin_key={os.environ.get("DCHUB_ADMIN_KEY", "")}',
        'method': 'POST',
        'hours': [16],               # 9 AM MST = 4 PM UTC
        'minute': 30,
        'timeout': 60,
    },
    'simple_alerts': {
        'name': 'Simple Alerts',
        'endpoint': '/api/jobs/simple-alerts',
        'method': 'POST',
        'hours': [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22],
        'minute': 45,
        'timeout': 120,
    },
    'market_report': {
        'name': 'Market Report',
        'endpoint': '/api/jobs/market-report',
        'method': 'POST',
        'hours': [12],
        'minute': 30,
        'timeout': 300,
    },
    # 'infra_sync' DISABLED v3.7 — runaway substation INSERT loop
    # exhausts Neon pool (49/50) due to missing UNIQUE constraint +
    # column il.latitude does not exist bug. Re-enable after:
    #   1) ALTER TABLE substations ADD CONSTRAINT ... UNIQUE(name, lat, lng)
    #   2) Fix il.latitude → lat column reference in infrastructure_layers query
    'capacity_headroom': {
        'name': 'Capacity Headroom',
        'endpoint': '/api/jobs/capacity-headroom',
        'method': 'POST',
        'hours': [6, 12, 18],
        'minute': 45,
        'timeout': 180,
    },
    'kmz_discovery': {
        'name': 'KMZ Infrastructure Discovery',
        'endpoint': '/api/kmz-discovery/run',
        'method': 'POST',
        'hours': [4, 16],
        'minute': 0,
        'timeout': 600,
    },
    'fiber_sync': {
        'name': 'Fiber Route Sync',
        'endpoint': '/api/jobs/fiber-sync',
        'method': 'POST',
        'hours': [3, 9, 15, 21],
        'minute': 30,
        'timeout': 300,
    },
    'mcp_rate_cleanup': {
        'name': 'MCP Rate Limit Cleanup',
        'endpoint': '/api/jobs/mcp-rate-cleanup',
        'method': 'POST',
        'hours': [3],
        'minute': 10,
        'timeout': 30,
    },
    'infra_sync_safe': {
        'name': 'Infrastructure Sync (pool-gated)',
        'endpoint': '/api/jobs/infrastructure-sync',
        'method': 'POST',
        'hours': [2],
        'minute': 30,
        'timeout': 300,
        'pool_gate': True,       # Pre-flight pool check — aborts if pool >60%
        'pool_max_pct': 60,
    },
    'pool_watchdog': {
        'name': 'Pool Health Watchdog',
        'endpoint': '/api/admin/pool-status',
        'method': 'GET',
        'hours': [1, 7, 13, 19],
        'minute': 0,
        'timeout': 15,
        'watchdog': True,        # Log alert level, don't treat non-200 as failure
    },
    'mcp_cache_seed': {
        'name': 'MCP Cache Seed',
        'endpoint': '/api/admin/seed-mcp-cache',
        'method': 'POST',
        'hours': [0, 6, 12, 18],
        'minute': 50,
        'timeout': 30,
    },
    'smoke_test': {
        'name': 'Production Smoke Test',
        'endpoint': '/api/jobs/smoke-test',
        'method': 'POST',
        'hours': [0, 4, 8, 12, 16, 20],
        'minute': 55,
        'timeout': 120,
    },
}

# ── Disabled jobs (modules not installed on Railway) ──────────
# Re-enable by moving back into JOBS dict when modules are deployed:
#   'autopilot'        → autonomous_brain module required
#   'autonomous_brain' → autonomous_brain module required
#   'ambassador'       → ai_outreach_agent ambassador module required
# NOTE: infra_sync moved to JOBS as 'infra_sync_safe' in v3.8 (pool-gated, 1x/day)
DISABLED_JOBS = {
    'autopilot': {
        'name': 'Auto-Pilot (Deals)',
        'endpoint': '/api/jobs/autopilot',
        'method': 'POST',
        'hours': [9, 21],
        'minute': 15,
        'timeout': 300,
        'disabled_reason': 'autonomous_brain module not installed',
    },
    'autonomous_brain': {
        'name': 'Autonomous Brain',
        'endpoint': '/api/jobs/autonomous-brain',
        'method': 'POST',
        'hours': [10, 22],
        'minute': 15,
        'timeout': 300,
        'disabled_reason': 'autonomous_brain module not installed',
    },
    'ambassador': {
        'name': 'Ambassador',
        'endpoint': '/api/jobs/ambassador',
        'method': 'POST',
        'hours': [16],
        'minute': 30,
        'timeout': 120,
        'disabled_reason': 'ai_outreach_agent ambassador module not installed',
    },
    # Phase RRR-winback-cron (2026-05-18) — wire the existing
    # winback delivery system. routes/winback_outreach.py:deliver_pending()
    # exists + works (uses Neon + Resend), but no cron called it. Same
    # "defined-but-never-scheduled" bug class as deal_ingestion +
    # content_publisher. Mondays 14:00 UTC: aggregates the week's
    # dormant agents, emails operator briefing per platform (7-day
    # cooldown built into deliver_pending), records to
    # winback_outreach_sent for the verifier loop. Brain's
    # check_winback_pitches_unsent detector watches output side.
    'winback_delivery': {
        'name': 'Winback Outreach Delivery',
        'endpoint': '/api/v1/media/winback/deliver',
        'method': 'POST',
        'hours': [14],
        'minute': 0,
        'day_of_week': 0,  # Monday
        'timeout': 300,
    },
    # Phase RRR-newsletter-cron (2026-05-18) — wire the public weekly
    # newsletter we built today. routes/weekly_public_newsletter.py
    # exists; this fires the send to all active public subscribers
    # every Monday at 13:00 UTC (1h before the winback briefing).
    'weekly_public_newsletter': {
        'name': 'Public Weekly Newsletter Send',
        'endpoint': '/api/v1/weekly/send-public',
        'method': 'POST',
        'hours': [13],
        'minute': 0,
        'day_of_week': 0,  # Monday
        'timeout': 300,
    },
    # Phase RRR-cron-batch (2026-05-18) — 6 endpoints surfaced by the
    # new check_cron_endpoint_unscheduled brain detector. All were
    # admin-gated + idempotent + already-built; just missing schedule.
    # Staggered minutes to avoid pool contention.

    # Site Sentinel: actively scan + persist health for all surfaces.
    # /scan returns cached; /scan-now triggers fresh probe. Every 4h.
    'sentinel_scan_now': {
        'name': 'Site Sentinel — Active Scan',
        'endpoint': '/api/v1/sentinel/scan-now',
        'method': 'POST',
        'hours': [1, 5, 9, 13, 17, 21],
        'minute': 20,
        'timeout': 300,
    },
    # Heartbeat: refresh per-surface freshness telemetry. Every 4h,
    # staggered :40 to follow sentinel :20.
    'heartbeat_refresh': {
        'name': 'Heartbeat Surface Refresh',
        'endpoint': '/api/v1/heartbeat/refresh',
        'method': 'POST',
        'hours': [1, 5, 9, 13, 17, 21],
        'minute': 40,
        'timeout': 300,
    },
    # Phase ZZZZ-heartbeat-auto (2026-05-18): drain auto-discovered stale
    # surfaces. /refresh only iterates the static SURFACES list and missed
    # 34 auto-discovered surfaces (iso_*, /api/v1/news, /api/v1/grid/*)
    # that were sitting RED on the /heartbeat dashboard. /auto sorts by
    # stale-age and drains up to 250/call. Every 30min keeps red counts
    # near zero for surfaces with stale_after as short as 1h.
    'heartbeat_auto_drain': {
        'name': 'Heartbeat Auto-Drain Stale Surfaces',
        'endpoint': '/api/v1/heartbeat/auto?batch=250',
        'method': 'POST',
        'minutes': [5, 35],   # every 30min — offset to avoid :00 cron pileup
        'timeout': 120,
    },
    # Phase ZZZZ-pulse-cron (2026-05-18): industry pulse compute is the
    # ~15-query roll-up that would 502 if run in a user request. This
    # cron populates the in-process cache every 30min so the public
    # /api/v1/industry/pulse endpoint can always serve from memory in
    # <5ms. CC-BY-4.0 analyst-citable surface stays always-available.
    'industry_pulse_refresh': {
        'name': 'Industry Pulse — Compute and Cache',
        'endpoint': '/api/v1/industry/pulse/refresh',
        'method': 'POST',
        'minutes': [7, 37],   # every 30min, offset to spread load
        'timeout': 60,
    },
    # Phase ZZZZ-brain-narrative (2026-05-18): every 6h have Claude
    # synthesize current brain findings into a 3-paragraph operational
    # narrative. Replaces "operator reads 43 flat findings" with
    # "operator reads 200-word story." Costs ~$0.001/call (haiku).
    'brain_narrative_refresh': {
        'name': 'Brain Narrative — Claude Digest',
        'endpoint': '/api/v1/brain/narrative/refresh',
        'method': 'POST',
        'hours': [3, 9, 15, 21],
        'minute': 25,
        'timeout': 60,
    },
    # Phase ZZZZ-brain-L8 (2026-05-19): Orchestrator — Claude synthesizes
    # all brain layers into a prioritized action plan, refreshed every
    # 6h offset from L2 narrative so both don't fire simultaneously.
    #
    # Phase FF+7-durability (2026-05-19): RE-ENABLED after Phase FF+7-
    # emergency disable. Endpoint is now fire-and-forget (returns 202
    # immediately; Claude call runs in a background thread). L20
    # durability guard refuses new calls when RSS is high. The 2-min
    # crash loop class is closed.
    'brain_orchestrator_refresh': {
        'name': 'Brain L8 Orchestrator — Action Plan (async-safe)',
        'endpoint': '/api/v1/brain/orchestrator/refresh',
        'method': 'POST',
        'hours': [3, 9, 15, 21],
        'minute': 45,
        'timeout': 15,  # bg-thread mode returns 202 in <1s; 15s is plenty
    },
    # Phase FF+6 (2026-05-18): Brain L11 QA Agent — probes every public
    # surface every 6h. Status, perf, dynamic-vs-static, regressions.
    # Offset :05 so it doesn't collide with L2 (:25) or L8 (:45).
    'brain_qa_agent_sweep': {
        'name': 'Brain L11 QA Agent — Surface Sweep',
        'endpoint': '/api/v1/brain/qa-agent',
        'method': 'POST',
        'hours': [3, 9, 15, 21],
        'minute': 5,
        'timeout': 240,
    },
    # Phase FF+6 (2026-05-18): Brain L12 expansion snapshot — once daily
    # so 7d/30d deltas are stable. Lightweight, no LLM, ~1s.
    'brain_expansion_snapshot': {
        'name': 'Brain L12 — Expansion Snapshot',
        'endpoint': '/api/v1/brain/expansion',
        'method': 'POST',
        'hours': [4],
        'minute': 0,
        'timeout': 30,
    },
    # Phase FF+7-durability (2026-05-19): RE-ENABLED with fire-and-
    # forget bg-thread pattern.
    'brain_causal_analyze': {
        'name': 'Brain L14 — Causal Reasoner (async-safe)',
        'endpoint': '/api/v1/brain/causal/analyze',
        'method': 'POST',
        'hours': [3, 9, 15, 21],
        'minute': 15,
        'timeout': 15,
    },
    # Phase FF+7-press-loop (2026-05-19): auto-draft press releases from
    # AI-citation observations. Closes the gap user spotted: ChatGPT +
    # Gemini cited dchub.cloud TODAY but /dc-hub-media still showed 73-
    # day-old releases. Now every dchub_cited=true observation becomes
    # a draft press release (slug-keyed, idempotent). Runs daily 14:30
    # UTC, after the morning press cron + agent-vendor digest.
    'ai_citation_press_draft': {
        'name': 'AI-Citation Press Loop — auto-draft releases',
        'endpoint': '/api/v1/ai-citations/draft-press?write=true&auto_approve=true&days=7',
        'method': 'POST',
        'hours': [14],
        'minute': 30,
        'timeout': 30,
    },
    # Phase FF+7 (2026-05-19): Brain L15 auto-action — scans L14's
    # high-confidence chains and opens GitHub issues. Runs at :20,
    # 5min after L14 lands the analysis. Idempotent (7d dedup).
    'brain_auto_action': {
        'name': 'Brain L15 — Auto-Action (GitHub issues)',
        'endpoint': '/api/v1/brain/auto-action/run',
        'method': 'POST',
        'hours': [3, 9, 15, 21],
        'minute': 20,
        'timeout': 60,
    },
    # Phase FF+7-meta — Brain L16 Self-Critique. DISABLED 2026-05-19
    # 10:19 UTC — same crash-loop class as L8/L14. Re-enable after
    # refactoring to background-thread mode.
    'brain_self_critique_DISABLED': {
        'name': 'Brain L16 — Self-Critique (DISABLED)',
        'endpoint': '/api/v1/brain/self-critique/run',
        'method': 'POST',
        'hours': [],
        'minute': 35,
        'timeout': 90,
    },
    # Phase FF+7-meta — Brain L18 Memory Consolidation. DISABLED
    # 2026-05-19 10:19 UTC — same crash-loop class. Re-enable after
    # refactoring.
    'brain_memory_consolidate_DISABLED': {
        'name': 'Brain L18 — Memory Consolidation (DISABLED)',
        'endpoint': '/api/v1/brain/lessons/consolidate',
        'method': 'POST',
        'hours': [],
        'minute': 40,
        'timeout': 90,
    },
    # Press queue scan: detects new auto-press triggers (DCPI movers,
    # facility events). Every 4h, staggered :50.
    'press_queue_scan': {
        'name': 'Press Queue Scan',
        'endpoint': '/api/v1/press/scan',
        'method': 'POST',
        'hours': [1, 5, 9, 13, 17, 21],
        'minute': 50,
        'timeout': 180,
    },
    # Competitor intel: snapshot competitor sites for change detection.
    # Daily at 02:30 UTC (off-peak).
    'competitor_scan': {
        'name': 'Competitor Intelligence Scan',
        'endpoint': '/api/v1/competitors/scan',
        'method': 'POST',
        'hours': [2],
        'minute': 30,
        'timeout': 600,
    },
    # Daily aggregation: market brief + alert digest + linkedin daily.
    # The omnibus daily cron, fires at 06:30 UTC (after morning data).
    'daily_aggregation': {
        'name': 'Daily Aggregation (omnibus)',
        'endpoint': '/api/v1/daily/run?job=all',
        'method': 'POST',
        'hours': [6],
        'minute': 30,
        'timeout': 600,
    },
    # Market alerts: snapshot + fan-out to subscribers. Daily at 09:00
    # UTC (avoid overlap with daily aggregation).
    'market_alerts_send': {
        'name': 'Market Alerts Send',
        'endpoint': '/api/v1/alerts/run?send=true',
        'method': 'POST',
        'hours': [9],
        'minute': 0,
        'timeout': 300,
    },
    # ─── Phase RRR-cron-batch-2 (2026-05-18) — 11 more from the 18
    # remaining unscheduled. Conservative cadences + heavy staggering
    # to keep pool pressure low. Skipped: /api/jobs/heal/run (needs
    # per-action parameter), /api/jobs/sync-all-tables (too heavy),
    # /api/jobs/db-backup (existing backup cron handles it),
    # /api/v1/packages/refresh (already wired as a thread).

    # ── Neon pool health: very lightweight (in-memory only), every 15min ──
    'neon_health': {
        'name': 'Neon Pool Health Probe',
        'endpoint': '/api/jobs/neon-health',
        'method': 'POST',
        'hours': list(range(24)),
        'minute': 7,                       # every hour at :07
        'timeout': 30,
    },
    # ── Health probe: deeper than neon_health, every 30min on the half ──
    'health_probe': {
        'name': 'Deep Health Probe',
        'endpoint': '/api/jobs/health-probe',
        'method': 'POST',
        'hours': list(range(24)),
        'minute': 37,                      # every hour at :37
        'timeout': 60,
    },

    # ── Job posting aggregator (5 endpoints) — daily, heavily staggered ──
    'jobs_trends': {
        'name': 'Job Postings — Trends',
        'endpoint': '/api/jobs/trends',
        'method': 'POST',
        'hours': [4],
        'minute': 0,
        'timeout': 300,
    },
    'jobs_expansion_signals': {
        'name': 'Job Postings — Expansion Signals',
        'endpoint': '/api/jobs/expansion-signals',
        'method': 'POST',
        'hours': [4],
        'minute': 15,
        'timeout': 300,
    },
    'jobs_skills': {
        'name': 'Job Postings — Skills Index',
        'endpoint': '/api/jobs/skills',
        'method': 'POST',
        'hours': [4],
        'minute': 30,
        'timeout': 300,
    },
    'jobs_market_heat': {
        'name': 'Job Postings — Market Heat',
        'endpoint': '/api/jobs/market-heat',
        'method': 'POST',
        'hours': [4],
        'minute': 45,
        'timeout': 300,
    },
    'jobs_summary': {
        'name': 'Job Postings — Summary Roll-up',
        'endpoint': '/api/jobs/summary',
        'method': 'POST',
        'hours': [5],
        'minute': 0,
        'timeout': 300,
    },

    # ── Network/IX sync (4 endpoints) — weekly Tuesday 03:00+, staggered ──
    'network_sync': {
        'name': 'Network — Full Sync',
        'endpoint': '/api/jobs/network-sync',
        'method': 'POST',
        'hours': [3],
        'minute': 0,
        'day_of_week': 1,                  # Tuesday
        'timeout': 1800,
    },
    'ix_sync': {
        'name': 'IX — Peering Sync',
        'endpoint': '/api/jobs/ix-sync',
        'method': 'POST',
        'hours': [3],
        'minute': 20,
        'day_of_week': 1,                  # Tuesday
        'timeout': 1800,
    },
    'campus_sync': {
        'name': 'Campus Layout Sync',
        'endpoint': '/api/jobs/campus-sync',
        'method': 'POST',
        'hours': [3],
        'minute': 40,
        'day_of_week': 1,                  # Tuesday
        'timeout': 1800,
    },
    'peeringdb_full_sync': {
        'name': 'PeeringDB — Full Sync',
        'endpoint': '/api/jobs/peeringdb-full-sync',
        'method': 'POST',
        'hours': [4],
        'minute': 0,
        'day_of_week': 1,                  # Tuesday
        'timeout': 3600,
    },

    # ── Fiber/subsea sync (3 endpoints) — weekly Wednesday 03:00+, staggered ──
    'fiber_full_sync': {
        'name': 'Fiber — Full Sync',
        'endpoint': '/api/jobs/fiber-full-sync',
        'method': 'POST',
        'hours': [3],
        'minute': 0,
        'day_of_week': 2,                  # Wednesday
        'timeout': 3600,
    },
    'subsea_sync': {
        'name': 'Subsea Cables — Sync',
        'endpoint': '/api/jobs/subsea-sync',
        'method': 'POST',
        'hours': [3],
        'minute': 30,
        'day_of_week': 2,                  # Wednesday
        'timeout': 1800,
    },
    'carrier_sync': {
        'name': 'Carrier — Sync',
        'endpoint': '/api/jobs/carrier-sync',
        'method': 'POST',
        'hours': [4],
        'minute': 0,
        'day_of_week': 2,                  # Wednesday
        'timeout': 1800,
    },
    # Phase RRR-press-loop-cron (2026-05-18) — close the brain ↔ press
    # loop. Saturday 11:00 UTC: brain pulls last 7d of ship-wins,
    # creates draft press_releases rows. Monday 14:00 UTC: winback cron
    # emails operator briefing. Monday 03:00+ : publish-now fans drafts
    # to LinkedIn/X/Bluesky. By Tuesday morning, every shipping win
    # from the prior week is in market with competitor-aware positioning.
    'brain_press_loop': {
        'name': 'Brain Press Loop — Draft Ship-Win Releases',
        'endpoint': '/api/v1/brain/press-loop?days=7&write=true',
        'method': 'POST',
        'hours': [11],
        'minute': 0,
        'day_of_week': 5,                  # Saturday
        'timeout': 120,
    },
    # Phase RRR-publish-cron (2026-05-18) — the smoking gun:
    # press_releases table has 6 auto-generated/7d but
    # published_7d.linkedin=0 + queued_unpublished=11. The marketing
    # engine generates press AND has /publish-now to push it to
    # LinkedIn/X/Bluesky, but NOTHING was calling /publish-now on a
    # schedule. Yet another "endpoint exists, no cron" silent skip.
    # Fires every 3h to catch new generations within a reasonable
    # delay — 11 currently queued will flush on first run.
    'marketing_publish_now': {
        'name': 'Marketing — Publish Pending Press Releases',
        'endpoint': '/api/v1/marketing/publish-now',
        'method': 'POST',
        'hours': [0, 3, 6, 9, 12, 15, 18, 21],
        'minute': 5,
        'timeout': 300,
    },
}

# ============================================================
# POOL GATE — Pre-flight check before heavy jobs (v3.8)
# ============================================================
def check_pool_health():
    """Query /api/admin/pool-status and return (utilization_pct, alert_level, is_safe).
    Returns (0, 'unknown', False) on failure."""
    try:
        status, data = api_call('/api/admin/pool-status', method='GET', timeout=10)
        if status != 200 or not isinstance(data, dict):
            return 0, 'unknown', False
        pool = data.get('pool', {})
        alert = data.get('alert', {})
        util_pct = pool.get('utilization_pct', 0)
        alert_level = alert.get('level', 'unknown')
        cb_open = data.get('circuit_breaker', {}).get('open', False)
        is_safe = not cb_open  # Safe if circuit breaker is closed
        return util_pct, alert_level, is_safe
    except Exception as e:
        log.warning(f"  Pool health check failed: {e}")
        return 0, 'error', False


def pool_gate(job):
    """Returns True if pool utilization is below the job's threshold, False to skip."""
    max_pct = job.get('pool_max_pct', 60)
    util_pct, alert_level, is_safe = check_pool_health()

    if not is_safe:
        log.warning(f"  🚫 POOL GATE: Circuit breaker open — skipping {job['name']}")
        return False

    if util_pct > max_pct:
        log.warning(f"  🚫 POOL GATE: Utilization {util_pct}% > {max_pct}% threshold — skipping {job['name']}")
        return False

    log.info(f"  ✅ POOL GATE: Utilization {util_pct}% (threshold {max_pct}%) — proceeding with {job['name']}")
    return True


# ============================================================
# HTTP HELPER
# ============================================================
def api_call(endpoint, method='POST', timeout=60):
    url = API_BASE.rstrip('/') + endpoint
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'DCHub-Scheduler/3.9',
    }
    if ADMIN_KEY:
        headers['X-Admin-Key'] = ADMIN_KEY
        headers['Authorization'] = f'Bearer {ADMIN_KEY}'
        headers['X-Internal-Key'] = get_internal_key_for_client()

    try:
        req = Request(url, method=method, headers=headers)
        if method == 'POST':
            req.data = b'{}'
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8')
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {'raw': body[:500]}
            return resp.status, data
    except HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')[:500]
        return e.code, {'error': body}
    except URLError as e:
        return 0, {'error': str(e.reason)}
    except Exception as e:
        return 0, {'error': str(e)}

# ============================================================
# SCHEDULER LOGIC
# ============================================================
def is_job_in_window(job, now=None, window_minutes=3):
    if now is None:
        now = datetime.now(timezone.utc)
    if job.get('minute') is None:
        return True
    for hour in job['hours']:
        diff = (now.hour - hour) * 60 + (now.minute - job['minute'])
        if 0 <= diff < window_minutes:
            return True
    return False


def run_job(key, job):
    # Pool gate: check pool health before heavy jobs
    if job.get('pool_gate'):
        if not pool_gate(job):
            return 0, {'skipped': 'pool_gate'}, 0

    log.info(f"▶ Running: {job['name']} → {job['endpoint']}")
    start = time.time()
    status, data = api_call(job['endpoint'], job['method'], job['timeout'])
    elapsed = round(time.time() - start, 1)

    # Watchdog jobs: log alert level from pool-status response
    if job.get('watchdog') and isinstance(data, dict):
        alert = data.get('alert', {})
        level = alert.get('level', 'unknown')
        reasons = alert.get('reasons', [])
        pool = data.get('pool', {})
        util = pool.get('utilization_pct', '?')
        emoji = {'green': '🟢', 'yellow': '🟡', 'red': '🔴'}.get(level, '⚪')
        log.info(f"  {emoji} POOL WATCHDOG: {level.upper()} — utilization {util}% — {'; '.join(reasons)}")
        leaked = data.get('leaked_connections', [])
        if leaked:
            for lc in leaked:
                log.warning(f"     🔪 Leaked conn {lc.get('conn_id')} held {lc.get('held_seconds')}s by {lc.get('thread')}")
        return status, data, elapsed

    if 200 <= status < 300:
        log.info(f"  ✅ {job['name']} completed in {elapsed}s (HTTP {status})")
        if isinstance(data, dict):
            # Log key result fields (truncated for readability)
            result_str = None
            for k in ('new_articles', 'found', 'added', 'results', 'result', 'size_mb', 'status', 'processed'):
                if k in data:
                    val = data[k]
                    if isinstance(val, (dict, list)):
                        val_str = json.dumps(val)[:200]
                    else:
                        val_str = str(val)
                    log.info(f"     {k}: {val_str}")
    elif status == 0:
        log.error(f"  ❌ {job['name']} — connection failed: {data.get('error','unknown')}")
    elif status in (401, 403):
        log.error(f"  🔒 {job['name']} — auth failed (HTTP {status}). Check DCHUB_ADMIN_KEY")
    elif status == 503:
        log.warning(f"  ⏸️ {job['name']} — service unavailable (HTTP 503)")
    else:
        log.warning(f"  ⚠️ {job['name']} returned HTTP {status} in {elapsed}s")

    return status, data, elapsed


def run_all_due(window_minutes=5):
    now = datetime.now(timezone.utc)
    log.info(f"Checking schedule at {now.strftime('%H:%M UTC')}...")
    ran = 0
    for key, job in JOBS.items():
        if is_job_in_window(job, now, window_minutes):
            run_job(key, job)
            ran += 1
            time.sleep(5)
    if ran == 0:
        log.info("  No jobs due right now.")
    return ran


def check_health():
    log.info("Checking DC Hub health...")
    status, data = api_call('/api/health', method='GET', timeout=10)
    if status == 200:
        fac = data.get('facility_count', data.get('facilities', '?'))
        log.info(f"  ✅ Healthy — {fac} facilities")
    else:
        log.error(f"  ❌ Health check failed (HTTP {status}): {data}")
    return status == 200


def show_status():
    healthy = check_health()
    now = datetime.now(timezone.utc)
    print(f"\n{'─'*65}")
    print(f"  DC Hub External Scheduler v3.9")
    print(f"  Time:   {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  API:    {API_BASE}")
    print(f"  Auth:   {'✅ key set' if ADMIN_KEY else '❌ DCHUB_ADMIN_KEY not set'}")
    print(f"  Health: {'✅ OK' if healthy else '❌ DOWN'}")
    print(f"  Jobs:   {len(JOBS)} active, {len(DISABLED_JOBS)} disabled")
    print(f"{'─'*65}")
    print(f"  {'Job':<30} {'Freq':<12} {'Next Run (UTC)'}")
    print(f"  {'─'*55}")
    for key, job in JOBS.items():
        freq = f"{len(job['hours'])}x/day"
        next_run = None
        for hour in sorted(job['hours']):
            if hour > now.hour or (hour == now.hour and job['minute'] > now.minute):
                next_run = f"{hour:02d}:{job['minute']:02d}"
                break
        if not next_run:
            next_run = f"{sorted(job['hours'])[0]:02d}:{job['minute']:02d} (+1d)"
        print(f"  {job['name']:<30} {freq:<12} {next_run}")
    if DISABLED_JOBS:
        print(f"\n  {'─'*55}")
        print(f"  DISABLED (modules not installed):")
        for key, job in DISABLED_JOBS.items():
            print(f"  ⏸️  {job['name']:<28} {job.get('disabled_reason','')}")
    print(f"{'─'*65}\n")


# ============================================================
# MAIN LOOP — no keep-alive, just scheduled jobs
# ============================================================
def scheduler_loop():
    log.info(f"DC Hub External Scheduler v3.9 starting")
    log.info(f"  API:  {API_BASE}")
    log.info(f"  Jobs: {len(JOBS)} active, {len(DISABLED_JOBS)} disabled")
    log.info(f"  Auth: {'✅ key set' if ADMIN_KEY else '❌ DCHUB_ADMIN_KEY not set — jobs will fail!'}")

    if not ADMIN_KEY:
        log.error("FATAL: DCHUB_ADMIN_KEY not set")

    check_health()

    last_ran = {}

    while True:
        now = datetime.now(timezone.utc)

        for key, job in JOBS.items():
            job_key = f"{key}:{now.strftime('%Y-%m-%d')}:{now.hour}:{job.get('minute',0)}"
            if is_job_in_window(job, now, window_minutes=3) and job_key not in last_ran:
                run_job(key, job)
                last_ran[job_key] = True
                time.sleep(5)

        # Midnight cleanup — purge yesterday's tracking keys
        if now.hour == 0 and now.minute < 2:
            today = now.strftime('%Y-%m-%d')
            last_ran = {k: v for k, v in last_ran.items() if today in k}

        time.sleep(60)


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='DC Hub External Scheduler v3.9')
    parser.add_argument('--once',   action='store_true', help='Run all due jobs once and exit')
    parser.add_argument('--job',    type=str,            help=f'Run specific job: {", ".join(JOBS.keys())}')
    parser.add_argument('--all',    action='store_true', help='Run ALL jobs immediately')
    parser.add_argument('--status', action='store_true', help='Show schedule status')
    parser.add_argument('--health', action='store_true', help='Quick health check')
    args = parser.parse_args()

    if args.status:
        show_status(); return
    if args.health:
        sys.exit(0 if check_health() else 1)
    if args.job:
        # Allow running disabled jobs manually for testing
        all_jobs = {**JOBS, **DISABLED_JOBS}
        if args.job not in all_jobs:
            print(f"Unknown job: {args.job}. Available: {', '.join(all_jobs.keys())}")
            sys.exit(1)
        if args.job in DISABLED_JOBS:
            log.warning(f"Running disabled job '{args.job}' — {DISABLED_JOBS[args.job].get('disabled_reason','')}")
        status, data, _ = run_job(args.job, all_jobs[args.job])
        sys.exit(0 if 200 <= status < 300 else 1)
    if args.all:
        log.info("Running ALL active jobs immediately...")
        check_health()
        for key, job in JOBS.items():
            run_job(key, job)
            time.sleep(10)
        return
    if args.once:
        run_all_due(window_minutes=10); return

    scheduler_loop()


if __name__ == '__main__':
    main()
