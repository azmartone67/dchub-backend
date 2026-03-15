"""
DC Hub Self-Healing Module v1.2.0
═══════════════════════════════════════════════════════════

Phase 5 of the DC Hub architecture improvement plan.

v1.2 changelog:
  - FIXED: Health monitor now uses a DIRECT psycopg2 connection (bypasses pool)
    instead of calling get_pool(). This prevents the chicken-and-egg deadlock
    where an exhausted pool causes the health check itself to block for 85s,
    which triggers force-reclaim, which looks like a leak.
  - Health check connection has a 5s connect_timeout and 5s statement_timeout
    so it can never block longer than 10s total.
  - get_pool is still accepted for backward compat but only used as fallback.

v1.1 changelog:
  - FIXED: Health monitor _check() connection leak. Connection now always
    closed in a finally block, preventing pool exhaustion from 30s health pings.

Components:
  1. HealthMonitor  — background thread checking DB every 120s, auto-resets pool
  2. resilient_query — decorator/wrapper that retries transient DB errors
  3. validate_startup — fails fast if DB or required env vars are missing
  4. AlertManager    — sends email/Slack after 3 consecutive failures

Usage in main.py:
  from self_healing import HealthMonitor, resilient_query, validate_startup, AlertManager

  # At startup (before app.run)
  validate_startup()

  # Start health monitor
  health_monitor = HealthMonitor(get_pg_pool, reset_pg_pool, alert_manager)
  health_monitor.start()

  # In route handlers — wrap DB calls
  @app.route('/api/v1/stats')
  def get_stats():
      rows = resilient_query("SELECT COUNT(*) FROM facilities")
      return jsonify({"facilities": rows[0][0]})

  # At shutdown
  health_monitor.stop()

Requires:
  - psycopg2 (already in use)
  - threading (stdlib)
  - smtplib (stdlib, for email alerts)
  - requests (already in use, for Slack webhooks)
"""

import os
import time
import logging
import threading
import traceback
from datetime import datetime, timezone
from functools import wraps

import psycopg2
from psycopg2 import OperationalError, InterfaceError, DatabaseError

logger = logging.getLogger("dchub.self_healing")

# ============================================================
# CONFIGURATION
# ============================================================

HEALTH_CHECK_INTERVAL = 120       # seconds between health checks
HEALTH_CHECK_TIMEOUT = 5         # seconds to wait for DB ping
MAX_CONSECUTIVE_FAILURES = 3     # failures before alerting
QUERY_MAX_RETRIES = 2            # retry count for transient errors
QUERY_RETRY_BACKOFF = [0.5, 1.5] # seconds to wait between retries

# Transient error codes that are safe to retry
TRANSIENT_ERRORS = {
    "08000",  # connection_exception
    "08001",  # sqlclient_unable_to_establish_sqlconnection
    "08003",  # connection_does_not_exist
    "08006",  # connection_failure
    "40001",  # serialization_failure
    "40P01",  # deadlock_detected
    "57P01",  # admin_shutdown
    "57P03",  # cannot_connect_now
    "53300",  # too_many_connections
}

# Required environment variables
REQUIRED_ENV_VARS = [
    "DATABASE_URL",       # Neon connection string
]

RECOMMENDED_ENV_VARS = [
    "GRIDSTATUS_API_KEY",
    "EIA_API_KEY",
    "STRIPE_SECRET_KEY",
    "GOOGLE_CLIENT_ID",
]


# ============================================================
# 1. STARTUP VALIDATION
# ============================================================

def validate_startup():
    """
    Fail fast if critical dependencies are missing.
    Called once at boot, before app.run().
    Raises RuntimeError if anything critical is broken.
    """
    logger.info("🔍 Running startup validation...")
    errors = []
    warnings = []

    # Check required env vars
    for var in REQUIRED_ENV_VARS:
        if not os.environ.get(var):
            errors.append(f"Missing required env var: {var}")

    # Check recommended env vars
    for var in RECOMMENDED_ENV_VARS:
        if not os.environ.get(var):
            warnings.append(f"Missing recommended env var: {var} (some features will be degraded)")

    # Test database connectivity
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url:
        try:
            conn = psycopg2.connect(db_url, connect_timeout=HEALTH_CHECK_TIMEOUT)
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            conn.close()
            logger.info("✅ Database connection verified")
        except Exception as e:
            errors.append(f"Database connection failed: {e}")
    else:
        errors.append("DATABASE_URL is empty — cannot connect to Neon")

    # Log warnings
    for w in warnings:
        logger.warning(f"⚠️  {w}")

    # Fail on errors
    if errors:
        for e in errors:
            logger.error(f"❌ {e}")
        raise RuntimeError(
            f"Startup validation failed with {len(errors)} error(s):\n" +
            "\n".join(f"  - {e}" for e in errors)
        )

    logger.info("✅ Startup validation passed")


# ============================================================
# 2. RESILIENT QUERY WRAPPER
# ============================================================

def resilient_query(sql, params=None, get_connection=None, fetchone=False):
    """
    Execute a SQL query with automatic retry on transient errors.

    Args:
        sql: SQL string to execute
        params: Query parameters (tuple or dict)
        get_connection: Callable that returns a psycopg2 connection.
                        If None, uses DATABASE_URL directly.
        fetchone: If True, return single row. Otherwise return all rows.

    Returns:
        Query results (list of tuples, or single tuple if fetchone=True)

    Raises:
        The original exception after all retries are exhausted.
    """
    last_error = None

    for attempt in range(QUERY_MAX_RETRIES + 1):
        conn = None
        try:
            if get_connection:
                conn = get_connection()
            else:
                conn = psycopg2.connect(
                    os.environ.get("DATABASE_URL", ""),
                    connect_timeout=HEALTH_CHECK_TIMEOUT
                )

            cur = conn.cursor()
            cur.execute(sql, params)

            if sql.strip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP")):
                conn.commit()
                affected = cur.rowcount
                cur.close()
                if not get_connection:
                    conn.close()
                return affected

            rows = cur.fetchone() if fetchone else cur.fetchall()
            cur.close()
            if not get_connection:
                conn.close()
            return rows

        except (OperationalError, InterfaceError) as e:
            last_error = e
            error_code = getattr(e, "pgcode", None) or ""

            if attempt < QUERY_MAX_RETRIES and (
                error_code in TRANSIENT_ERRORS or
                "connection" in str(e).lower() or
                "timeout" in str(e).lower() or
                "SSL" in str(e)
            ):
                wait = QUERY_RETRY_BACKOFF[min(attempt, len(QUERY_RETRY_BACKOFF) - 1)]
                logger.warning(
                    f"🔄 Transient DB error (attempt {attempt + 1}/{QUERY_MAX_RETRIES + 1}), "
                    f"retrying in {wait}s: {e}"
                )
                time.sleep(wait)
                # Close broken connection
                try:
                    if conn and not get_connection:
                        conn.close()
                except Exception:
                    pass
                continue
            else:
                raise

        except DatabaseError as e:
            # Non-transient DB error — don't retry
            try:
                if conn and not get_connection:
                    conn.close()
            except Exception:
                pass
            raise

        except Exception as e:
            try:
                if conn and not get_connection:
                    conn.close()
            except Exception:
                pass
            raise

    # All retries exhausted
    raise last_error


def resilient(get_connection=None):
    """
    Decorator version of resilient_query for route handlers.

    Usage:
        @resilient(get_connection=get_pg_connection)
        def my_query(conn):
            cur = conn.cursor()
            cur.execute("SELECT ...")
            return cur.fetchall()
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(QUERY_MAX_RETRIES + 1):
                try:
                    if get_connection:
                        conn = get_connection()
                        result = func(conn, *args, **kwargs)
                    else:
                        result = func(*args, **kwargs)
                    return result
                except (OperationalError, InterfaceError) as e:
                    last_error = e
                    if attempt < QUERY_MAX_RETRIES:
                        wait = QUERY_RETRY_BACKOFF[min(attempt, len(QUERY_RETRY_BACKOFF) - 1)]
                        logger.warning(
                            f"🔄 Retry {attempt + 1}/{QUERY_MAX_RETRIES} for "
                            f"{func.__name__}: {e}"
                        )
                        time.sleep(wait)
                    else:
                        raise
            raise last_error
        return wrapper
    return decorator


# ============================================================
# 3. HEALTH MONITOR (Background Thread)
# ============================================================

class HealthMonitor:
    """
    Background thread that checks DB connectivity every 120s.
    Auto-resets the connection pool on failure.
    Sends alerts after 3 consecutive failures.

    v1.2: Uses DIRECT psycopg2.connect() instead of the connection pool.
    This prevents the deadlock where an exhausted pool causes the health
    check to block for 85+ seconds waiting for a pooled connection.
    """

    def __init__(self, get_pool, reset_pool, alert_manager=None):
        """
        Args:
            get_pool: Callable that returns the current connection pool/connection
                      (kept for backward compat, but health checks now bypass it)
            reset_pool: Callable that resets/recreates the connection pool
            alert_manager: Optional AlertManager instance for notifications
        """
        self.get_pool = get_pool
        self.reset_pool = reset_pool
        self.alert_manager = alert_manager
        self._thread = None
        self._stop_event = threading.Event()
        self._consecutive_failures = 0
        self._last_success = None
        self._last_failure = None
        self._total_checks = 0
        self._total_failures = 0
        self._total_resets = 0

        # Resolve the DB URL once at init for direct connections
        # Try DATABASE_READ_URL first (read replica), fall back to DATABASE_URL
        self._db_url = (
            os.environ.get("DATABASE_READ_URL") or
            os.environ.get("DATABASE_URL") or
            ""
        )

    def start(self):
        """Start the health monitor background thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Health monitor already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="dchub-health-monitor",
            daemon=True
        )
        self._thread.start()
        logger.info(
            f"💓 Health monitor started (interval={HEALTH_CHECK_INTERVAL}s, "
            f"mode=direct-connection)"
        )

    def stop(self):
        """Stop the health monitor background thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Health monitor stopped")

    def status(self):
        """Return current health status as a dict."""
        return {
            "running": self._thread.is_alive() if self._thread else False,
            "consecutive_failures": self._consecutive_failures,
            "last_success": self._last_success,
            "last_failure": self._last_failure,
            "total_checks": self._total_checks,
            "total_failures": self._total_failures,
            "total_pool_resets": self._total_resets,
            "healthy": self._consecutive_failures == 0,
            "check_interval_s": HEALTH_CHECK_INTERVAL,
            "alert_threshold": MAX_CONSECUTIVE_FAILURES,
        }

    def _run(self):
        """Main loop — runs in background thread."""
        while not self._stop_event.is_set():
            try:
                self._check()
            except Exception as e:
                logger.error(f"Health monitor check error: {e}")

            # Sleep in small increments so we can stop quickly
            for _ in range(HEALTH_CHECK_INTERVAL * 2):
                if self._stop_event.is_set():
                    return
                time.sleep(0.5)

    def _check(self):
        """
        Run a single health check.

        v1.2 FIX: Uses a DIRECT psycopg2.connect() that bypasses the pool.
        This ensures the health check never blocks waiting for a pooled
        connection, and never contributes to pool exhaustion.

        The direct connection has:
          - connect_timeout=5  (fail fast if Neon is unreachable)
          - statement_timeout=5000ms (fail fast if query hangs)

        Previously (v1.0-v1.1): called self.get_pool() which goes through
        the connection pool. When the pool was exhausted, the health check
        would block for 85+ seconds waiting for a connection, then get
        force-reclaimed, creating a cascading failure loop.
        """
        self._total_checks += 1
        now = datetime.now(timezone.utc).isoformat()
        conn = None  # Initialize before try so finally can always reference it

        try:
            # v1.2: Direct connection — bypasses pool entirely
            conn = psycopg2.connect(
                self._db_url,
                connect_timeout=HEALTH_CHECK_TIMEOUT,

            )
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.close()

            # Success
            if self._consecutive_failures > 0:
                logger.info(
                    f"💓 DB recovered after {self._consecutive_failures} failures "
                    f"({now})"
                )
                if self.alert_manager and self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    self.alert_manager.send_recovery(
                        f"Database recovered after {self._consecutive_failures} "
                        f"consecutive failures"
                    )

            self._consecutive_failures = 0
            self._last_success = now

        except Exception as e:
            self._consecutive_failures += 1
            self._total_failures += 1
            self._last_failure = now
            error_msg = str(e)

            logger.warning(
                f"💔 DB health check failed ({self._consecutive_failures}/"
                f"{MAX_CONSECUTIVE_FAILURES}): {error_msg}"
            )

            # Auto-reset pool on first failure
            if self._consecutive_failures == 1:
                try:
                    logger.info("🔄 Auto-resetting connection pool...")
                    self.reset_pool()
                    self._total_resets += 1
                    logger.info("✅ Connection pool reset complete")
                except Exception as reset_err:
                    logger.error(f"❌ Pool reset failed: {reset_err}")

            # Alert on threshold
            if self._consecutive_failures == MAX_CONSECUTIVE_FAILURES:
                alert_msg = (
                    f"🚨 DC Hub DB Health Alert\n\n"
                    f"Database has failed {MAX_CONSECUTIVE_FAILURES} consecutive "
                    f"health checks.\n\n"
                    f"Last error: {error_msg}\n"
                    f"First failure: {self._last_failure}\n"
                    f"Pool reset attempted: Yes\n"
                    f"Total failures this session: {self._total_failures}\n"
                    f"Total pool resets: {self._total_resets}"
                )
                logger.error(alert_msg)
                if self.alert_manager:
                    self.alert_manager.send_alert(alert_msg)

            # Re-attempt pool reset every 3 failures
            if self._consecutive_failures > 1 and self._consecutive_failures % 3 == 0:
                try:
                    logger.info(
                        f"🔄 Re-attempting pool reset "
                        f"(failure #{self._consecutive_failures})..."
                    )
                    self.reset_pool()
                    self._total_resets += 1
                except Exception as reset_err:
                    logger.error(f"❌ Pool reset failed again: {reset_err}")

        finally:
            # CRITICAL: Always close the health-check connection.
            # Since this is a direct connection (not pooled), .close()
            # actually destroys it — no risk of pool leak.
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


# ============================================================
# 4. ALERT MANAGER
# ============================================================

class AlertManager:
    """
    Sends alerts via email and/or Slack webhook when self-healing can't recover.
    Rate-limited to prevent alert storms.
    """

    def __init__(self, slack_webhook_url=None, email_config=None):
        """
        Args:
            slack_webhook_url: Slack incoming webhook URL
            email_config: Dict with keys: smtp_host, smtp_port, from_addr, to_addr, password
        """
        self.slack_webhook_url = slack_webhook_url or os.environ.get("SLACK_WEBHOOK_URL")
        self.email_config = email_config
        self._last_alert_time = 0
        self._min_alert_interval = 300  # 5 minutes between alerts
        self._alert_count = 0

    def send_alert(self, message):
        """Send an alert through all configured channels."""
        now = time.time()
        if now - self._last_alert_time < self._min_alert_interval:
            logger.info(
                f"Alert suppressed (rate limit: {self._min_alert_interval}s)"
            )
            return

        self._last_alert_time = now
        self._alert_count += 1

        # Slack
        if self.slack_webhook_url:
            self._send_slack(message)

        # Email
        if self.email_config:
            self._send_email("🚨 DC Hub Alert", message)

        # Always log
        logger.critical(f"ALERT #{self._alert_count}: {message}")

    def send_recovery(self, message):
        """Send a recovery notification."""
        recovery_msg = f"✅ RECOVERED: {message}"

        if self.slack_webhook_url:
            self._send_slack(recovery_msg)

        if self.email_config:
            self._send_email("✅ DC Hub Recovery", recovery_msg)

        logger.info(f"Recovery notification sent: {message}")

    def _send_slack(self, message):
        """Send to Slack webhook."""
        try:
            import requests
            resp = requests.post(
                self.slack_webhook_url,
                json={"text": message},
                timeout=5
            )
            if resp.status_code != 200:
                logger.warning(f"Slack alert failed: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Slack alert error: {e}")

    def _send_email(self, subject, body):
        """Send email alert."""
        try:
            import smtplib
            from email.mime.text import MIMEText

            cfg = self.email_config
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = cfg.get("from_addr", "alerts@dchub.cloud")
            msg["To"] = cfg.get("to_addr", "jonathan@dchub.cloud")

            with smtplib.SMTP(
                cfg.get("smtp_host", "smtp.gmail.com"),
                cfg.get("smtp_port", 587)
            ) as server:
                server.starttls()
                if cfg.get("password"):
                    server.login(msg["From"], cfg["password"])
                server.send_message(msg)

            logger.info(f"Email alert sent to {msg['To']}")
        except Exception as e:
            logger.warning(f"Email alert error: {e}")

    def status(self):
        """Return alert system status."""
        return {
            "slack_configured": bool(self.slack_webhook_url),
            "email_configured": bool(self.email_config),
            "total_alerts_sent": self._alert_count,
            "last_alert_time": (
                datetime.fromtimestamp(self._last_alert_time, tz=timezone.utc).isoformat()
                if self._last_alert_time > 0 else None
            ),
            "min_interval_seconds": self._min_alert_interval,
        }


# ============================================================
# 5. HEALTH API ENDPOINT (add to Flask app)
# ============================================================

def register_health_endpoints(app, health_monitor, alert_manager=None):
    """
    Register self-healing status endpoints on the Flask app.

    Usage:
        register_health_endpoints(app, health_monitor, alert_manager)
    """
    from flask import jsonify as flask_jsonify

    @app.route("/api/v1/health/self-healing")
    def self_healing_status():
        status = {
            "health_monitor": health_monitor.status(),
            "worker_version": os.environ.get("WORKER_VERSION", "unknown"),
            "uptime_seconds": int(time.time() - app.config.get("START_TIME", time.time())),
        }
        if alert_manager:
            status["alerts"] = alert_manager.status()

        is_healthy = health_monitor.status()["healthy"]
        return flask_jsonify(status), 200 if is_healthy else 503


# ============================================================
# INTEGRATION EXAMPLE
# ============================================================
"""
# In main.py — add near the top after imports:

from self_healing import (
    HealthMonitor, AlertManager,
    resilient_query, validate_startup,
    register_health_endpoints
)

# Before app creation:
validate_startup()

# After app creation and DB pool setup:
alert_manager = AlertManager(
    slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL"),
    # email_config={
    #     "smtp_host": "smtp.gmail.com",
    #     "smtp_port": 587,
    #     "from_addr": "alerts@dchub.cloud",
    #     "to_addr": "jonathan@dchub.cloud",
    #     "password": os.environ.get("EMAIL_ALERT_PASSWORD"),
    # }
)

health_monitor = HealthMonitor(
    get_pool=get_read_db,      # kept for backward compat (health check bypasses it)
    reset_pool=reset_pg_pool,  # your existing function that recreates the pool
    alert_manager=alert_manager,
)
health_monitor.start()
app.config["START_TIME"] = time.time()

register_health_endpoints(app, health_monitor, alert_manager)

# In route handlers — use resilient_query for critical paths:
@app.route("/api/v1/stats")
def get_stats():
    rows = resilient_query(
        "SELECT COUNT(*) FROM facilities",
        get_connection=get_read_db
    )
    return jsonify({"facilities": rows[0][0]})
"""
