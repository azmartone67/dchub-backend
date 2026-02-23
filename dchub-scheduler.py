#!/usr/bin/env python3
"""
DC Hub External Scheduler v3.0
===============================
Triggers discovery jobs via HTTP POST to the DC Hub API /api/jobs/* endpoints.
Run this anywhere: local machine, Railway, GitHub Actions, or cron.

Usage:
  python3 dchub-scheduler.py              # Run the full scheduler loop
  python3 dchub-scheduler.py --once       # Run all due jobs once and exit
  python3 dchub-scheduler.py --job news   # Run a specific job and exit
  python3 dchub-scheduler.py --all        # Run ALL jobs immediately
  python3 dchub-scheduler.py --status     # Check health + job status

Environment:
  DCHUB_API_BASE    — API base URL (default: https://dchub-backend-production.up.railway.app)
  DCHUB_ADMIN_KEY   — Admin API key for authenticated endpoints (required)

Schedule (all times UTC):
  News/RSS Refresh     Every 4 hours     (0, 4, 8, 12, 16, 20)
  Facility Discovery   Every 6 hours     (1, 7, 13, 19)
  Auto-Approve         Every 4 hours     (0, 4, 8, 12, 16, 20) :15
  Global Intelligence  Twice daily       (6, 18)
  AI Ecosystem Agent   Every 6 hours     (3, 9, 15, 21)
  AI Outreach Agent    Every 8 hours     (5, 13, 21)
  Evolution Engine     Twice daily       (8, 20)
  Content Publishing   Daily             (11)
  Keep-Alive           Every 5 minutes   (continuous)
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

# ============================================================
# CONFIG
# ============================================================
API_BASE = os.environ.get('DCHUB_API_BASE', 'https://dchub-backend-production.up.railway.app')
ADMIN_KEY = os.environ.get('DCHUB_ADMIN_KEY', '')

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s UTC [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('dchub-scheduler')

# ============================================================
# JOB DEFINITIONS — maps to /api/jobs/* endpoints
# ============================================================
JOBS = {
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
        'hours': [1, 7, 13, 19],
        'minute': 0,
        'timeout': 180,
    },
    'auto_approve': {
        'name': 'Auto-Approve',
        'endpoint': '/api/jobs/auto-approve',
        'method': 'POST',
        'hours': [0, 4, 8, 12, 16, 20],
        'minute': 15,
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
        'hours': [3, 9, 15, 21],
        'minute': 30,
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
        'minute': 0,
        'timeout': 120,
    },
    'content': {
        'name': 'Content Publishing',
        'endpoint': '/api/jobs/content-publish',
        'method': 'POST',
        'hours': [11],
        'minute': 0,
        'timeout': 120,
    },
    'keepalive': {
        'name': 'Keep-Alive',
        'endpoint': '/api/jobs/keep-alive',
        'method': 'POST',
        'hours': list(range(24)),  # every hour
        'minute': None,            # special: runs every 5 minutes
        'timeout': 15,
    },
}

# ============================================================
# HTTP HELPER
# ============================================================
def api_call(endpoint, method='POST', timeout=60):
    """Make an HTTP request to the DC Hub API."""
    url = API_BASE.rstrip('/') + endpoint
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'DCHub-Scheduler/3.0',
    }
    if ADMIN_KEY:
        headers['X-Admin-Key'] = ADMIN_KEY
        headers['Authorization'] = f'Bearer {ADMIN_KEY}'

    try:
        req = Request(url, method=method, headers=headers)
        if method == 'POST':
            req.data = b'{}'

        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode('utf-8')
            status = resp.status
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = {'raw': body[:500]}
            return status, data
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
    """Check if current time is within window of the job's scheduled time."""
    if now is None:
        now = datetime.now(timezone.utc)

    # Keep-alive runs every 5 minutes — always in window
    if job.get('minute') is None:
        return True

    for hour in job['hours']:
        scheduled_minute = job['minute']
        diff = (now.hour - hour) * 60 + (now.minute - scheduled_minute)
        if 0 <= diff < window_minutes:
            return True
    return False


def run_job(key, job):
    """Execute a single job."""
    log.info(f"▶ Running: {job['name']} → {job['endpoint']}")
    start = time.time()
    status, data = api_call(job['endpoint'], job['method'], job['timeout'])
    elapsed = round(time.time() - start, 1)

    if 200 <= status < 300:
        log.info(f"  ✅ {job['name']} completed in {elapsed}s (HTTP {status})")
        if isinstance(data, dict):
            # Log key result fields
            for k in ('new_articles', 'found', 'added', 'result'):
                if k in data:
                    log.info(f"     {k}: {data[k]}")
    elif status == 0:
        log.error(f"  ❌ {job['name']} — connection failed: {data.get('error', 'unknown')}")
    elif status == 401:
        log.error(f"  🔒 {job['name']} — authentication failed (HTTP 401). Check DCHUB_ADMIN_KEY")
    elif status == 503:
        log.warning(f"  ⏸️ {job['name']} — service unavailable (HTTP 503)")
    else:
        log.warning(f"  ⚠️ {job['name']} returned HTTP {status} in {elapsed}s")

    return status, data, elapsed


def run_all_due(window_minutes=5):
    """Run all jobs that are currently due (within window)."""
    now = datetime.now(timezone.utc)
    log.info(f"Checking schedule at {now.strftime('%H:%M UTC')}...")

    ran = 0
    for key, job in JOBS.items():
        if key == 'keepalive':
            continue  # Skip keep-alive in batch mode
        if is_job_in_window(job, now, window_minutes):
            run_job(key, job)
            ran += 1
            time.sleep(5)

    if ran == 0:
        log.info("  No jobs due right now.")
    return ran


def check_health():
    """Check DC Hub API health."""
    log.info("Checking DC Hub health...")
    status, data = api_call('/api/health', method='GET', timeout=10)
    if status == 200:
        log.info(f"  ✅ Healthy — {data.get('facility_count', '?')} facilities, "
                 f"{data.get('news_count', '?')} news articles")
    else:
        log.error(f"  ❌ Health check failed (HTTP {status}): {data}")
    return status == 200


def show_status():
    """Show health + next scheduled run for each job."""
    healthy = check_health()
    now = datetime.now(timezone.utc)

    print(f"\n{'─' * 65}")
    print(f"  DC Hub External Scheduler v3.0 Status")
    print(f"  Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  API:  {API_BASE}")
    print(f"  Auth: {'✅ key set' if ADMIN_KEY else '❌ DCHUB_ADMIN_KEY not set'}")
    print(f"  Health: {'✅ OK' if healthy else '❌ DOWN'}")
    print(f"{'─' * 65}")
    print(f"  {'Job':<25} {'Endpoint':<30} {'Next Run (UTC)'}")
    print(f"  {'─' * 62}")

    for key, job in JOBS.items():
        if key == 'keepalive':
            next_run = "every 5 min"
        else:
            next_run = None
            for hour in sorted(job['hours']):
                if hour > now.hour or (hour == now.hour and job['minute'] > now.minute):
                    next_run = f"{hour:02d}:{job['minute']:02d}"
                    break
            if not next_run:
                next_run = f"{sorted(job['hours'])[0]:02d}:{job['minute']:02d} (+1d)"

        print(f"  {job['name']:<25} {job['endpoint']:<30} {next_run}")

    print(f"{'─' * 65}\n")


# ============================================================
# MAIN LOOP
# ============================================================
def scheduler_loop():
    """Main scheduler loop — checks every 60s, keep-alive every 5 min."""
    log.info(f"DC Hub External Scheduler v3.0 starting")
    log.info(f"  API:  {API_BASE}")
    log.info(f"  Jobs: {len(JOBS)}")
    log.info(f"  Auth: {'✅ key set' if ADMIN_KEY else '❌ DCHUB_ADMIN_KEY not set'}")

    if not ADMIN_KEY:
        log.error("FATAL: DCHUB_ADMIN_KEY not set — all jobs will fail auth")

    if not check_health():
        log.warning("DC Hub API is not healthy — scheduler will continue but jobs may fail")

    # Track what we've run to avoid double-triggers
    last_ran = {}
    keepalive_counter = 0

    while True:
        now = datetime.now(timezone.utc)

        # Keep-alive every 5 minutes (runs on every 5th loop iteration)
        if keepalive_counter % 5 == 0:
            job = JOBS['keepalive']
            run_job('keepalive', job)

        keepalive_counter += 1

        # Check scheduled jobs
        for key, job in JOBS.items():
            if key == 'keepalive':
                continue

            # Create unique key per job per scheduled hour
            job_key = f"{key}:{now.strftime('%Y-%m-%d')}:{now.hour}"
            if is_job_in_window(job, now, window_minutes=3) and job_key not in last_ran:
                run_job(key, job)
                last_ran[job_key] = True
                time.sleep(5)

        # Clean old tracking entries at midnight
        if now.hour == 0 and now.minute < 2:
            old_keys = [k for k in last_ran if not k.endswith(f":{now.strftime('%Y-%m-%d')}:{now.hour}")]
            for k in old_keys:
                del last_ran[k]

        time.sleep(60)


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='DC Hub External Scheduler v3.0')
    parser.add_argument('--once', action='store_true', help='Run all due jobs once and exit')
    parser.add_argument('--job', type=str, help=f'Run a specific job: {", ".join(JOBS.keys())}')
    parser.add_argument('--all', action='store_true', help='Run ALL jobs immediately')
    parser.add_argument('--status', action='store_true', help='Show health and schedule status')
    parser.add_argument('--health', action='store_true', help='Quick health check')
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    if args.health:
        healthy = check_health()
        sys.exit(0 if healthy else 1)

    if args.job:
        if args.job not in JOBS:
            print(f"Unknown job: {args.job}")
            print(f"Available: {', '.join(JOBS.keys())}")
            sys.exit(1)
        status, data, elapsed = run_job(args.job, JOBS[args.job])
        sys.exit(0 if 200 <= status < 300 else 1)

    if args.all:
        log.info("Running ALL jobs immediately...")
        check_health()
        for key, job in JOBS.items():
            run_job(key, job)
            time.sleep(10)
        log.info("All jobs completed.")
        return

    if args.once:
        run_all_due(window_minutes=10)
        return

    # Default: run the loop
    scheduler_loop()


if __name__ == '__main__':
    main()
