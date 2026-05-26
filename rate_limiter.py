from internal_auth import is_valid_internal_key
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

logger = logging.getLogger('rate_limiter')

# ---------------------------------------------------------------------------
# Token bucket - in-memory, resets on deploy (fine for single Railway instance)
# ---------------------------------------------------------------------------

_buckets = {}
_last_cleanup = time.time()

# Phase FF+14-ratelog (2026-05-19) — per-key log throttle. A single
# bot IP (e.g. 162.220.232.99 on Spamhaus zen) was filling Railway
# logs with hundreds of "Rate limit hit:" lines per minute, which
# (a) makes real signal hard to spot and (b) costs Railway log
# storage / bandwidth. The rate limiter itself was already
# enforcing — we just don't need to log every single denial.
# Now: log at most LOG_BUDGET hits per LOG_WINDOW seconds per key.
_log_budget = {}   # key -> {'count': int, 'window_start': float, 'silenced_at': float|None}
_LOG_BUDGET_PER_WINDOW = 5     # max log lines per key per window
_LOG_BUDGET_WINDOW_SEC = 3600  # 1 hour


def _should_log_rate_hit(key: str) -> bool:
    """Return True if we should log this rate-limit hit for `key`.
    First N hits per hour log normally; the (N+1)th logs a single
    "silenced further hits" line; subsequent hits are silent until
    the window rolls over."""
    now = time.time()
    bucket = _log_budget.get(key)
    if bucket is None or now - bucket['window_start'] > _LOG_BUDGET_WINDOW_SEC:
        _log_budget[key] = {'count': 1, 'window_start': now, 'silenced_at': None}
        return True
    bucket['count'] += 1
    if bucket['count'] <= _LOG_BUDGET_PER_WINDOW:
        return True
    if bucket['silenced_at'] is None:
        bucket['silenced_at'] = now
        logger.warning(
            "Rate limit log throttle: key=%s exceeded %d hits/hour; "
            "further hits silenced until window rolls over.",
            key, _LOG_BUDGET_PER_WINDOW
        )
        return False
    return False


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
    # Phase FF (2026-05-22): public showcase/content pages (/dcpi, /markets,
    # /reports, /brain/*). These are the flagship SEO + AI-citation surfaces
    # (datacenterpowerindex.com redirects to /dcpi). Throttling anonymous
    # visitors AND crawlers at 20rpm/200rph here was producing 429s on the
    # most important pages — directly undercutting the citation strategy.
    # Generous (not unlimited): a hammering scraper still caps, but normal
    # crawl + browse never trips. These pages are cacheable + cheap.
    'public_content': {'rpm': 120, 'rph': 2000},
}

# DC Hub internal key values (same as used in main.py route guards)


def _get_client_ip():
    """Get real client IP via Cloudflare headers."""
    return (request.headers.get('CF-Connecting-IP') or
            request.headers.get('X-Forwarded-For', '').split(',')[0].strip() or
            request.remote_addr or 'unknown')


def _get_key_and_tier():
    """
    Identify client and their rate limit tier.
    Checks: X-Internal-Key → X-API-Key → request.user (JWT) → IP

    PATCH 2026-04-24 (jm): Added X-API-Key recognition. Customers calling
    any API route (not just /mcp) with a valid-looking API key were being
    lumped into anonymous IP-based rate limiting (20 rpm), which is way
    too low for real usage patterns. Paying customers with keys now get
    'authenticated' tier (120 rpm / 5000 rph). This is a first-gate check
    — downstream handlers still validate the key properly via
    api_tier_gating.validate_api_key(). We don't DB-validate here (it
    would add a round-trip per request and the rate limiter should be
    cheap); we just recognize the format and trust-but-verify.
    """
    # 1. MCP / internal service-to-service traffic
    ik = request.headers.get('X-Internal-Key', '')
    if is_valid_internal_key(ik):
        return 'internal:mcp', 'internal'

    # 2. Customer API-key traffic (dchub_*, dch_* prefixes)
    # We use the key's prefix as the bucket key so a Pro customer's burst
    # of 100 requests in a second doesn't spill into another customer's bucket.
    api_key = (
        request.headers.get('X-API-Key', '') or
        request.args.get('api_key', '')
    )
    if not api_key:
        auth_h = request.headers.get('Authorization', '')
        if auth_h.startswith('Bearer ') and auth_h[7:].startswith(('dchub_', 'dch_')):
            api_key = auth_h[7:]
    if api_key and api_key.startswith(('dchub_', 'dch_')) and len(api_key) >= 20:
        # Bucket by first 16 chars (stable prefix, avoids logging full key)
        return f'apikey:{api_key[:16]}', 'authenticated'

    # 3. Authenticated user (JWT decoded by require_auth / optional_auth)
    user = getattr(request, 'user', None)
    if user and isinstance(user, dict):
        uid = user.get('user_id') or user.get('email') or 'unknown'
        return f'user:{uid}', 'authenticated'

    # 4. Anonymous - rate limit by IP
    return f'ip:{_get_client_ip()}', 'anonymous'


# ---------------------------------------------------------------------------
# Paths to skip (health checks, static assets)
# ---------------------------------------------------------------------------

SKIP_PATHS = frozenset([
    '/health', '/api/health', '/api/v1/circuit-status', '/favicon.ico',
    '/robots.txt', '/sitemap.xml', '/.well-known/mcp-registry-auth',
    # PATCH 2026-04-24 (jm): /mcp has its own tier-aware rate limiter inside
    # mcp_gatekeeper.py (_rl.check). Double-limiting here was causing every
    # real MCP customer (mcp-remote from Claude Desktop, dchub CLI, etc.) to
    # hit anonymous 20-rpm caps during the normal 5-message init handshake
    # (initialize → notifications/initialized → tools/list → prompts/list →
    # resources/list, all in under 1 second) — tripping 429 on request #2
    # and putting the client into a reconnect storm that never resolves.
    '/mcp', '/mcp/',
])

# PATCH 2026-04-24 (jm): Added '/mcp/' prefix so any MCP sub-paths
# (e.g. /mcp/sessions/xyz if future transport uses them) also bypass the
# Flask-level rate limiter.
SKIP_PREFIXES = ('/static/', '/assets/', '/js/', '/css/', '/images/', '/mcp/')

# Phase FF (2026-05-22): public showcase/content HTML pages get the generous
# 'public_content' tier instead of the strict anonymous IP cap, so crawlers +
# visitors aren't 429'd on the flagship citation surfaces. Prefix match.
PUBLIC_CONTENT_PREFIXES = ('/dcpi', '/markets', '/reports', '/brain/')


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

    # r42r (2026-05-26): Sentinel + brain self-probes send X-DC-Probe
    # identifying themselves; bypass rate limit regardless of IP/UA so
    # the platform's own health checks never appear "broken" in the
    # Sentinel dashboard. Catches 14/67 sentinel failures that all read
    # HTTP 429 — self-inflicted, not real degradation.
    probe_marker = (request.headers.get('X-DC-Probe') or '').lower()
    if probe_marker in ('site-sentinel', 'brain-radar', 'self-heal',
                          'dc-brain-site-probe', 'dc-security-audit',
                          'dc-healer', 'autopilot'):
        return None

    # r42t (2026-05-26): bypass on X-Admin-Key match. The brain
    # autopilot's _execute_action sends X-Admin-Key but no X-DC-Probe;
    # was getting throttled, leading to 1,869 actions/24h ALL failing
    # rate_limited — root cause of the persistent data_freshness_sla_
    # breach stack (1,375 findings). free_tier_gate already bypasses
    # on this header; mirror it here.
    _admin_env = os.environ.get("DCHUB_ADMIN_KEY", "")
    if _admin_env and request.headers.get('X-Admin-Key') == _admin_env:
        return None

    # Phase ZZZZZ-round6c (2026-05-23): Bypass Railway's own internal
    # IP ranges. Our brain-radar, dchub-selfheal, healer, sentinel, etc.
    # all hit dchub.cloud from Railway infrastructure to verify endpoint
    # health. WHOIS-confirmed AS400940 Railway (RLWY-METALGEN1-01) IPs
    # were generating ~22k/14d hits — flagged as "enterprise_bot_present"
    # whales by the bot-outreach detector, then rate-limited at the
    # public-content tier as if they were external scrapers. They are
    # the platform talking to itself.
    #
    # Railway publishes their egress IP range as 162.220.232.0/24 + a
    # few other /24s. We can't trust the IP alone (could be spoofed in
    # XFF), so also check the User-Agent matches one of our internal
    # crawler signatures — defense in depth.
    if raw_ip.startswith('162.220.232.') or raw_ip.startswith('162.220.233.'):
        ua = (request.headers.get('User-Agent') or '').lower()
        # Allow if either (a) the UA is one of our known internal ones,
        # OR (b) the request is for /api/health or a known healthcheck
        # path that has to work for Railway-side liveness probes.
        internal_ua_markers = (
            'dchubhealer', 'dchub-brain', 'dchub-redircheck',
            'dchub-grid', 'brain-v2-headless', 'brain-radar',
            'uptimerobot', 'dchub-selfheal', 'dchub-scheduler',
            # Round 25 (2026-05-23): site-probe + security-audit UAs.
            # The round 24 site-probe runs from localhost:8080 and was
            # getting 429'd because its UA didn't match this list —
            # 14/15 probes failed as 429. Whitelist explicitly.
            'dc-brain-site-probe', 'dc-security-audit',
        )
        if any(m in ua for m in internal_ua_markers) or path in ('/api/health', '/alive'):
            return None
        # Even without a markeruct UA, Railway-egress IPs hitting public
        # pages get a generous lift to public_content tier (not the
        # strict anonymous cap). This catches our own crawlers using
        # generic Python urllib UA without log-spamming.

    # Bypass dchub.cloud frontend — the map fires dozens of spatial API
    # calls on every pan/zoom.  These are already gated by the tier-aware
    # enforce_tier_rate_limits() in main.py; double-limiting here causes
    # pro/paid users to see 429s with tier=anonymous.
    # v2.7: Fixes orange-dot disappearing bug on Land & Power map.
    origin = request.headers.get('Origin', '') or request.headers.get('Referer', '')
    if 'dchub.cloud' in origin:
        return None

    key, tier = _get_key_and_tier()
    # Phase FF: elevate anonymous hits on public showcase pages to the
    # generous public_content tier (flagship SEO/citation surfaces). Authed/
    # internal tiers already have higher limits, so only lift anonymous.
    if tier == 'anonymous' and path.startswith(PUBLIC_CONTENT_PREFIXES):
        tier = 'public_content'
    limits = LIMITS[tier]

    # Per-minute check
    ok, remaining, retry = _check(f"{key}:min", limits['rpm'], 60)
    if not ok:
        if _should_log_rate_hit(key):
            logger.warning(f"Rate limit hit: {key} tier={tier} path={path} ip={_get_client_ip()}")
        return _resp(retry)

    # Per-hour check
    ok_h, rem_h, retry_h = _check(f"{key}:hr", limits['rph'], 3600)
    if not ok_h:
        if _should_log_rate_hit(key):
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
