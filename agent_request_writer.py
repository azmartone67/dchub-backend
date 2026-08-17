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

Observability (both halves were broken until 2026-08-17, see `_beat`):
  GET /api/v1/ops/deadman                        public; `agent_requests_writer:<idx>`
                                                 feed, counters in its `note`
  GET /api/v1/admin/agent-request-writer/stats   admin; stats() live, one REPLICA's

Envs:
  AGENT_REQUESTS_WRITER_ENABLE   default off   -> "1"/"true"/"yes"/"on" turns it on
  AGENT_REQUESTS_FLUSH_ROWS      default 200    flush when the buffer reaches this
  AGENT_REQUESTS_FLUSH_SECONDS   default 2.0    ...or at least this often
  AGENT_REQUESTS_BUFFER_CAP      default 5000   drop OLDEST above this (counted)
  AGENT_REQUESTS_BEAT_SECONDS    default 60     dead-man heartbeat cadence
  AGENT_REQUESTS_MAX_ATTEMPTS    default 10     quarantine a row after N failed flushes
  DCHUB_REPLICA_INDEX            default "0"    STABLE per-replica id for the feed key

Poison rows: a flush failure used to re-queue the batch unconditionally, so a
row Postgres can NEVER accept retried forever at the flush cadence and the queue
only grew. Two guards now: NUL bytes are scrubbed before adaptation (`_scrub`,
the observed 2026-08-17 cause), and any row that fails AGENT_REQUESTS_MAX_ATTEMPTS
flushes is dropped and counted as `poisoned` in stats() and the dead-man detail.
Failures where we never got a connection do not count against a row, so a
database outage still costs nothing but the cap.

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
_MAX_TRIES  = max(1, int(os.environ.get("AGENT_REQUESTS_MAX_ATTEMPTS", "10")))
_FEED       = "agent_requests_writer:" + os.environ.get("DCHUB_REPLICA_INDEX", "0")

_buf = deque()
_lock = threading.Lock()
_wake = threading.Event()
_started = False
_start_lock = threading.Lock()
_last_beat = 0.0
_stats = {"enqueued": 0, "inserted": 0, "dropped": 0, "poisoned": 0,
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
    """Hot path: append-only, bounded, no DB I/O, never blocks.

    Buffer entries are (row, tries). Scrubbing/validation deliberately does NOT
    happen here — it runs on the flusher thread, which is the whole point of
    this module: keep work off the request path.
    """
    if len(row) != len(_COLS):
        return
    _ensure_started()
    with _lock:
        if len(_buf) >= _CAP:
            _buf.popleft()
            _stats["dropped"] += 1
        _buf.append((tuple(row), 0))
        _stats["enqueued"] += 1
        n = len(_buf)
    if n >= _FLUSH_ROWS:
        _wake.set()


def _scrub(row):
    """Strip NULs, which Postgres text columns can never hold.

    psycopg2 raises ValueError("A string literal cannot contain NUL (0x00)
    characters.") while ADAPTING the row — client-side, before a single byte
    reaches the server — so one poisoned row kills the whole execute_values
    batch. Combined with the unconditional re-queue below that produced a
    permanent ~1/sec retry loop in prod on 2026-08-17: the buffer climbed
    2704 -> 3039 rows in 64s and no row in it could ever be written.

    Returns the row unchanged (no copy) in the overwhelmingly common clean case.
    """
    out = None
    for i, v in enumerate(row):
        if isinstance(v, str) and "\x00" in v:
            if out is None:
                out = list(row)
            out[i] = v.replace("\x00", "")
    return tuple(out) if out is not None else row


def _drain():
    with _lock:
        if not _buf:
            return []
        b = list(_buf)
        _buf.clear()
        return b


def _requeue(entries, count_try=True):
    """Bounded retry; honors the cap (drops oldest on overflow).

    A row that has failed _MAX_TRIES times is QUARANTINED — dropped and counted
    as `poisoned` — so a row the database will never accept cannot pin the queue
    open forever. `count_try` is False when we never got a connection at all
    (pool timeout, circuit breaker): the rows never reached the server, so that
    is no evidence against them and a DB outage must not eat the buffer.

    Returns the number quarantined.
    """
    dead = 0
    with _lock:
        for row, tries in reversed(entries):
            tries = tries + 1 if count_try else tries
            if tries >= _MAX_TRIES:
                dead += 1
                continue
            if len(_buf) >= _CAP:
                _stats["dropped"] += 1
                continue
            _buf.appendleft((row, tries))
        if dead:
            _stats["poisoned"] += dead
    return dead


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
    entries = _drain()
    if not entries:
        return
    batch = [_scrub(row) for row, _ in entries]     # NUL-free before adaptation
    from psycopg2.extras import execute_values
    from main import get_pg_connection, return_pg_connection
    conn = None
    err = False
    reached_db = False
    try:
        conn = get_pg_connection()
        reached_db = True
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
        dead = _requeue(entries, count_try=reached_db)
        log.warning("agent_requests flush failed; %d rows re-queued, %d quarantined "
                    "after %d attempts: %s", len(entries) - dead, dead, _MAX_TRIES, e)
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
    """Record liveness in the dead-man ledger by calling the upsert DIRECTLY.

    This used to POST 127.0.0.1:$PORT/api/v1/admin/ingest-runs/beat carrying
    `X-Admin-Key: $ADMIN_API_KEY`. Two independent faults, both silent:

      1. ★ `ADMIN_API_KEY` is this module's name for it and nothing else's.
         `routes.ingest_runs._admin_ok()` compares against DCHUB_ADMIN_KEY /
         DCHUB_INTERNAL_KEY. Both names are SET in the Railway web service to
         DIFFERENT values, so `ADMIN_API_KEY or DCHUB_ADMIN_KEY` never reached
         its fallback and every beat got 401 "admin key required" — swallowed
         at log.debug, invisible at the default level. Measured 2026-08-17:
         /api/v1/ops/deadman tracked 68 feeds and not one matched
         `agent_request`, while AGENT_REQUESTS_WRITER_ENABLE=1 in prod — so the
         flusher was running and 401ing once a minute the whole time.
      2. The body's stats rode in a `detail` key. The handler reads `note`.
         `detail` is not a field it has ever known, so even a beat that HAD
         authenticated would have dropped the counters on the floor — the
         `poisoned` counter added the same day to make poison drops observable
         could not have shown up either way.

    record_beat() is the shared upsert the HTTP handler itself calls, so this
    is the same ledger write with no loopback hop, no admin key, no gate and no
    request context — exactly the migration routes.ingest_runs.beat_feed's
    docstring prescribes for the hand-rolled copies of that POST. It also puts
    this beat beyond the whole class of loopback-self-call failures (metered
    before_request gates, the CF `Python-urllib` UA block) by not making one.

    ★ TWO REPLICAS, ONE FEED KEY. The web service runs 2 replicas and
    DCHUB_REPLICA_INDEX is unset, so both beat `agent_requests_writer:0` and
    overwrite each other. The feed therefore proves "at least one writer is
    alive", NOT that both are; the counters in `note` are one replica's and
    will appear to jump backwards as the two interleave. `replica=` names which
    one wrote the row so that is legible instead of alarming.
    """
    with _lock:
        buffered = len(_buf)
    note = "replica=%s enqueued=%d inserted=%d dropped=%d poisoned=%d buffered=%d" % (
        (os.environ.get("RAILWAY_REPLICA_ID") or "?")[:8],
        _stats["enqueued"], _stats["inserted"], _stats["dropped"],
        _stats["poisoned"], buffered)
    try:
        from routes.ingest_runs import record_beat
        record_beat(_FEED, status=status,
                    rows=1,                          # liveness sentinel (never zero-row)
                    mcd=_iso(),
                    cad=round(_BEAT_SECS / 3600.0, 4),
                    note=note[:280])                 # the handler's own cap
    except Exception as e:
        # ★ WARNING, never debug. A dropped beat is not a detail — it is the
        # entire reason this module has no observability, and log.debug is how
        # that stayed invisible for the life of the feature.
        log.warning("agent_requests writer beat DROPPED feed=%s status=%s err=%s",
                    _FEED, status, e)


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
             flush_rows=_FLUSH_ROWS, flush_seconds=_FLUSH_SECS, cap=_CAP,
             max_attempts=_MAX_TRIES)
    return d
