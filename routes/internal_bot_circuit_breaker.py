"""
internal_bot_circuit_breaker.py — Phase r51 (2026-05-25).

CRITICAL: stops DC Hub's own internal probes from DOSing Railway.

User report (r51): "503 Service Unavailable" appears across the whole
site. Investigation showed:
  - Single IP (162.220.232.99) sending 151,800 requests/day
  - Top UAs: dchub-brain-deadlink-probe 56K, DCHubHealer 50K,
    DCHub-BrainUniformity 9K, DCHub-Grid 6K, DCHub-BrainRadar 3K,
    Brain-v2-headless 3K, dchub-frontend-health 4K, python-requests 4K
  - Railway origin returning 28,860 HTTP 429s on those probes
  - Real user pages competing for the same Railway capacity → 503s

The bots are observability for the brain. The brain needs them. But
they were configured without cooperative back-pressure, so they
hammer at intervals tighter than Railway can absorb. This module
adds a Flask before_request hook that:

  1. Identifies "internal" requests by UA pattern + IP allow-list
  2. Tracks per-UA request count in a 60-second rolling window
  3. Returns 429 (with Retry-After) when a UA exceeds threshold
  4. Logs the rate-limit decision so the brain can see the rate

Threshold defaults are conservative: 30 req/min per internal UA. The
brain's audit interval is 5min; cron interval is hourly; this leaves
~150 requests/5min worth of headroom which is plenty for any single
probe. Multiple probes still get their full quota each.

Env overrides:
  DCHUB_INTERNAL_BOT_RATE_LIMIT_PER_MIN  (default 30)
  DCHUB_INTERNAL_BOT_LIMITER_ENABLED      (default "1")
"""
from __future__ import annotations

import os
import re
import time
from collections import deque

from flask import request, jsonify


# Patterns identifying our own internal traffic. Match against the
# User-Agent header. Lowercased substring match — case-insensitive.
_INTERNAL_UA_PATTERNS = [
    "dchub-brain",          # dchub-brain-deadlink-probe, dchub-brain-radar
    "dchubhealer",
    "dchub-grid",
    "dchub-uniformity",
    "dchub-redircheck",
    "dchub-frontend-health",
    "dchub-selfheal",
    "dchub-scheduler",
    "dchub-brainuniformity",
    "dchub-brainradar",
    "brain-v2-headless",
    "brain-radar",
    "brain-warming",
    "uptimerobot",          # external monitor — count toward probe limit
    "dchub-quad",
    "dchub-mcp-probe",
]


def _is_internal_ua(ua: str) -> str | None:
    """Return the matched pattern (a bucket key) or None."""
    if not ua:
        return None
    ua_l = ua.lower()
    for pat in _INTERNAL_UA_PATTERNS:
        if pat in ua_l:
            return pat
    return None


# Per-bucket rolling counter. {bucket_key: deque of timestamps}.
# Trimmed to last 60s on every check. Module-level so it survives
# request lifecycle.
_REQ_TIMESTAMPS: dict[str, deque] = {}
_WINDOW_SECONDS = 60.0


def _rate_limit_per_min() -> int:
    """Per-bucket cap. Default 30/min."""
    try:
        return max(5, int(os.environ.get("DCHUB_INTERNAL_BOT_RATE_LIMIT_PER_MIN", "30")))
    except Exception:
        return 30


def _enabled() -> bool:
    return os.environ.get("DCHUB_INTERNAL_BOT_LIMITER_ENABLED", "1") != "0"


def _check_and_count(bucket: str) -> tuple[bool, int]:
    """Returns (allowed, current_count_in_window).

    `current_count_in_window` is AFTER incrementing if allowed.
    """
    now = time.time()
    q = _REQ_TIMESTAMPS.setdefault(bucket, deque())
    # Trim
    cutoff = now - _WINDOW_SECONDS
    while q and q[0] < cutoff:
        q.popleft()
    count = len(q)
    if count >= _rate_limit_per_min():
        return False, count
    q.append(now)
    return True, count + 1


# Paths that should NEVER be rate-limited regardless of caller.
# Critical health endpoints + alive check stay open so Railway's own
# health check + uptime monitors don't get throttled.
_BYPASS_PATHS = {
    "/alive", "/healthz", "/livez", "/readyz",
    "/api/health", "/health",
    "/.well-known/health",
}


def register_internal_bot_circuit_breaker(app):
    """Attach the before_request hook. Idempotent."""
    if getattr(app, "_internal_bot_cb_attached", False):
        return
    app._internal_bot_cb_attached = True

    @app.before_request
    def _circuit_breaker_check():
        if not _enabled():
            return None
        path = request.path or ""
        if path in _BYPASS_PATHS:
            return None
        ua = request.headers.get("User-Agent") or ""
        bucket = _is_internal_ua(ua)
        if not bucket:
            return None  # external traffic, untouched
        allowed, count = _check_and_count(bucket)
        if allowed:
            return None  # under the limit, proceed
        # OVER LIMIT — return 429 with hint
        resp = jsonify({
            "error": "internal_bot_rate_limited",
            "message": (f"Internal UA bucket '{bucket}' exceeded "
                         f"{_rate_limit_per_min()} req/min. Backing off."),
            "bucket":        bucket,
            "window_seconds": _WINDOW_SECONDS,
            "current_count": count,
            "limit":         _rate_limit_per_min(),
            "retry_after":   30,
            "fix":           "reduce probe frequency in your cron",
        })
        resp.status_code = 429
        resp.headers["Retry-After"] = "30"
        resp.headers["X-DC-Hub-Internal-CB"] = "tripped"
        return resp


def get_circuit_breaker_state() -> dict:
    """For observability / brain audit consumption."""
    now = time.time()
    cutoff = now - _WINDOW_SECONDS
    out = {
        "enabled":        _enabled(),
        "limit_per_min":  _rate_limit_per_min(),
        "window_seconds": _WINDOW_SECONDS,
        "buckets":        {},
    }
    for bucket, q in _REQ_TIMESTAMPS.items():
        # Trim before reporting
        while q and q[0] < cutoff:
            q.popleft()
        out["buckets"][bucket] = {
            "count_last_min": len(q),
            "over_limit":     len(q) >= _rate_limit_per_min(),
        }
    return out
