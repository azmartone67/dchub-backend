from internal_auth import is_valid_internal_key
from railway_egress import is_railway_egress
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
from urllib.parse import urlsplit
from flask import request, jsonify, g

logger = logging.getLogger('rate_limiter')

# ---------------------------------------------------------------------------
# SH52-126: Origin/Referer allowlist for the same-origin frontend bypass.
# The bypass exists so the map's pan/zoom storm from the dchub.cloud SPA is
# not rate limited. It must key on the request's HOST, never a substring —
# `'dchub.cloud' in origin` accepted any client that merely sent the header
# and also accepted lookalike hosts like `dchub.cloud.evil.com`.
# ---------------------------------------------------------------------------
_TRUSTED_ORIGIN_HOSTS = ("dchub.cloud",)


def _origin_host_is_trusted(origin):
    """True only when the Origin/Referer header's HOST equals dchub.cloud or a
    *.dchub.cloud subdomain. Returns False for empty/garbage headers and for
    lookalike hosts that merely contain the string (e.g. dchub.cloud.evil.com,
    xdchub.cloud)."""
    if not origin:
        return False
    try:
        host = (urlsplit(origin).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == h or host.endswith("." + h) for h in _TRUSTED_ORIGIN_HOSTS)

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
    # GEO-0704: verified AI + search crawlers (GPTBot/ClaudeBot/PerplexityBot/
    # Googlebot etc.) crawl the whole 21k-facility surface — across content
    # prefixes AND the /facility singular 301, /states, /hyperscalers, llms.txt.
    # Generous cap so a full-site crawl never 429s, but bounded so a spoofed
    # crawler UA can't hammer origin unbounded (content is public HTML anyway).
    'verified_bot':   {'rpm': 300, 'rph': 12000},
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
SKIP_PREFIXES = ('/static/', '/assets/', '/js/', '/css/', '/images/', '/mcp/',
                 # r-wellknown (2026-06-23): registry crawlers fetch the manifest +
                 # tool catalog under /.well-known/* (mcp.json, mcp-tools.json). They
                 # were tripping the anonymous 20rpm cap → HTTP 429 → registries saw
                 # NO manifest (the mcp_health_*_unreachable findings). Public metadata
                 # must never be rate-limited; exempt the whole prefix.
                 '/.well-known/')

# Phase FF (2026-05-22): public showcase/content HTML pages get the generous
# 'public_content' tier instead of the strict anonymous IP cap, so crawlers +
# visitors aren't 429'd on the flagship citation surfaces. Prefix match.
#
# GEO-0704: added the 21k-facility SEO surface + the other crawler-heavy public
# content hubs. GPTBot alone crawls ~28k facility pages/wk; at the 20rpm/200rph
# anonymous cap it was served ~24k 429s/wk — landing on the exact AI/search
# crawlers the citation strategy depends on. Canonical facility detail pages are
# /facilities/<slug> (the /facility/<id> singular form 301-redirects here);
# /news, /operators/<slug> and /vs/<slug> are the other high-volume generated
# content hubs. Prefix match, so /facilities also covers the /facilities hub.
PUBLIC_CONTENT_PREFIXES = ('/dcpi', '/markets', '/reports', '/brain/', '/grid',
                           '/facilities', '/news', '/operators', '/vs')

# GEO-0704: recognized AI-answer + search-engine crawler UA substrings — the
# engines the GEO/citation strategy wants indexing every facility page. The
# public_content lift only covers content PREFIXES, so a crawler hitting the
# /facility/<id> singular 301, /states/*, /hyperscalers/*, llms.txt or any other
# non-prefixed page still 429'd at the anonymous cap. Elevate these UAs to the
# generous (still bounded) 'verified_bot' tier on ALL paths. This is a rate-limit
# lift ONLY — the tier/paywall gates on the /api data endpoints still apply, and
# these are public HTML content pages, so a spoofed crawler UA gains nothing but
# a higher req/min ceiling (same risk posture as the internal-probe UA bypass
# above). If Cloudflare is configured to forward its verified-bot signal
# (cf-verified-bot: true via a Managed Transform), that header is honored as a
# stronger, unforgeable confirmation.
_VERIFIED_CRAWLER_UA = (
    'gptbot', 'oai-searchbot', 'chatgpt-user', 'claudebot', 'claude-web',
    'anthropic-ai', 'perplexitybot', 'googlebot', 'google-extended',
    'bingbot', 'applebot', 'duckduckbot', 'ccbot', 'bytespider',
    'google-inspectiontool', 'meta-externalagent', 'amazonbot',
)


def _is_verified_crawler():
    """True if the request is a recognized AI/search crawler that must never be
    429'd on public content. Matches Cloudflare's forwarded cf-verified-bot
    header (unforgeable, when present) or a known crawler UA substring."""
    if (request.headers.get('cf-verified-bot') or '').lower() == 'true':
        return True
    ua = (request.headers.get('User-Agent') or '').lower()
    return bool(ua) and any(m in ua for m in _VERIFIED_CRAWLER_UA)


# ---------------------------------------------------------------------------
# Flask middleware
# ---------------------------------------------------------------------------

def rate_limit_before():
    """
    Register as: app.before_request(rate_limit_before)
    Place AFTER the request timer, BEFORE route handlers.
    """
    # BUG-003 FIX: Skip rate limiting for the dchub.cloud frontend.
    # SH52-126: exact-host allowlist. The prior test was `'dchub.cloud' in
    # origin` — a substring any client can satisfy by sending the header, and
    # which also matched hostile hosts such as `dchub.cloud.evil.com`. Parse
    # the header and compare the HOST against the allowlist instead.
    origin = request.headers.get("Origin", "") or request.headers.get("Referer", "")
    if _origin_host_is_trusted(origin):
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

    # r58c (2026-06-01): trusted internal callers bypass entirely, regardless
    # of remote_addr. The brain's radar/layer self-calls to localhost:8080 are
    # the documented 429 storm (brain-radar HTTP 429 on funnel-stats /
    # reports/monthly / freshness/radar / ai-citations/history / memory/stats).
    # The loopback check above SHOULD catch them, but under 2 Railway replicas
    # the self-call can surface with a non-loopback remote_addr; the X-Internal-
    # Key check is IP-independent so it always exempts the platform's own probes.
    # (The prior fix 53e7fa79 added this to tier_gate.py's per-route decorator,
    # which NONE of these endpoints use — it was a no-op. This is the limiter
    # that actually runs as a before_request hook.) is_valid_internal_key is
    # imported at module top.
    if is_valid_internal_key(request.headers.get('X-Internal-Key', '')):
        return None

    # r42r (2026-05-26): Sentinel + brain self-probes send X-DC-Probe
    # identifying themselves; bypass rate limit regardless of IP/UA so
    # the platform's own health checks never appear "broken" in the
    # Sentinel dashboard. Catches 14/67 sentinel failures that all read
    # HTTP 429 — self-inflicted, not real degradation.
    probe_marker = (request.headers.get('X-DC-Probe') or '').lower()
    if probe_marker in ('site-sentinel', 'brain-radar', 'self-heal',
                          'dc-brain-site-probe', 'dc-security-audit',
                          'dc-healer', 'autopilot', 'failover-warm', 'cache-warm'):
        return None

    # 2026-06-08: UA-based bypass for the platform's OWN read-only probes.
    # The header bypasses above only fire if the probe sets X-DC-Probe/
    # X-Internal-Key — but many don't, so under 2 Railway replicas (non-loopback
    # self-call remote_addr) they 429. CF logs showed these UAs are ~40% of all
    # traffic (DCHubHealer 57k, deadlink-probe 34k, route-audit, frontend-health,
    # uniformity, redir/schema-audit, failover/render/self-heal probes) and the
    # 429s make the brain rate-limit ITSELF (then retry → more load). These are
    # internal health/audit crawlers hitting READ endpoints; the tier/paywall
    # gates still apply (this only lifts the req/min cap, not auth). Matched on
    # probe-specific UA substrings so the public 'dchub' SDK is NOT exempted.
    _ua = (request.headers.get('User-Agent') or '').lower()
    _INTERNAL_PROBE_UA = (
        'dchubhealer', 'dchub-brain', 'dchub-frontend-health', 'dchub-redircheck',
        'dchub-schema-audit', 'dchub-selfheal', 'dchub-failoverprobe',
        'dchub-renderflapcheck', 'brain-radar', 'brainuniformity',
        'dc-security-audit', 'deadlink', 'route-audit',
        # r-warmer-429 (2026-07-14): the cache/failover warmer probes were 429ing
        # themselves (DCHub-Warmer ~41k in CF over 7d) — same read-only-probe posture.
        'dchub-warmer', 'dchub-failoverwarmer',
    )
    if _ua and any(m in _ua for m in _INTERNAL_PROBE_UA):
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
    if is_railway_egress(raw_ip):
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
        # r49-selfcall (2026-05-31): this branch used to be a COMMENT ONLY
        # — it described a "generous lift to public_content tier" but never
        # actually did anything, so a Railway-egress request with a generic
        # python-requests UA fell through to the strict anonymous 20rpm cap
        # and got 429'd. That is exactly the self-call → 429 → worker-pool
        # starvation path (grid pages re-fetching their own /api over the
        # edge). Make it real: any request originating from Railway egress
        # is the platform talking to itself — skip limiting entirely.
        # (IP alone could be spoofed via XFF, but _get_client_ip() prefers
        # the CF-Connecting-IP / first XFF hop set by our own edge, and the
        # blast radius of a forged Railway IP is just "not rate limited",
        # not auth — acceptable for an internal-traffic bypass.)
        return None

    # ★2026-09-02 (SH52-126, second call site) — THE SUBSTRING BYPASS THAT
    # SURVIVED ITS OWN FIX. This function carried TWO same-origin bypasses.
    # The one at the top was migrated to the exact-host allowlist
    # (`_origin_host_is_trusted`); this one was left behind, still testing
    # `'dchub.cloud' in origin`. Because the host-exact gate returns FIRST,
    # this block was only ever reached when the host gate had ALREADY said
    # no — so it did nothing except re-admit exactly the hosts the fix
    # existed to exclude:
    #     Origin: https://dchub.cloud.evil.com   -> host gate False, substring True
    #     Origin: https://evil.com/?r=dchub.cloud -> host gate False, substring True
    # and rate limiting was skipped entirely for both.
    #
    # It is deleted rather than migrated: the legitimate case (the map's
    # pan/zoom storm from the dchub.cloud SPA, and the pro/paid 429s that
    # motivated v2.7) is fully covered by the host-exact gate above, which
    # runs unconditionally on every request. A second copy can only differ
    # from the first by being wrong.
    #
    # ★ tests/test_rate_limit_origin_host.py proved the HELPER was correct
    # the whole time — it never called rate_limit_before(). A unit test on a
    # helper cannot see an unmigrated call site. The behavioural tests added
    # alongside this change drive the real function.

    key, tier = _get_key_and_tier()
    # Phase FF / GEO-0704: elevate anonymous hits so crawlers + visitors aren't
    # 429'd on public surfaces. Authed/internal tiers already have higher limits,
    # so only lift anonymous. A recognized AI/search crawler gets the generous
    # verified_bot tier on ANY path (it crawls the whole site — including the
    # /facility singular 301 and llms.txt that aren't content PREFIXES); other
    # anonymous hits on the public showcase/content prefixes get public_content.
    if tier == 'anonymous':
        if _is_verified_crawler():
            tier = 'verified_bot'
        elif path.startswith(PUBLIC_CONTENT_PREFIXES):
            tier = 'public_content'
    limits = LIMITS[tier]
    g._rl_limit = limits['rpm']  # stash early so the 429 path can emit X-RateLimit-Limit too

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

    # Stash for after_request headers (standard X-RateLimit-* — platform clients
    # back off BEFORE hitting 429).
    g._rl_remaining = remaining
    g._rl_reset = int(time.time()) + (60 - int(time.time()) % 60)  # next per-minute window (epoch s)
    return None


def rate_limit_after(response):
    """
    Register as: app.after_request(rate_limit_after)
    Adds the standard X-RateLimit-Limit / -Remaining / -Reset headers so platform
    clients (and AI-agent runtimes, e.g. xAI/Grok, Mistral) can back off BEFORE
    hitting 429.
    """
    rem = getattr(g, '_rl_remaining', None)
    lim = getattr(g, '_rl_limit', None)
    rst = getattr(g, '_rl_reset', None)
    if rem is not None:
        response.headers['X-RateLimit-Remaining'] = str(rem)
    if lim is not None:
        response.headers['X-RateLimit-Limit'] = str(lim)
    if rst is not None:
        response.headers['X-RateLimit-Reset'] = str(rst)
    return response


def _resp(retry_after):
    """429 Too Many Requests — with the standard rate-limit headers so a client
    knows the cap (Limit), that it's exhausted (Remaining 0), and exactly when to
    retry (Retry-After + Reset)."""
    body = {
        'error': 'rate_limit_exceeded',
        'message': f'Too many requests. Retry after {retry_after}s.',
        'retry_after': retry_after
    }
    # error_version:1 — rate-limit is transient_backoff: NO suggested_params
    # (no param change unlocks it; the agent waits and retries the same call).
    # Fail-soft: the envelope must never break the 429 response.
    try:
        from routes.error_envelope import merge_error_mitigation
        merge_error_mitigation(
            body,
            'rate_limit_exceeded', 'transient_backoff',
            f'Per-window request cap exhausted; wait {retry_after}s '
            '(Retry-After) and retry the same request.',
        )
    except Exception:
        pass
    r = jsonify(body)
    r.status_code = 429
    r.headers['Retry-After'] = str(retry_after)
    lim = getattr(g, '_rl_limit', None)
    if lim is not None:
        r.headers['X-RateLimit-Limit'] = str(lim)
    r.headers['X-RateLimit-Remaining'] = '0'
    r.headers['X-RateLimit-Reset'] = str(int(time.time()) + int(retry_after or 60))
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
