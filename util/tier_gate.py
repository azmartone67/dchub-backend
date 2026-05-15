"""
util/tier_gate.py — Phase GG (2026-05-15): Bundle 5B soft-paywall decorator.

The existing free_tier_gate.py returns 401 to anonymous users on every
gated REST endpoint. That's a hard wall — anonymous users hit it and
bounce. Meanwhile the MCP path uses TOOL_TEASER to give anonymous users
a value-first "here's what you'd unlock" message that converts much
better.

This module brings the MCP teaser pattern to REST. The `soft_gate`
decorator resolves the caller's tier from API key or JWT, and:

  - tier >= required  → run the handler, return full response
  - tier < required   → return 200 with truncated data + upgrade CTA

Endpoints opt in by importing the decorator. The existing 401 path
remains untouched — this is purely additive infrastructure that new
endpoints (and gradually older ones in follow-up PRs) can adopt.

Usage:
    from util.tier_gate import soft_gate, Tier

    @app.route("/api/v1/some-endpoint")
    @soft_gate(min_tier=Tier.IDENTIFIED,
               teaser="full market intelligence with day-over-day deltas",
               truncate_to=3)
    def some_endpoint():
        ...
"""
import functools
import hashlib
import os
from datetime import datetime, timezone
from enum import IntEnum

from flask import request, jsonify, g


class Tier(IntEnum):
    """Mirror of mcp_gatekeeper.Tier so REST gating matches MCP gating."""
    ANONYMOUS = 0
    IDENTIFIED = 1
    DEVELOPER = 2
    PRO = 3
    ENTERPRISE = 4


TIER_NAME = {
    Tier.ANONYMOUS: "Anonymous",
    Tier.IDENTIFIED: "Identified",
    Tier.DEVELOPER: "Developer",
    Tier.PRO: "Pro",
    Tier.ENTERPRISE: "Enterprise",
}


# Plan/string → Tier mapping. Identical vocabulary to mcp_gatekeeper.
_PLAN_TO_TIER = {
    "free":       Tier.ANONYMOUS,
    "identified": Tier.IDENTIFIED,
    "dev":        Tier.DEVELOPER,
    "developer":  Tier.DEVELOPER,
    "pro":        Tier.PRO,
    "founding":   Tier.PRO,    # founding members get Pro
    "enterprise": Tier.ENTERPRISE,
    "ent":        Tier.ENTERPRISE,
}


def _conn():
    import psycopg2
    c = psycopg2.connect(os.environ.get("DATABASE_URL"), connect_timeout=5)
    c.autocommit = True
    return c


def resolve_tier(req=None) -> tuple[Tier, dict]:
    """Resolve the caller's tier. Returns (tier, context dict).

    Context dict carries: api_key, user_id, email, plan, source.
    Never raises — defaults to Tier.ANONYMOUS on any error.
    """
    r = req or request
    ctx = {"source": "anonymous", "api_key": None, "user_id": None,
           "email": None, "plan": None}

    # 1. Try X-API-Key (or ?api_key=) → mcp_dev_keys.tier
    api_key = (r.headers.get("X-API-Key") or
               r.args.get("api_key") or "").strip()
    if api_key:
        ctx["api_key"] = api_key[:8] + "…"  # never expose full key
        try:
            key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    """SELECT tier, email, user_id
                         FROM mcp_dev_keys
                        WHERE key_hash = %s AND COALESCE(status, 'active') = 'active'""",
                    (key_hash,))
                row = cur.fetchone()
                if row:
                    plan = (row[0] or "").lower().strip()
                    ctx["plan"] = plan
                    ctx["email"] = row[1]
                    ctx["user_id"] = row[2]
                    ctx["source"] = "api_key"
                    return _PLAN_TO_TIER.get(plan, Tier.IDENTIFIED), ctx
        except Exception:
            pass

    # 2. Try JWT (Authorization: Bearer ...) → users.plan
    auth = r.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
        try:
            import jwt
            secret = os.environ.get("JWT_SECRET") or os.environ.get("SECRET_KEY", "")
            payload = jwt.decode(token, secret, algorithms=["HS256"])
            user_id = payload.get("user_id") or payload.get("sub")
            if user_id:
                with _conn() as c, c.cursor() as cur:
                    cur.execute("""SELECT plan, email FROM users
                                    WHERE id = %s LIMIT 1""", (user_id,))
                    row = cur.fetchone()
                    if row:
                        plan = (row[0] or "").lower().strip()
                        ctx["plan"] = plan
                        ctx["email"] = row[1]
                        ctx["user_id"] = user_id
                        ctx["source"] = "jwt"
                        tier = _PLAN_TO_TIER.get(plan, Tier.IDENTIFIED)
                        # Free signed-up user with verified email → identified
                        if tier == Tier.ANONYMOUS and row[1]:
                            tier = Tier.IDENTIFIED
                        return tier, ctx
        except Exception:
            pass

    # 3. Default — anonymous
    return Tier.ANONYMOUS, ctx


def soft_gate(min_tier: Tier, teaser: str = "premium intelligence",
              truncate_to: int | None = None,
              truncate_keys: list[str] | None = None):
    """Decorator: soft-paywall a REST endpoint.

    If caller's tier >= min_tier: run handler, return full response.
    If caller's tier < min_tier: still run handler (so caller sees real
        data shape) but truncate the response and inject an upgrade CTA.

    Args:
        min_tier: minimum tier required for full access
        teaser: what the caller would unlock with that tier (1 sentence)
        truncate_to: if response has a list field, cap it to this many items
        truncate_keys: which top-level list fields to truncate; if None,
                       truncates every list field
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            tier, ctx = resolve_tier()
            g.tier = tier
            g.tier_ctx = ctx

            response = fn(*args, **kwargs)

            # If the handler already returned a non-200, leave it alone.
            if hasattr(response, "status_code") and response.status_code != 200:
                return response

            # Extract data (handle (response, status) tuple too)
            payload = response[0].get_json() if isinstance(response, tuple) else (
                response.get_json() if hasattr(response, "get_json") else response)

            if not isinstance(payload, dict):
                return response

            # If tier is sufficient → no modification, just stamp tier
            if tier >= min_tier:
                payload["_tier"] = TIER_NAME[tier]
                return jsonify(payload), 200

            # Insufficient tier — soft-paywall the response.
            truncated_fields = []
            if truncate_to is not None and truncate_to >= 0:
                keys = truncate_keys or [k for k, v in payload.items()
                                          if isinstance(v, list)]
                for k in keys:
                    v = payload.get(k)
                    if isinstance(v, list) and len(v) > truncate_to:
                        payload[k] = v[:truncate_to]
                        truncated_fields.append({
                            "field": k,
                            "shown": truncate_to,
                            "total": len(v),
                            "hidden": len(v) - truncate_to,
                        })

            payload["_gated"] = True
            payload["_tier"] = TIER_NAME[tier]
            payload["_required_tier"] = TIER_NAME[min_tier]
            payload["_teaser"] = teaser
            payload["_upgrade_cta"] = _build_cta(tier, min_tier, teaser)
            payload["_truncated"] = truncated_fields
            payload["_signup_url"] = "https://dchub.cloud/signup"
            payload["_pricing_url"] = "https://dchub.cloud/pricing"
            return jsonify(payload), 200
        return wrapper
    return decorator


def _build_cta(current: Tier, required: Tier, teaser: str) -> str:
    """Tier-appropriate CTA message. Identical pattern to MCP TOOL_TEASER."""
    if current == Tier.ANONYMOUS:
        if required == Tier.IDENTIFIED:
            return (f"Sign up free with your email to unlock {teaser}. "
                    "No credit card. Takes 30 seconds: dchub.cloud/signup")
        return (f"To unlock {teaser}, sign up free (email only) — then "
                f"upgrade to {TIER_NAME[required]} ($49/mo) for the full "
                "data set: dchub.cloud/pricing")
    if current == Tier.IDENTIFIED and required == Tier.DEVELOPER:
        return (f"Upgrade to Developer ($49/mo) to unlock {teaser} + "
                "2000 calls/day + 100 results/call. dchub.cloud/pricing")
    if current == Tier.DEVELOPER and required == Tier.PRO:
        return (f"Upgrade to Pro ($199/mo) to unlock {teaser} + "
                "10k calls/day + 500 results/call. dchub.cloud/pricing")
    return (f"This endpoint requires {TIER_NAME[required]} tier. "
            "See dchub.cloud/pricing for upgrade options.")
