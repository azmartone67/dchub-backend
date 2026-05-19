import threading
import time
import os
import signal
import logging
import psutil
from datetime import datetime, timedelta

logger = logging.getLogger('watchdog')

class HealthWatchdog:
    def __init__(self, app=None, check_interval=90, max_failures=5):
        self.app = app
        self.check_interval = check_interval
        self.max_failures = max_failures
        self.consecutive_failures = 0
        self.last_check_time = None
        self.last_check_status = 'starting'
        self.last_check_details = {}
        self.start_time = time.time()
        self.total_checks = 0
        self.total_restarts = 0
        self.restart_history = []
        self._thread = None
        self._running = False
        self._news_scheduler_ref = None
        self._news_restart_count = 0
        self._news_restart_history = []

    def register_news_scheduler(self, scheduler_ref):
        self._news_scheduler_ref = scheduler_ref
        logger.info("NEWS WATCHDOG: News scheduler registered for monitoring")

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._thread.start()
        logger.info("Health Watchdog started (check every %ds, restart after %d failures)", 
                     self.check_interval, self.max_failures)

    def stop(self):
        self._running = False

    def _check_postgres(self):
        pg_url = os.environ.get('NEON_DATABASE_URL', '') or os.environ.get('DATABASE_URL', '')
        if not pg_url:
            return True, "not configured (skipped)"
        try:
            from db_utils import get_bg_db
            conn = get_bg_db()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            conn.close()
            return True, "ok"
        except Exception as e:
            return False, f"PostgreSQL error: {str(e)[:100]}"

    def _check_memory(self):
        # Phase FF+7-survive (2026-05-19): RAISED from 450MB → 2200MB.
        # The old 450MB limit was set when the app had ~20 blueprints;
        # we now register 60+, so steady-state RSS is ~460MB which
        # tripped the watchdog every 60s × 3 = 3 min into a kill cycle.
        # 2200MB matches the L20 durability soft-limit (70% of the
        # 3072MB ceiling reported by /api/v1/health). Configurable via
        # WATCHDOG_PROCESS_MEMORY_LIMIT_MB env var if you need to
        # adjust without a deploy.
        try:
            limit_mb = int(os.environ.get("WATCHDOG_PROCESS_MEMORY_LIMIT_MB", "2200"))
            process = psutil.Process(os.getpid())
            mem_mb = process.memory_info().rss / (1024 * 1024)
            mem_percent = psutil.virtual_memory().percent
            if mem_mb > limit_mb:
                return False, f"Process memory critical: {mem_mb:.0f}MB (limit {limit_mb}MB)"
            if mem_percent > 95:
                return False, f"System memory critical: {mem_percent:.0f}% used, process {mem_mb:.0f}MB"
            return True, f"ok ({mem_mb:.0f}MB process, {mem_percent:.0f}% system)"
        except Exception as e:
            return True, f"check unavailable: {str(e)[:50]}"

    def _check_disk(self):
        try:
            usage = psutil.disk_usage('/')
            if usage.percent > 95:
                return False, f"Disk critical: {usage.percent:.0f}% used"
            return True, f"ok ({usage.percent:.0f}% used)"
        except Exception as e:
            return True, f"check unavailable: {str(e)[:50]}"

    def _check_self_response(self):
        try:
            import requests
            port = os.environ.get("PORT", "5000")
            resp = requests.get(f"http://127.0.0.1:{port}/api/v1/stats", timeout=10)
            if resp.status_code == 200:
                return True, f"ok ({resp.elapsed.total_seconds():.2f}s)"
            return False, f"HTTP {resp.status_code}"
        except Exception as e:
            return False, f"No response: {str(e)[:100]}"

    def _check_thread_count(self):
        try:
            thread_count = threading.active_count()
            if thread_count > 100:
                return False, f"Too many threads: {thread_count}"
            return True, f"ok ({thread_count} threads)"
        except Exception as e:
            return True, f"check unavailable: {str(e)[:50]}"

    def _check_news_scheduler(self):
        if self._news_scheduler_ref is None:
            return True, "not registered (skipped)"
        try:
            sched = self._news_scheduler_ref
            if hasattr(sched, '_scheduler') and sched._scheduler:
                inner = sched._scheduler
                is_running = getattr(inner, '_running', False)
                timer = getattr(inner, '_timer', None)
                timer_alive = timer is not None and timer.is_alive() if timer else False
                sync_in_progress = getattr(inner, '_sync_in_progress', False)
                if is_running and (timer_alive or sync_in_progress):
                    return True, f"ok (syncs={inner.sync_count}, errors={inner.error_count})"
                elif is_running and not timer_alive and not sync_in_progress:
                    return False, f"timer dead (syncs={inner.sync_count}, errors={inner.error_count})"
                else:
                    return False, f"not running (running={is_running})"
            elif hasattr(sched, '_running'):
                if sched._running:
                    return True, "ok (direct scheduler)"
                return False, "scheduler stopped"
            return True, "ok (unknown type)"
        except Exception as e:
            return False, f"check error: {str(e)[:80]}"

    def _restart_news_scheduler(self):
        try:
            sched = self._news_scheduler_ref
            if hasattr(sched, '_scheduler') and sched._scheduler:
                inner = sched._scheduler
                inner._running = True
                inner._sync_in_progress = False
                inner._schedule_next()
                self._news_restart_count += 1
                self._news_restart_history.append({
                    'time': datetime.utcnow().isoformat(),
                    'sync_count_at_restart': inner.sync_count,
                    'error_count_at_restart': inner.error_count,
                })
                if len(self._news_restart_history) > 20:
                    self._news_restart_history = self._news_restart_history[-20:]
                logger.warning("NEWS WATCHDOG: Scheduler died, restarting... (restart #%d)", self._news_restart_count)
                print(f"NEWS WATCHDOG: Scheduler died, restarting... (restart #{self._news_restart_count})")
                return True
            elif hasattr(sched, 'start'):
                sched.start()
                self._news_restart_count += 1
                self._news_restart_history.append({
                    'time': datetime.utcnow().isoformat(),
                })
                if len(self._news_restart_history) > 20:
                    self._news_restart_history = self._news_restart_history[-20:]
                logger.warning("NEWS WATCHDOG: Scheduler died, restarting... (restart #%d)", self._news_restart_count)
                return True
        except Exception as e:
            logger.error("NEWS WATCHDOG: Failed to restart scheduler: %s", str(e))
        return False

    def get_news_scheduler_status(self):
        status = {
            'thread_alive': False,
            'total_runs': 0,
            'last_run': None,
            'articles_in_pg': 0,
            'restart_count': self._news_restart_count,
            'restart_history': self._news_restart_history[-5:],
        }
        if self._news_scheduler_ref is None:
            status['registered'] = False
            return status
        status['registered'] = True
        try:
            sched = self._news_scheduler_ref
            if hasattr(sched, '_scheduler') and sched._scheduler:
                inner = sched._scheduler
                timer = getattr(inner, '_timer', None)
                timer_alive = timer is not None and timer.is_alive() if timer else False
                sync_in_progress = getattr(inner, '_sync_in_progress', False)
                status['thread_alive'] = getattr(inner, '_running', False) and (timer_alive or sync_in_progress)
                status['total_runs'] = inner.sync_count
                status['last_run'] = inner.last_sync
                status['error_count'] = inner.error_count
            elif hasattr(sched, '_running'):
                status['thread_alive'] = sched._running
                status['total_runs'] = getattr(sched, 'sync_count', 0)
                status['last_run'] = getattr(sched, 'last_sync', None)
        except Exception as e:
            status['check_error'] = str(e)[:100]
        try:
            from db_utils import get_bg_db
            pg_conn = get_bg_db()
            pg_cur = pg_conn.cursor()
            pg_cur.execute("SELECT COUNT(*) FROM news_articles")
            status['articles_in_pg'] = pg_cur.fetchone()[0]
            pg_conn.close()
        except Exception:
            status['articles_in_pg'] = -1
        return status

    def run_health_check(self):
        self.total_checks += 1
        self.last_check_time = datetime.utcnow()
        details = {}
        all_ok = True

        checks = [
            ('self_response', self._check_self_response),
            ('postgres', self._check_postgres),
            ('memory', self._check_memory),
            ('disk', self._check_disk),
            ('threads', self._check_thread_count),
            ('news_scheduler', self._check_news_scheduler),
        ]

        critical_checks = {'self_response', 'postgres', 'memory'}

        for name, check_fn in checks:
            try:
                ok, msg = check_fn()
                details[name] = {'healthy': ok, 'message': msg}
                if not ok and name in critical_checks:
                    all_ok = False
                if not ok and name == 'news_scheduler':
                    self._restart_news_scheduler()
            except Exception as e:
                details[name] = {'healthy': False, 'message': f'Exception: {str(e)[:100]}'}
                if name in critical_checks:
                    all_ok = False

        self.last_check_details = details

        if all_ok:
            self.consecutive_failures = 0
            self.last_check_status = 'healthy'
        else:
            self.consecutive_failures += 1
            failed = [k for k, v in details.items() if not v['healthy']]
            self.last_check_status = 'degraded' if self.consecutive_failures < self.max_failures else 'critical'
            logger.warning("Health check failed (%d/%d): %s", 
                          self.consecutive_failures, self.max_failures, ', '.join(failed))

        return all_ok

    def _kill_app_port_processes(self):
        _app_port = os.environ.get("PORT", "8080")
        try:
            import subprocess
            result = subprocess.run(
                ['fuser', f'{_app_port}/tcp'], capture_output=True, text=True, timeout=5
            )
            pids = result.stdout.strip().split()
            my_pid = os.getpid()
            my_ppid = os.getppid()
            for pid_str in pids:
                try:
                    pid = int(pid_str.strip())
                    if pid != my_pid and pid != my_ppid:
                        os.kill(pid, signal.SIGKILL)
                        logger.warning("WATCHDOG: Killed stuck process PID %d on port %s", pid, _app_port)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
        except Exception as e:
            logger.warning("WATCHDOG: Port %s cleanup failed: %s", _app_port, str(e)[:100])

    def _trigger_restart(self):
        self.total_restarts += 1
        restart_time = datetime.utcnow().isoformat()
        reason = f"Health check failed {self.consecutive_failures} consecutive times"
        failed_checks = [k for k, v in self.last_check_details.items() if not v['healthy']]

        self.restart_history.append({
            'time': restart_time,
            'reason': reason,
            'failed_checks': failed_checks
        })
        if len(self.restart_history) > 20:
            self.restart_history = self.restart_history[-20:]

        logger.critical("WATCHDOG: Server unresponsive %d times, forcing full restart! Failed: %s",
                        self.consecutive_failures, ', '.join(failed_checks))
        print(f"WATCHDOG: Server unresponsive {self.consecutive_failures} times, forcing full restart")

        self._kill_app_port_processes()

        import sys
        sys.stdout.flush()
        sys.stderr.flush()

        ppid = os.getppid()
        try:
            os.kill(ppid, signal.SIGTERM)
            print(f"WATCHDOG: Sent SIGTERM to gunicorn master PID {ppid}")
            time.sleep(3)
            os.kill(ppid, signal.SIGKILL)
            print(f"WATCHDOG: Sent SIGKILL to gunicorn master PID {ppid}")
        except (ProcessLookupError, PermissionError):
            pass

        os._exit(1)

    def _rotate_logs(self):
        max_bytes = 50 * 1024 * 1024
        try:
            for handler in logging.root.handlers:
                if hasattr(handler, 'baseFilename'):
                    fpath = handler.baseFilename
                    if os.path.exists(fpath) and os.path.getsize(fpath) > max_bytes:
                        with open(fpath, 'w') as f:
                            f.write(f"[LOG ROTATED at {datetime.utcnow().isoformat()} — exceeded 50MB]\n")
                        logger.info("LOG ROTATION: Truncated %s (exceeded 50MB)", fpath)
            log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)))
            for fname in os.listdir(log_dir):
                if fname.endswith('.log'):
                    fpath = os.path.join(log_dir, fname)
                    try:
                        if os.path.getsize(fpath) > max_bytes:
                            with open(fpath, 'w') as f:
                                f.write(f"[LOG ROTATED at {datetime.utcnow().isoformat()} — exceeded 50MB]\n")
                            logger.info("LOG ROTATION: Truncated %s", fname)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("Log rotation check failed: %s", str(e)[:80])

    def _cleanup_stale_news(self):
        try:
            from db_utils import get_bg_db
            conn = get_bg_db()
            try:
                conn.rollback()
            except Exception:
                pass
            cur = conn.cursor()
            cur.execute("DELETE FROM news_articles WHERE fetched_at::timestamptz < NOW() - INTERVAL '90 days'")
            deleted = cur.rowcount
            conn.commit()
            cur.close()
            conn.close()
            if deleted > 0:
                logger.info("NEWS CLEANUP: Purged %d articles older than 90 days", deleted)
        except Exception as e:
            logger.debug("News cleanup failed: %s", str(e)[:80])

    def _watchdog_loop(self):
        time.sleep(30)
        logger.info("Watchdog: Initial delay complete, starting checks")

        while self._running:
            try:
                healthy = self.run_health_check()

                if not healthy and self.consecutive_failures >= self.max_failures:
                    self._trigger_restart()

                if self.total_checks % 10 == 0:
                    news_status = "n/a"
                    if self._news_scheduler_ref:
                        ns = self._check_news_scheduler()
                        news_status = f"alive={ns[0]}, restarts={self._news_restart_count}"
                    logger.info("Watchdog: check #%d | status=%s | failures=%d | restarts=%d | news=%s",
                               self.total_checks, self.last_check_status, 
                               self.consecutive_failures, self.total_restarts, news_status)

                if self.total_checks % 60 == 0:
                    self._rotate_logs()

                if self.total_checks % 360 == 0:
                    self._cleanup_stale_news()

            except Exception as e:
                logger.error("Watchdog loop error: %s", str(e))

            time.sleep(self.check_interval)

    def get_status(self):
        uptime = time.time() - self.start_time
        status = {
            'watchdog': 'active' if self._running else 'stopped',
            'status': self.last_check_status,
            'uptime_seconds': round(uptime),
            'uptime_human': str(timedelta(seconds=int(uptime))),
            'check_interval_seconds': self.check_interval,
            'total_checks': self.total_checks,
            'consecutive_failures': self.consecutive_failures,
            'max_failures_before_restart': self.max_failures,
            'total_restarts': self.total_restarts,
            'last_check_time': self.last_check_time.isoformat() if self.last_check_time else None,
            'last_check_details': self.last_check_details,
            'restart_history': self.restart_history[-5:],
            'news_scheduler': {
                'monitored': self._news_scheduler_ref is not None,
                'restart_count': self._news_restart_count,
            },
        }
        return status


watchdog_instance = None

def register_watchdog_routes(app):
    """Register watchdog routes during startup (before first request)."""
    @app.route('/api/health/watchdog', methods=['GET'])
    def watchdog_status():
        from flask import jsonify
        if watchdog_instance:
            return jsonify(watchdog_instance.get_status())
        # The watchdog thread may not have spun up yet in the first
        # moments after boot. That is NOT a service error — the route
        # itself is fine, the subsystem is just initializing. Returning
        # 503 here failed post-deploy-smoke on every deploy and kicked
        # off the auto-repair loop. Report 200 + an explicit
        # 'initializing' state instead so health checks stay honest
        # without false-failing.
        return jsonify({'watchdog': 'initializing', 'status': 'starting'}), 200

    @app.route('/api/health/watchdog/check', methods=['POST'])
    def watchdog_manual_check():
        from flask import jsonify, request
        admin_key = request.headers.get('X-Admin-Key') or request.args.get('admin_key')
        expected = os.environ.get('DCHUB_ADMIN_KEY', '')
        if not admin_key or admin_key != expected:
            return jsonify({'error': 'unauthorized'}), 401
        if watchdog_instance:
            healthy = watchdog_instance.run_health_check()
            status = watchdog_instance.get_status()
            status['manual_check_result'] = 'healthy' if healthy else 'unhealthy'
            return jsonify(status)
        return jsonify({'watchdog': 'not initialized'}), 503

    @app.route('/api/news/scheduler-status', methods=['GET'])
    def news_scheduler_status():
        from flask import jsonify
        if watchdog_instance:
            return jsonify(watchdog_instance.get_news_scheduler_status())
        return jsonify({'error': 'watchdog not initialized'}), 503


def init_watchdog(app, check_interval=90, max_failures=5):
    """Start the watchdog background thread (call after routes are registered)."""
    global watchdog_instance
    watchdog_instance = HealthWatchdog(app=app, check_interval=check_interval, max_failures=max_failures)
    watchdog_instance.start()
    return watchdog_instance
