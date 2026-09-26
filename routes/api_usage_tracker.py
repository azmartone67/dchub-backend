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
  We track a key of a shape in TRACKED_KEY_PREFIXES: a 'dchub_' X-API-Key,
  and — since 2026-09-21 — a self-serve 'dch_live_' key sent as X-API-Key or
  Authorization: Bearer by a caller that is not the server itself (see
  _recorded_as). Anonymous traffic is NOT tracked (intentional — keeps
  cardinality bounded). For anonymous-traffic counts use cron_observability or
  brain_http_capture.

  One exception (2026-09-21): KEYLESS traffic from a declared partner egress
  (partner_egress.py), which the limiters bucket as the 'partner' tier, is
  COUNTED per day, route and status into partner_keyless_daily. Counts only:
  no row per request, no key, no address. It never touches api_endpoint_log
  or api_usage_meter, whose readers assume a key.

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

# The key shapes this tracker records per key, and how much of a key it keeps.
# Named so a reader that joins on the stored prefix (routes/install_stats.py)
# derives the same prefix and the same eligibility instead of retyping them.
#   ACCOUNT_KEY_PREFIX     api_keys / partner keys. Recorded from X-API-Key only,
#                          exactly as since 2026-06-03 — partner-usage reports
#                          read these rows, so nothing about them changed.
#   SELF_SERVE_KEY_PREFIX  every mcp_dev_keys key: /api/v1/keys/claim, the
#                          usage-based checkout, redeem, ... Recorded since
#                          2026-09-21; before that no per-key table held one.
ACCOUNT_KEY_PREFIX    = "dchub_"
SELF_SERVE_KEY_PREFIX = "dch_live_"
TRACKED_KEY_PREFIXES  = (ACCOUNT_KEY_PREFIX, SELF_SERVE_KEY_PREFIX)
STORED_PREFIX_LEN     = 24

# Headers that mean the SERVER is calling — recorded as a credential CLASS,
# never as a key. In precedence order.
_SERVER_CREDENTIAL_CLASSES = (
    ("admin",    ("X-Admin-Key", "X-Admin-Token")),
    ("cron",     ("X-Internal-Cron", "X-DC-Internal-Cron")),
    ("internal", ("X-Internal-Key", "X-DC-Internal-Token")),
)

# Paths we never track (would inflate volume or feedback-loop)
_SKIP_PATH_PREFIXES = (
    "/static/",
    "/api/v1/admin/partner-usage",   # this endpoint reads tracker data
    "/api/v1/admin/partner-key",
    "/api/v1/admin/usage-tracker/partner-traffic",   # reads tracker data
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


# === Keyless partner-egress counts =======================================
# (usage_date, partner, method, endpoint_rule, status) -> requests. A counter,
# not a row per request: the partner's keyless volume is the whole of one
# hosted catalogue's traffic. Bounded; a key that does not fit is counted in
# _PARTNER_DROPPED so a full buffer is visible rather than silent.
_PARTNER_LOCK = threading.Lock()
_PARTNER_COUNTS: dict = {}
_PARTNER_MAX_KEYS = 5000
_PARTNER_DROPPED = [0]
UNMATCHED_RULE = "(no route)"


def _track_partner(partner: str, method: str, rule: str, status: int) -> None:
    k = (_dt.datetime.now(_dt.timezone.utc).date(), partner, method, rule, int(status))
    with _PARTNER_LOCK:
        if k in _PARTNER_COUNTS:
            _PARTNER_COUNTS[k] += 1
        elif len(_PARTNER_COUNTS) < _PARTNER_MAX_KEYS:
            _PARTNER_COUNTS[k] = 1
        else:
            _PARTNER_DROPPED[0] += 1


def _drain_partner() -> dict:
    global _PARTNER_COUNTS
    with _PARTNER_LOCK:
        out, _PARTNER_COUNTS = _PARTNER_COUNTS, {}
    return out


def _requeue_partner(counts: dict) -> None:
    with _PARTNER_LOCK:
        for k, n in counts.items():
            if k in _PARTNER_COUNTS or len(_PARTNER_COUNTS) < _PARTNER_MAX_KEYS:
                _PARTNER_COUNTS[k] = _PARTNER_COUNTS.get(k, 0) + n
            else:
                _PARTNER_DROPPED[0] += n


def _count_partner_keyless(response) -> None:
    """Count this response if its request is keyless traffic from a declared
    partner egress: the limiter's own classification (the bucket it charged),
    so this never disagrees with the 'partner' tier. The route is the matched
    URL rule (a template), so cardinality stays bounded."""
    from rate_limiter import partner_of_request
    partner = partner_of_request()
    if not partner:
        return
    rule = request.url_rule.rule if request.url_rule is not None else UNMATCHED_RULE
    _track_partner(partner, request.method, rule, int(response.status_code))


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
CREATE TABLE IF NOT EXISTS partner_keyless_daily (
    usage_date     DATE         NOT NULL,
    partner        TEXT         NOT NULL,
    method         TEXT         NOT NULL,
    endpoint_rule  TEXT         NOT NULL,
    status         SMALLINT     NOT NULL,
    requests       BIGINT       NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (usage_date, partner, method, endpoint_rule, status)
);
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
            # is also TEXT).
            #
            # r79 (2026-06-13): tier is RESOLVED from the key registries, not
            # hardcoded 'developer'. The hardcode labeled the owner's
            # enterprise key, partner keys AND unregistered internal/test keys
            # as paying developer traffic — which polluted the 2026-06-12
            # revenue audit (an internal-looking 49k-call month masqueraded as
            # ~$580 customer overage; it was the owner's own key). Prefix-match
            # against mcp_dev_keys / api_keys (the meter stores 24-char
            # prefixes by design; LIKE needs the underscores escaped);
            # anything in NEITHER registry is ours → tier='internal'.
            from collections import defaultdict

            def _tier_for_prefix(kp: str) -> str:
                esc = kp.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
                try:
                    cur.execute(
                        "SELECT tier FROM mcp_dev_keys "
                        " WHERE api_key LIKE %s ESCAPE '\\' LIMIT 1", (esc + "%",))
                    row = cur.fetchone()
                    if row and row[0]:
                        return str(row[0])
                    cur.execute(
                        "SELECT COALESCE(NULLIF(rate_limit_tier,''),'developer') "
                        "  FROM api_keys "
                        " WHERE key_hash LIKE %s ESCAPE '\\' "
                        "   AND COALESCE(is_active, 1) <> 0 LIMIT 1", (esc + "%",))
                    row = cur.fetchone()
                    if row and row[0]:
                        return str(row[0])
                except Exception:
                    pass
                return "internal"

            _tier_cache: dict = {}
            by_key_day = defaultdict(int)
            for e in entries:
                day = e["ts"].date()
                by_key_day[(e["key_prefix"], day)] += 1
            for (kp, day), cnt in by_key_day.items():
                if kp not in _tier_cache:
                    _tier_cache[kp] = _tier_for_prefix(kp)
                cur.execute(
                    """
                    INSERT INTO api_usage_meter
                          (api_key, tier, usage_date, calls_count, last_call_at, updated_at)
                    VALUES (%s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING, NOW())
                    ON CONFLICT (api_key, usage_date) DO UPDATE
                       SET calls_count  = api_usage_meter.calls_count + EXCLUDED.calls_count,
                           tier         = EXCLUDED.tier,
                           last_call_at = NOW(),
                           updated_at   = NOW()
                    """,
                    (kp, _tier_cache[kp], day, cnt),
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


_PARTNER_UPSERT = """
INSERT INTO partner_keyless_daily
       (usage_date, partner, method, endpoint_rule, status, requests, updated_at)
VALUES (%s, %s, %s, %s, %s, %s, NOW() ON CONFLICT DO NOTHING)
ON CONFLICT (usage_date, partner, method, endpoint_rule, status) DO UPDATE
   SET requests   = partner_keyless_daily.requests + EXCLUDED.requests,
       updated_at = NOW()
"""


def _flush_partner() -> dict:
    """Roll the keyless partner counts, and the partner pay-link refs
    (routes/partner_attribution), into their tables. Additive, so every
    replica's counts sum. On failure the counts go back into the buffer."""
    counts = _drain_partner()
    try:
        from routes import partner_attribution
    except Exception:
        partner_attribution = None
    if not counts and not (partner_attribution and partner_attribution.pending()):
        return {"partner_keyless_rows": 0, "partner_refs": {"refs": 0}}
    c = _pg_conn()
    out = {}
    if not counts:
        out["partner_keyless_rows"] = 0
    elif c is None:
        _requeue_partner(counts)
        out["partner_keyless_rows"] = 0
        out["partner_keyless_skipped"] = "no_db"
    else:
        try:
            with c.cursor() as cur:
                for (day, partner, method, rule, status), n in counts.items():
                    cur.execute(_PARTNER_UPSERT,
                                (day, partner, method, rule[:200], status, n))
            c.commit()
            out["partner_keyless_rows"] = len(counts)
        except Exception as ex:
            try:
                c.rollback()
            except Exception:
                pass
            _requeue_partner(counts)
            out["partner_keyless_rows"] = 0
            out["partner_keyless_error"] = str(ex)[:200]
    try:
        out["partner_refs"] = (partner_attribution.flush(c) if partner_attribution
                               else {"refs": 0, "error": "partner_attribution unavailable"})
    except Exception as ex:
        out["partner_refs"] = {"refs": 0, "error": str(ex)[:200]}
    _safe_close(c)
    return out


def _tick() -> None:
    """One flusher pass: the keyed log, then the partner counts and refs. A
    failure in one never skips the other."""
    global _LAST_FLUSH
    try:
        _flush()
        _LAST_FLUSH = time.time()
    except Exception:
        pass
    try:
        _flush_partner()
    except Exception:
        pass


def _flush_loop() -> None:
    while True:
        time.sleep(_FLUSH_INTERVAL_SEC)
        _tick()


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


# === Identification ==================================================

def _bearer(headers) -> str:
    auth = headers.get("Authorization") or ""
    return auth[7:].strip() if auth.startswith("Bearer ") else ""


def _recorded_as(headers, args):
    """What one request is recorded under: a key's first STORED_PREFIX_LEN
    chars, a credential-class marker, or None (not recorded)."""
    ak = (headers.get("X-API-Key") or "").strip()
    if ak.startswith(ACCOUNT_KEY_PREFIX) and len(ak) >= STORED_PREFIX_LEN:
        return ak[:STORED_PREFIX_LEN]
    # r-admin-observability (2026-07-27): ADMIN traffic was invisible.
    # _record() below tracks a request only if it carries an X-API-Key,
    # and admin/cron calls authenticate with X-Admin-Key or
    # X-Internal-Key instead — so NONE of them were ever logged. Live
    # proof: api_endpoint_log held 56 admin rows out of 548,096, and 0
    # of ~50 admin reindex calls made on 2026-07-27.
    #
    # That blindness is not cosmetic. It made "which destructive admin
    # endpoint has NEVER actually run?" unanswerable, and Shell #37
    # lane 1 had to be rebuilt around it — `purge-noise` had a
    # catastrophically wrong predicate and survived only because
    # nobody happened to call it. You cannot audit a surface you
    # cannot see.
    #
    # ★ The marker is a CREDENTIAL CLASS, never the secret. Storing a
    # hash would still be needless key material in a 90-day table; the
    # question this answers is "was this endpoint ever invoked, and by
    # what kind of caller", which a class answers completely.
    # ★ It deliberately does NOT start with "dchub_", so it can never
    # collide with a real partner prefix — partner-usage reporting
    # filters `WHERE api_key_prefix = <dchub_...>` and stays exact.
    # ★ Query-string creds (?admin_key=) are detected but NEVER logged:
    # _collapse_path() records request.path only, which excludes the
    # query string.
    if args.get("admin_key"):
        return "admin"
    for marker, names in _SERVER_CREDENTIAL_CLASSES:
        if any(headers.get(n) for n in names):
            return marker
    # r-self-serve-rest (2026-09-21): a self-serve key was never recorded, so
    # REST use by every /api/v1/keys/claim key reached no per-key table — web-map
    # sends its key as X-API-Key (js/map.js) AND as Bearer (map.html →
    # /api/auth/me). Both are read now, X-API-Key first, as the backend resolves
    # them. ★ Only when NO server credential is present, even an empty one: the
    # MCP server's callAPI()/callAPIWrite() (dchub-mcp-server server.mjs) send
    # X-Internal-Key WITH the caller's X-API-Key on every tool's backend fan-out.
    # Recording that under the key would count MCP use as REST use; it stays
    # "internal", exactly as before this change.
    if "admin_key" in args or any(
            n in headers for _, names in _SERVER_CREDENTIAL_CLASSES for n in names):
        return None
    key = ak or _bearer(headers)
    if key.startswith(SELF_SERVE_KEY_PREFIX) and len(key) >= STORED_PREFIX_LEN:
        return key[:STORED_PREFIX_LEN]
    return None


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
        recorded_as = _recorded_as(request.headers, request.args)
        if recorded_as:
            g._usage_key_prefix = recorded_as
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
                # Keyed requests only, with one exception: keyless traffic
                # from a declared partner egress is COUNTED (not logged).
                _count_partner_keyless(response)
                return response
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
    return jsonify({"ok": True, **result, "partner": _flush_partner()}), 200


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


PARTNER_TRAFFIC_BASIS = {
    "keyless_requests": (
        "Requests the ORIGIN answered with no API key from a declared partner "
        "egress (partner_egress.py), as the rate limiter classifies them (the "
        "'partner' tier), counted per UTC day, method, matched route template "
        "and status, 429s included. A response the edge served from its cache "
        "never reached the origin and is not here. Each replica flushes every "
        "flush_interval_sec, so the newest interval is not in the table yet."),
    "attribution": (
        "refs: the DCM- pair codes on the paywalls served to those requests, "
        "and the a- offer ids on the /go/c links of walls built for them "
        "(routes/partner_attribution.py). pricing_clicks: /go/p presses whose "
        "ref is /pricing's ref_<code>__ wrapper around one; go_c_clicks: signed "
        "/go/c clicks on one; bot user agents are not excluded from either. "
        "payments: live-mode paid checkouts whose client_reference_id is a "
        "recorded ref or that wrapper. Walls served from a shared cache carry "
        "no partner ref, so these are floors."),
}


@api_usage_tracker_bp.route("/api/v1/admin/usage-tracker/partner-traffic", methods=["GET"])
def partner_traffic():
    """Keyless requests from declared partner egresses per day, route and
    status, and what the pay links served to them led to. Admin only."""
    if not _admin_authorized():
        return jsonify({"ok": False, "error": "admin_key_required"}), 401
    try:
        days = max(1, min(int(request.args.get("days", 30)), 400))
    except (TypeError, ValueError):
        days = 30
    c = _pg_conn()
    if c is None:
        return jsonify({"ok": False, "error": "no_db"}), 503
    try:
        with c.cursor() as cur:
            cur.execute("SELECT to_regclass('partner_keyless_daily') IS NOT NULL")
            if not cur.fetchone()[0]:
                keyless = "absent: partner_keyless_daily does not exist yet"
            else:
                cur.execute(
                    "SELECT usage_date, partner, method, endpoint_rule, status, requests "
                    "  FROM partner_keyless_daily "
                    " WHERE usage_date > (NOW() AT TIME ZONE 'UTC')::date - %s "
                    " ORDER BY usage_date DESC, requests DESC, endpoint_rule",
                    (days,))
                rows = [{"date": d.isoformat(), "partner": pt, "method": m,
                         "route": r, "status": int(st), "requests": int(n)}
                        for d, pt, m, r, st, n in cur.fetchall()]
                by_partner: dict = {}
                for row in rows:
                    agg = by_partner.setdefault(row["partner"], {
                        "total": 0, "by_day": {}, "by_route": {}, "by_status": {}})
                    agg["total"] += row["requests"]
                    for field, val in (("by_day", row["date"]),
                                       ("by_route", row["method"] + " " + row["route"]),
                                       ("by_status", str(row["status"]))):
                        agg[field][val] = agg[field].get(val, 0) + row["requests"]
                keyless = {"partners": by_partner, "rows": rows}
            from routes.partner_attribution import read_attribution
            attribution = read_attribution(cur, days)
    except Exception as ex:
        _safe_close(c)
        return jsonify({"ok": False, "error": str(ex)[:200]}), 500
    _safe_close(c)
    with _PARTNER_LOCK:
        buffered = sum(_PARTNER_COUNTS.values())
    return jsonify({
        "ok": True,
        "window_days": days,
        "keyless_requests": keyless,
        "attribution": attribution,
        "this_process": {"buffered_unflushed": buffered,
                         "dropped_buffer_full": _PARTNER_DROPPED[0]},
        "basis": PARTNER_TRAFFIC_BASIS,
    }), 200
