#!/usr/bin/env python3
"""
DC Hub External Scheduler v3.2
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
  DCHUB_API_BASE    — API base URL (default: https://dchub-backend-production.up.railway.app)
  DCHUB_ADMIN_KEY   — Admin API key (required)

Schedule (UTC) — verified no overlaps:
  00:00  News/RSS Refresh        (also 04, 08, 12, 16, 20)
  00:20  Auto-Approve            (also 04, 08, 12, 16, 20)
  01:00  Facility Discovery      (also 07, 14, 19)
  03:00  AI Ecosystem Agent      (also 10, 15, 22)
  03:15  Neon DB Backup
  05:00  AI Outreach Agent       (also 13, 21)
  06:00  Global Intelligence     (also 18)
  08:30  Evolution Engine        (also 20:30)
  09:15  Auto-Pilot (Deals)      (also 21:15)
  11:30  Content Publishing
  Keep-Alive every 5 minutes
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s UTC [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('dchub-scheduler')

# ============================================================
# JOB DEFINITIONS
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
        'hours': [1, 7, 14, 19],        # was [1,7,13,19] — 13→14 avoids outreach
        'minute': 0,
        'timeout': 180,
    },
    'auto_approve': {
        'name': 'Auto-Approve',
        'endpoint': '/api/jobs/auto-approve',
        'method': 'POST',
        'hours': [0, 4, 8, 12, 16, 20],
        'minute': 20,                   # was :15 — pushed to :20 for breathing room
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
        'hours': [3, 10, 15, 22],       # was [3,9,15,21] — 9→10, 21→22
        'minute': 0,                    # was :30
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
        'minute': 30,                   # was :00 — pushed to :30 after news+auto_approve
        'timeout': 120,
    },
    'autopilot': {
        'name': 'Auto-Pilot (Deals)',
        'endpoint': '/api/jobs/autopilot',
        'method': 'POST',
        'hours': [9, 21],               # 90min after news refresh
        'minute': 15,                   # :15 — clears outreach at 21:00
        'timeout': 300,
    },
    'content': {
        'name': 'Content Publishing',
        'endpoint': '/api/jobs/content-publish',
        'method': 'POST',
        'hours': [11],
        'minute': 30,                   # was :00
        'timeout': 120,
    },
    'backup': {
        'name': 'Neon DB Backup',
        'endpoint': '/api/jobs/backup',
        'method': 'POST',
        'hours': [3],
        'minute': 15,                   # was :00 — pushed to :15 after ecosystem
        'timeout': 600,
    },
    'keepalive': {
        'name': 'Keep-Alive',
        'endpoint': '/api/jobs/keep-alive',
        'method': 'POST',
        'hours': list(range(24)),
        'minute': None,                 # special: runs every 5 minutes
        'timeout': 15,
    },
}

# ============================================================
# HTTP HELPER
# ============================================================
def api_call(endpoint, method='POST', timeout=60):
    url = API_BASE.rstrip('/') + endpoint
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'DCHub-Scheduler/3.2',
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
    log.info(f"▶ Running: {job['name']} → {job['endpoint']}")
    start = time.time()
    status, data = api_call(job['endpoint'], job['method'], job['timeout'])
    elapsed = round(time.time() - start, 1)

    if 200 <= status < 300:
        log.info(f"  ✅ {job['name']} completed in {elapsed}s (HTTP {status})")
        if isinstance(data, dict):
            for k in ('new_articles', 'found', 'added', 'results', 'result', 'size_mb'):
                if k in data:
                    log.info(f"     {k}: {data[k]}")
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
        if key == 'keepalive':
            continue
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
        log.info(f"  ✅ Healthy — {data.get('facility_count','?')} facilities")
    else:
        log.error(f"  ❌ Health check failed (HTTP {status}): {data}")
    return status == 200


def show_status():
    healthy = check_health()
    now = datetime.now(timezone.utc)
    print(f"\n{'─'*65}")
    print(f"  DC Hub External Scheduler v3.2")
    print(f"  Time:   {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  API:    {API_BASE}")
    print(f"  Auth:   {'✅ key set' if ADMIN_KEY else '❌ DCHUB_ADMIN_KEY not set'}")
    print(f"  Health: {'✅ OK' if healthy else '❌ DOWN'}")
    print(f"{'─'*65}")
    print(f"  {'Job':<25} {'Next Run (UTC)'}")
    print(f"  {'─'*40}")
    for key, job in JOBS.items():
        if key == 'keepalive':
            print(f"  {job['name']:<25} every 5 min")
            continue
        next_run = None
        for hour in sorted(job['hours']):
            if hour > now.hour or (hour == now.hour and job['minute'] > now.minute):
                next_run = f"{hour:02d}:{job['minute']:02d}"
                break
        if not next_run:
            next_run = f"{sorted(job['hours'])[0]:02d}:{job['minute']:02d} (+1d)"
        print(f"  {job['name']:<25} {next_run}")
    print(f"{'─'*65}\n")


# ============================================================
# MAIN LOOP
# ============================================================
def scheduler_loop():
    log.info(f"DC Hub External Scheduler v3.2 starting")
    log.info(f"  API:  {API_BASE}")
    log.info(f"  Jobs: {len(JOBS)} ({len(JOBS)-1} scheduled + keepalive)")
    log.info(f"  Auth: {'✅ key set' if ADMIN_KEY else '❌ DCHUB_ADMIN_KEY not set — jobs will fail!'}")

    if not ADMIN_KEY:
        log.error("FATAL: DCHUB_ADMIN_KEY not set")

    check_health()

    last_ran = {}
    keepalive_counter = 0

    while True:
        now = datetime.now(timezone.utc)

        if keepalive_counter % 5 == 0:
            run_job('keepalive', JOBS['keepalive'])
        keepalive_counter += 1

        for key, job in JOBS.items():
            if key == 'keepalive':
                continue
            job_key = f"{key}:{now.strftime('%Y-%m-%d')}:{now.hour}:{job.get('minute',0)}"
            if is_job_in_window(job, now, window_minutes=3) and job_key not in last_ran:
                run_job(key, job)
                last_ran[job_key] = True
                time.sleep(5)

        if now.hour == 0 and now.minute < 2:
            today = now.strftime('%Y-%m-%d')
            last_ran = {k: v for k, v in last_ran.items() if today in k}

        time.sleep(60)


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='DC Hub External Scheduler v3.2')
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
        if args.job not in JOBS:
            print(f"Unknown job: {args.job}. Available: {', '.join(JOBS.keys())}")
            sys.exit(1)
        status, data, _ = run_job(args.job, JOBS[args.job])
        sys.exit(0 if 200 <= status < 300 else 1)
    if args.all:
        log.info("Running ALL jobs immediately...")
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
