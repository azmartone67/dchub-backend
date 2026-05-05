
# ============================================================================
# Phase 32D — MANUAL INTEGRATION NEEDED
# ============================================================================
# When building post text, append the daily landing URL so LinkedIn renders
# the rich card preview:
#
#     text += "\n\n" + _phase30c_landing_url()  # phase32_landing_appended
#
# Place this RIGHT BEFORE the call that publishes the post (e.g.
# linkedin.publish(text=text), client.share(message=text), etc.)
# ============================================================================
"""
DC Hub LinkedIn Poster — Cron Scheduler
========================================
Runs the LinkedIn poster on a daily schedule.
Add this to your Replit project alongside linkedin_poster.py.

Option 1: Import into your existing main.py Flask app
Option 2: Run standalone as a background worker

For Replit, use the "Always On" or "Scheduled" deployment type.
"""

import os
import time
import threading
import logging
from datetime import datetime, timezone

# Phase 30C — daily landing URL (LinkedIn renders rich card from this URL's OG)
def _phase30c_landing_url(d=None):
    import datetime
    if d is None:
        d = datetime.date.today()
    return f"https://dchub.cloud/api/v1/social/posts/{d.isoformat()}"  # phase31_canonical_url

log = logging.getLogger('linkedin-scheduler')

POST_HOUR = int(os.environ.get('POST_HOUR', '14'))  # UTC (14 UTC = 7am MST)
POST_MINUTE = int(os.environ.get('POST_MINUTE', '0'))


def scheduler_loop():
    """Background thread that checks every 5 minutes if it's time to post."""
    posted_today = False
    last_post_date = None

    log.info(f"LinkedIn scheduler started. Will post daily at {POST_HOUR:02d}:{POST_MINUTE:02d} UTC")

    while True:
        now = datetime.now(timezone.utc)
        today = now.date()

        # Reset daily flag
        if last_post_date != today:
            posted_today = False

        # Check if it's time to post
        if (not posted_today
                and now.hour == POST_HOUR
                and now.minute >= POST_MINUTE
                and now.minute < POST_MINUTE + 10):  # 10-minute window

            log.info(f"Posting time reached ({now.strftime('%H:%M UTC')}). Running poster...")

            try:
                from linkedin_poster import run
                success = run()
                posted_today = True
                last_post_date = today
                if success:
                    log.info(f"Daily post completed successfully for {today}")
                else:
                    log.error(f"Daily post failed for {today}")
            except Exception as e:
                log.error(f"Scheduler error: {e}")
                posted_today = True  # Don't retry on error today
                last_post_date = today

        # Sleep 5 minutes between checks
        time.sleep(300)


def start_scheduler():
    """Start the scheduler as a background daemon thread."""
    thread = threading.Thread(target=scheduler_loop, daemon=True)
    thread.start()
    log.info("LinkedIn poster scheduler running in background")
    return thread


# ============================================================
# INTEGRATION WITH EXISTING FLASK APP
# ============================================================
def integrate_with_flask(app):
    """
    Call this from your main Flask app to:
    1. Register LinkedIn API routes (/api/linkedin/*)
    2. Start the background scheduler

    Usage in your main.py:
        from linkedin_scheduler import integrate_with_flask
        integrate_with_flask(app)
    """
    from linkedin_poster import register_linkedin_routes
    register_linkedin_routes(app)
    start_scheduler()
    log.info("LinkedIn poster integrated with Flask app")


# ============================================================
# STANDALONE MODE
# ============================================================
if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s'
    )
    log.info("Starting LinkedIn poster in standalone mode")
    start_scheduler()

    # Keep alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Scheduler stopped")
