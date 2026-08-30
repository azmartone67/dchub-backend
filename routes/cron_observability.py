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
                VALUES (%s, %s, %s, %s, %s)
            """, (ua, ip, jobs_run, jobs_total, elapsed_ms))
    except Exception:
        note_swallowed_write("cron_heartbeat_log", where="cron_observability.log_heartbeat")
        pass


# ── per-job dispatch outcomes (r-cron-outcome 2026-08-29) ─────────────
# The heartbeat dispatches ~60 jobs per fire and, until now, observed NONE of
# them. cron_heartbeat._run() did `ex.submit(_hit, url, method)` and never read
# the future, so _hit's return value went nowhere; _hit itself had already
# thrown the response BODY away (`resp.read(512)` -> {"status","bytes"}).
#
# The consequence is one class of silence with three faces, all reported as a
# successful run by /api/v1/cron/last-fired's jobs_run:
#   · HTTP 500 from a job                      -> invisible
#   · a job that timed out                     -> invisible
#   · HTTP 200 carrying {"ok":false,...}       -> invisible
#
# That third one is the expensive one, because our endpoints self-report:
# brain_fix_verify_sweep answers {"ok":false,"disabled":true} with HTTP 200
# whenever BRAIN_FIX_VERIFY!=1, so an unarmed verifier fires twice a day
# forever and every dashboard says the job ran.
#
# ★ ONLY NON-OK OUTCOMES ARE WRITTEN. A green job writes nothing, so a healthy
# system writes ~0 rows/day and the TABLE ITSELF IS THE ALERT — no threshold to
# tune, no rollup to read. It also keeps volume sane: recording every job every
# fire would be ~17k rows/day (60 jobs x 288 fires).
# Kinds that mean THE JOB DID NOT DO ITS WORK. `healthy` is computed from
# these alone, and they are the reason this table exists.
CRON_FAILURE_KINDS = ("unreachable", "http_error", "disarmed", "skipped",
                      "self_reported_failure")
# ★ Recorded and readable, but NOT a failure. `dispatch_timeout` says the
# dispatcher stopped waiting — the request was ACCEPTED (BASE is loopback, so
# a dead port would have been ECONNREFUSED in microseconds) and the handler
# runs to completion whether or not anyone reads the response. Proven
# 2026-08-30: iso_queue_ingest_daily was logged "unreachable" at 06:03:33Z and
# finished writing 10 of 10 ISOs by 06:03:47Z. Keeping the row preserves a
# real signal (a handler over 30s on web is pool pressure); keeping it out of
# `healthy` stops it asserting a failure that did not happen.
# See routes/cron_heartbeat._hit.
CRON_INFO_KINDS = ("dispatch_timeout",)
CRON_OUTCOME_KINDS = CRON_FAILURE_KINDS + CRON_INFO_KINDS


def _ensure_outcome_table():
    if not (_pg and _dsn()):
        return
    try:
        with _conn() as c, c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cron_job_outcomes (
                    id          SERIAL PRIMARY KEY,
                    seen_at     TIMESTAMPTZ DEFAULT NOW(),
                    label       TEXT NOT NULL,
                    outcome     TEXT NOT NULL,
                    http_status INT,
                    detail      TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_cjo_ts ON cron_job_outcomes(seen_at DESC);
                CREATE INDEX IF NOT EXISTS ix_cjo_label ON cron_job_outcomes(label, seen_at DESC);
            """)
    except Exception:
        pass


_ensure_outcome_table()


def record_job_outcomes(rows):
    """Persist NON-OK dispatch outcomes. `rows` is an iterable of dicts with
    label / outcome / status / detail — anything whose outcome is "ok" or
    unrecognised is dropped here rather than at the call site, so the caller
    can hand over its whole batch.

    ONE connection for the batch (this runs in the heartbeat's daemon thread;
    a connection per row would churn the pool for no reason). Never raises —
    a failed diagnostic write must not break the dispatch it is observing."""
    try:
        payload = []
        for r in rows or []:
            try:
                outcome = (r.get("outcome") or "").strip()
                if outcome not in CRON_OUTCOME_KINDS:
                    continue
                payload.append(((r.get("label") or "")[:120], outcome,
                                r.get("status"), (r.get("detail") or "")[:400]))
            except Exception:
                continue
        if not payload:
            return 0
        with _conn() as c, c.cursor() as cur:
            # ON CONFLICT DO NOTHING with no target: this is an append-only
            # log with no natural key, so nothing can conflict today. It is
            # here so that if a constraint is ever added to this table, the
            # write DEGRADES instead of raising — an observability write must
            # never be able to break the dispatch it is observing.
            # ★ ONE string literal, not two adjacent ones: regression_lint's
            # insert-no-on-conflict rule scans the FILE TEXT with
            # `INSERT\s+INTO\s+(\w+)[^;"']*`, which stops at the first quote.
            # Split across two literals the ON CONFLICT lands outside the match
            # and the rule reports the insert as unguarded (it did).
            cur.executemany("""
                INSERT INTO cron_job_outcomes (label, outcome, http_status, detail)
                VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING""", payload)
        return len(payload)
    except Exception:
        note_swallowed_write("cron_job_outcomes",
                             where="cron_observability.record_job_outcomes")
        return 0


# ★ TWO paths, deliberately. Cloudflare CACHES /api/v1/cron/* and rewrites the
# origin's Cache-Control on the way out, so the `no-store` set below never
# reaches the client and three reads measured MISS -> HIT -> HIT with `age`
# climbing (2026-08-30) — a table whose whole purpose is "what is failing RIGHT
# NOW" was being answered from the edge. No origin-side header can fix that,
# because the edge is the thing overwriting them. /api/v1/brain/* already
# carries the bypass and measures cf-cache-status: DYNAMIC on every read, so
# the brain-prefixed path is the one to READ. The /cron/ path is retained so
# nothing that already points at it breaks.
@cron_observability_bp.route("/api/v1/brain/cron-job-outcomes", methods=["GET"])
@cron_observability_bp.route("/api/v1/cron/job-outcomes", methods=["GET"])
def job_outcomes():
    """Cron jobs that did NOT do their work, newest first.

    An EMPTY list is the healthy state — only non-ok outcomes are recorded.
    `by_label` counts the window so a job failing every fire is obvious
    without reading the rows.

    ?hours=24 (1..720)   window
    ?label=<job>         one job only
    """
    try:
        hours = max(1, min(720, int(request.args.get("hours", "24"))))
    except Exception:
        hours = 24
    label = (request.args.get("label") or "").strip() or None
    if not (_pg and _dsn()):
        return jsonify(ok=False, error="db_unavailable"), 200
    try:
        with _conn() as c, c.cursor() as cur:
            sql = ("SELECT seen_at, label, outcome, http_status, detail"
                   "  FROM cron_job_outcomes"
                   " WHERE seen_at >= NOW() - make_interval(hours => %s)")
            args = [hours]
            if label:
                sql += " AND label = %s"
                args.append(label)
            sql += " ORDER BY seen_at DESC LIMIT 200"
            cur.execute(sql, tuple(args))
            rows = [{"at": str(r[0]), "label": r[1], "outcome": r[2],
                     "http_status": r[3], "detail": r[4]} for r in cur.fetchall()]
            cur.execute(
                "SELECT label, outcome, count(*) FROM cron_job_outcomes"
                " WHERE seen_at >= NOW() - make_interval(hours => %s)"
                " GROUP BY label, outcome ORDER BY 3 DESC", (hours,))
            by_label = [{"label": r[0], "outcome": r[1], "count": int(r[2])}
                        for r in cur.fetchall()]
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}"), 200
    # ★ `healthy` reads FAILURE kinds only. An informational row (today:
    # dispatch_timeout) records that we stopped waiting for a verdict, not
    # that a job failed — letting it flip this boolean is exactly the
    # cried-wolf shape this sensor has now shipped three times.
    failing = [r for r in by_label if r["outcome"] in CRON_FAILURE_KINDS]
    resp = jsonify(ok=True, window_hours=hours, count=len(rows),
                   healthy=(len(failing) == 0),
                   failing_labels=len(failing),
                   note=("Rows are outcomes that were not ok. `healthy` counts "
                         "FAILURE kinds only — informational kinds "
                         f"({', '.join(CRON_INFO_KINDS)}) are recorded but "
                         "never flip it."),
                   by_label=by_label, outcomes=rows)
    resp.headers["Cache-Control"] = "no-store"
    return resp, 200


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
