#!/usr/bin/env python3
"""
DC Hub Discovery Scheduler — Standalone Cron Runner
====================================================
Triggers discovery jobs via HTTP POST to the DC Hub API.
Run this anywhere: local machine, Railway, GitHub Actions, or cron.

Usage:
  python3 dchub-scheduler.py              # Run the full scheduler loop
  python3 dchub-scheduler.py --once       # Run all due jobs once and exit
  python3 dchub-scheduler.py --job news   # Run a specific job and exit
  python3 dchub-scheduler.py --status     # Check health + job status

Environment:
  DCHUB_API_BASE    — API base URL (default: https://dchub.cloud)
  DCHUB_ADMIN_KEY   — Admin API key for authenticated endpoints (optional)

Schedule (all times UTC):
  News/RSS Refresh     Every 4 hours     (2am, 6am, 10am, 2pm, 6pm, 10pm)
  API Auto-Discovery   Daily 3:00am
  Global Intelligence  Twice daily       (7:00am, 7:00pm)
  AI Ecosystem Agent   Daily 9:30am
  Evolution Engine     Daily 12:00pm
  AI Outreach Agent    Daily 2:30pm
  Enhanced Promotion   Daily 5:00pm
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
API_BASE = os.environ.get('DCHUB_API_BASE', 'https://dchub.cloud')
ADMIN_KEY = os.environ.get('DCHUB_ADMIN_KEY', '')

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S UTC'
)
log = logging.getLogger('dchub-scheduler')

# ============================================================
# JOB DEFINITIONS
# ============================================================
JOBS = {
    'news': {
        'name': 'News/RSS Refresh',
        'endpoint': '/api/news/refresh',
        'method': 'POST',
        'schedule': 'every_4h',
        'hours': [2, 6, 10, 14, 18, 22],
        'minute': 0,
        'timeout': 300,
    },
    'api_discovery': {
        'name': 'API Auto-Discovery',
        'endpoint': '/api/discovery/run',
        'method': 'POST',
        'schedule': 'daily',
        'hours': [3],
        'minute': 0,
        'timeout': 120,
    },
    'global_intel': {
        'name': 'Global Intelligence',
        'endpoint': '/api/infrastructure/sync',
        'method': 'POST',
        'schedule': 'twice_daily',
        'hours': [7, 19],
        'minute': 0,
        'timeout': 180,
    },
    'ecosystem': {
        'name': 'AI Ecosystem Agent',
        'endpoint': '/api/ai-ecosystem/run',
        'method': 'POST',
        'schedule': 'daily',
        'hours': [9],
        'minute': 30,
        'timeout': 120,
    },
    'evolution': {
        'name': 'Evolution Engine',
        'endpoint': '/api/evolution/run',
        'method': 'POST',
        'schedule': 'daily',
        'hours': [12],
        'minute': 0,
        'timeout': 120,
    },
    'outreach': {
        'name': 'AI Outreach Agent',
        'endpoint': '/api/outreach/run',
        'method': 'POST',
        'schedule': 'daily',
        'hours': [14],
        'minute': 30,
        'timeout': 120,
    },
    'promotion': {
        'name': 'Enhanced Promotion',
        'endpoint': '/api/promotion/run',
        'method': 'POST',
        'schedule': 'daily',
        'hours': [17],
        'minute': 0,
        'timeout': 120,
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
        'User-Agent': 'DCHub-Scheduler/1.0',
    }
    if ADMIN_KEY:
        headers['Authorization'] = f'Bearer {ADMIN_KEY}'
        headers['X-API-Key'] = ADMIN_KEY

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
def is_job_due(job, now=None):
    """Check if a job should run at the current time."""
    if now is None:
        now = datetime.now(timezone.utc)
    return now.hour in job['hours'] and now.minute == job['minute']

def is_job_in_window(job, now=None, window_minutes=5):
    """Check if current time is within window_minutes of the job's scheduled time."""
    if now is None:
        now = datetime.now(timezone.utc)
    for hour in job['hours']:
        scheduled_minute = job['minute']
        diff = (now.hour - hour) * 60 + (now.minute - scheduled_minute)
        if 0 <= diff < window_minutes:
            return True
    return False

def run_job(key, job):
    """Execute a single discovery job."""
    log.info(f"▶ Running: {job['name']} → {job['endpoint']}")
    start = time.time()
    status, data = api_call(job['endpoint'], job['method'], job['timeout'])
    elapsed = round(time.time() - start, 1)
    
    if 200 <= status < 300:
        log.info(f"  ✅ {job['name']} completed in {elapsed}s (HTTP {status})")
    elif status == 0:
        log.error(f"  ❌ {job['name']} — connection failed: {data.get('error', 'unknown')}")
    else:
        log.warning(f"  ⚠️ {job['name']} returned HTTP {status} in {elapsed}s")
    
    return status, data, elapsed

def run_all_due(window_minutes=5):
    """Run all jobs that are currently due (within window)."""
    now = datetime.now(timezone.utc)
    log.info(f"Checking schedule at {now.strftime('%H:%M UTC')}...")
    
    ran = 0
    for key, job in JOBS.items():
        if is_job_in_window(job, now, window_minutes):
            run_job(key, job)
            ran += 1
            time.sleep(5)  # Small gap between jobs
    
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
    
    print(f"\n{'─' * 60}")
    print(f"  DC Hub Discovery Scheduler Status")
    print(f"  Time: {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  API:  {API_BASE}")
    print(f"  Health: {'✅ OK' if healthy else '❌ DOWN'}")
    print(f"{'─' * 60}")
    print(f"  {'Job':<25} {'Schedule':<20} {'Next Run (UTC)'}")
    print(f"  {'─' * 55}")
    
    for key, job in JOBS.items():
        # Find next run time
        for hour in sorted(job['hours']):
            if hour > now.hour or (hour == now.hour and job['minute'] > now.minute):
                next_run = f"{hour:02d}:{job['minute']:02d}"
                break
        else:
            next_run = f"{sorted(job['hours'])[0]:02d}:{job['minute']:02d} (+1d)"
        
        schedule_label = {
            'every_4h': 'Every 4 hours',
            'twice_daily': 'Twice daily',
            'daily': 'Daily',
        }.get(job['schedule'], job['schedule'])
        
        print(f"  {job['name']:<25} {schedule_label:<20} {next_run}")
    
    print(f"{'─' * 60}\n")

# ============================================================
# MAIN LOOP
# ============================================================
def scheduler_loop():
    """Main scheduler loop — checks every minute, runs due jobs."""
    log.info(f"DC Hub Discovery Scheduler starting")
    log.info(f"  API: {API_BASE}")
    log.info(f"  Jobs: {len(JOBS)}")
    log.info(f"  Admin key: {'set' if ADMIN_KEY else 'not set'}")
    
    if not check_health():
        log.warning("DC Hub API is not healthy — scheduler will continue but jobs may fail")
    
    # Track what we've run to avoid double-triggers
    last_ran = {}
    
    while True:
        now = datetime.now(timezone.utc)
        current_key = now.strftime('%Y-%m-%d-%H-%M')
        
        for key, job in JOBS.items():
            job_key = f"{key}:{now.strftime('%Y-%m-%d')}:{now.hour}"
            if is_job_in_window(job, now, window_minutes=2) and job_key not in last_ran:
                run_job(key, job)
                last_ran[job_key] = True
                time.sleep(5)
        
        # Clean old tracking entries daily
        if now.hour == 0 and now.minute == 0:
            last_ran.clear()
        
        time.sleep(60)

# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='DC Hub Discovery Scheduler')
    parser.add_argument('--once', action='store_true', help='Run all due jobs once and exit')
    parser.add_argument('--job', type=str, help='Run a specific job by key (news, api_discovery, global_intel, ecosystem, evolution, outreach, promotion)')
    parser.add_argument('--all', action='store_true', help='Run ALL jobs immediately regardless of schedule')
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
            time.sleep(10)  # 10s gap between jobs
        log.info("All jobs completed.")
        return
    
    if args.once:
        run_all_due(window_minutes=10)
        return
    
    # Default: run the loop
    scheduler_loop()

if __name__ == '__main__':
    main()
