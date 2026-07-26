"""routes/perf_timing.py — slow-request capture (seven-levers #32, 2026-07-25).

WHY: the p99 tail was 10.8s / p95 3.9s on ship day and the ONLY place that
number existed was Railway's HTTP logs — which the app, the brain, and the
shells cannot read. brain_http_errors captures failures; nothing captured
slowness, so the tail had no owner, no trend, and no per-path attribution.

WHAT: two app-level hooks. before_app_request stamps a monotonic t0;
after_app_request computes dt and, ONLY for requests slower than
PERF_SLOW_MS (default 2000ms), records path/method/status/duration into
slow_requests — sampled, bounded, and fail-soft.

HOT-PATH DISCIPLINE (this runs on EVERY request):
  · fast path cost is one time.monotonic() call and one subtraction;
  · a DB write happens only for requests that were ALREADY ≥2s slow, and
    at most once per _MIN_WRITE_GAP_S per process (a stampede of slow
    requests records one row, not a write-storm on a struggling DB);
  · the path is normalized (query stripped, numeric/hex segments →
    ':id') so cardinality stays bounded;
  · EVERYTHING is wrapped — a telemetry failure must never fail a
    request. No exception escapes either hook.

Kill: PERF_TIMING_DISABLE=1 (checked per request, env-flip takes effect
without redeploy). Read surface: the Seven Levers shell (lane 3) reads
slow_requests for count/worst-path; there is no HTTP endpoint here.
"""

from __future__ import annotations

import logging
import os
import re
import time

from flask import Blueprint, g, request

logger = logging.getLogger(__name__)

perf_timing_bp = Blueprint("perf_timing", __name__)

_MIN_WRITE_GAP_S = 10.0     # at most one slow-row per process per 10s
_LAST_WRITE = [0.0]         # boxed for closure mutation
_DDL_DONE = [False]

# /api/v1/facility/abc123 → /api/v1/facility/:id — keeps path cardinality
# bounded so GROUP BY path stays useful. Hex ≥8 chars catches slugs/hashes.
_NUM_SEG = re.compile(r"/(?:\d+|[0-9a-f]{8,})(?=/|$)")


def _slow_ms() -> float:
    try:
        return float(os.environ.get("PERF_SLOW_MS", "2000"))
    except Exception:
        return 2000.0


def _disabled() -> bool:
    return (os.environ.get("PERF_TIMING_DISABLE") or "").strip() == "1"


def _norm_path(p: str) -> str:
    try:
        return _NUM_SEG.sub("/:id", (p or "/").split("?")[0])[:160]
    except Exception:
        return "/?"


def _record(path: str, method: str, status: int, dt_ms: int) -> None:
    """Best-effort insert. Never raises. Rate-limited per process."""
    now = time.monotonic()
    if now - _LAST_WRITE[0] < _MIN_WRITE_GAP_S:
        return
    _LAST_WRITE[0] = now
    url = (os.environ.get("DATABASE_URL")
           or os.environ.get("NEON_DATABASE_URL") or "").strip()
    if not url:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=3)
        try:
            with conn.cursor() as cur:
                if not _DDL_DONE[0]:
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS slow_requests ("
                        " id BIGSERIAL PRIMARY KEY,"
                        " ts TIMESTAMPTZ DEFAULT NOW(),"
                        " path TEXT,"
                        " method TEXT,"
                        " status INT,"
                        " dt_ms INT)")
                    _DDL_DONE[0] = True
                cur.execute(
                    "INSERT INTO slow_requests (path, method, status, dt_ms)"
                    " VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (path, method[:12], int(status), int(dt_ms)))
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:  # noqa: BLE001 — telemetry must never break serving
        logger.debug("slow_requests write swallowed: %s", e)


@perf_timing_bp.before_app_request
def _perf_t0():
    try:
        g._perf_t0 = time.monotonic()
    except Exception:
        pass


@perf_timing_bp.after_app_request
def _perf_capture(resp):
    try:
        t0 = getattr(g, "_perf_t0", None)
        if t0 is None or _disabled():
            return resp
        dt_ms = (time.monotonic() - t0) * 1000.0
        if dt_ms >= _slow_ms():
            _record(_norm_path(request.path), request.method,
                    resp.status_code, int(dt_ms))
    except Exception:  # noqa: BLE001
        pass
    return resp
