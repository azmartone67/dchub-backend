"""
health_alerter.py — proactive health escalation (2026-06-19).

The backend already DETECTS trouble (75% pool warning, slow-request logs, the
60s forced-reclaim, the circuit breaker, the watchdog) but only LOGGED it — so a
DB-pool-exhaustion outage was discovered by a human finding a 404, and the
watchdog restart-looped with nobody paged. This closes the ESCALATION gap with
two independent, fail-soft escalators that EMAIL the admin (no new infra —
reuses SENDGRID_API_KEY / RESEND_API_KEY + ADMIN_ALERT_EMAIL, the same path the
Neon boot-guard uses):

  1. POOL/HEALTH MONITOR (in-process thread): every ~60s reads get_pool_health()
     IN-MEMORY (no DB query, no pool checkout) and emails the moment DB-pool
     utilization stays high or the circuit breaker opens — WHILE the backend is
     still alive, minutes before the watchdog cascade.

  2. RESTART-LOOP DETECTOR (boot-time): each boot records itself via a DIRECT DB
     connection (NOT the contended pool) and, if >= N boots happened in the last
     M minutes, emails "backend is restart-looping" — this reaches the admin
     DURING a loop (every boot has a send window), the exact case that made the
     2026-06-19 outage drag on.

Both send by DIRECT HTTP (bypassing the DB-backed email queue, since the DB/pool
is the very thing that may be failing). Fully wrapped — a monitor must never
crash or slow the app. Kill switch: HEALTH_ALERTER_DISABLE=1.

The "site is fully down / can't send its own email" case is covered separately
by the external GitHub-Actions uptime probe (.github/workflows/uptime-probe.yml).
"""
from __future__ import annotations
import os, time, json, threading, logging, urllib.request

log = logging.getLogger("health_alerter")

_DISABLED = str(os.environ.get("HEALTH_ALERTER_DISABLE", "")).lower() in ("1", "true", "yes")
_TO   = (os.environ.get("ADMIN_ALERT_EMAIL") or os.environ.get("DCHUB_ADMIN_EMAIL") or "azmartone@gmail.com").strip()
_FROM = os.environ.get("SENDGRID_FROM_EMAIL", "alerts@dchub.cloud").strip()

_POOL_UTIL_ALERT    = float(os.environ.get("HEALTH_ALERT_POOL_PCT", "85"))
_CHECK_EVERY_S      = int(os.environ.get("HEALTH_ALERT_INTERVAL_S", "60"))
_INITIAL_DELAY_S    = int(os.environ.get("HEALTH_ALERT_INITIAL_DELAY_S", "90"))
_RESTART_WINDOW_MIN = int(os.environ.get("HEALTH_ALERT_RESTART_WINDOW_MIN", "15"))
_RESTART_THRESHOLD  = int(os.environ.get("HEALTH_ALERT_RESTART_N", "4"))  # distinct RESTART EVENTS (one deploy = 1)
_RATE_LIMIT_S       = int(os.environ.get("HEALTH_ALERT_RATE_LIMIT_S", "600"))  # 10 min between same-kind alerts

# Auto-recovery (shadow→arm, like everything else here). The existing HealthMonitor
# resets the pool only on DB-UNREACHABLE; the 2026-06-19 outage was the pool FULL
# while the DB was fine (1802 conns free), so it slipped past. This catches that:
# critical utilization + forced_reclaims RISING = stuck/leaked connections → reset
# helps; reclaims FLAT = a flood → reset is futile, so alert "find the source"
# instead. The reset itself only fires when ARMED (default OFF — observe first).
_AUTORESET_PCT     = float(os.environ.get("HEALTH_AUTORESET_PCT", "95"))
_AUTORESET_ENABLE  = str(os.environ.get("HEALTH_AUTORESET_ENABLE", "")).lower() in ("1", "true", "yes")
_AUTORESET_GAP_S   = int(os.environ.get("HEALTH_AUTORESET_GAP_S", "300"))  # ≥5 min between auto-resets

_last_sent = {}        # in-process rate limit: kind -> ts
_consec_high = 0       # consecutive high-pool readings (require 2 → ignore brief spikes)
_last_reclaims = None  # forced_reclaims at last check (rise = stuck/leaked conns)
_last_reset = 0.0      # ts of the last auto-reset (rate limit)
_started = False


def _send_email(subject: str, html: str) -> bool:
    """Email the admin via the shared Resend-first helper (one path, not a 2nd copy).
    The helper is stdlib+HTTP only — DB-independent, so it's safe to call during the
    pool exhaustion this alerter exists to report."""
    try:
        from email_fallback import send_email_resilient
        return send_email_resilient(_TO, subject, html_content=html,
                                    from_email=_FROM, from_name="DC Hub Health Alerter")
    except Exception as e:
        log.warning("health_alerter: send failed: %s", e)
        return False


def _alert(kind: str, subject: str, html: str):
    now = time.time()
    if now - _last_sent.get(kind, 0) < _RATE_LIMIT_S:
        return
    if _send_email(subject, html):
        _last_sent[kind] = now
        log.warning("health_alerter: ALERT sent [%s] %s", kind, subject)


# ── 1) pool / circuit-breaker monitor (in-process, in-memory reads) ──────────
def _monitor_loop():
    global _consec_high, _last_reclaims, _last_reset
    time.sleep(_INITIAL_DELAY_S)  # let boot noise settle (a brief boot spike isn't a problem)
    while True:
        try:
            time.sleep(_CHECK_EVERY_S)
            from main import get_pool_health
            h = get_pool_health() or {}
            pool = h.get("pool") or {}
            cb = h.get("circuit_breaker") or {}
            stats = h.get("stats") or {}
            util = float(pool.get("utilization_pct") or 0)
            checked, mx = pool.get("checked_out"), pool.get("max_configured")
            reclaims = int(stats.get("forced_reclaims") or 0)
            reclaim_delta = (reclaims - _last_reclaims) if _last_reclaims is not None else 0
            _last_reclaims = reclaims
            stuck = reclaim_delta > 0  # connections being force-reclaimed = leaked/stuck → reset frees them

            if cb.get("open"):
                _alert("circuit_open", "🚨 DC Hub: DB circuit breaker OPEN",
                       f"<h2>DB circuit breaker is OPEN</h2>"
                       f"<p>The backend is failing fast on DB connections "
                       f"({cb.get('consecutive_failures')} consecutive failures). "
                       f"Pool {checked}/{mx} ({util}%).</p>"
                       f"<p>The site is likely degrading. Check Railway logs + "
                       f"<code>/api/health/db</code>.</p>")
                _consec_high = 0
            elif util >= _AUTORESET_PCT:
                # CRITICAL. Diagnose stuck (reset helps) vs flood (reset is futile).
                _consec_high += 1
                if _consec_high >= 2:
                    import time as _t
                    now = _t.time()
                    if stuck and _AUTORESET_ENABLE and (now - _last_reset) > _AUTORESET_GAP_S:
                        try:
                            from main import _reset_all_pools
                            _reset_all_pools()  # proven HealthMonitor reset — frees stuck/leaked conns
                            _last_reset = now
                            log.warning("health_alerter: AUTO-RESET pool (util=%s reclaims+=%s)", util, reclaim_delta)
                            _alert("autoreset", f"🔧 DC Hub: AUTO-RESET stuck DB pool (was {util}%, +{reclaim_delta} reclaims)",
                                   f"<h2>Auto-recovered: reset the DB pool</h2>"
                                   f"<p>Pool was {util}% ({checked}/{mx}) with connections being force-reclaimed "
                                   f"(+{reclaim_delta}) — i.e. stuck/leaked. The brain reset the pool (the proven "
                                   f"HealthMonitor reset) to free them. If this repeats, find the leak.</p>")
                        except Exception as _re:
                            log.warning("health_alerter: auto-reset failed: %s", _re)
                    elif stuck:
                        _alert("pool_stuck", f"🚨 DC Hub: DB pool {util}% STUCK ({checked}/{mx}, +{reclaim_delta} reclaims)",
                               f"<h2>DB pool critical + STUCK: {util}%</h2>"
                               f"<p>{checked}/{mx} checked out, connections being force-reclaimed "
                               f"(+{reclaim_delta}) = leaked/stuck. A pool reset would free them — the brain "
                               f"<b>would auto-reset</b> here but auto-recovery is OFF. Arm with "
                               f"<code>HEALTH_AUTORESET_ENABLE=1</code> once you've seen it diagnose correctly, "
                               f"or reset now via <code>/api/health/db</code> tooling.</p>")
                    else:
                        _alert("pool_flood", f"🚨 DC Hub: DB pool {util}% (FLOOD — reclaims flat)",
                               f"<h2>DB pool critical: {util}% — looks like a FLOOD</h2>"
                               f"<p>{checked}/{mx} checked out but reclaims are flat — connections are churning "
                               f"fast (a load flood), not stuck. A pool reset would NOT help (it re-fills "
                               f"instantly). Find + shed the load SOURCE (a hot endpoint / a caller hammering "
                               f"the backend) — this is what the 2026-06-19 outage was.</p>")
            elif util >= _POOL_UTIL_ALERT:
                _consec_high += 1
                if _consec_high >= 2:  # sustained, not a blip
                    _alert("pool_high", f"⚠️ DC Hub: DB pool at {util}% ({checked}/{mx})",
                           f"<h2>DB pool utilization high: {util}%</h2>"
                           f"<p>{checked}/{mx} connections checked out (alert at {_POOL_UTIL_ALERT}%, "
                           f"sustained &gt;{_CHECK_EVERY_S}s).</p>"
                           f"<p>This is the EARLY warning before a connection-pool exhaustion / watchdog "
                           f"restart loop. See what's holding connections at "
                           f"<code>/api/admin/pool-status</code> before it cascades.</p>")
            else:
                _consec_high = 0
        except Exception as e:
            log.warning("health_alerter monitor loop: %s", e)


# ── 2) restart-loop detector (boot-time, DIRECT connection — bypasses the pool) ─
def _check_restart_loop():
    try:
        import psycopg2
        url = os.environ.get("DATABASE_URL")
        if not url:
            return
        c = psycopg2.connect(url, connect_timeout=10)  # DIRECT — not the (maybe-exhausted) pool
        c.autocommit = True
        cur = c.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS app_health_boots "
                    "(id BIGSERIAL PRIMARY KEY, boot_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        cur.execute("ALTER TABLE app_health_boots ADD COLUMN IF NOT EXISTS deploy_id TEXT")
        cur.execute("CREATE TABLE IF NOT EXISTS app_health_alerts "
                    "(kind TEXT, sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        # THE discriminator: a DEPLOY gets a NEW Railway deployment id each time;
        # a CRASH LOOP restarts the SAME deployment. So we only count restarts that
        # share THIS deployment's id — operator deploy bursts (each a fresh id) can
        # never trip it, only one image restarting repeatedly does. (The first two
        # versions counted raw boots / total events and false-fired on my own
        # rapid deploy session, 2026-06-19.)
        deploy_id = (os.environ.get("RAILWAY_DEPLOYMENT_ID")
                     or os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "").strip()
        cur.execute("INSERT INTO app_health_boots (deploy_id) VALUES (%s) ON CONFLICT DO NOTHING", (deploy_id or None,))
        n = 0
        if deploy_id:
            # distinct restart events (boots <60s apart collapse to 1) for THIS deploy_id
            cur.execute("""WITH b AS (
                             SELECT boot_at, LAG(boot_at) OVER (ORDER BY boot_at) AS prev
                             FROM app_health_boots
                             WHERE deploy_id = %s AND boot_at > NOW() - make_interval(mins => %s))
                           SELECT count(*) FROM b
                           WHERE prev IS NULL OR (boot_at - prev) > INTERVAL '60 seconds'""",
                        (deploy_id, _RESTART_WINDOW_MIN))
            n = int(cur.fetchone()[0] or 0)
        if deploy_id and n >= _RESTART_THRESHOLD:
            cur.execute("SELECT max(sent_at) FROM app_health_alerts WHERE kind='restart_loop'")
            last = cur.fetchone()[0]
            recent = False
            if last:
                cur.execute("SELECT (NOW() - %s) < INTERVAL '10 minutes'", (last,))
                recent = bool(cur.fetchone()[0])
            if not recent:
                if _send_email("🚨 DC Hub backend RESTART LOOP",
                               f"<h2>Backend is restart-looping</h2>"
                               f"<p>The SAME deployment ({deploy_id[:12]}…) has restarted {n} times in the "
                               f"last {_RESTART_WINDOW_MIN} min (threshold {_RESTART_THRESHOLD}). This is a real "
                               f"crash/restart loop — NOT a deploy burst (those get new ids) — likely DB-pool "
                               f"exhaustion, OOM, or a boot crash.</p>"
                               f"<p>Check Railway deploy logs; consider rolling back or flipping a kill-switch.</p>"):
                    cur.execute("INSERT INTO app_health_alerts (kind) VALUES ('restart_loop') ON CONFLICT DO NOTHING")
                    log.warning("health_alerter: RESTART-LOOP alert sent (%s boots/%smin)", n, _RESTART_WINDOW_MIN)
        cur.execute("DELETE FROM app_health_boots WHERE boot_at < NOW() - INTERVAL '2 days'")
        cur.close()
        c.close()
    except Exception as e:
        log.warning("health_alerter restart-loop check: %s", e)


def start():
    """Idempotent. Records this boot + starts the pool monitor thread."""
    global _started
    if _DISABLED:
        log.info("health_alerter: disabled via HEALTH_ALERTER_DISABLE")
        return
    if _started:
        return
    _started = True
    try:
        _check_restart_loop()
    except Exception:
        pass
    threading.Thread(target=_monitor_loop, daemon=True, name="health-alerter").start()
    log.info("health_alerter: started (pool alert >=%s%% sustained, restart-loop >=%s/%smin, to=%s)",
             _POOL_UTIL_ALERT, _RESTART_THRESHOLD, _RESTART_WINDOW_MIN, _TO)
