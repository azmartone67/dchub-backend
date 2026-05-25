"""
sentinel_ratelimit_bypass.py — exempt internal sentinel UA from rate limits.

Phase ZZZZZ-round38 (2026-05-25). Sentinel reported 9 routes 429ing:
/total-power, /live-pulse, /bs-translator, /markets/, /spare-capacity,
/pocket-listings, /grid + 3 grid ISO pages. These aren't real overload —
the sentinel polls every page every minute from a single IP (162.220.232.99
= Railway's outbound shared IP), trips the per-IP rate limit, then reports
its own polls as 429 errors.

This module registers a before_request hook that bypasses the rate
limiter when the request has User-Agent matching DCHub-internal patterns.
The legitimate per-IP rate limit still applies to real human traffic from
the same IP — only the sentinel/warmer/healer UAs get the bypass.
"""
import os
from flask import Blueprint, current_app, request, jsonify

ratelimit_bypass_bp = Blueprint("ratelimit_bypass", __name__)

# Internal UA patterns that should bypass rate limits
INTERNAL_UA_PATTERNS = (
    "DCHub-",                       # DCHub-Warmer, DCHub-BrainRadar, DCHub-Healer, etc.
    "DCHub-Sentinel",               # site sentinel
    "Brain-v2-headless",            # Layer 4 brain probes
    "dchub-brain-deadlink-probe",   # dead-link probe
    "dchub-frontend-health",        # frontend health checks
    "dchub-cors-probe",             # cors probes
    "GH-Actions-CronHeartbeat",     # GH Actions cron
)


def _is_internal(ua):
    if not ua: return False
    return any(p in ua for p in INTERNAL_UA_PATTERNS)


@ratelimit_bypass_bp.before_app_request
def _bypass_rate_limit_for_internal():
    """Tag internal-UA requests with a flag that the rate limiter checks."""
    ua = request.headers.get("User-Agent", "")
    if _is_internal(ua):
        # Set flask.g flag — existing rate limiter middleware reads this
        from flask import g
        g.dchub_internal_bypass = True
        g.dchub_internal_ua = ua[:80]


@ratelimit_bypass_bp.route("/api/v1/internal-bypass/health", methods=["GET"])
def health():
    return jsonify({
        "blueprint": "ratelimit_bypass_bp",
        "patterns_count": len(INTERNAL_UA_PATTERNS),
        "patterns": list(INTERNAL_UA_PATTERNS),
        "would_bypass_current_request": _is_internal(request.headers.get("User-Agent", "")),
        "current_ua": request.headers.get("User-Agent", "")[:120],
    }), 200
