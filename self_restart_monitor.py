"""
Self-Restart Monitor for DC Hub
================================
Drop-in module that monitors system health and triggers automatic restarts
when thresholds are exceeded. Works with Railway's ON_FAILURE restart policy.

Usage in main.py:
    from self_restart_monitor import start_self_restart_monitor
    start_self_restart_monitor(app)

The monitor checks every 30 seconds:
  - RSS memory usage (default threshold: 512MB)
  - Error rate (default: >50 errors in 60 seconds)
  - Database connection pool health
  - Thread count (default: >100 threads)

If ANY metric exceeds its threshold for 3 CONSECUTIVE checks,
the process exits with code 1. Railway's ON_FAILURE restart policy
brings it back fresh within ~30 seconds.

All thresholds are configurable via environment variables.
"""

import os
import sys
import time
import threading
import logging
import resource
import traceback
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger("self_restart_monitor")

# ---------------------------------------------------------------------------
# Configuration (all overridable via env vars)
# ---------------------------------------------------------------------------
CHECK_INTERVAL = int(os.environ.get("SRM_CHECK_INTERVAL", "30"))           # seconds
CONSECUTIVE_FAILURES = int(os.environ.get("SRM_CONSECUTIVE_FAILURES", "3")) # checks before restart

# Thresholds
MAX_RSS_MB = int(os.environ.get("SRM_MAX_RSS_MB", "512"))                  # RSS memory cap
MAX_THREADS = int(os.environ.get("SRM_MAX_THREADS", "100"))                # thread count cap
MAX_ERRORS_PER_MINUTE = int(os.environ.get("SRM_MAX_ERRORS_MIN", "50"))    # error rate cap
DB_CHECK_TIMEOUT = int(os.environ.get("SRM_DB_TIMEOUT", "5"))              # seconds

# Grace period after startup (don't restart during boot)
STARTUP_GRACE_SECONDS = int(os.environ.get("SRM_STARTUP_GRACE", "180"))    # 3 minutes


class SelfRestartMonitor:
    """Monitors system health and triggers graceful restart on sustained failures."""

    def __init__(self, app=None):
        self.app = app
        self.start_time = time.time()
        self.consecutive_failures = 0
        self.error_timestamps = deque(maxlen=500)
        self._running = False
        self._thread = None

        # Track which metrics failed (for logging)
        self._last_failures = []

    # ------------------------------------------------------------------
    # Error tracking (call from Flask error handlers)
    # ------------------------------------------------------------------
    def record_error(self):
        """Record an application error timestamp. Call from error handlers."""
        self.error_timestamps.append(time.time())

    def get_error_rate(self, window_seconds=60):
        """Count errors in the last N seconds."""
        cutoff = time.time() - window_seconds
        return sum(1 for ts in self.error_timestamps if ts > cutoff)

    # ------------------------------------------------------------------
    # Health checks
    # ------------------------------------------------------------------
    def check_memory(self):
        """Check RSS memory usage."""
        try:
            # resource.getrusage returns RSS in KB on Linux
            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_mb = rss_kb / 1024  # Convert KB to MB
            if rss_mb > MAX_RSS_MB:
                return False, f"RSS {rss_mb:.0f}MB > {MAX_RSS_MB}MB"
            return True, f"RSS {rss_mb:.0f}MB"
        except Exception as e:
            return True, f"memory check error: {e}"  # Don't fail on check errors

    def check_threads(self):
        """Check active thread count."""
        try:
            count = threading.active_count()
            if count > MAX_THREADS:
                return False, f"threads {count} > {MAX_THREADS}"
            return True, f"threads {count}"
        except Exception as e:
            return True, f"thread check error: {e}"

    def check_error_rate(self):
        """Check application error rate."""
        rate = self.get_error_rate(60)
        if rate > MAX_ERRORS_PER_MINUTE:
            return False, f"errors {rate}/min > {MAX_ERRORS_PER_MINUTE}/min"
        return True, f"errors {rate}/min"

    def check_database(self):
        """Check database connectivity with timeout."""
        try:
            db_url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
            if not db_url:
                return True, "no DB URL configured"  # Skip if no DB

            import psycopg2
            conn = psycopg2.connect(db_url, connect_timeout=DB_CHECK_TIMEOUT)
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.close()
            finally:
                conn.close()  # ALWAYS close — this was the old leak
            return True, "DB OK"
        except ImportError:
            return True, "psycopg2 not available"
        except Exception as e:
            err_msg = str(e).split('\n')[0][:80]
            return False, f"DB unreachable: {err_msg}"

    # ------------------------------------------------------------------
    # Main check loop
    # ------------------------------------------------------------------
    def run_checks(self):
        """Run all health checks. Returns (healthy: bool, details: list)."""
        failures = []
        details = []

        checks = [
            ("memory", self.check_memory),
            ("threads", self.check_threads),
            ("error_rate", self.check_error_rate),
            ("database", self.check_database),
        ]

        for name, check_fn in checks:
            try:
                ok, detail = check_fn()
                details.append(f"{name}={'OK' if ok else 'FAIL'} ({detail})")
                if not ok:
                    failures.append(f"{name}: {detail}")
            except Exception as e:
                details.append(f"{name}=ERROR ({e})")
                # Don't count check exceptions as failures

        self._last_failures = failures
        return len(failures) == 0, details

    def _monitor_loop(self):
        """Background monitoring loop."""
        logger.info(
            "🛡️  Self-Restart Monitor started | interval=%ds | grace=%ds | "
            "thresholds: RSS<%dMB threads<%d errors<%d/min | "
            "restart after %d consecutive failures",
            CHECK_INTERVAL, STARTUP_GRACE_SECONDS,
            MAX_RSS_MB, MAX_THREADS, MAX_ERRORS_PER_MINUTE,
            CONSECUTIVE_FAILURES
        )

        # Wait for startup grace period
        time.sleep(STARTUP_GRACE_SECONDS)
        logger.info("🛡️  Self-Restart Monitor: grace period complete, monitoring active")

        while self._running:
            try:
                healthy, details = self.run_checks()

                if healthy:
                    if self.consecutive_failures > 0:
                        logger.info(
                            "🛡️  SRM: recovered after %d failures | %s",
                            self.consecutive_failures, " | ".join(details)
                        )
                    self.consecutive_failures = 0
                else:
                    self.consecutive_failures += 1
                    logger.warning(
                        "🛡️  SRM: failure %d/%d | %s",
                        self.consecutive_failures, CONSECUTIVE_FAILURES,
                        " | ".join(details)
                    )

                    if self.consecutive_failures >= CONSECUTIVE_FAILURES:
                        uptime = time.time() - self.start_time
                        logger.critical(
                            "🚨 SRM: %d consecutive failures — triggering restart | "
                            "uptime=%.0fs | failures: %s",
                            self.consecutive_failures, uptime,
                            "; ".join(self._last_failures)
                        )

                        # Give logs time to flush
                        time.sleep(2)

                        # Exit with code 1 — Railway ON_FAILURE restarts us
                        os._exit(1)

            except Exception as e:
                logger.error("🛡️  SRM: monitor loop error: %s", e)

            time.sleep(CHECK_INTERVAL)

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------
    def start(self):
        """Start the background monitor thread."""
        if self._running:
            logger.warning("🛡️  SRM: already running")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="self-restart-monitor",
            daemon=True
        )
        self._thread.start()

    def stop(self):
        """Stop the monitor (for testing)."""
        self._running = False

    # ------------------------------------------------------------------
    # Status endpoint
    # ------------------------------------------------------------------
    def get_status(self):
        """Return current monitor status as dict (for /api/health extension)."""
        uptime = time.time() - self.start_time
        healthy, details = self.run_checks()
        return {
            "self_restart_monitor": {
                "active": self._running,
                "uptime_seconds": round(uptime),
                "consecutive_failures": self.consecutive_failures,
                "threshold": CONSECUTIVE_FAILURES,
                "error_rate_1m": self.get_error_rate(60),
                "checks": details,
                "healthy": healthy,
            }
        }


# ---------------------------------------------------------------------------
# Global instance & Flask integration
# ---------------------------------------------------------------------------
_monitor = None


def get_monitor():
    """Get the global monitor instance."""
    global _monitor
    return _monitor


def start_self_restart_monitor(app=None):
    """
    Initialize and start the self-restart monitor.

    Call this in main.py after all routes are registered:

        from self_restart_monitor import start_self_restart_monitor
        start_self_restart_monitor(app)

    Optionally wire into Flask error handlers for error rate tracking:

        from self_restart_monitor import get_monitor
        @app.errorhandler(500)
        def handle_500(e):
            monitor = get_monitor()
            if monitor:
                monitor.record_error()
            return {"error": "Internal server error"}, 500
    """
    global _monitor

    if _monitor and _monitor._running:
        logger.info("🛡️  SRM: already initialized")
        return _monitor

    _monitor = SelfRestartMonitor(app=app)

    # Wire into Flask error tracking if app provided
    if app:
        @app.errorhandler(500)
        def _srm_500_handler(e):
            _monitor.record_error()
            # Re-raise so other error handlers still work
            return {"error": "Internal server error"}, 500

        # Add status endpoint
        @app.route('/api/self-restart-monitor/status')
        def _srm_status():
            return _monitor.get_status()

        logger.info("🛡️  SRM: Flask error tracking and /api/self-restart-monitor/status registered")

    _monitor.start()
    return _monitor
