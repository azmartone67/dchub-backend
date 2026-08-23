"""
cron_observability.py — log heartbeat calls + alert when external cron stops.

Phase ZZZZZ-round47.18 (2026-05-26). All 4 LinkedIn quad slots + the
weekly partnership LinkedIn + Tuesday partnership press release + every
other scheduled job depend on something OUTSIDE the app hitting
/api/v1/cron/heartbeat every 5 minutes. If that external cron stops,
everything silently dies.

This blueprint:
  1. Listens to a Flask `before_request` hook for /api/v1/cron/heartbeat
     and writes the timestamp + UA into cron_heartbeat_log.
  2. Exposes /api/v1/cron/last-fired returning the last 10 fires +
     time-since-last-fire, so the operator can see at a glance if the
     external scheduler is alive.

Endpoint:
  GET /api/v1/cron/last-fired
    → {
        "last_fire_at": "...",
        "minutes_since": 4.2,
        "healthy":       true,    # false if > 10 min stale
        "fires_today":   147,
        "recent": [...],
      }
"""
import os
import datetime
from contextlib import contextmanager
from flask import Blueprint, jsonify, request
from routes._swallowed_writes import note_swallowed_write

try:
    import psycopg2 as _pg
except Exception:
    _pg = None

cron_observability_bp = Blueprint("cron_observability", __name__)

STALE_THRESHOLD_MIN = 10  # external cron should fire every 5min; 10 = sick


def _dsn():
    return os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL") or ""


@contextmanager
def _conn():
    c = _pg.connect(_dsn())
    c.autocommit = True
    try: yield c
    finally: c.close()


def _ensure_table():
    if not (_pg and _dsn()):
        return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cron_heartbeat_log (
                    id          SERIAL PRIMARY KEY,
                    fired_at    TIMESTAMPTZ DEFAULT NOW(),
                    user_agent  TEXT,
                    source_ip   TEXT,
                    jobs_run    INT,
                    jobs_total  INT,
                    elapsed_ms  INT
                );
                CREATE INDEX IF NOT EXISTS ix_chl_ts ON cron_heartbeat_log(fired_at DESC);
            """)
    except Exception:
        pass


_ensure_table()


def log_heartbeat(jobs_run=None, jobs_total=None, elapsed_ms=None, ua=None, ip=None):
    """Called by the cron_heartbeat handler after each run.

    FIX 2026-07-03: the 2026-06-08 async-dispatch change moved this call into a
    background thread (the heartbeat fires jobs concurrently, then logs), where
    Flask's `request` proxy is OUT OF CONTEXT — so reading request.headers here
    threw RuntimeError every time, the bare except swallowed it, and NOTHING was
    logged for ~24 days. /api/v1/cron/last-fired then read empty and reported
    healthy=false ("cron dead since 06-08") while the GitHub-Actions heartbeat
    was actually firing fine. The caller now captures ua/ip in the request
    thread and passes them in; request access is only a best-effort fallback.
    Wrapped in try/except so a log failure never breaks the dispatch."""
    try:
        if ua is None or ip is None:
            try:
                if ua is None:
                    ua = request.headers.get("User-Agent", "") or ""
                if ip is None:
                    ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or ""
            except Exception:
                pass
        ua = (ua or "")[:200]
        ip = (ip or "")[:80]
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                INSERT INTO cron_heartbeat_log
                  (user_agent, source_ip, jobs_run, jobs_total, elapsed_ms)
                VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING
            """, (ua, ip, jobs_run, jobs_total, elapsed_ms))
    except Exception:
        note_swallowed_write("cron_heartbeat_log", where="cron_observability.log_heartbeat")
        pass


@cron_observability_bp.route("/api/v1/cron/last-fired", methods=["GET"])
def last_fired():
    if not (_pg and _dsn()):
        return jsonify({"error": "no_db"}), 503
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT MAX(fired_at), COUNT(*)
                  FROM cron_heartbeat_log
                 WHERE fired_at::date = CURRENT_DATE
            """)
            row = cur.fetchone()
            last_fire = row[0]
            fires_today = int(row[1] or 0)

            cur.execute("""
                SELECT fired_at, user_agent, jobs_run, jobs_total, elapsed_ms
                  FROM cron_heartbeat_log
                 ORDER BY fired_at DESC LIMIT 10
            """)
            recent = [{
                "fired_at": r[0].isoformat() if r[0] else None,
                "user_agent": (r[1] or "")[:80],
                "jobs_run": r[2], "jobs_total": r[3],
                "elapsed_ms": r[4],
            } for r in cur.fetchall()]

            cur.execute("""
                SELECT user_agent, COUNT(*)
                  FROM cron_heartbeat_log
                 WHERE fired_at > NOW() - INTERVAL '24 hours'
                 GROUP BY user_agent ORDER BY 2 DESC LIMIT 5
            """)
            by_ua = [{"user_agent": (r[0] or "(none)")[:80], "fires_24h": int(r[1])}
                     for r in cur.fetchall()]

        now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc) if last_fire and last_fire.tzinfo else datetime.datetime.utcnow()
        minutes_since = None
        healthy = False
        if last_fire:
            try:
                # both aware or both naive
                if last_fire.tzinfo and not now.tzinfo:
                    now = now.replace(tzinfo=datetime.timezone.utc)
                elif now.tzinfo and not last_fire.tzinfo:
                    last_fire = last_fire.replace(tzinfo=datetime.timezone.utc)
                minutes_since = round((now - last_fire).total_seconds() / 60.0, 1)
                healthy = (minutes_since is not None and minutes_since <= STALE_THRESHOLD_MIN)
            except Exception:
                pass

        return jsonify({
            "last_fire_at":  last_fire.isoformat() if last_fire else None,
            "minutes_since": minutes_since,
            "healthy":       healthy,
            "stale_threshold_min": STALE_THRESHOLD_MIN,
            "fires_today":   fires_today,
            "by_ua_24h":     by_ua,
            "recent":        recent,
            "hint":          ("If healthy=false, the external scheduler hitting "
                              "/api/v1/cron/heartbeat has stopped. Common sources: "
                              "Railway service cron, GitHub Actions, cron-job.org, "
                              "EasyCron. Restart whichever was wired."),
        }), 200, {
            # r47.18.1: no-store so CF edge doesn't cache "healthy=false" from
            # before cron was wired and serve it forever after. This endpoint
            # is a real-time health probe; it MUST always hit Flask.
            "Cache-Control":     "no-store, no-cache, must-revalidate, max-age=0",
            "CDN-Cache-Control": "no-store",
            "Surrogate-Control": "no-store",
        }
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {str(e)[:140]}"}), 500
