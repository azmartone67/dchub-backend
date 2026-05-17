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


# Stripe one-click checkout links — pulled from main.py:7269 payment_links
# (kept in sync with that block; if those change, update here too).
_STRIPE_LINKS = {
    "developer_monthly": "https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c",
    "pro_monthly":       "https://buy.stripe.com/dRm7sMbRgcfPg97buiaZi02",
    "pro_annual":        "https://buy.stripe.com/4gM3cwcVk3JjbSR9maaZi01",
    "enterprise_monthly":"https://buy.stripe.com/fZueVe5sS6Vv7CB41QaZi0a",
}

_TIER_PRICE = {
    "FREE":       "$0",
    "IDENTIFIED": "$0 (free with email)",
    "DEVELOPER":  "$49/mo",
    "PRO":        "$199/mo",
    "ENTERPRISE": "Custom",
}

_TIER_RANK = {
    "FREE":       0,
    "IDENTIFIED": 1,
    "DEVELOPER":  2,
    "PRO":        3,
    "ENTERPRISE": 4,
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


def _gate_response(current_tier: str, required_tier: str,
                   gate_id: str, preview: dict | None = None):
    """Standardized 402 response — conversion-friendly. Includes
    inline Stripe checkout URL + preview of what's behind the wall."""
    required_upper = required_tier.upper()
    checkout_key = {"DEVELOPER": "developer_monthly",
                    "PRO":       "pro_monthly",
                    "ENTERPRISE":"enterprise_monthly"}.get(required_upper, "pro_monthly")
    stripe_url = _STRIPE_LINKS.get(checkout_key, _STRIPE_LINKS["pro_monthly"])
    upgrade_url = (f"https://dchub.cloud/pricing"
                   f"?utm_source=rest_gate&utm_medium={gate_id}"
                   f"&utm_campaign={required_upper.lower()}_upgrade")
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
        "claim_free_key_first": (
            "https://dchub.cloud/api/v1/keys/claim"
            if current_tier == "FREE" else None
        ),
    }
    resp = jsonify(payload)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 402


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
