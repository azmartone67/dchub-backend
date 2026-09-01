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
        cur.execute("INSERT INTO app_health_boots (deploy_id) VALUES (%s)", (deploy_id or None,))
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
                    cur.execute("INSERT INTO app_health_alerts (kind) VALUES ('restart_loop')")
                    log.warning("health_alerter: RESTART-LOOP alert sent (%s boots/%smin)", n, _RESTART_WINDOW_MIN)
        cur.execute("DELETE FROM app_health_boots WHERE boot_at < NOW() - INTERVAL '2 days'")
        cur.close()
        c.close()
    except Exception as e:
        log.warning("health_alerter restart-loop check: %s", e)


# ── 3) AI Gateway spend-block monitor (2026-09-01) ───────────────────────────
# WHY. On 2026-09-01 a Cloudflare AI Gateway spend rule ($100 per 604800s,
# sliding) went over and the gateway 429'd EVERY Anthropic request — every
# model, including a 1-token haiku call — for an unknown number of hours. The
# whole brain went quiet: no PR drafts, no narratives, no layer work. Nothing
# paged, because nothing was watching. It surfaced only when a human clicked
# approve and read "PR draft skipped: claude call failed: http_429".
#
# Cloudflare has no notification for this: its alert catalog carries no AI
# Gateway type at all, and under BYOK the spend is billed by Anthropic, so
# Cloudflare's own usage-billing alerts never see it. Hence a probe here.
#
# ★ THE PROBE SENDS A DELIBERATELY INVALID API KEY, and that is the point:
#   · it costs nothing — the request never reaches a model, so no tokens burn;
#   · no real credential goes over the wire;
#   · the gateway's spend rule fires BEFORE it authenticates upstream, so the
#     status alone separates the two states. Verified live on 2026-09-01:
#     blocked -> 429 {"name":"AiGatewayError","message":"Spend limit exceeded:
#     rule '5e7f1b6b' ..."}; after the cap was raised -> 401
#     authentication_error, i.e. the gateway forwarded to Anthropic.
# Sending the REAL key here would cost money on every probe and prove less.
_GW_DISABLED       = str(os.environ.get("GATEWAY_SPEND_MONITOR_DISABLE", "")).lower() in ("1", "true", "yes")
_GW_CHECK_EVERY_S  = int(os.environ.get("GATEWAY_SPEND_CHECK_S", "600"))      # 10 min; a spend block persists
_GW_RENOTIFY_S     = int(os.environ.get("GATEWAY_SPEND_RENOTIFY_S", "86400")) # nag once a day while still blocked
_GW_PROBE_MODEL    = os.environ.get("GATEWAY_SPEND_PROBE_MODEL", "claude-haiku-4-5")
_GW_INVALID_KEY    = "sk-ant-health-alerter-probe-not-a-real-key"

_gw_blocked_since = None   # ts of the first blocked reading of the current episode
_gw_last_notified = 0.0    # ts of the last block email (for the daily nag)


def _gateway_spend_state() -> tuple[str, str]:
    """Probe the AI Gateway with an INVALID key. Returns (state, detail).

    state is "blocked" (a spend/budget rule is refusing traffic), "open" (the
    gateway forwarded upstream and Anthropic rejected the bogus key, which is
    the healthy answer), or "unknown" (no gateway configured, or anything we
    cannot classify — never treated as either good or bad news).

    ★ An Anthropic 429 is NOT a spend block. A genuine per-model rate limit
    also arrives as 429, and paging the owner about it would train them to
    ignore this alarm — so the body must actually name the gateway rule.
    """
    import urllib.error as _uerr
    import urllib.request as _ureq
    try:
        from utils.anthropic_helper import anthropic_messages_url, gateway_active
        if not gateway_active():
            return "unknown", "no AI gateway configured (ANTHROPIC_BASE_URL is not a CF gateway)"
        url = anthropic_messages_url()
    except Exception as e:
        return "unknown", f"helper unavailable: {e!r}"
    payload = json.dumps({
        "model": _GW_PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode("utf-8")
    req = _ureq.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "X-API-Key": _GW_INVALID_KEY,      # ★ deliberately invalid — see above
        "Anthropic-Version": "2023-06-01",
        "User-Agent": "dchub-health-alerter/1.0",
    })
    try:
        _ureq.urlopen(req, timeout=20).read()
        # A bogus key authenticating means the probe is not probing what it
        # thinks it is. Say so rather than reporting a healthy gateway.
        return "unknown", "invalid-key probe returned 200 — probe no longer valid"
    except _uerr.HTTPError as e:
        detail = ""
        try:
            detail = " ".join((e.read() or b"").decode("utf-8", "replace").split())[:300]
        except Exception:
            pass
        low = detail.lower()
        if e.code == 429 and ("spend limit" in low or "aigatewayerror" in low
                              or "budget limit" in low):
            return "blocked", f"http_429: {detail}"
        if e.code in (401, 403):
            return "open", f"http_{e.code} — gateway forwarded upstream"
        return "unknown", f"http_{e.code}: {detail}"
    except Exception as e:
        return "unknown", f"probe failed: {e!r}"


def _gateway_alert_decision(state, blocked_since, last_notified, now):
    """Pure transition logic. Returns (action, blocked_since, last_notified).

    action is "block" (page: newly blocked, or the daily nag while still
    blocked), "clear" (it recovered — worth knowing, and it re-arms the alarm),
    or None. "unknown" changes NOTHING: a probe that could not classify must
    not clear a live block, which would silently disarm the alarm mid-outage.
    """
    if state == "blocked":
        first = blocked_since is None
        if first:
            blocked_since = now
        if first or (now - last_notified) >= _GW_RENOTIFY_S:
            return "block", blocked_since, now
        return None, blocked_since, last_notified
    if state == "open" and blocked_since is not None:
        return "clear", None, 0.0
    return None, blocked_since, last_notified


def _gateway_spend_loop():
    global _gw_blocked_since, _gw_last_notified
    time.sleep(_INITIAL_DELAY_S)
    while True:
        try:
            state, detail = _gateway_spend_state()
            now = time.time()
            action, _gw_blocked_since, _gw_last_notified = _gateway_alert_decision(
                state, _gw_blocked_since, _gw_last_notified, now)
            if action == "block":
                mins = int((now - (_gw_blocked_since or now)) / 60)
                _alert("gateway_spend_block",
                       "🚨 DC Hub: AI Gateway is REFUSING all Claude calls (spend rule)",
                       f"<h2>The AI gateway's spend rule is blocking every model</h2>"
                       f"<p>Every Anthropic call from the backend and the worker is "
                       f"being refused by Cloudflare <b>before it reaches Anthropic</b> "
                       f"— the brain cannot draft PRs, write narratives, or run any "
                       f"layer. Blocked for ~{mins} min.</p>"
                       f"<p><b>Gateway said:</b><br><code>{detail}</code></p>"
                       f"<p>Fix: Cloudflare dashboard &rarr; AI &rarr; AI Gateway &rarr; "
                       f"your gateway &rarr; Settings &rarr; spend limits, and raise or "
                       f"clear the rule named above. Nothing in the code can lift it.</p>")
            elif action == "clear":
                _alert("gateway_spend_clear",
                       "✅ DC Hub: AI Gateway spend block cleared",
                       "<h2>Claude calls are getting through again</h2>"
                       "<p>The gateway now forwards to Anthropic; the brain should "
                       "resume on its own. No action needed.</p>")
        except Exception as e:
            log.warning("health_alerter gateway-spend probe: %s", e)
        time.sleep(_GW_CHECK_EVERY_S)


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
    # The gateway probe only makes sense when a gateway is actually in front of
    # Anthropic; without one there is no spend rule that could block anything.
    _gw_on = False
    if not _GW_DISABLED:
        try:
            from utils.anthropic_helper import gateway_active
            _gw_on = bool(gateway_active())
        except Exception:
            _gw_on = False
    if _gw_on:
        threading.Thread(target=_gateway_spend_loop, daemon=True,
                         name="gateway-spend-alerter").start()
    log.info("health_alerter: started (pool alert >=%s%% sustained, restart-loop >=%s/%smin, "
             "gateway-spend probe %s, to=%s)",
             _POOL_UTIL_ALERT, _RESTART_THRESHOLD, _RESTART_WINDOW_MIN,
             ("every %ss" % _GW_CHECK_EVERY_S) if _gw_on else "off", _TO)
