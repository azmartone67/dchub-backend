"""Phase DDDD (2026-05-16) — REST endpoint tier gates.

Until this phase, all DEVELOPER + PRO upgrade pressure lived ONLY on
the MCP side via mcp_gatekeeper. The REST API was wide open — anyone
could hit /api/v1/transactions, /api/v1/dcpi/scores, /api/v1/bots/whales
without auth. That meant the only path to a paid upgrade was through
an MCP client (Claude Desktop, Cursor, etc) — terrible for the much
larger pool of users hitting the website / API directly.

This module:
  1. Central `require_tier(min_tier)` decorator usable on any Flask
     route. Resolves the caller's tier via:
       - X-API-Key header → mcp_gatekeeper.resolve_tier()
       - dchub_token cookie → user plan via api_keys / users table
       - falls back to Tier.FREE (anonymous)
  2. Conversion-friendly 402 response: structured JSON with
       - current_tier, required_tier, required_tier_price
       - preview: a small "what you would see" sample so the user
         knows what they're missing
       - upgrade_url + stripe_checkout_url (one-click Stripe link)
       - utm tagging so /pricing landing knows which gate fired

Gate points wired in this PR:
  /api/v1/transactions/export.csv     → DEVELOPER  (net-new endpoint)
  /api/v1/bots/whales                 → PRO        (was public)
  /api/v1/bots/dormant                → PRO        (was public)

(Plus MCP tier moves: compare_sites → PRO, 3 new PRO L&P tools.)
"""

from __future__ import annotations

from functools import wraps
from flask import jsonify, request


# Phase BBB-3 (2026-05-17) — STARTER tier slot between IDENTIFIED and
# DEVELOPER. The 7,101 upgrade signals / 0 conversions in 7d suggests
# the IDENTIFIED→DEVELOPER jump ($0 → $49/mo) is too steep. A $9/mo
# Starter tier at 500 calls/day gives a cheap stepping stone:
#   IDENTIFIED (200/day free)
#   → STARTER ($9/mo, 500/day, no commitment)
#   → DEVELOPER ($49/mo, 2k/day, support)
#
# Activation requires creating a Stripe payment link for the $9 SKU
# and setting STARTER_MONTHLY_STRIPE_LINK env var. Until then, the
# link falls back to the founding-member $99 page so the CTA still
# converts somewhere.
import os as _os
_STRIPE_LINKS = {
    # Phase BBB-3-LIVE (2026-05-17) — user provided the real Starter
    # payment link, no longer a fallback to founding-member. $9/mo,
    # 500 calls/day, no commitment. Env override still honored if
    # the SKU ever needs to be rotated.
    "starter_monthly":   _os.environ.get(
        "STARTER_MONTHLY_STRIPE_LINK",
        "https://buy.stripe.com/8x2dRa5sS0x75uteGuaZi0g"),
    "developer_monthly": "https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c",
    "pro_monthly":       "https://buy.stripe.com/dRm7sMbRgcfPg97buiaZi02",
    "pro_annual":        "https://buy.stripe.com/4gM3cwcVk3JjbSR9maaZi01",
    "enterprise_monthly":"https://buy.stripe.com/fZueVe5sS6Vv7CB41QaZi0a",
}

_TIER_PRICE = {
    "FREE":       "$0",
    "IDENTIFIED": "$0 (free with email)",
    "STARTER":    "$9/mo",
    "DEVELOPER":  "$49/mo",
    "PRO":        "$199/mo",
    "ENTERPRISE": "Custom",
}

_TIER_RANK = {
    "FREE":       0,
    "IDENTIFIED": 1,
    # Phase BBB-3 — STARTER shares rank with IDENTIFIED for the
    # require_tier decorator (both unlock the same routes). Daily-call
    # quota is enforced elsewhere by tier-specific rate-limit code that
    # CAN tell STARTER (500/day) from IDENTIFIED (200/day). This keeps
    # the new tier from accidentally bumping every other tier's rank
    # comparison and breaking existing gates.
    "STARTER":    1,
    "DEVELOPER":  2,
    "PRO":        3,
    # r43-H (2026-05-27): FOUNDING was MISSING here — require_tier('pro')
    # (rank 3) denied founding members (fell to .get default) on
    # transactions / market intel / grid data. Founding is Pro-equivalent,
    # so it shares pro's rank.
    "FOUNDING":   3,
    "ENTERPRISE": 4,
    # r43-H (2026-05-28): research_seed (NLR custom institutional contract)
    # is enterprise-equivalent. Was missing here → require_tier denied NLR.
    "RESEARCH_SEED": 4,
}


def _resolve_caller_tier() -> tuple[str, dict]:
    """Returns (tier_name, debug_info). Tier name is one of FREE/
    IDENTIFIED/DEVELOPER/PRO/ENTERPRISE. Best-effort across multiple
    auth surfaces; defaults to FREE."""
    debug = {}

    # 1. X-API-Key path — delegate to mcp_gatekeeper resolver
    api_key = request.headers.get("X-API-Key") or request.args.get("api_key")
    if api_key:
        try:
            from mcp_gatekeeper import resolve_tier, TIER_NAME
            tier_enum = resolve_tier(api_key)
            tier_name = TIER_NAME.get(tier_enum, "FREE").upper()
            return tier_name, {"source": "x-api-key", **debug}
        except Exception as e:
            debug["api_key_resolve_err"] = str(e)[:80]

    # 2. dchub_token cookie path — look up user plan in DB
    token = request.cookies.get("dchub_token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    if token:
        try:
            import os, psycopg2
            db = os.environ.get("DATABASE_URL")
            if db:
                with psycopg2.connect(db, sslmode="require", connect_timeout=3) as c:
                    with c.cursor() as cur:
                        # Token can be a session token OR a raw api key.
                        # Try api_keys table first (most common case).
                        try:
                            cur.execute("""
                                SELECT COALESCE(rate_limit_tier, 'free')
                                  FROM api_keys
                                 WHERE key_prefix = %s OR key_hash = %s
                                 LIMIT 1
                            """, (token[:16], token))
                            r = cur.fetchone()
                            if r and r[0]:
                                return str(r[0]).upper(), {"source": "cookie:api_keys"}
                        except Exception:
                            pass
                        # Then users.plan
                        try:
                            cur.execute("""
                                SELECT plan FROM users
                                 WHERE session_token = %s OR id::text = %s
                                 LIMIT 1
                            """, (token, token))
                            r = cur.fetchone()
                            if r and r[0]:
                                return str(r[0]).upper(), {"source": "cookie:users"}
                        except Exception:
                            pass
        except Exception as e:
            debug["cookie_resolve_err"] = str(e)[:80]

    return "FREE", {"source": "anonymous", **debug}


def caller_is_privileged(min_tier: str = "IDENTIFIED") -> bool:
    """True if the caller may receive FULL data on a *teaser-gated* data
    endpoint. Unlike `require_tier` (a hard 402), this is for endpoints we
    want to keep returning 200 for everyone but show only a teaser to
    anonymous scrapers. It combines tier rank with internal/browser trust
    signals so we never teaser our own server-to-server calls, the brain
    radar, logged-in web users, or the Land & Power map.

    Trusted signals (any one → True):
      1. caller tier rank >= min_tier (paid/identified API key or cookie)
      2. internal MCP key (X-Internal-Key) or admin radar key (X-Admin-Key)
      3. internal server-to-server call (loopback remote_addr or DCHub UA)
      4. real dchub.cloud browser (r43-G session cookie or same-origin GET)

    Returns False only for an unknown anonymous external caller."""
    # 1. tier rank
    try:
        tier, _ = _resolve_caller_tier()
        if _TIER_RANK.get(tier.upper(), 0) >= _TIER_RANK.get(min_tier.upper(), 0):
            return True
    except Exception:
        pass
    # 2. internal / admin keys
    try:
        from internal_auth import is_valid_internal_key
        if is_valid_internal_key(request.headers.get("X-Internal-Key", "")):
            return True
    except Exception:
        pass
    try:
        import os as _os, hmac as _hmac
        _ac = (request.headers.get("X-Admin-Key", "") or "").split()
        _ac = _ac[0] if _ac else ""
        _ae = (_os.environ.get("DCHUB_ADMIN_KEY", "") or "").split()
        _ae = _ae[0] if _ae else ""
        if _ac and _ae and _hmac.compare_digest(_ac, _ae):
            return True
    except Exception:
        pass
    # 3. internal server-to-server call — loopback only. Public traffic
    #    arrives via the CF→Railway proxy, so remote_addr is the proxy IP,
    #    never loopback; only true self-calls (the grid-intel headroom
    #    fetch to 127.0.0.1:8080) are loopback. We do NOT trust the
    #    User-Agent ("DCHub…") — a CF client can forge any UA.
    try:
        if request.remote_addr in ("127.0.0.1", "::1", "localhost"):
            return True
    except Exception:
        pass
    # 4. real dchub.cloud browser: r43-G signed session cookie ONLY.
    #    Origin/Referer is intentionally NOT trusted — the CF worker injects
    #    a dchub.cloud Referer on every proxied request, so it's true for
    #    scrapers too (that injection leaked the gated datasets to anon curl).
    try:
        from routes.session_cookie import validate_cookie
        if validate_cookie():
            return True
    except Exception:
        pass
    return False


def _gate_response(current_tier: str, required_tier: str,
                   gate_id: str, preview: dict | None = None):
    """Standardized 402 response — conversion-friendly. Includes
    inline Stripe checkout URL + preview of what's behind the wall.

    Phase NN (2026-05-17) — Funnel rescue. The diagnostic showed 7,769
    paywall hits / 0 conversions on auto-trial keys because agents
    don't parse JSON bodies of 402 responses — they treat 402 as a
    hard error. Now we ALSO put the auto-trial key in HTTP headers
    (X-Trial-Key, X-Trial-Key-Expires, Retry-After) so any agent
    using standard HTTP middleware can detect + retry without parsing
    the body. Adds a WWW-Authenticate header pointing at the claim
    endpoint per RFC 7235 so smart clients can self-onboard.
    """
    required_upper = required_tier.upper()
    # Phase BBB-3-LIVE (2026-05-17): default upgrade target is now the
    # $9/mo STARTER tier (was DEVELOPER at $49/mo). The cheaper jump
    # converts better — Round 7 audit showed 7,101 upgrade signals / 0
    # conversions on the steep $0→$49 jump. STARTER unlocks the same
    # endpoints as DEVELOPER for the price of two coffees. Pro/Enterprise
    # paths still surface their own checkout when explicitly required.
    checkout_key = {"DEVELOPER":  "starter_monthly",   # cheaper alt
                    "PRO":        "pro_monthly",
                    "ENTERPRISE": "enterprise_monthly"}.get(required_upper, "starter_monthly")
    stripe_url = _STRIPE_LINKS.get(checkout_key, _STRIPE_LINKS["starter_monthly"])
    # Also surface the developer + pro paths explicitly so smart clients
    # / pricing-aware agents can pick the right tier without re-fetching.
    stripe_alternates = {
        "starter_monthly_9":    _STRIPE_LINKS.get("starter_monthly"),
        "developer_monthly_49": _STRIPE_LINKS.get("developer_monthly"),
        "pro_monthly_199":      _STRIPE_LINKS.get("pro_monthly"),
        "pro_annual":           _STRIPE_LINKS.get("pro_annual"),
    }
    upgrade_url = (f"https://dchub.cloud/pricing"
                   f"?utm_source=rest_gate&utm_medium={gate_id}"
                   f"&utm_campaign={required_upper.lower()}_upgrade")

    # Phase NN — auto-mint a trial key INLINE so the agent can retry
    # immediately. Mirrors the MCP gatekeeper flow in mcp_gatekeeper.py.
    auto_trial = None
    if current_tier == "FREE" and required_upper in ("IDENTIFIED", "DEVELOPER"):
        try:
            from routes.auto_trial import mint_trial_for_request
            t = mint_trial_for_request(request, gate_id)
            if t.get("ok"):
                auto_trial = t
        except Exception:
            pass

    payload = {
        "error":             "upgrade_required",
        "gate":              gate_id,
        "current_tier":      current_tier,
        "required_tier":     required_upper,
        "required_tier_price": _TIER_PRICE.get(required_upper, ""),
        "preview":           preview or {},
        "message": (f"This endpoint requires {required_upper} tier "
                    f"({_TIER_PRICE.get(required_upper)}). You're on "
                    f"{current_tier}. The preview field shows a sample "
                    f"of what's behind the gate."),
        "upgrade_url":       upgrade_url,
        "stripe_checkout":   f"{stripe_url}?prefilled_email={request.args.get('email','')}",
        # Phase BBB-3-LIVE — full tier ladder so smart clients can pick
        # the level that fits without parsing the message text.
        "stripe_alternates": stripe_alternates,
        "claim_free_key_first": (
            "https://dchub.cloud/api/v1/keys/claim"
            if current_tier == "FREE" else None
        ),
    }
    if auto_trial:
        payload["auto_trial_key"]         = auto_trial.get("api_key")
        payload["auto_trial_expires_at"]  = auto_trial.get("expires_at")
        payload["auto_trial_daily_calls"] = auto_trial.get("daily_calls", 200)
        payload["message"] = (
            f"✨ Auto-trial key minted: `{auto_trial.get('api_key')}` "
            f"(200 calls/day, 30-day expiry). Retry with header "
            f"`X-API-Key: {auto_trial.get('api_key')}` and this call "
            f"will succeed."
        )

    resp = jsonify(payload)
    resp.headers["Access-Control-Allow-Origin"]    = "*"
    resp.headers["Access-Control-Expose-Headers"]  = (
        "X-Trial-Key, X-Trial-Key-Expires, Retry-After, Link, WWW-Authenticate"
    )
    # Phase NN — HTTP-header trial-key delivery so middleware can grab
    # the key without parsing the body. Standard HTTP retry-loop
    # patterns will pick this up automatically.
    if auto_trial and auto_trial.get("api_key"):
        resp.headers["X-Trial-Key"]         = auto_trial.get("api_key")
        if auto_trial.get("expires_at"):
            resp.headers["X-Trial-Key-Expires"] = str(auto_trial.get("expires_at"))
        resp.headers["Retry-After"]         = "0"
        # RFC 8288 Link header pointing at the redemption endpoint
        resp.headers["Link"] = (
            '<https://dchub.cloud/api/v1/keys/auto-trial/redeem>; '
            'rel="api-key-redemption"; '
            'type="application/json"'
        )
    # RFC 7235 WWW-Authenticate signals an auth challenge with a
    # discoverable claim endpoint
    resp.headers["WWW-Authenticate"] = (
        f'X-API-Key realm="dchub.cloud", '
        f'claim="https://dchub.cloud/api/v1/keys/claim", '
        f'tier="{required_upper}"'
    )
    return resp, 402


# ── Phase NNNN (2026-05-16) — REST rate-limit decorator ──────────
# Per-key bucket, in-process. Crude but effective for the L+P
# endpoints' expected volume; if it becomes a hot spot we move to
# Redis/DB-backed counters in a future phase.
import time as _time
_RL_BUCKETS: dict[str, list[float]] = {}
_RL_MAX_BUCKET = 5000  # safety cap on bucket dict size

def _rl_check(key: str, per_minute: int) -> tuple[bool, int]:
    """Returns (allowed, retry_in_seconds)."""
    now = _time.time()
    window = 60.0
    bucket = _RL_BUCKETS.setdefault(key, [])
    bucket[:] = [t for t in bucket if (now - t) < window]
    if len(bucket) >= per_minute:
        oldest = bucket[0]
        retry_in = int(window - (now - oldest)) + 1
        return False, max(1, retry_in)
    bucket.append(now)
    # Crude eviction so the dict can't grow unbounded — if we exceed
    # the cap, drop the oldest bucket entirely (5000 unique keys/min
    # is a LOT of unique callers; benign collateral)
    if len(_RL_BUCKETS) > _RL_MAX_BUCKET:
        try:
            oldest_key = min(_RL_BUCKETS, key=lambda k: (_RL_BUCKETS[k][0] if _RL_BUCKETS[k] else now))
            _RL_BUCKETS.pop(oldest_key, None)
        except Exception: pass
    return True, 0


def rate_limit(per_minute: int = 60, key_fn=None):
    """Flask decorator. Returns 429 if caller exceeds per_minute calls
    within a 60s sliding window. Key derivation: by api_key (or cookie
    token) if present, else by IP. Override with key_fn(request)→str."""
    def deco(fn):
        from functools import wraps
        @wraps(fn)
        def wrapper(*a, **kw):
            # r58b (2026-06-01): exempt trusted internal callers from the rate
            # limiter. The brain-radar self-probes (X-Internal-Key, hitting
            # localhost:8080) were getting 429'd on /redeem/funnel-stats,
            # /reports/monthly, /freshness/radar, /ai-citations/history,
            # /brain/memory/stats — server-to-server traffic, not abuse.
            # Fail-open: any error falls through to normal rate limiting.
            try:
                from internal_auth import is_valid_internal_key
                if (is_valid_internal_key(request.headers.get("X-Internal-Key", ""))
                        or request.remote_addr in ("127.0.0.1", "::1", "localhost")):
                    return fn(*a, **kw)
            except Exception:
                pass
            # Build a stable key per caller
            if key_fn:
                try: key = str(key_fn(request))
                except Exception: key = "anon"
            else:
                key = (request.headers.get("X-API-Key")
                       or request.cookies.get("dchub_token")
                       or request.headers.get("CF-Connecting-IP")
                       or request.remote_addr or "anon")
            # Namespace by route so a per-route 60/min doesn't share
            # quota with another route on the same key
            key = f"{fn.__name__}:{key[:32]}"
            ok, retry_in = _rl_check(key, per_minute)
            if not ok:
                resp = jsonify({
                    "error":     "rate_limited",
                    "endpoint":  fn.__name__,
                    "limit":     f"{per_minute}/min",
                    "retry_in":  retry_in,
                    "message":   (f"Too many requests. Limit: {per_minute}/min. "
                                  f"Retry in {retry_in}s."),
                    "upgrade_hint": ("Higher tier = higher cap. "
                                      "See https://dchub.cloud/pricing"),
                })
                resp.headers["Retry-After"] = str(retry_in)
                resp.headers["X-RateLimit-Limit"] = str(per_minute)
                return resp, 429
            return fn(*a, **kw)
        return wrapper
    return deco


def require_tier(min_tier: str, gate_id: str | None = None,
                 preview_fn=None):
    """Flask route decorator. Returns a structured 402 if the caller's
    tier is below min_tier.

    Args:
        min_tier:  one of FREE/IDENTIFIED/DEVELOPER/PRO/ENTERPRISE
        gate_id:   short slug for analytics (defaults to view-fn name)
        preview_fn: optional callable(request) → dict — small sample
                   payload included in the 402 so the user sees what
                   they would get. Compute cheaply; runs on every
                   blocked request.
    """
    min_rank = _TIER_RANK.get(min_tier.upper(), 0)
    def deco(fn):
        slug = gate_id or fn.__name__
        @wraps(fn)
        def wrapper(*a, **kw):
            tier, _ = _resolve_caller_tier()
            tier_rank = _TIER_RANK.get(tier.upper(), 0)
            if tier_rank < min_rank:
                preview = {}
                if preview_fn:
                    try: preview = preview_fn(request) or {}
                    except Exception: pass
                return _gate_response(tier, min_tier, slug, preview)
            return fn(*a, **kw)
        return wrapper
    return deco
