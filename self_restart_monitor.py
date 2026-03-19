"""
Self-Restart Monitor for DC Hub
================================
Monitors system health and triggers automatic restarts when thresholds
are exceeded. Works with Railway's ON_FAILURE restart policy.

Usage in main.py:
    from self_restart_monitor import start_self_restart_monitor, get_monitor
    start_self_restart_monitor()

    @app.route('/api/self-restart-monitor/status')
    def srm_status():
        m = get_monitor()
        return m.get_status() if m else {"error": "not initialized"}
"""

import os
import time
import threading
import logging
import resource
from collections import deque

logger = logging.getLogger("self_restart_monitor")

CHECK_INTERVAL = int(os.environ.get("SRM_CHECK_INTERVAL", "30"))
CONSECUTIVE_FAILURES = int(os.environ.get("SRM_CONSECUTIVE_FAILURES", "3"))
MAX_RSS_MB = int(os.environ.get("SRM_MAX_RSS_MB", "512"))
MAX_THREADS = int(os.environ.get("SRM_MAX_THREADS", "100"))
MAX_ERRORS_PER_MINUTE = int(os.environ.get("SRM_MAX_ERRORS_MIN", "50"))
DB_CHECK_TIMEOUT = int(os.environ.get("SRM_DB_TIMEOUT", "5"))
STARTUP_GRACE_SECONDS = int(os.environ.get("SRM_STARTUP_GRACE", "180"))


class SelfRestartMonitor:
    def __init__(self):
        self.start_time = time.time()
        self.consecutive_failures = 0
        self.error_timestamps = deque(maxlen=500)
        self._running = False
        self._thread = None
        self._last_failures = []

    def record_error(self):
        self.error_timestamps.append(time.time())

    def get_error_rate(self, window_seconds=60):
        cutoff = time.time() - window_seconds
        return sum(1 for ts in self.error_timestamps if ts > cutoff)

    def check_memory(self):
        try:
            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_mb = rss_kb / 1024
            if rss_mb > MAX_RSS_MB:
                return False, f"RSS {rss_mb:.0f}MB > {MAX_RSS_MB}MB"
            return True, f"RSS {rss_mb:.0f}MB"
        except Exception as e:
            return True, f"memory check error: {e}"

    def check_threads(self):
        try:
            count = threading.active_count()
            if count > MAX_THREADS:
                return False, f"threads {count} > {MAX_THREADS}"
            return True, f"threads {count}"
        except Exception as e:
            return True, f"thread check error: {e}"

    def check_error_rate(self):
        rate = self.get_error_rate(60)
        if rate > MAX_ERRORS_PER_MINUTE:
            return False, f"errors {rate}/min > {MAX_ERRORS_PER_MINUTE}/min"
        return True, f"errors {rate}/min"

    def check_database(self):
        try:
            db_url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
            if not db_url:
                return True, "no DB URL configured"
            import psycopg2
            conn = psycopg2.connect(db_url, connect_timeout=DB_CHECK_TIMEOUT)
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.close()
            finally:
                conn.close()
            return True, "DB OK"
        except ImportError:
            return True, "psycopg2 not available"
        except Exception as e:
            err_msg = str(e).split('\n')[0][:80]
            return False, f"DB unreachable: {err_msg}"

    def run_checks(self):
        failures = []
        details = []
        for name, check_fn in [("memory", self.check_memory), ("threads", self.check_threads), ("error_rate", self.check_error_rate), ("database", self.check_database)]:
            try:
                ok, detail = check_fn()
                details.append(f"{name}={'OK' if ok else 'FAIL'} ({detail})")
                if not ok:
                    failures.append(f"{name}: {detail}")
            except Exception as e:
                details.append(f"{name}=ERROR ({e})")
        self._last_failures = failures
        return len(failures) == 0, details

    def _monitor_loop(self):
        logger.info(
            "🛡️  Self-Restart Monitor started | interval=%ds | grace=%ds | "
            "thresholds: RSS<%dMB threads<%d errors<%d/min | restart after %d consecutive failures",
            CHECK_INTERVAL, STARTUP_GRACE_SECONDS, MAX_RSS_MB, MAX_THREADS, MAX_ERRORS_PER_MINUTE, CONSECUTIVE_FAILURES
        )
        time.sleep(STARTUP_GRACE_SECONDS)
        logger.info("🛡️  Self-Restart Monitor: grace period complete, monitoring active")
        while self._running:
            try:
                healthy, details = self.run_checks()
                if healthy:
                    if self.consecutive_failures > 0:
                        logger.info("🛡️  SRM: recovered after %d failures | %s", self.consecutive_failures, " | ".join(details))
                    self.consecutive_failures = 0
                else:
                    self.consecutive_failures += 1
                    logger.warning("🛡️  SRM: failure %d/%d | %s", self.consecutive_failures, CONSECUTIVE_FAILURES, " | ".join(details))
                    if self.consecutive_failures >= CONSECUTIVE_FAILURES:
                        uptime = time.time() - self.start_time
                        logger.critical("🚨 SRM: %d consecutive failures — triggering restart | uptime=%.0fs | failures: %s", self.consecutive_failures, uptime, "; ".join(self._last_failures))
                        time.sleep(2)
                        os._exit(1)
            except Exception as e:
                logger.error("🛡️  SRM: monitor loop error: %s", e)
            time.sleep(CHECK_INTERVAL)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, name="self-restart-monitor", daemon=True)
        self._thread.start()

    def get_status(self):
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


_monitor = None

def get_monitor():
    global _monitor
    return _monitor

def start_self_restart_monitor(app=None):
    global _monitor
    if _monitor and _monitor._running:
        return _monitor
    _monitor = SelfRestartMonitor()
    _monitor.start()
    logger.info("🛡️  SRM: initialized and thread started")
    return _monitor
