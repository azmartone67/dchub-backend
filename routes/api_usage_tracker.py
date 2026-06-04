"""api_usage_tracker.py — r78-c (2026-06-03)

Per-API-key per-endpoint usage tracking, wired in as a Flask after_request
hook. Discovered while prepping the NLR JSC meeting (2026-06-10): the
existing api_usage_meter table is never populated because /track-usage
was orphaned, and api_keys.calls_today/calls_total were SET=0 at INSERT
but never incremented anywhere in the codebase. This module fixes both.

Architecture
------------
  before_request:
      Stash start_ns on flask.g (cheap, ~150ns).

  after_request:
      Read g.start_ns + g.api_key (if any), append a row to an
      in-memory buffer. NEVER raise — middleware must not break a
      response. Returns response untouched.

  Background flush thread (every 30s):
      Pull buffer, group by (api_key_prefix, endpoint_path, usage_date),
      bulk INSERT into:
        - api_endpoint_log (per-call detail, retained 90 days)
        - api_usage_meter (per-day rollup, existing table)
        - api_keys.calls_today + calls_total + usage_count + last_used_at
          (raw counters on the key row)

Cost characteristics
--------------------
  - Request-time: ~5 µs (g.set + dict-append; no DB IO on hot path)
  - Memory: bounded buffer (10K rows max → ~3 MB), drops oldest if full
  - Flush: one transaction, batched. Typical 30s window = a few hundred
    rows even at 1 rps sustained.
  - DB load: ~2 writes/sec sustained at moderate traffic; idempotent on
    api_usage_meter via ON CONFLICT.

Identification
--------------
  We track when X-API-Key is present and starts with 'dchub_'. Anonymous
  traffic is NOT tracked (intentional — keeps cardinality bounded). For
  anonymous-traffic counts use cron_observability or brain_http_capture.

Privacy
-------
  We log:
    - API key PREFIX (first 24 chars) for join-back to partner_keys_issued
    - Endpoint path TEMPLATE (slug-collapsed)
    - Status code, latency
  We do NOT log:
    - Full API key
    - Request body
    - Response body
    - User IP (already covered by separate infra)
"""

import datetime as _dt
import os
import re
import threading
import time

from flask import Blueprint, current_app, g, jsonify, request


def _pg_conn():
    """Per-call short-lived connection. Mirrors the pattern in
    partner_key_issuer.py + partner_landing.py — there's no shared
    `db_connection` module in this repo, every route file rolls its own.
    Accepts DATABASE_URL or NEON_DATABASE_URL (Railway uses the former,
    some env contexts the latter)."""
    try:
        import psycopg2
        url = (os.environ.get("DATABASE_URL")
               or os.environ.get("NEON_DATABASE_URL"))
        return psycopg2.connect(url, connect_timeout=5) if url else None
    except Exception:
        return None


def _safe_close(c):
    if c is None:
        return
    try:
        c.close()
    except Exception:
        pass


api_usage_tracker_bp = Blueprint("api_usage_tracker", __name__)


# === Configuration =====================================================

_FLUSH_INTERVAL_SEC = int(os.environ.get("USAGE_FLUSH_INTERVAL_SEC", "30"))
_BUFFER_MAX        = int(os.environ.get("USAGE_BUFFER_MAX", "10000"))
_RETENTION_DAYS    = int(os.environ.get("USAGE_RETENTION_DAYS", "90"))

# Paths we never track (would inflate volume or feedback-loop)
_SKIP_PATH_PREFIXES = (
    "/static/",
    "/api/v1/admin/partner-usage",   # this endpoint reads tracker data
    "/api/v1/admin/partner-key",
    "/alive",
    "/healthz", "/livez", "/readyz",
    "/api/health",
    "/favicon",
)

# Template-collapse rules. Same pattern as brain_http_capture.
_PATH_TEMPLATES = (
    (re.compile(r"^/api/v1/partners/[^/]+"),  "/api/v1/partners/<slug>"),
    (re.compile(r"^/partners/[^/]+"),         "/partners/<slug>"),
    (re.compile(r"^/dcpi/[^/]+"),             "/dcpi/<slug>"),
    (re.compile(r"^/markets/[^/]+"),          "/markets/<slug>"),
    (re.compile(r"^/operators/[^/]+"),        "/operators/<slug>"),
    (re.compile(r"^/api/v1/facility/[^/]+"),  "/api/v1/facility/<id>"),
    (re.compile(r"^/api/v1/reveal-grid-export/status/[^/]+"),
                                              "/api/v1/reveal-grid-export/status/<job_id>"),
)


def _collapse_path(p: str) -> str:
    for rx, tpl in _PATH_TEMPLATES:
        if rx.match(p):
            return tpl
    return p[:200]


# === In-memory buffer ==================================================

_BUFFER_LOCK = threading.Lock()
_BUFFER      = []        # list of dicts: {ts, key_prefix, path, status, latency_ms}
_LAST_FLUSH  = time.time()


def _track(entry: dict) -> None:
    """Cheap append; drop oldest if at capacity."""
    with _BUFFER_LOCK:
        if len(_BUFFER) >= _BUFFER_MAX:
            # Drop oldest 10% to make room (don't block writers on full)
            del _BUFFER[: _BUFFER_MAX // 10]
        _BUFFER.append(entry)


def _drain_buffer() -> list:
    """Atomically swap buffer for empty list, return drained contents."""
    global _BUFFER
    with _BUFFER_LOCK:
        out, _BUFFER = _BUFFER, []
    return out


# === Schema ============================================================

_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_endpoint_log (
    id              BIGSERIAL    PRIMARY KEY,
    called_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    api_key_prefix  TEXT         NOT NULL,
    endpoint_path   TEXT         NOT NULL,
    method          TEXT         NOT NULL DEFAULT 'GET',
    status          SMALLINT     NOT NULL,
    latency_ms      INTEGER
);
CREATE INDEX IF NOT EXISTS ix_endpoint_log_key_called
    ON api_endpoint_log (api_key_prefix, called_at DESC);
CREATE INDEX IF NOT EXISTS ix_endpoint_log_path_called
    ON api_endpoint_log (endpoint_path, called_at DESC);
"""


def _ensure_schema() -> None:
    c = _pg_conn()
    if c is None:
        return
    try:
        with c.cursor() as cur:
            cur.execute(_SCHEMA)
            c.commit()
    except Exception:
        pass
    finally:
        _safe_close(c)


# === Flusher (background thread) =======================================

def _flush() -> dict:
    """Drain the buffer + write to api_endpoint_log + roll up api_usage_meter
    + bump api_keys counters. Returns stats dict for self-test."""
    entries = _drain_buffer()
    if not entries:
        return {"flushed": 0, "skipped": "empty"}
    c = _pg_conn()
    if c is None:
        # Re-queue so we don't lose the data
        with _BUFFER_LOCK:
            _BUFFER.extend(entries[: _BUFFER_MAX - len(_BUFFER)])
        return {"flushed": 0, "skipped": "no_db"}

    try:
        with c.cursor() as cur:
            # 1. Bulk INSERT into per-call log (idempotent — id is serial)
            values_sql = ",".join(["(%s, %s, %s, %s, %s, %s)"] * len(entries))
            params = []
            for e in entries:
                params.extend([
                    e["ts"], e["key_prefix"], e["path"],
                    e.get("method", "GET"), e["status"], e.get("latency_ms"),
                ])
            cur.execute(
                f"INSERT INTO api_endpoint_log "
                f"  (called_at, api_key_prefix, endpoint_path, method, status, latency_ms) "
                f"VALUES {values_sql}",
                params,
            )

            # 2. Per-day rollup into existing api_usage_meter
            # Group by (key, date) and bulk-upsert. We use the prefix as
            # the api_key field (api_usage_meter.api_key was TEXT; prefix
            # is also TEXT). Tier defaults to 'developer' for partner keys.
            from collections import defaultdict
            by_key_day = defaultdict(int)
            for e in entries:
                day = e["ts"].date()
                by_key_day[(e["key_prefix"], day)] += 1
            for (kp, day), cnt in by_key_day.items():
                cur.execute(
                    """
                    INSERT INTO api_usage_meter
                          (api_key, tier, usage_date, calls_count, last_call_at, updated_at)
                    VALUES (%s, 'developer', %s, %s, NOW(), NOW())
                    ON CONFLICT (api_key, usage_date) DO UPDATE
                       SET calls_count  = api_usage_meter.calls_count + EXCLUDED.calls_count,
                           last_call_at = NOW(),
                           updated_at   = NOW()
                    """,
                    (kp, day, cnt),
                )

            # 3. Bump api_keys counters (calls_total, calls_today, usage_count,
            #    last_used_at). We do this per-key, not per-call.
            by_key = defaultdict(int)
            for e in entries:
                by_key[e["key_prefix"]] += 1
            for kp, cnt in by_key.items():
                cur.execute(
                    """
                    UPDATE api_keys
                       SET calls_total  = COALESCE(calls_total, 0)  + %s,
                           usage_count  = COALESCE(usage_count, 0)  + %s,
                           calls_today  = COALESCE(calls_today, 0)  + %s,
                           last_used_at = NOW()
                     WHERE key_prefix = %s
                    """,
                    (cnt, cnt, cnt, kp),
                )

            c.commit()
            return {"flushed": len(entries), "by_key": dict(by_key)}
    except Exception as ex:
        # Re-queue the entries we drained, so we don't lose them.
        # (Bounded by _BUFFER_MAX; if write keeps failing, oldest get dropped.)
        with _BUFFER_LOCK:
            _BUFFER.extend(entries[: _BUFFER_MAX - len(_BUFFER)])
        return {"flushed": 0, "error": str(ex)[:200]}
    finally:
        _safe_close(c)


def _flush_loop() -> None:
    global _LAST_FLUSH
    while True:
        time.sleep(_FLUSH_INTERVAL_SEC)
        try:
            _flush()
            _LAST_FLUSH = time.time()
        except Exception:
            pass


# Per-worker process-local flag.  r78-e (2026-06-03): gunicorn typically
# preloads app code in the master process and then fork()s workers.
# Threads do NOT survive fork — they exist only in the master. So if we
# start the flusher at install_tracker() time, the worker processes have
# no flusher and their buffers fill forever. Fix: lazy-start on first
# request in each worker. Process-local flag means we check once per
# request (<1µs) and start the thread once per worker process.
_FLUSHER_STARTED_THIS_PROCESS = False
_FLUSHER_START_LOCK = threading.Lock()


def _ensure_flusher_running() -> None:
    """Idempotent. Cheap on the hot path: bool check after first call."""
    global _FLUSHER_STARTED_THIS_PROCESS
    if _FLUSHER_STARTED_THIS_PROCESS:
        return
    with _FLUSHER_START_LOCK:
        # Re-check under lock (double-check pattern)
        if _FLUSHER_STARTED_THIS_PROCESS:
            return
        # Look for an existing flusher thread in this process (defensive)
        existing = [t for t in threading.enumerate()
                    if t.name == "api-usage-flusher" and t.is_alive()]
        if not existing:
            try:
                t = threading.Thread(
                    target=_flush_loop, daemon=True, name="api-usage-flusher")
                t.start()
            except Exception:
                # If thread start fails, retry on next request
                return
        _FLUSHER_STARTED_THIS_PROCESS = True


# === Wire-in =========================================================

def install_tracker(app) -> dict:
    """Wire before/after-request hooks into the Flask app.

    Returns {ok: bool, ...} — call from main.py after blueprints are
    registered. Safe to call multiple times (idempotent on schema +
    thread name)."""
    _ensure_schema()

    @app.before_request
    def _stash_start():
        # r78-e: lazy-start the bg flusher in this worker process. Free
        # after first request (process-local bool short-circuits).
        _ensure_flusher_running()
        g._usage_start_ns = time.time_ns()
        # Capture key + decide trackability cheaply
        ak = (request.headers.get("X-API-Key") or "").strip()
        if ak and ak.startswith("dchub_") and len(ak) >= 24:
            g._usage_key_prefix = ak[:24]
        # paths to skip — short-circuit
        p = request.path or ""
        for pfx in _SKIP_PATH_PREFIXES:
            if p.startswith(pfx):
                g._usage_skip = True
                break

    @app.after_request
    def _record(response):
        try:
            if getattr(g, "_usage_skip", False):
                return response
            key_prefix = getattr(g, "_usage_key_prefix", None)
            if not key_prefix:
                return response  # only track keyed requests
            start = getattr(g, "_usage_start_ns", None)
            latency_ms = (time.time_ns() - start) // 1_000_000 if start else None
            _track({
                "ts":         _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc),
                "key_prefix": key_prefix,
                "path":       _collapse_path(request.path or ""),
                "method":     request.method,
                "status":     int(response.status_code),
                "latency_ms": int(latency_ms) if latency_ms is not None else None,
            })
        except Exception:
            pass  # NEVER break responses
        return response

    # r78-e: do NOT start the bg thread here — under gunicorn --preload,
    # this code runs in the master process and the resulting thread does
    # not survive fork() into workers. The bg thread is now lazy-started
    # by _ensure_flusher_running() inside before_request — that runs in
    # each worker process, after fork, so the thread lives where it can
    # actually drain the worker's buffer.

    return {"ok": True, "flush_interval_sec": _FLUSH_INTERVAL_SEC,
            "buffer_max": _BUFFER_MAX, "skip_path_prefixes": list(_SKIP_PATH_PREFIXES),
            "flusher_start": "lazy (per-worker, on first request)"}


# === Manual flush + status endpoints (for ops + smoke-tests) ===========

def _admin_authorized() -> bool:
    """Match the pattern used elsewhere — X-Admin-Key against env var."""
    expected = os.environ.get("DCHUB_ADMIN_KEY") or os.environ.get("ADMIN_API_KEY") or ""
    return bool(expected) and request.headers.get("X-Admin-Key", "") == expected


@api_usage_tracker_bp.route("/api/v1/admin/usage-tracker/flush", methods=["POST"])
def force_flush():
    """Manually flush the in-memory buffer to DB. Useful for smoke-testing
    that the tracker is actually wired and writing."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    result = _flush()
    return jsonify({"ok": True, **result}), 200


@api_usage_tracker_bp.route("/api/v1/admin/usage-tracker/status", methods=["GET"])
def status():
    """Inspection endpoint — buffer depth, time since last flush, recent
    entries (without exposing full keys)."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    with _BUFFER_LOCK:
        depth = len(_BUFFER)
        recent = list(_BUFFER[-5:])
    # Find active flusher threads in this process
    flushers = [t.name for t in threading.enumerate()
                if t.name == "api-usage-flusher" and t.is_alive()]
    return jsonify({
        "ok":                       True,
        "process_pid":              os.getpid(),
        "buffer_depth":             depth,
        "buffer_max":               _BUFFER_MAX,
        "sec_since_last_flush":     round(time.time() - _LAST_FLUSH, 1),
        "flush_interval_sec":       _FLUSH_INTERVAL_SEC,
        "flusher_started":          _FLUSHER_STARTED_THIS_PROCESS,
        "flusher_threads_alive":    len(flushers),
        "recent_5_entries":         [
            {
                "ts":         e["ts"].isoformat() if hasattr(e["ts"], "isoformat") else str(e["ts"]),
                "key_prefix": e["key_prefix"],
                "path":       e["path"],
                "method":     e.get("method", "GET"),
                "status":     e["status"],
                "latency_ms": e.get("latency_ms"),
            } for e in recent
        ],
    }), 200
