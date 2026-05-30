"""
Brain HTTP Error Capture (2026-05-19) — the missing real-time signal.

Existing detectors run every 4-6h via the consistency radar. That's
fine for SLA breaches and schema drift, but useless for live failures:
the user already hit the broken page hours before the radar notices.

This module is a Flask middleware that captures EVERY 4xx + 5xx
response into an in-memory ring buffer + a brain_http_errors table.
Brain L21 (Auto-Pilot) reads this every 60s instead of every 6h —
that's the 360x detection speedup needed for MTTR-5.

What gets captured per error:
  - path (regex-collapsed to pattern: /api/v1/facility/<slug>)
  - method
  - status
  - referer (which page sent the broken request)
  - timestamp
  - 200-char response body preview (helps L21 classify the fix)

Capacity:
  - In-memory ring buffer: last 500 errors (microsecond reads for L21)
  - DB writes batched every 30s OR when buffer hits 100 (whichever first)
  - Auto-trim DB to 7 days

Endpoints:
  GET  /api/v1/brain/http-errors            — recent errors (5min window)
  GET  /api/v1/brain/http-errors/patterns   — grouped by pattern
"""

import os
import re
import time
import threading
import logging
import datetime as _dt
from collections import deque
from flask import Blueprint, jsonify, request, g

logger = logging.getLogger(__name__)
brain_http_capture_bp = Blueprint("brain_http_capture", __name__)

# Ring buffer: last 500 errors, microsecond reads, lock-free for appenders
_ERROR_BUFFER: deque = deque(maxlen=500)
_BUFFER_LOCK = threading.Lock()
_LAST_FLUSH_AT = 0.0
_FLUSH_INTERVAL_SECONDS = 30
_FLUSH_BATCH_SIZE = 100

# Path-to-pattern collapsers — keep this list short, regex is hot path
_PATH_PATTERNS = [
    (re.compile(r"/api/v1/facility/[^/]+"), "/api/v1/facility/<slug>"),
    (re.compile(r"/api/v1/facilities/[^/]+"), "/api/v1/facilities/<slug>"),
    (re.compile(r"/api/v1/markets/[^/]+"), "/api/v1/markets/<slug>"),
    (re.compile(r"/api/v1/operators/[^/]+"), "/api/v1/operators/<slug>"),
    (re.compile(r"/redeem/[^/]+"), "/redeem/<code>"),
    (re.compile(r"/dcpi/[^/]+"), "/dcpi/<slug>"),
    (re.compile(r"/markets/[^/]+"), "/markets/<slug>"),
    (re.compile(r"/operators/[^/]+"), "/operators/<slug>"),
    (re.compile(r"/iso/[^/]+"), "/iso/<iso>"),
    (re.compile(r"/transactions/[^/]+"), "/transactions/<id>"),
    (re.compile(r"/news/[^/]+"), "/news/<slug>"),
]


def _collapse_path(path: str) -> str:
    """Collapse dynamic segments to <param> so grouping works."""
    for pat, replacement in _PATH_PATTERNS:
        m = pat.match(path)
        if m:
            return replacement
    return path


def _ensure_table():
    try:
        from main import get_db
        conn = get_db()
        if not conn: return
        try:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS brain_http_errors (
                    id          BIGSERIAL PRIMARY KEY,
                    occurred_at TIMESTAMPTZ DEFAULT NOW(),
                    method      TEXT,
                    path        TEXT,
                    pattern     TEXT,
                    status      INTEGER,
                    referer     TEXT,
                    body_preview TEXT
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_http_err_time "
                        "ON brain_http_errors(occurred_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_http_err_pattern_time "
                        "ON brain_http_errors(pattern, occurred_at DESC)")
            conn.commit()
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"[http-capture] table create failed: {e}")


# ── Flask middleware hooks (caller registers in main.py) ────────────

def register_capture(app):
    """Call this from main.py: `register_capture(app)`. Idempotent."""
    if getattr(app, "_http_capture_registered", False):
        return
    app._http_capture_registered = True
    # r43-bootfix (2026-05-29): do NOT call _ensure_table() here.
    # register_capture() runs at module-import time in main.py (~line 1556),
    # which is BEFORE main.get_db is defined (~line 4224). The eager
    # `from main import get_db` therefore hit a partially-initialized
    # `main` and raised "cannot import name 'get_db'", logged every boot as
    # "[http-capture] table create failed". It was cosmetic (the table got
    # created moments later) but it was real boot noise. The background
    # flush thread started just below calls _flush_to_db() after a 30s
    # sleep, and _flush_to_db() does its own CREATE TABLE IF NOT EXISTS on
    # its connection — by then `main` is fully initialized. So table
    # creation is simply deferred to first flush; nothing else needs it
    # sooner (reads tolerate a missing table).

    @app.after_request
    def _capture_errors(response):
        try:
            status = response.status_code
            if status < 400:
                return response  # only capture errors
            # Skip the radar's own probes (would create feedback loop)
            ua = (request.headers.get("User-Agent") or "")[:200]
            if "dchub-brain-radar" in ua or "brain-l11" in ua.lower():
                return response
            path = request.path[:300]
            pattern = _collapse_path(path)
            referer = (request.headers.get("Referer") or "")[:300]
            body_preview = ""
            try:
                if response.is_json:
                    body_preview = str(response.get_json())[:200]
                else:
                    body_preview = response.get_data(as_text=True)[:200]
            except Exception: pass
            entry = {
                "occurred_at": time.time(),
                "method": request.method,
                "path": path,
                "pattern": pattern,
                "status": status,
                "referer": referer,
                "body_preview": body_preview,
            }
            with _BUFFER_LOCK:
                _ERROR_BUFFER.append(entry)
            # Flush conditions
            _maybe_flush()
        except Exception:
            pass  # NEVER let middleware break a response
        return response

    # Start background flush thread
    threading.Thread(target=_flush_loop, daemon=True,
                     name="brain-http-capture-flush").start()


def _maybe_flush():
    """Trigger a flush if buffer is large or interval elapsed."""
    global _LAST_FLUSH_AT
    now = time.time()
    if (len(_ERROR_BUFFER) >= _FLUSH_BATCH_SIZE or
            (now - _LAST_FLUSH_AT) > _FLUSH_INTERVAL_SECONDS):
        threading.Thread(target=_flush_to_db, daemon=True,
                         name="brain-http-capture-flush-once").start()


def _flush_loop():
    """Background loop: flush buffer every 30s regardless."""
    while True:
        try:
            time.sleep(_FLUSH_INTERVAL_SECONDS)
            _flush_to_db()
        except Exception as e:
            logger.warning(f"[http-capture] flush loop iteration failed: {e}")


_TABLE_ENSURED = False


def _flush_to_db():
    """Write current buffer contents to brain_http_errors."""
    global _LAST_FLUSH_AT
    with _BUFFER_LOCK:
        to_write = list(_ERROR_BUFFER)
        _ERROR_BUFFER.clear()
    _LAST_FLUSH_AT = time.time()
    if not to_write:
        return
    # Phase FF+7-survive (2026-05-19) — final race-condition fix. The
    # module-level _TABLE_ENSURED flag wasn't shared across worker
    # processes, so each fresh worker would re-hit "relation does not
    # exist" once. We now CREATE the table on the SAME connection right
    # before the inserts run, in the same transaction view. This makes
    # the operation truly idempotent per-connection.
    try:
        from main import get_db
        conn = get_db()
        if not conn: return
        try:
            cur = conn.cursor()
            # Ensure table on THIS connection's view before inserting.
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS brain_http_errors (
                        id          BIGSERIAL PRIMARY KEY,
                        occurred_at TIMESTAMPTZ DEFAULT NOW(),
                        method      TEXT,
                        path        TEXT,
                        pattern     TEXT,
                        status      INTEGER,
                        referer     TEXT,
                        body_preview TEXT
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_http_err_time "
                            "ON brain_http_errors(occurred_at DESC)")
                conn.commit()
            except Exception:
                try: conn.rollback()
                except Exception: pass
            for e in to_write:
                try:
                    cur.execute(
                        "INSERT INTO brain_http_errors "
                        "(occurred_at, method, path, pattern, status, "
                        " referer, body_preview) "
                        "VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s)",
                        (e["occurred_at"], e["method"], e["path"],
                         e["pattern"], e["status"],
                         e["referer"][:300], e["body_preview"][:300]),
                    )
                except Exception:
                    try: conn.rollback()
                    except Exception: pass
            # Trim old rows opportunistically — keep last 7 days
            try:
                cur.execute("DELETE FROM brain_http_errors "
                            "WHERE occurred_at < NOW() - INTERVAL '7 days'")
            except Exception:
                try: conn.rollback()
                except Exception: pass
            try: conn.commit()
            except Exception: pass
        finally:
            try: conn.close()
            except Exception: pass
    except Exception as e:
        logger.warning(f"[http-capture] flush write failed: {e}")


# ── Public helpers for L21 + radar ──────────────────────────────────

def get_recent_errors(window_seconds: int = 300) -> list[dict]:
    """Brain L21 reads this every 60s. Returns recent errors from
    the in-memory buffer (fast — no DB hit)."""
    cutoff = time.time() - window_seconds
    with _BUFFER_LOCK:
        snap = list(_ERROR_BUFFER)
    return [e for e in snap if e["occurred_at"] > cutoff]


def get_pattern_counts(window_seconds: int = 300) -> dict[str, int]:
    """Pattern → count map. Used by L21 to detect spikes."""
    out: dict[str, int] = {}
    for e in get_recent_errors(window_seconds):
        k = f"{e['method']} {e['pattern']} [{e['status']}]"
        out[k] = out.get(k, 0) + 1
    return out


# ── Endpoints ───────────────────────────────────────────────────────

@brain_http_capture_bp.route("/api/v1/brain/http-errors", methods=["GET"])
def http_errors_recent():
    """Recent errors (default 5min window)."""
    window = int(request.args.get("window", "300"))
    errors = get_recent_errors(window)
    # Most recent first
    errors_sorted = sorted(errors, key=lambda e: -e["occurred_at"])[:50]
    return jsonify(
        ok=True,
        window_seconds=window,
        count=len(errors),
        recent=[{
            "at": _dt.datetime.fromtimestamp(e["occurred_at"]).isoformat(),
            "method": e["method"],
            "path": e["path"],
            "pattern": e["pattern"],
            "status": e["status"],
            "referer": e["referer"][:120],
            "body_preview": e["body_preview"][:120],
        } for e in errors_sorted],
    )


@brain_http_capture_bp.route("/api/v1/brain/http-errors/patterns",
                              methods=["GET"])
def http_errors_patterns():
    """Patterns grouped by count. L21 reads this to detect spikes."""
    window = int(request.args.get("window", "300"))
    patterns = get_pattern_counts(window)
    sorted_patterns = sorted(patterns.items(), key=lambda kv: -kv[1])
    return jsonify(
        ok=True,
        window_seconds=window,
        patterns=[{"pattern": p, "count": n} for p, n in sorted_patterns],
        total_errors=sum(patterns.values()),
        unique_patterns=len(patterns),
    )
