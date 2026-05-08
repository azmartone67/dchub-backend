"""
dchub_heartbeat.py — tiny client for the source-registry heartbeat endpoint.

Usage from any extractor:

    from dchub_heartbeat import heartbeat
    heartbeat("backend-eia-api", status="success", rows_affected=142)

Or as a context manager that auto-times and reports:

    from dchub_heartbeat import tracked_run
    with tracked_run("backend-news-engine") as run:
        rows = do_extraction()
        run.rows_affected = rows
    # heartbeat fires automatically with success/failure on exit

Or as a decorator on a main entry function:

    from dchub_heartbeat import with_heartbeat

    @with_heartbeat("backend-facility-ingestion")
    def main():
        ...
        return rows_processed   # gets used as rows_affected

Best-effort: never crashes the caller. Network errors get swallowed and
logged but the extractor continues.
"""

import json
import os
import time
import urllib.request
import urllib.error
from contextlib import contextmanager
from functools import wraps


HEARTBEAT_BASE = os.environ.get(
    "DCHUB_HEARTBEAT_BASE",
    "https://dchub.cloud/api/v1/sources",
)
HEARTBEAT_SECRET = os.environ.get(
    "DCHUB_ADMIN_SECRET",
    "dchub-admin-secret-2026",
)
HEARTBEAT_TIMEOUT = 5  # seconds — short, never blocks an extractor


def heartbeat(source_id, status="success", rows_affected=None,
              duration_ms=None, error=None, metadata=None):
    """Fire a single heartbeat. Returns True on success, False on any failure."""
    if not source_id:
        return False

    body = {"status": status}
    if rows_affected is not None:
        body["rows_affected"] = int(rows_affected)
    if duration_ms is not None:
        body["duration_ms"] = int(duration_ms)
    if error:
        body["error"] = str(error)[:500]
    if metadata:
        body["metadata"] = metadata

    url = f"{HEARTBEAT_BASE}/{source_id}/heartbeat"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Authorization": f"Bearer {HEARTBEAT_SECRET}",
            "Content-Type": "application/json",
            "User-Agent": "dchub-heartbeat/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HEARTBEAT_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False  # silent fail by design


@contextmanager
def tracked_run(source_id):
    """Context manager: auto-time the block, fire success/failure heartbeat.

    Set ctx.rows_affected = N inside the block to record row count.
    Set ctx.metadata = {...} for richer dashboard data.
    """
    class _Ctx:
        rows_affected = None
        metadata = None
    ctx = _Ctx()
    started = time.time()
    try:
        yield ctx
        elapsed_ms = int((time.time() - started) * 1000)
        heartbeat(
            source_id,
            status="success",
            rows_affected=ctx.rows_affected,
            duration_ms=elapsed_ms,
            metadata=ctx.metadata,
        )
    except Exception as e:
        elapsed_ms = int((time.time() - started) * 1000)
        heartbeat(
            source_id,
            status="failure",
            duration_ms=elapsed_ms,
            error=f"{type(e).__name__}: {e}",
        )
        raise


def with_heartbeat(source_id, on_success_use_return_as_rows=True):
    """Decorator: wrap a function with heartbeat reporting.

    If `on_success_use_return_as_rows=True` and the wrapped function
    returns an int, that int is used as rows_affected.
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            started = time.time()
            try:
                result = fn(*args, **kwargs)
                elapsed_ms = int((time.time() - started) * 1000)
                rows = result if (on_success_use_return_as_rows and isinstance(result, int)) else None
                heartbeat(
                    source_id,
                    status="success",
                    rows_affected=rows,
                    duration_ms=elapsed_ms,
                )
                return result
            except Exception as e:
                elapsed_ms = int((time.time() - started) * 1000)
                heartbeat(
                    source_id,
                    status="failure",
                    duration_ms=elapsed_ms,
                    error=f"{type(e).__name__}: {e}",
                )
                raise
        return wrapper
    return deco
