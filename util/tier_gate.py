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
import logging
import os
from datetime import datetime, timezone
from enum import IntEnum

from flask import request, jsonify, g

# ★A module-level logger, because the gate's except handlers used to be
# bare `pass`. A swallowed NameError inside a logging call is the same silence
# one layer deeper, so define this once and use it.
logger = logging.getLogger(__name__)


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
    "free":          Tier.ANONYMOUS,
    "identified":    Tier.IDENTIFIED,
    "starter":       Tier.IDENTIFIED,   # starter = identified-equivalent here
    "dev":           Tier.DEVELOPER,
    "developer":     Tier.DEVELOPER,
    "pro":           Tier.PRO,
    "founding":      Tier.PRO,          # founding members get Pro
    "paid":          Tier.PRO,          # generic "paid" (Stripe) → Pro-level
    "team":          Tier.PRO,
    "metered":       Tier.PRO,
    "enterprise":    Tier.ENTERPRISE,
    "ent":           Tier.ENTERPRISE,
    "research_seed": Tier.ENTERPRISE,   # parity with keys/validate _ENT_PLANS
    "admin":         Tier.ENTERPRISE,
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

    # 1. Try X-API-Key (or ?api_key=) → mcp_dev_keys.tier OR auto_trial_keys
    api_key = (r.headers.get("X-API-Key") or
               r.args.get("api_key") or "").strip()
    if api_key:
        ctx["api_key"] = api_key[:8] + "…"  # never expose full key

        # Phase GGG (2026-05-17) — recognize auto-trial keys.
        # Trial keys (dch_trial_xxx, minted by Phase DDDDD auto_trial flow)
        # promote callers to IDENTIFIED tier. Without this check, the
        # soft-paywall (Phase WW + WW-2) saw trial-keyed callers as anon
        # and kept truncating their responses to 10 rows — making the
        # whole trial-key promise (200 calls/day of full data) a lie.
        # Check this FIRST since trial keys are the hot path post-Phase NN.
        if api_key.startswith("dch_trial_"):
            try:
                from routes.auto_trial import validate_trial_key
                valid, reason = validate_trial_key(api_key)
                if valid:
                    ctx["plan"] = "identified"
                    ctx["source"] = "auto_trial_key"
                    ctx["trial"] = True
                    return Tier.IDENTIFIED, ctx
            except Exception:
                pass  # fall through to mcp_dev_keys check

        key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        # 1a. mcp_dev_keys (the dev-key table) — the historical hot path.
        #
        # ★★★2026-08-28: THIS BRANCH HAD NEVER MATCHED A SINGLE KEY. It
        # filtered on `key_hash`, and mcp_dev_keys HAS NO key_hash COLUMN —
        # every one of the three INSERT sites writes the raw `api_key`
        # (claim_free_key, the subscription handler, and welcome_ensure). So
        # the query raised UndefinedColumn on every call, the bare `except:
        # pass` below swallowed it, and every dch_live_ key fell through to
        # ANONYMOUS. Confirmed end-to-end, not inferred: paid keys with 7,831
        # and 2,715 successful /mcp calls both resolved `anonymous` through
        # /api/v1/account/entitlements, and `information_schema` has no
        # key_hash on the table.
        #
        # Only Flask was affected — the live MCP gate is the Node server
        # (server.mjs), which reads api_key directly, which is why the product
        # worked while this said anonymous. The exposure was a paying customer
        # using their connector key on a Flask REST route and being silently
        # served free-tier depth.
        #
        # Same shape as validate_key's api_keys cross-check: a query naming a
        # column the live schema lacks, inside a bare except, is dead from
        # birth AND silent. The handler now logs, so the next one is not.
        try:
            with _conn() as c, c.cursor() as cur:
                cur.execute(
                    # Live columns are api_key, developer_id, email, tier,
                    # status, created_at, last_used_at, metadata. The original
                    # named TWO that do not exist — key_hash and user_id — so
                    # it could never have run. ctx keeps the "user_id" key for
                    # its callers; the value is developer_id.
                    """SELECT tier, email, developer_id
                         FROM mcp_dev_keys
                        WHERE api_key = %s AND COALESCE(status, 'active') = 'active'""",
                    (api_key,))
                row = cur.fetchone()
                if row:
                    plan = (row[0] or "").lower().strip()
                    ctx["plan"] = plan
                    ctx["email"] = row[1]
                    ctx["user_id"] = row[2]
                    ctx["source"] = "api_key"
                    return _PLAN_TO_TIER.get(plan, Tier.IDENTIFIED), ctx
        except Exception as _e:
            # Never raise on a gate lookup — but never go quiet either. A
            # schema drift here downgrades paying customers to anonymous.
            try:
                logger.warning("[tier_gate] mcp_dev_keys lookup failed (%s) — "
                               "callers with a dev key resolve ANONYMOUS while "
                               "this persists", str(_e)[:160])
            except Exception:
                pass

        # 1b. 2026-06-12 tier-table-gap fallback. A web-signup founding/pro/
        # enterprise customer points an agent at a soft-gated REST route using
        # their DASHBOARD key (dchub_…), which lives in api_keys + users.plan
        # and has NO mcp_dev_keys row — so 1a missed and we fell straight
        # through to ANONYMOUS, soft-gating a PAYING customer (e.g. the
        # get_tax_incentives detail fields returned _detail_gated to an
        # enterprise key). Mirror the SAME cross-table promotion that
        # /api/v1/keys/validate already does (flask_mcp_endpoints.py →
        # tier_source 'api_keys_no_mcp_row'): take the highest plan across
        # api_keys.rate_limit_tier / api_keys.plan / users.plan. Additive &
        # fail-soft — it can only PROMOTE a genuinely-paid key, never downgrade.
        try:
            with _conn() as c, c.cursor() as cur:
                # Match BOTH storage conventions: standard customer keys store
                # key_hash = sha256(api_key); partner/admin keys (minted
                # pre-revealed by partner_key_issuer) store key_hash = the RAW
                # api_key string. /api/v1/me + api_tier_gating.validate_api_key
                # do the same dual match (r79.1) — the owner's own enterprise
                # key is raw-stored, so a hash-only lookup missed it entirely.
                # NB: api_keys.is_active is an INTEGER column — `IN (1, TRUE)`
                # throws "operator does not exist: integer = boolean" and the
                # except below would swallow it → silent fall-through to anon
                # (this is exactly why the first cut still gated). Use `= 1`,
                # matching /api/v1/me + free_tier_limiter.
                cur.execute(
                    """SELECT ak.rate_limit_tier, ak.plan, u.plan, u.email, ak.user_id
                         FROM api_keys ak
                         LEFT JOIN users u ON u.id = ak.user_id
                        WHERE ak.key_hash IN (%s, %s)
                          AND (ak.is_active IS NULL OR ak.is_active = 1)
                        LIMIT 1""",
                    (key_hash, api_key))
                arow = cur.fetchone()
            if arow:
                best = Tier.ANONYMOUS
                for _p in (arow[0], arow[1], arow[2]):
                    _t = _PLAN_TO_TIER.get((_p or "").lower().strip())
                    if _t is not None and _t > best:
                        best = _t
                if best > Tier.ANONYMOUS:
                    ctx["plan"] = (arow[2] or arow[1] or arow[0] or "").lower().strip()
                    ctx["email"] = arow[3]
                    ctx["user_id"] = arow[4]
                    ctx["source"] = "api_keys_no_mcp_row"
                    return best, ctx
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
