# rate_limiter.py
# DC Hub - Rate Limiting Middleware
# Location: root level (alongside main.py)
# Integration: 3 lines in main.py (see bottom of file)
# No external dependencies - pure Python stdlib
# ============================================================================

import os
import time
import logging
from functools import wraps
from flask import request, jsonify, g

logger = logging.getLogger(__name__)

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

    return False, 0, max(1, int(window - (now - b['ts'])))


# ---------------------------------------------------------------------------
# Plan-based limits (matches users.plan column: free/pro/enterprise)
# ---------------------------------------------------------------------------

LIMITS = {
    'free':       {'rpm': 30,  'rph': 500},
    'pro':        {'rpm': 120, 'rph': 5000},
    'enterprise': {'rpm': 600, 'rph': 50000},
    'internal':   {'rpm': 300, 'rph': 20000},  # MCP / X-Internal-Key
}

ANON_LIMITS = {'rpm': 20, 'rph': 200}


def _get_key():
    """Client identifier: user_id > IP"""
    uid = getattr(g, 'user_id', None)
    if uid:
        return f"u:{uid}"
    ip = (request.headers.get('CF-Connecting-IP') or
          request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
          request.remote_addr)
    return f"ip:{ip}"


def _get_plan():
    """Detect plan tier."""
    # MCP internal bypass
    ik = request.headers.get('X-Internal-Key')
    expected = os.environ.get('MCP_INTERNAL_KEY') or os.environ.get('INTERNAL_API_KEY')
    if ik and expected and ik == expected:
        return 'internal'

    plan = getattr(g, 'user_plan', None)
    if plan in LIMITS:
        return plan
    return None


# ---------------------------------------------------------------------------
# Flask middleware
# ---------------------------------------------------------------------------

SKIP_PATHS = frozenset([
    '/health', '/api/health', '/api/v1/circuit-status', '/favicon.ico'
])


def rate_limit_before():
    """Register as app.before_request(rate_limit_before)"""
    path = request.path

    # Skip health checks and static
    if path in SKIP_PATHS or path.startswith(('/static/', '/assets/')):
        return None

    key = _get_key()
    plan = _get_plan()
    limits = LIMITS.get(plan, ANON_LIMITS)

    # Per-minute check
    ok, remaining, retry = _check(f"{key}:min", limits['rpm'], 60)
    if not ok:
        logger.warning(f"Rate limit: {key} plan={plan} path={path}")
        return _resp(remaining, retry)

    # Per-hour check
    ok_h, rem_h, retry_h = _check(f"{key}:hr", limits['rph'], 3600)
    if not ok_h:
        logger.warning(f"Hourly limit: {key} plan={plan} path={path}")
        return _resp(rem_h, retry_h)

    g._rl_remaining = remaining
    return None


def rate_limit_after(response):
    """Register as app.after_request(rate_limit_after)"""
    rem = getattr(g, '_rl_remaining', None)
    if rem is not None:
        response.headers['X-RateLimit-Remaining'] = str(rem)
    return response


def _resp(remaining, retry_after):
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
    Usage:
        @app.route('/api/site-score', methods=['POST'])
        @rate_limit(rpm=5)
        def site_score():
            ...
    """
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            key = _get_key()
            ok, _, retry = _check(f"{key}:route:{f.__name__}", rpm, 60)
            if not ok:
                return _resp(0, retry)
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ---------------------------------------------------------------------------
# main.py integration (3 lines):
#
#   from rate_limiter import rate_limit_before, rate_limit_after
#   app.before_request(rate_limit_before)
#   app.after_request(rate_limit_after)
#
# Optional per-route:
#   from rate_limiter import rate_limit
#   @rate_limit(rpm=5)
#
# ---------------------------------------------------------------------------
