"""Best-effort REST API-key usage tracking.

The `api_keys` table already has `calls_today` / `calls_total` / `usage_count` /
`last_used_at` columns, but the read endpoints never incremented them — so paid
REST keys (e.g. the ChatGPT Custom GPT's `dchub_ent_` key) showed 0 usage even
while being actively called, making GPT/partner adoption impossible to measure.

This wires the counters up for any `dchub_`-prefixed REST key. It runs in a
daemon thread OFF the request path and swallows every error, so it can never
slow, hang, or break a request — telemetry must be invisible to the caller.
`calls_today` self-resets via `last_reset_date` (no cron needed).
"""
import hashlib
import logging
import threading

log = logging.getLogger("api_key_usage")

_BUMP_SQL = """
    UPDATE api_keys
       SET calls_total     = COALESCE(calls_total, 0) + 1,
           usage_count     = COALESCE(usage_count, 0) + 1,
           calls_today     = CASE WHEN last_reset_date = CURRENT_DATE::text
                                  THEN COALESCE(calls_today, 0) + 1
                                  ELSE 1 END,
           last_reset_date = CURRENT_DATE::text,
           last_used_at    = NOW(),
           last_used       = NOW()
     WHERE key_hash = %s
"""


def _bump(key_hash):
    # Self-contained short-lived connection (off the request path, in a daemon
    # thread) — avoids any dependency on the app's pool from a worker thread.
    conn = None
    try:
        import os
        import psycopg2
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            return
        conn = psycopg2.connect(url, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute(_BUMP_SQL, (key_hash,))
        conn.commit()
    except Exception as e:  # best-effort telemetry — never propagate
        log.debug("usage bump skipped: %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def track_rest_key_usage(raw_key):
    """Fire-and-forget: increment usage counters for a `dchub_` REST key.

    No-op for missing keys, MCP keys (`dch_live_`), or anything else — only
    REST `dchub_*` keys are counted, so high-volume anonymous/MCP traffic is
    untouched.
    """
    try:
        if not raw_key or not raw_key.startswith("dchub_"):
            return
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        threading.Thread(target=_bump, args=(key_hash,), daemon=True).start()
    except Exception:
        pass
