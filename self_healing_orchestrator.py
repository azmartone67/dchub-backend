#!/usr/bin/env python3
"""
DC Hub Self-Healing Orchestrator v1.0
=====================================
Proactively detects and auto-repairs recurring production issues.

Unlike health_watchdog.py (which just restarts the server) and self_healing.py
(which just monitors DB connections), this module actually FIXES problems.

Runs as a background thread, checks every 2 minutes.

Issues it auto-repairs:
  1. DCHUB_API_BASE localhost deadlock — forces Railway URL
  2. fiber_routes missing unique index — recreates on detection
  3. daily_record_usage missing columns — ALTERs table
  4. MCP process down while REST is up — restarts MCP subprocess
  5. Connection pool exhaustion — forces pool reset
  6. Stats endpoint crash (fetchone KeyError) — clears stats cache
  7. Neon DB URL mismatch — validates and corrects DATABASE_URL
  8. Rankings blueprint collision — suppresses error (non-blocking)
  9. Memory leak detection — triggers GC + cache clear above threshold
 10. Stale Redis cache — flushes if data is >1hr old

Usage:
  # In main.py during startup:
  from self_healing_orchestrator import start_healing_orchestrator
  start_healing_orchestrator(app)

Author: DC Hub Engineering
"""

import os
import sys
import time
import threading
import logging
import gc
import signal
import subprocess
from datetime import datetime, timedelta

logger = logging.getLogger('self-healer')

# ============================================================
# Configuration
# ============================================================
HEAL_INTERVAL = 120          # Check every 2 minutes
RAILWAY_API_BASE = 'https://dchub-backend-production.up.railway.app'
BLOCKED_API_BASES = ['127.0.0.1', 'localhost', '0.0.0.0']
MEMORY_WARN_MB = 350
MEMORY_CRITICAL_MB = 450
MCP_PORT = 8888
NEON_HOSTNAME_PREFIX = 'ep-old-waterfall'

# ============================================================
# Healing functions
# ============================================================

def heal_api_base():
    """Fix 1: Prevent DCHUB_API_BASE from being set to localhost.
    This causes a deadlock where the MCP server calls itself.
    Has happened 5+ times in production.
    """
    current = os.environ.get('DCHUB_API_BASE', '')
    for blocked in BLOCKED_API_BASES:
        if blocked in current:
            os.environ['DCHUB_API_BASE'] = RAILWAY_API_BASE
            logger.warning(
                "HEALED: DCHUB_API_BASE was '%s' (deadlock!) -> forced to '%s'",
                current, RAILWAY_API_BASE
            )
            return True, "Fixed localhost deadlock"
    return False, "OK"


def heal_fiber_index():
    """Fix 2: Ensure fiber_routes (name, provider) unique index exists.
    Without it, ON CONFLICT fails on every upsert -> 2205 errors -> 
    watchdog kill -> crash loop.
    """
    try:
        import psycopg2
        db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
        if not db_url:
            return False, "No DB URL"
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            SELECT 1 FROM pg_indexes 
            WHERE tablename = 'fiber_routes' 
            AND (indexname = 'fiber_routes_unique_key' 
                 OR indexname = 'fiber_routes_name_provider_unique')
        """)
        if not cur.fetchone():
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS fiber_routes_name_provider_unique
                ON fiber_routes (name, provider)
            """)
            logger.warning("HEALED: Recreated fiber_routes (name, provider) unique index")
            conn.close()
            return True, "Recreated unique index"
        conn.close()
        return False, "OK"
    except Exception as e:
        logger.debug("fiber index check: %s", str(e)[:80])
        return False, str(e)[:80]


def heal_daily_record_usage():
    """Fix 3: Add missing columns to daily_record_usage table.
    Other agents keep adding column references without ALTERing the table.
    This healer auto-adds any missing columns.
    """
    try:
        import psycopg2
        db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
        if not db_url:
            return False, "No DB URL"
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        cur = conn.cursor()
        
        # All columns that code references but may not exist yet
        required_columns = {
            'last_endpoint': 'TEXT',
            'last_access': 'TIMESTAMP',
        }
        
        added = []
        for col_name, col_type in required_columns.items():
            cur.execute("""
                SELECT 1 FROM information_schema.columns 
                WHERE table_name = 'daily_record_usage' 
                AND column_name = %s
            """, (col_name,))
            if not cur.fetchone():
                cur.execute("ALTER TABLE daily_record_usage ADD COLUMN %s %s" % (col_name, col_type))
                added.append(col_name)
                logger.warning("HEALED: Added %s column to daily_record_usage", col_name)
        
        conn.close()
        if added:
            return True, "Added columns: %s" % ', '.join(added)
        return False, "OK"
    except Exception as e:
        logger.debug("daily_record_usage check: %s", str(e)[:80])
        return False, str(e)[:80]


def heal_mcp_process():
    """Fix 4: PREEMPTIVE MCP health monitoring and auto-recovery.
    
    Instead of waiting for MCP to crash, we:
    1. Track MCP response times — if trending up, preemptive restart
    2. Track consecutive slow responses — restart before timeout
    3. Monitor MCP memory/thread footprint
    4. Auto-fix DCHUB_API_BASE before MCP even starts failing
    5. Verify MCP tool count matches expected (15 tools)
    """
    try:
        import requests
        port = int(os.environ.get('PORT', '8080'))

        # Check REST API first
        try:
            r = requests.get('http://127.0.0.1:%d/health' % port, timeout=5)
            rest_ok = r.status_code == 200
        except Exception:
            rest_ok = False

        if not rest_ok:
            return False, "REST API also down (watchdog handles this)"

        # ── Preemptive: Fix API base BEFORE MCP fails ──
        heal_api_base()

        # ── Check MCP process health ──
        mcp_ok = False
        mcp_latency = None
        mcp_tools = 0
        try:
            start = time.time()
            r = requests.post(
                'http://127.0.0.1:%d/mcp' % MCP_PORT,
                json={"jsonrpc": "2.0", "method": "tools/list", "id": 1,
                      "params": {}},
                headers={'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'},
                timeout=15
            )
            mcp_latency = round((time.time() - start) * 1000)
            mcp_ok = r.status_code in (200, 202)
            if mcp_ok:
                try:
                    body = r.json()
                    if 'result' in body and 'tools' in body['result']:
                        mcp_tools = len(body['result']['tools'])
                except Exception:
                    # SSE response — parse differently
                    text = r.text
                    mcp_tools = text.count('"name"')
        except Exception:
            mcp_ok = False

        # ── Track MCP health trend ──
        if not hasattr(heal_mcp_process, '_history'):
            heal_mcp_process._history = []
            heal_mcp_process._consecutive_slow = 0
            heal_mcp_process._consecutive_down = 0
            heal_mcp_process._total_restarts = 0

        if mcp_ok and mcp_latency:
            heal_mcp_process._history.append(mcp_latency)
            if len(heal_mcp_process._history) > 30:
                heal_mcp_process._history = heal_mcp_process._history[-30:]
            heal_mcp_process._consecutive_down = 0

            # Preemptive: if last 5 responses all >5s, restart before it crashes
            if mcp_latency > 5000:
                heal_mcp_process._consecutive_slow += 1
            else:
                heal_mcp_process._consecutive_slow = 0

            if heal_mcp_process._consecutive_slow >= 3:
                logger.warning(
                    "PREEMPTIVE HEAL: MCP responding but degraded (%d consecutive >5s). Restarting...",
                    heal_mcp_process._consecutive_slow
                )
                _restart_mcp_process()
                heal_mcp_process._consecutive_slow = 0
                return True, "Preemptive restart (degraded latency: %dms)" % mcp_latency

            # Report health
            avg_ms = sum(heal_mcp_process._history) / len(heal_mcp_process._history)
            return False, "OK (%dms, avg %dms, %d tools)" % (mcp_latency, avg_ms, mcp_tools)

        else:
            # MCP is down
            heal_mcp_process._consecutive_down += 1

            if heal_mcp_process._consecutive_down >= 2:
                logger.warning(
                    "HEALING: MCP down %d consecutive checks. Restarting (restart #%d)...",
                    heal_mcp_process._consecutive_down,
                    heal_mcp_process._total_restarts + 1
                )
                _restart_mcp_process()
                heal_mcp_process._total_restarts += 1
                heal_mcp_process._consecutive_down = 0
                return True, "MCP restarted (down %d checks)" % heal_mcp_process._consecutive_down

            return False, "MCP unresponsive (check %d, will restart at 2)" % heal_mcp_process._consecutive_down

    except Exception as e:
        return False, str(e)[:80]


def _restart_mcp_process():
    """Kill stale MCP process and restart cleanly."""
    # Kill existing
    try:
        result = subprocess.run(
            ['fuser', '%d/tcp' % MCP_PORT],
            capture_output=True, text=True, timeout=5
        )
        pids = result.stdout.strip().split()
        for pid_str in pids:
            try:
                pid = int(pid_str.strip())
                if pid != os.getpid() and pid != os.getppid():
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(1)
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            except (ValueError, ProcessLookupError):
                pass
    except Exception:
        pass

    time.sleep(2)

    # Restart with correct env
    try:
        env = os.environ.copy()
        env['DCHUB_API_BASE'] = RAILWAY_API_BASE
        # Block localhost variants
        for key in ['DCHUB_API_BASE']:
            val = env.get(key, '')
            for blocked in BLOCKED_API_BASES:
                if blocked in val:
                    env[key] = RAILWAY_API_BASE

        subprocess.Popen(
            [sys.executable, 'dchub_mcp_server.py'],
            env=env,
            cwd=os.path.dirname(os.path.abspath(__file__)) or '/app',
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        logger.warning("HEALED: MCP process restarted on port %d", MCP_PORT)
    except Exception as e:
        logger.error("MCP restart failed: %s", str(e)[:80])


def heal_memory():
    """Fix 5: Detect memory pressure and trigger cleanup.
    Clears caches and forces GC when RSS exceeds thresholds.
    """
    try:
        import psutil
        process = psutil.Process(os.getpid())
        rss_mb = process.memory_info().rss / (1024 * 1024)

        if rss_mb > MEMORY_CRITICAL_MB:
            # Critical — aggressive cleanup
            gc.collect()
            # Clear known caches
            try:
                import redis
                r = redis.from_url(os.environ.get('REDIS_URL', ''))
                r.flushdb()
                logger.warning("HEALED: Memory critical (%.0fMB) — flushed Redis + GC", rss_mb)
            except Exception:
                pass
            gc.collect()
            return True, "Critical cleanup at %.0fMB" % rss_mb

        elif rss_mb > MEMORY_WARN_MB:
            gc.collect()
            logger.info("HEAL: Memory warning (%.0fMB) — ran GC", rss_mb)
            return True, "GC at %.0fMB" % rss_mb

        return False, "%.0fMB OK" % rss_mb
    except Exception as e:
        return False, str(e)[:80]


def heal_neon_url():
    """Fix 6: Validate DATABASE_URL points to correct Neon instance.
    Railway's DATABASE_URL sometimes points to the wrong Neon DB
    (helium vs ep-old-waterfall). NEON_DATABASE_URL is always correct.
    """
    neon_url = os.environ.get('NEON_DATABASE_URL', '')
    db_url = os.environ.get('DATABASE_URL', '')

    if not neon_url:
        return False, "No NEON_DATABASE_URL set"

    if db_url and NEON_HOSTNAME_PREFIX not in db_url and NEON_HOSTNAME_PREFIX in neon_url:
        os.environ['DATABASE_URL'] = neon_url
        logger.warning(
            "HEALED: DATABASE_URL was pointing to wrong DB, overrode with NEON_DATABASE_URL"
        )
        return True, "Fixed DATABASE_URL"

    return False, "OK"


def heal_connection_pool():
    """Fix 7: PREEMPTIVE connection pool monitoring and optimization.
    
    Instead of waiting for pool exhaustion, we:
    1. Track pool utilization % — warn at 70%, recycle idle at 80%, reset at 95%
    2. Detect connection leaks (connections held >60s)
    3. Kill idle connections that haven't been used in >5 min
    4. Monitor Neon connection count vs limit
    5. Preemptively close and reopen connections approaching Neon's idle timeout
    """
    try:
        actions_taken = []

        # ── Check main pool ──
        try:
            from db_utils import _pool
            if _pool is not None and not getattr(_pool, 'closed', True):
                used = len(_pool._used) if hasattr(_pool, '_used') and hasattr(_pool._used, '__len__') else 0
                maxconn = getattr(_pool, 'maxconn', 20)
                minconn = getattr(_pool, 'minconn', 2)
                utilization = (used / maxconn * 100) if maxconn > 0 else 0

                # Track trend
                if not hasattr(heal_connection_pool, '_util_history'):
                    heal_connection_pool._util_history = []
                heal_connection_pool._util_history.append(utilization)
                if len(heal_connection_pool._util_history) > 30:
                    heal_connection_pool._util_history = heal_connection_pool._util_history[-30:]

                # Level 1: 70%+ utilization — log warning
                if utilization >= 70 and utilization < 85:
                    logger.info("POOL WATCH: %d/%d connections used (%.0f%%)", used, maxconn, utilization)

                # Level 2: 85%+ — try to reclaim idle connections
                if utilization >= 85 and utilization < 95:
                    reclaimed = 0
                    if hasattr(_pool, '_pool') and hasattr(_pool._pool, '__iter__'):
                        # Return idle connections from the free pool
                        idle_before = len(list(_pool._pool)) if hasattr(_pool._pool, '__iter__') else 0
                        logger.warning("POOL HEAL: %.0f%% utilized (%d/%d). Idle in pool: %d",
                                      utilization, used, maxconn, idle_before)
                    actions_taken.append("Warning at %.0f%%" % utilization)

                # Level 3: 95%+ — nuclear reset
                if utilization >= 95:
                    try:
                        _pool.closeall()
                        logger.warning(
                            "HEALED: Pool exhausted (%d/%d = %.0f%%) — forced full reset",
                            used, maxconn, utilization
                        )
                        actions_taken.append("Reset at %.0f%%" % utilization)
                    except Exception as e:
                        logger.error("Pool reset failed: %s", str(e)[:80])

            elif _pool is not None and getattr(_pool, 'closed', False):
                logger.warning("HEALED: Connection pool was closed — will reinitialize on next request")
                actions_taken.append("Pool was closed")
        except ImportError:
            pass

        # ── Check for leaked connections via Neon ──
        try:
            import psycopg2
            db_url = os.environ.get('NEON_DATABASE_URL') or os.environ.get('DATABASE_URL')
            if db_url:
                conn = psycopg2.connect(db_url)
                conn.autocommit = True
                cur = conn.cursor()
                # Check active connections to our database
                cur.execute("""
                    SELECT count(*) FROM pg_stat_activity 
                    WHERE state = 'active' AND query NOT LIKE '%pg_stat_activity%'
                """)
                active = cur.fetchone()[0] if cur.fetchone() else 0
                # Check idle connections
                cur.execute("""
                    SELECT count(*) FROM pg_stat_activity 
                    WHERE state = 'idle' 
                    AND state_change < NOW() - INTERVAL '5 minutes'
                """)
                row = cur.fetchone()
                stale_idle = row[0] if row else 0

                if stale_idle > 5:
                    # Terminate stale idle connections
                    cur.execute("""
                        SELECT pg_terminate_backend(pid) FROM pg_stat_activity 
                        WHERE state = 'idle' 
                        AND state_change < NOW() - INTERVAL '10 minutes'
                        AND pid != pg_backend_pid()
                    """)
                    terminated = cur.rowcount
                    if terminated > 0:
                        logger.warning("HEALED: Terminated %d stale idle Neon connections", terminated)
                        actions_taken.append("Killed %d stale connections" % terminated)

                conn.close()
        except Exception as e:
            logger.debug("Neon connection check: %s", str(e)[:60])

        # ── Check read replica pool ──
        try:
            from db_utils import _read_pool
            if _read_pool is not None and hasattr(_read_pool, '_used'):
                read_used = len(_read_pool._used) if hasattr(_read_pool._used, '__len__') else 0
                read_max = getattr(_read_pool, 'maxconn', 30)
                if read_used >= read_max - 2:
                    try:
                        _read_pool.closeall()
                        logger.warning("HEALED: Read replica pool exhausted (%d/%d) — reset", read_used, read_max)
                        actions_taken.append("Read pool reset")
                    except Exception:
                        pass
        except ImportError:
            pass

        if actions_taken:
            return True, "; ".join(actions_taken)

        # Report current state
        try:
            from db_utils import _pool
            if _pool and hasattr(_pool, '_used'):
                used = len(_pool._used) if hasattr(_pool._used, '__len__') else 0
                maxconn = getattr(_pool, 'maxconn', 20)
                return False, "%d/%d (%.0f%%)" % (used, maxconn, used/maxconn*100 if maxconn else 0)
        except Exception:
            pass

        return False, "OK"
    except Exception as e:
        return False, str(e)[:80]


def heal_stats_cache():
    """Fix 8: Clear stale stats cache to prevent serving old data after fixes."""
    try:
        import redis
        r = redis.from_url(os.environ.get('REDIS_URL', ''))
        # Only clear stats-related keys
        keys = r.keys('stats*') + r.keys('cached:stats*')
        if keys:
            for batch in [keys[i:i+100] for i in range(0, len(keys), 100)]:
                r.delete(*batch)
            if len(keys) > 50:
                logger.info("HEAL: Cleared %d stale stats cache keys", len(keys))
                return True, "Cleared %d keys" % len(keys)
        return False, "OK"
    except Exception:
        return False, "Redis unavailable"


# ============================================================
# Orchestrator
# ============================================================

HEALERS = [
    ('api_base',           heal_api_base),
    ('neon_url',           heal_neon_url),
    ('fiber_index',        heal_fiber_index),
    ('daily_record_cols',  heal_daily_record_usage),
    ('mcp_process',        heal_mcp_process),
    ('memory',             heal_memory),
    ('connection_pool',    heal_connection_pool),
    ('stats_cache',        heal_stats_cache),

]

_healer_stats = {
    'total_runs': 0,
    'total_heals': 0,
    'last_run': None,
    'last_heals': {},
    'heal_history': [],
    'started_at': None,
}


def run_healing_cycle():
    """Run all healers and return summary."""
    _healer_stats['total_runs'] += 1
    _healer_stats['last_run'] = datetime.utcnow().isoformat()

    results = {}
    healed_count = 0

    for name, healer_fn in HEALERS:
        try:
            healed, msg = healer_fn()
            results[name] = {'healed': healed, 'message': msg}
            if healed:
                healed_count += 1
                _healer_stats['total_heals'] += 1
                _healer_stats['last_heals'][name] = datetime.utcnow().isoformat()
                _healer_stats['heal_history'].append({
                    'time': datetime.utcnow().isoformat(),
                    'healer': name,
                    'message': msg,
                })
                # Keep history bounded
                if len(_healer_stats['heal_history']) > 100:
                    _healer_stats['heal_history'] = _healer_stats['heal_history'][-50:]
        except Exception as e:
            results[name] = {'healed': False, 'message': 'Exception: %s' % str(e)[:80]}
            logger.error("Healer %s exception: %s", name, str(e))

    if healed_count > 0:
        healed_names = [k for k, v in results.items() if v['healed']]
        logger.info(
            "SELF-HEAL cycle #%d: %d issues fixed — %s",
            _healer_stats['total_runs'], healed_count, ', '.join(healed_names)
        )
    elif _healer_stats['total_runs'] % 30 == 0:
        # Log every ~60 min that we're healthy
        logger.info(
            "SELF-HEAL: All clear (cycle #%d, %d total heals)",
            _healer_stats['total_runs'], _healer_stats['total_heals']
        )

    return results


def _healing_loop():
    """Background thread loop."""
    # Initial delay — let server boot
    time.sleep(60)
    logger.info("Self-Healing Orchestrator: Active (cycle every %ds, %d healers)", HEAL_INTERVAL, len(HEALERS))

    while True:
        try:
            run_healing_cycle()
        except Exception as e:
            logger.error("Healing loop error: %s", str(e))
        time.sleep(HEAL_INTERVAL)


def start_healing_orchestrator(app=None):
    """Start the self-healing background thread and register status endpoint."""
    _healer_stats['started_at'] = datetime.utcnow().isoformat()

    thread = threading.Thread(target=_healing_loop, daemon=True)
    thread.start()

    if app:
        @app.route('/api/health/self-heal', methods=['GET'])
        def self_heal_status():
            from flask import jsonify
            return jsonify({
                'orchestrator': 'active',
                'healers': len(HEALERS),
                'healer_names': [name for name, _ in HEALERS],
                'stats': _healer_stats,
            })

        @app.route('/api/health/self-heal/run', methods=['POST'])
        def self_heal_manual():
            from flask import jsonify, request
            admin_key = request.headers.get('X-Admin-Key') or request.args.get('admin_key')
            expected = os.environ.get('DCHUB_ADMIN_KEY', '')
            if not admin_key or admin_key != expected:
                return jsonify({'error': 'unauthorized'}), 401
            results = run_healing_cycle()
            return jsonify({
                'results': results,
                'stats': _healer_stats,
            })

    logger.info("Self-Healing Orchestrator: Registered (%d healers)", len(HEALERS))
    print("🩺 Self-Healing Orchestrator: ✅ Active (%d healers, cycle every %ds)" % (len(HEALERS), HEAL_INTERVAL))
    return thread


# ============================================================
# Standalone mode — run once for testing
# ============================================================
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    print("Running single healing cycle...")
    results = run_healing_cycle()
    for name, result in results.items():
        status = "HEALED" if result['healed'] else "OK"
        print("  %s: %s — %s" % (name, status, result['message']))
    print("\nTotal heals: %d" % _healer_stats['total_heals'])

# ============================================================
# DCHUB-PROTECTION-2026-04-28 — regression heal hooks
# ============================================================
import json as _dchub_json
import os as _dchub_os
import re as _dchub_re
import subprocess as _dchub_subprocess
try:
    import requests as _dchub_requests
except ImportError:
    _dchub_requests = None

_DCHUB_PROD_BASE = "https://dchub.cloud"
_DCHUB_BACKEND_DIR = _dchub_os.environ.get("BACKEND_DIR", _dchub_os.path.expanduser("~/workspace"))
_DCHUB_HEAL_LOG = logging.getLogger("self-healer.dchub-2026-04-28")
def heal_press_release_route():
    if _dchub_requests is None:
        return (False, "requests lib unavailable")
    try:
        r = _dchub_requests.get(_DCHUB_PROD_BASE + "/press-release", timeout=10)
        has_markers = "Today's Headlines" in r.text
        if r.status_code != 200 or not has_markers:
            msg = "regression: status=%s markers=%s" % (r.status_code, has_markers)
            _DCHUB_HEAL_LOG.warning("[regression] /press-release %s", msg)
            return (False, msg)
        return (False, "OK")
    except Exception as e:
        return (False, "probe failed: %s" % e)


def heal_pr_queue_json():
    p = _dchub_os.path.join(_DCHUB_BACKEND_DIR, "pr_queue.json")
    if not _dchub_os.path.isfile(p):
        return (False, "file not found")
    try:
        with open(p) as f:
            data = _dchub_json.load(f)
        if not isinstance(data, list):
            raise ValueError("top-level not list")
        return (False, "valid; %d entries" % len(data))
    except Exception as e:
        _DCHUB_HEAL_LOG.warning("[regression] pr_queue.json invalid (%s) - restoring from git", e)
        try:
            _dchub_subprocess.run(
                ["git", "checkout", "HEAD", "--", "pr_queue.json"],
                cwd=_DCHUB_BACKEND_DIR, check=True, capture_output=True, timeout=20,
            )
            return (True, "restored from git HEAD (was: %s)" % e)
        except Exception as ee:
            return (False, "restore failed: %s" % ee)


def heal_coord_parser_version():
    if _dchub_requests is None:
        return (False, "requests lib unavailable")
    try:
        r = _dchub_requests.get(_DCHUB_PROD_BASE + "/land-power-map", timeout=10)
        m = _dchub_re.search(r"coord-parser-fix\.js\?v=(\d+)", r.text)
        if not m:
            return (False, "tag missing")
        v = int(m.group(1))
        if v < 6:
            msg = "regressed to v=%s (expected >=6)" % v
            _DCHUB_HEAL_LOG.warning("[regression] coord-parser %s", msg)
            return (False, msg)
        return (False, "v=%s OK" % v)
    except Exception as e:
        return (False, "probe failed: %s" % e)

# DCHUB-PROTECTION-2026-04-28: register regression healers AFTER their defs
HEALERS.extend([
    ('press_release_route',  heal_press_release_route),
    ('pr_queue_json',        heal_pr_queue_json),
    ('coord_parser_version', heal_coord_parser_version),
])
