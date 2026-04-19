"""
Staggered Discovery Scheduler
==============================
Lightweight scheduler that triggers discovery tasks at staggered intervals.
Each task runs as a one-shot HTTP call to the local API, keeping memory low.

Schedule (all times UTC, with minute-level precision):
  - News/RSS refresh:      every 4 hours  (02:00, 06:00, 10:00, 14:00, 18:00, 22:00)
  - API Auto-Discovery:    daily at 03:00
  - Global Intelligence:   2x/day at 07:00 and 19:00
  - AI Ecosystem Agent:    daily at 09:30
  - Evolution Engine:      daily at 12:00
  - AI Outreach Agent:     daily at 14:30
  - Enhanced Promotion:    daily at 17:00
"""

import threading
import time
import logging
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_BASE_URL = "http://localhost:5000"
_INTERNAL_KEY = "dchub-internal-sync-2026"
_scheduler_thread = None
_running = False

SCHEDULE = [
    {
        "name": "News/RSS Refresh",
        "endpoint": "/api/news/refresh",
        "method": "POST",
        "run_at": [(2, 0), (6, 0), (10, 0), (14, 0), (18, 0), (22, 0)],
    },
    {
        "name": "API Auto-Discovery",
        "endpoint": "/api/discovery/run",
        "method": "POST",
        "run_at": [(3, 0)],
    },
    {
        "name": "Global Intelligence",
        "endpoint": "/api/facilities/refresh",
        "method": "POST",
        "run_at": [(7, 0), (19, 0)],
    },
    {
        "name": "AI Ecosystem Agent",
        "endpoint": "/api/ecosystem/search?query=data+center&limit=5",
        "method": "GET",
        "run_at": [(9, 30)],
    },
    {
        "name": "Evolution Engine",
        "endpoint": "/api/evolution/run",
        "method": "POST",
        "run_at": [(12, 0)],
    },
    {
        "name": "AI Outreach Agent",
        "endpoint": "/api/outreach/run",
        "method": "POST",
        "run_at": [(14, 30)],
    },
    {
        "name": "Enhanced Promotion",
        "endpoint": "/api/promotion/run",
        "method": "POST",
        "run_at": [(17, 0)],
    },
]

_last_run = {}


def _should_run(task, now_utc):
    """Check if a task should run based on current hour:minute."""
    current_hm = (now_utc.hour, now_utc.minute)
    task_name = task["name"]

    matched = False
    for run_hour, run_minute in task["run_at"]:
        if current_hm[0] == run_hour and abs(current_hm[1] - run_minute) <= 2:
            matched = True
            break

    if not matched:
        return False

    last = _last_run.get(task_name)
    if last:
        elapsed = (now_utc - last).total_seconds()
        if elapsed < 3500:
            return False

    return True


def _run_task(task):
    """Execute a single discovery task via HTTP."""
    try:
        headers = {"X-Internal-Key": _INTERNAL_KEY}
        url = f"{_BASE_URL}{task['endpoint']}"

        if task["method"] == "POST":
            resp = requests.post(url, headers=headers, timeout=120)
        else:
            resp = requests.get(url, headers=headers, timeout=120)

        _last_run[task["name"]] = datetime.now(timezone.utc)

        if resp.status_code < 400:
            logger.info(f"SCHEDULER: {task['name']} completed ({resp.status_code})")
        else:
            logger.warning(f"SCHEDULER: {task['name']} returned {resp.status_code}")

    except requests.exceptions.Timeout:
        logger.warning(f"SCHEDULER: {task['name']} timed out (120s)")
    except Exception as e:
        logger.warning(f"SCHEDULER: {task['name']} failed: {e}")


def _scheduler_loop():
    """Main scheduler loop — checks every 60 seconds."""
    global _running
    logger.info("SCHEDULER: Staggered discovery scheduler started (checking every 60s)")

    time.sleep(120)

    while _running:
        now_utc = datetime.now(timezone.utc)

        for task in SCHEDULE:
            if _should_run(task, now_utc):
                logger.info(f"SCHEDULER: Triggering {task['name']}")
                try:
                    _run_task(task)
                except Exception as e:
                    logger.error(f"SCHEDULER: Error running {task['name']}: {e}")
                time.sleep(30)

        for _ in range(12):
            if not _running:
                break
            time.sleep(5)


def start_scheduled_discovery():
    """Start the background scheduler thread."""
    global _scheduler_thread, _running

    if _running:
        logger.info("SCHEDULER: Already running")
        return

    _running = True
    _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True, name="DiscoveryScheduler")
    _scheduler_thread.start()
    logger.info("SCHEDULER: Background discovery scheduler started")


def stop_scheduled_discovery():
    """Stop the scheduler."""
    global _running
    _running = False
    logger.info("SCHEDULER: Stopped")


def get_scheduler_status():
    """Return current scheduler status."""
    now = datetime.now(timezone.utc)
    tasks_info = []
    for t in SCHEDULE:
        run_times = [f"{h:02d}:{m:02d}" for h, m in t["run_at"]]
        last = _last_run.get(t["name"])
        tasks_info.append({
            "name": t["name"],
            "endpoint": t["endpoint"],
            "run_at_utc": run_times,
            "last_run": last.isoformat() if last else "never",
        })
    return {
        "running": _running,
        "current_utc": now.strftime("%H:%M"),
        "tasks": tasks_info,
    }
