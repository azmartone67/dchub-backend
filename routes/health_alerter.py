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
_RESTART_THRESHOLD  = int(os.environ.get("HEALTH_ALERT_RESTART_N", "5"))  # > a normal 2-replica deploy burst
_RATE_LIMIT_S       = int(os.environ.get("HEALTH_ALERT_RATE_LIMIT_S", "600"))  # 10 min between same-kind alerts

_last_sent = {}   # in-process rate limit: kind -> ts
_consec_high = 0  # consecutive high-pool readings (require 2 → ignore brief spikes)
_started = False


def _send_email(subject: str, html: str) -> bool:
    """Direct-HTTP email: SendGrid primary, Resend fallback. No DB, no pool."""
    sg = os.environ.get("SENDGRID_API_KEY", "").strip()
    if sg:
        try:
            payload = json.dumps({
                "personalizations": [{"to": [{"email": _TO}]}],
                "from": {"email": _FROM, "name": "DC Hub Health Alerter"},
                "subject": subject,
                "content": [{"type": "text/html", "value": html}],
            }).encode()
            r = urllib.request.Request("https://api.sendgrid.com/v3/mail/send", data=payload, method="POST")
            r.add_header("Authorization", f"Bearer {sg}")
            r.add_header("Content-Type", "application/json")
            urllib.request.urlopen(r, timeout=6)
            return True
        except Exception as e:
            log.warning("health_alerter: SendGrid send failed: %s", e)
    rk = (os.environ.get("RESEND_API_KEY") or os.environ.get("DCHUB_RESEND_API_KEY") or "").strip()
    if rk:
        try:
            payload = json.dumps({"from": f"DC Hub Alerts <{_FROM}>", "to": [_TO],
                                  "subject": subject, "html": html}).encode()
            r = urllib.request.Request("https://api.resend.com/emails", data=payload, method="POST")
            r.add_header("Authorization", f"Bearer {rk}")
            r.add_header("Content-Type", "application/json")
            r.add_header("User-Agent", "dchub-health-alerter/1.0")  # Resend/CF want a UA
            urllib.request.urlopen(r, timeout=6)
            return True
        except Exception as e:
            log.warning("health_alerter: Resend send failed: %s", e)
    log.warning("health_alerter: no email channel available (SENDGRID/RESEND key unset)")
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
    global _consec_high
    time.sleep(_INITIAL_DELAY_S)  # let boot noise settle (a brief boot spike isn't a problem)
    while True:
        try:
            time.sleep(_CHECK_EVERY_S)
            from main import get_pool_health
            h = get_pool_health() or {}
            pool = h.get("pool") or {}
            cb = h.get("circuit_breaker") or {}
            util = float(pool.get("utilization_pct") or 0)
            checked, mx = pool.get("checked_out"), pool.get("max_configured")
            if cb.get("open"):
                _alert("circuit_open", "🚨 DC Hub: DB circuit breaker OPEN",
                       f"<h2>DB circuit breaker is OPEN</h2>"
                       f"<p>The backend is failing fast on DB connections "
                       f"({cb.get('consecutive_failures')} consecutive failures). "
                       f"Pool {checked}/{mx} ({util}%).</p>"
                       f"<p>The site is likely degrading. Check Railway logs + "
                       f"<code>/api/health/db</code>.</p>")
                _consec_high = 0
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
        cur.execute("CREATE TABLE IF NOT EXISTS app_health_alerts "
                    "(kind TEXT, sent_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
        cur.execute("INSERT INTO app_health_boots DEFAULT VALUES")
        cur.execute("SELECT count(*) FROM app_health_boots "
                    "WHERE boot_at > NOW() - make_interval(mins => %s)", (_RESTART_WINDOW_MIN,))
        n = int(cur.fetchone()[0] or 0)
        if n >= _RESTART_THRESHOLD:
            cur.execute("SELECT max(sent_at) FROM app_health_alerts WHERE kind='restart_loop'")
            last = cur.fetchone()[0]
            recent = False
            if last:
                cur.execute("SELECT (NOW() - %s) < INTERVAL '10 minutes'", (last,))
                recent = bool(cur.fetchone()[0])
            if not recent:
                if _send_email("🚨 DC Hub backend RESTART LOOP",
                               f"<h2>Backend is restart-looping</h2>"
                               f"<p>{n} boots in the last {_RESTART_WINDOW_MIN} min "
                               f"(threshold {_RESTART_THRESHOLD}). The container is crash/restart-looping — "
                               f"likely a bad deploy, DB-pool exhaustion, or OOM.</p>"
                               f"<p>Check Railway deploy logs; consider rolling back the last deploy or "
                               f"flipping the relevant kill-switch.</p>"):
                    cur.execute("INSERT INTO app_health_alerts (kind) VALUES ('restart_loop')")
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
