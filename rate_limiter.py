# rate_limiter.py
# DC Hub - Rate Limiting Middleware
# Location: root level (alongside main.py)
# Integration: 3 lines in main.py (see bottom of file)
# No external dependencies - pure Python stdlib
# ============================================================================

import time
import logging
from functools import wraps
from flask import request, jsonify, g

logger = logging.getLogger('rate_limiter')

# ---------------------------------------------------------------------------
# Token bucket - in-memory, resets on deploy (fine for single Railway instance)
# ---------------------------------------------------------------------------

_buckets = {}
_last_cleanup = time.time()


def _cleanup():
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 300:
        return
    cutoff = now - 3600
    stale = [k for k, v in _buckets.items() if v['ts'] < cutoff]
    for k in stale:
        del _buckets[k]
    _last_cleanup = now


def _check(key, limit, window=60):
    """Returns (allowed, remaining, retry_after)"""
    _cleanup()
    now = time.time()

    if key not in _buckets:
        _buckets[key] = {'tokens': limit - 1, 'ts': now}
        return True, limit - 1, 0

    b = _buckets[key]
    elapsed = now - b['ts']
    refills = int(elapsed / window)

    if refills > 0:
        b['tokens'] = min(limit, b['tokens'] + (refills * limit))
        b['ts'] = now

    if b['tokens'] > 0:
        b['tokens'] -= 1
        return True, b['tokens'], 0

    return False, 0, max(1, int(window - elapsed))


# ---------------------------------------------------------------------------
# Limits by client type
# Uses request.user from JWT (set by require_auth / optional_auth decorators)
# JWT payload has: user_id, email, role — but NOT plan
# So we tier by: internal > authenticated > anonymous
# ---------------------------------------------------------------------------

LIMITS = {
    'internal':      {'rpm': 300, 'rph': 20000},   # MCP / X-Internal-Key
    'authenticated': {'rpm': 120, 'rph': 5000},     # Any logged-in user
    'anonymous':     {'rpm': 20,  'rph': 200},      # No auth
}

# DC Hub internal key values (same as used in main.py route guards)
INTERNAL_KEYS = frozenset(['dchub-internal-2024', 'dchub-internal-sync-2026'])


def _get_client_ip():
    """Get real client IP via Cloudflare headers."""
    return (request.headers.get('CF-Connecting-IP') or
            request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
            request.remote_addr or 'unknown')


def _get_key_and_tier():
    """
    Identify client and their rate limit tier.
    Checks: X-Internal-Key → request.user (JWT) → IP
    """
    # 1. MCP / internal traffic
    ik = request.headers.get('X-Internal-Key', '')
    if ik in INTERNAL_KEYS:
        return 'internal:mcp', 'internal'

    # 2. Authenticated user (JWT decoded by require_auth / optional_auth)
    user = getattr(request, 'user', None)
    if user and isinstance(user, dict):
        uid = user.get('user_id') or user.get('email') or 'unknown'
        return f'user:{uid}', 'authenticated'

    # 3. Anonymous - rate limit by IP
    return f'ip:{_get_client_ip()}', 'anonymous'


# ---------------------------------------------------------------------------
# Paths to skip (health checks, static assets)
# ---------------------------------------------------------------------------

SKIP_PATHS = frozenset([
    '/health', '/api/health', '/api/v1/circuit-status', '/favicon.ico',
    '/robots.txt', '/sitemap.xml', '/.well-known/mcp-registry-auth',
])

SKIP_PREFIXES = ('/static/', '/assets/', '/js/', '/css/', '/images/')


# ---------------------------------------------------------------------------
# Flask middleware
# ---------------------------------------------------------------------------

def rate_limit_before():
    """
    Register as: app.before_request(rate_limit_before)
    Place AFTER the request timer, BEFORE route handlers.
    """
    # BUG-003 FIX: Skip rate limiting for dchub.cloud frontend requests
    origin = request.headers.get("Origin", "") or request.headers.get("Referer", "")
    if "dchub.cloud" in origin:
        return None

    path = request.path

    # Skip health checks and static files
    if path in SKIP_PATHS:
        return None
    if path.startswith(SKIP_PREFIXES):
        return None

    # Bypass localhost — test_client (tier gate self-test), health checks,
    # and internal calls from 127.0.0.1 should never be rate limited.
    # v2.6: Without this, verify_tier_gating() fires 70+ requests from
    # test_client (127.0.0.1) at startup and all get 429'd.
    raw_ip = request.remote_addr or ''
    if raw_ip in ('127.0.0.1', '::1', 'localhost'):
        return None

    # Bypass dchub.cloud frontend — the map fires dozens of spatial API
    # calls on every pan/zoom.  These are already gated by the tier-aware
    # enforce_tier_rate_limits() in main.py; double-limiting here causes
    # pro/paid users to see 429s with tier=anonymous.
    # v2.7: Fixes orange-dot disappearing bug on Land & Power map.
    origin = request.headers.get('Origin', '') or request.headers.get('Referer', '')
    if 'dchub.cloud' in origin:
        return None

    key, tier = _get_key_and_tier()
    limits = LIMITS[tier]

    # Per-minute check
    ok, remaining, retry = _check(f"{key}:min", limits['rpm'], 60)
    if not ok:
        logger.warning(f"Rate limit hit: {key} tier={tier} path={path} ip={_get_client_ip()}")
        return _resp(retry)

    # Per-hour check
    ok_h, rem_h, retry_h = _check(f"{key}:hr", limits['rph'], 3600)
    if not ok_h:
        logger.warning(f"Hourly limit hit: {key} tier={tier} path={path} ip={_get_client_ip()}")
        return _resp(retry_h)

    # Stash for after_request headers
    g._rl_remaining = remaining
    return None


def rate_limit_after(response):
    """
    Register as: app.after_request(rate_limit_after)
    Adds X-RateLimit-Remaining header to all responses.
    """
    rem = getattr(g, '_rl_remaining', None)
    if rem is not None:
        response.headers['X-RateLimit-Remaining'] = str(rem)
    return response


def _resp(retry_after):
    """429 Too Many Requests response."""
    r = jsonify({
        'error': 'rate_limit_exceeded',
        'message': f'Too many requests. Retry after {retry_after}s.',
        'retry_after': retry_after
    })
    r.status_code = 429
    r.headers['Retry-After'] = str(retry_after)
    return r


# ---------------------------------------------------------------------------
# Optional: per-route decorator for expensive endpoints
# ---------------------------------------------------------------------------

def rate_limit(rpm=10):
    """
    Per-route rate limiter on top of global limits.

    Usage:
        @app.route('/api/site-score', methods=['GET'])
        @rate_limit(rpm=5)
        def api_site_score():
            ...
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            key, _ = _get_key_and_tier()
            ok, _, retry = _check(f"{key}:route:{f.__name__}", rpm, 60)
            if not ok:
                return _resp(retry)
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# main.py integration — add these 3 lines after the request timeout middleware
# (around line 1108, after _check_request_timeout):
#
#   from rate_limiter import rate_limit_before, rate_limit_after
#   app.before_request(rate_limit_before)
#   app.after_request(rate_limit_after)
#
# ---------------------------------------------------------------------------
