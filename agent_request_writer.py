"""PR2b — batched agent_requests writer (pool-floor fix).

Buffers per-request audit rows in-process and flushes them to Postgres in
BATCHES on one pooled write connection, instead of the synchronous
INSERT + platform_connections UPDATE per request that shows up as steady-state
connection churn on the Neon primary (idle-in-transaction + per-request writes).

ENABLE-GATED OFF by default. When AGENT_REQUESTS_WRITER_ENABLE is unset/0,
mcp_gateway.MCPGateway.log_request uses its existing synchronous path UNCHANGED,
so merging this PR is a no-op. Flip the env to turn it on, then verify with
pg_stat_activity (idle-in-transaction + primary active-backend count should drop)
and the `agent_requests_writer:<idx>` feed on GET /api/v1/ops/deadman.

Envs:
  AGENT_REQUESTS_WRITER_ENABLE   default off   -> "1"/"true"/"yes"/"on" turns it on
  AGENT_REQUESTS_FLUSH_ROWS      default 200    flush when the buffer reaches this
  AGENT_REQUESTS_FLUSH_SECONDS   default 2.0    ...or at least this often
  AGENT_REQUESTS_BUFFER_CAP      default 5000   drop OLDEST above this (counted)
  AGENT_REQUESTS_BEAT_SECONDS    default 60     dead-man heartbeat cadence
  DCHUB_REPLICA_INDEX            default "0"    STABLE per-replica id for the feed key

Residual risk: rows buffered but not yet flushed are lost on SIGKILL/OOM (atexit
does not run on SIGKILL, and gunicorn recycles workers at max-requests). This is
AUDIT/analytics data, not user/business state; the small cap + short flush window
bound the loss, and dropped rows are counted and surfaced in the dead-man detail.
Logging itself is best-effort (mcp_gateway wraps it in try/except), so a writer
fault degrades logging, never a user request.
"""
import os
import time
import atexit
import threading
import logging
from collections import deque

log = logging.getLogger("agent_request_writer")

# exact agent_requests column order (mcp_gateway.py CREATE TABLE + INSERT)
_COLS = ("platform_id", "user_agent", "ip_address", "method", "path",
         "query_params", "request_body", "response_code", "response_time_ms",
         "tools_invoked", "session_id")

_FLUSH_ROWS = int(os.environ.get("AGENT_REQUESTS_FLUSH_ROWS", "200"))
_FLUSH_SECS = float(os.environ.get("AGENT_REQUESTS_FLUSH_SECONDS", "2.0"))
_CAP        = int(os.environ.get("AGENT_REQUESTS_BUFFER_CAP", "5000"))
_BEAT_SECS  = float(os.environ.get("AGENT_REQUESTS_BEAT_SECONDS", "60"))
_FEED       = "agent_requests_writer:" + os.environ.get("DCHUB_REPLICA_INDEX", "0")

_buf = deque()
_lock = threading.Lock()
_wake = threading.Event()
_started = False
_start_lock = threading.Lock()
_last_beat = 0.0
_stats = {"enqueued": 0, "inserted": 0, "dropped": 0,
          "last_flush_rows": 0, "last_flush_ts": None, "last_error": None}


def enabled():
    return os.environ.get("AGENT_REQUESTS_WRITER_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on")


def _iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _ensure_started():
    global _started
    if _started:
        return
    with _start_lock:
        if _started:
            return
        threading.Thread(target=_run, name="agent_requests_writer", daemon=True).start()
        atexit.register(_drain_on_exit)
        _started = True


def enqueue(*row):
    """Hot path: append-only, bounded, no DB I/O, never blocks."""
    if len(row) != len(_COLS):
        return
    _ensure_started()
    with _lock:
        if len(_buf) >= _CAP:
            _buf.popleft()
            _stats["dropped"] += 1
        _buf.append(tuple(row))
        _stats["enqueued"] += 1
        n = len(_buf)
    if n >= _FLUSH_ROWS:
        _wake.set()


def _drain():
    with _lock:
        if not _buf:
            return []
        b = list(_buf)
        _buf.clear()
        return b


def _requeue(batch):
    """Transient-failure retry; honors the cap (drops oldest on overflow)."""
    with _lock:
        for row in reversed(batch):
            if len(_buf) >= _CAP:
                _stats["dropped"] += 1
                continue
            _buf.appendleft(row)


def _run():
    while True:
        _wake.wait(timeout=_FLUSH_SECS)
        _wake.clear()
        try:
            _flush_once()
        except Exception as e:                      # never let the loop die
            _stats["last_error"] = repr(e)[:200]
            log.warning("agent_requests flush loop error: %s", e)
        _maybe_beat()


def _flush_once():
    batch = _drain()
    if not batch:
        return
    from psycopg2.extras import execute_values
    from main import get_pg_connection, return_pg_connection
    conn = None
    err = False
    try:
        conn = get_pg_connection()
        conn.autocommit = False                     # ONE tx + commit; NO savepoint
        cur = conn.cursor()
        execute_values(
            cur,
            "INSERT INTO agent_requests "
            "(platform_id, user_agent, ip_address, method, path, query_params, "
            "request_body, response_code, response_time_ms, tools_invoked, session_id) "
            "VALUES %s",
            batch, page_size=200)
        conn.commit()
        cur.close()
        _stats["inserted"] += len(batch)
        _stats["last_flush_rows"] = len(batch)
        _stats["last_flush_ts"] = _iso()
        _stats["last_error"] = None
    except Exception as e:
        err = True
        _stats["last_error"] = repr(e)[:200]
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        _requeue(batch)                             # do not lose rows on a transient error
        log.warning("agent_requests flush failed; %d rows re-queued: %s", len(batch), e)
    finally:
        if conn is not None:
            try:
                return_pg_connection(conn, error=err)
            except Exception:
                pass
    if not err:
        try:
            _update_platform_counters(batch)        # secondary, isolated, non-critical
        except Exception:
            log.debug("platform_connections update skipped", exc_info=True)


def _update_platform_counters(batch):
    agg = {}
    for r in batch:
        pid = r[0]
        if not pid:
            continue
        a = agg.setdefault(pid, [0, 0.0, 0])
        a[0] += 1
        a[1] += (r[8] or 0)                          # response_time_ms
        a[2] += 1 if (r[7] or 0) >= 400 else 0       # response_code >= 400
    if not agg:
        return
    from main import get_pg_connection, return_pg_connection
    conn = None
    err = False
    try:
        conn = get_pg_connection()
        conn.autocommit = False
        cur = conn.cursor()
        for pid, (n, sum_rt, errs) in agg.items():
            # auto-create without ON CONFLICT (avoids the partial-index match trap)
            cur.execute(
                "INSERT INTO platform_connections "
                "(platform_id, platform_name, protocol, status, total_requests, total_errors, avg_latency_ms) "
                "SELECT %s, %s, 'auto', 'active', 0, 0, 0 "
                "WHERE NOT EXISTS (SELECT 1 FROM platform_connections WHERE platform_id = %s)",
                (pid, pid, pid))
            # SET RHS reads the OLD row values, so the running-avg stays correct
            cur.execute(
                "UPDATE platform_connections SET "
                "total_errors = total_errors + %s, "
                "avg_latency_ms = (avg_latency_ms * total_requests + %s) / NULLIF(total_requests + %s, 0), "
                "total_requests = total_requests + %s, updated_at = NOW() "
                "WHERE platform_id = %s",
                (errs, sum_rt, n, n, pid))
        conn.commit()
        cur.close()
    except Exception:
        err = True
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            try:
                return_pg_connection(conn, error=err)
            except Exception:
                pass


def _maybe_beat():
    global _last_beat
    now = time.time()
    if now - _last_beat < _BEAT_SECS:
        return
    _last_beat = now
    _beat("error" if _stats["last_error"] else "ok")


def _beat(status):
    import json
    import urllib.request
    with _lock:
        buffered = len(_buf)
    body = json.dumps({
        "feed": _FEED,
        "status": status,
        "rows_inserted": 1,                          # liveness sentinel (never zero-row)
        "cadence_hours": round(_BEAT_SECS / 3600.0, 4),
        "last_run": _iso(),
        "max_content_date": _iso(),
        "detail": "enqueued=%d inserted=%d dropped=%d buffered=%d" % (
            _stats["enqueued"], _stats["inserted"], _stats["dropped"], buffered),
    }).encode()
    port = os.environ.get("PORT", "8080")
    req = urllib.request.Request(
        "http://127.0.0.1:%s/api/v1/admin/ingest-runs/beat" % port,
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "dchub-pool-floor/1.0",
                 "X-Admin-Key": os.environ.get("ADMIN_API_KEY") or os.environ.get("DCHUB_ADMIN_KEY", "")})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        log.debug("writer beat failed: %s", e)


def _drain_on_exit():
    try:
        _flush_once()
    except Exception:
        pass


def stats():
    with _lock:
        buffered = len(_buf)
    d = dict(_stats)
    d.update(enabled=enabled(), buffered=buffered, feed=_FEED,
             flush_rows=_FLUSH_ROWS, flush_seconds=_FLUSH_SECS, cap=_CAP)
    return d
