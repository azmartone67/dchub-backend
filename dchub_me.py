"""
dchub_me.py  —  GET /api/me + better upgrade_required for Flask.

Purpose
-------
1. `/api/me` endpoint that returns exactly what the server sees about the
   current caller, using the SAME resolution logic as `_get_request_tier()`
   and `_get_mcp_caller_tier()` already in main.py. Customers (and Cowork)
   hit this to self-verify before debugging further.

2. `better_upgrade_required(tool_name, required_tier)` — a richer 403 body
   that distinguishes no_key / key_not_recognized / tier_insufficient with
   an actionable fix_url, rather than the flat "upgrade_required" nudge.

This file does NOT touch your existing tier gate — it just adds a read-only
diagnostic surface + a nicer error helper.

Wire-up (one edit to main.py, near the other blueprint registrations):

    from dchub_me import me_blueprint, better_upgrade_required
    app.register_blueprint(me_blueprint)

Then, anywhere you currently return a flat `upgrade_required` 403:

    return better_upgrade_required("get_grid_intelligence", required_tier="enterprise")

That's it.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

# ---------------------------------------------------------------------------
# Tier rate-limit table — mirrors _tier_rate_limits in main.py.
# Kept in sync by convention; if you change main.py's table, change this too.
# ---------------------------------------------------------------------------

LIMITS = {
    "anonymous":  {"per_minute": 60,   "per_hour": 500,   "daily_mcp_calls": 5},
    "free":       {"per_minute": 60,   "per_hour": 500,   "daily_mcp_calls": 5},
    "pro":        {"per_minute": 300,  "per_hour": 5000,  "daily_mcp_calls": 1000},
    "founding":   {"per_minute": 300,  "per_hour": 5000,  "daily_mcp_calls": 1000},
    "enterprise": {"per_minute": 1000, "per_hour": 20000, "daily_mcp_calls": 100000},
}

ME_BUILD = "me-endpoint-1.0.0"  # bump per deploy so /api/me tells you which build answered


# ---------------------------------------------------------------------------
# resolve_auth — one pass that mirrors both _get_request_tier and
# _get_mcp_caller_tier, returning a rich dict we can hand to /api/me.
# ---------------------------------------------------------------------------

def resolve_auth() -> dict:
    """Build an auth context from the current Flask request. Read-only."""
    ctx = {
        "tier": "anonymous",
        "auth_source": "none",
        "key_detected": False,
        "key_recognized": False,
        "key_last4": None,
        "user_email": None,
        "user_id": None,
        "platform": None,          # set when AI Wars key matched
        "observed_header": None,   # masked echo of what we received
    }

    # ---- 1. Internal key (Cloudflare Worker → app) ----
    internal_incoming = request.headers.get("X-Internal-Key", "")
    if internal_incoming:
        try:
            # Lazy import — main.py defines is_valid_internal_key.
            from main import is_valid_internal_key  # type: ignore
            if is_valid_internal_key(internal_incoming):
                ctx.update({
                    "tier": "enterprise",
                    "auth_source": "internal_key",
                    "key_detected": True,
                    "key_recognized": True,
                    "key_last4": _last4(internal_incoming),
                    "user_id": "internal",
                    "observed_header": f"X-Internal-Key: {_mask(internal_incoming)}",
                })
                return _finalize(ctx)
        except Exception:
            pass  # fall through — treat as no internal key

    # ---- 2. AI Wars verification keys (hardcoded Pro-tier) ----
    try:
        from main import get_ai_wars_key_info  # type: ignore
        ai_info = get_ai_wars_key_info()
    except Exception:
        ai_info = None

    if ai_info:
        raw = _extract_raw_key()
        ctx.update({
            "tier": ai_info.get("tier", "pro"),
            "auth_source": _detect_source(),
            "key_detected": True,
            "key_recognized": True,
            "key_last4": _last4(raw) if raw else None,
            "platform": ai_info.get("platform"),
            "observed_header": _observed_header(raw),
        })
        return _finalize(ctx)

    # ---- 3. Database api_keys table (dchub_* keys) ----
    raw_key = _extract_raw_key()
    if raw_key and raw_key.startswith("dchub_"):
        ctx["key_detected"] = True
        ctx["auth_source"] = _detect_source()
        ctx["key_last4"] = _last4(raw_key)
        ctx["observed_header"] = _observed_header(raw_key)

        try:
            from main import _pg_execute  # type: ignore
            key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
            _, rows = _pg_execute(
                "SELECT u.plan, u.id, u.email "
                "FROM api_keys ak JOIN users u ON ak.user_id = u.id "
                "WHERE ak.key_hash = %s AND ak.is_active = 1",
                (key_hash,), fetch=True)
            if rows:
                plan, user_id, email = rows[0][0] or "free", rows[0][1], rows[0][2]
                ctx.update({
                    "tier": plan,
                    "key_recognized": True,
                    "user_id": user_id,
                    "user_email": email,
                })
                return _finalize(ctx)
        except Exception:
            pass
        # Key present but not recognized — stay on free, flag it.
        ctx["tier"] = "free"
        return _finalize(ctx)

    # ---- 4. JWT (cookie or Authorization: Bearer non-dchub_) ----
    try:
        auth_header = request.headers.get("Authorization", "")
        token = None
        if auth_header.startswith("Bearer ") and not auth_header[7:].strip().startswith("dchub_"):
            token = auth_header[7:].strip()
        if not token:
            token = request.cookies.get("auth_token") or request.cookies.get("token")
        if token:
            import jwt
            from main import JWT_SECRET  # type: ignore
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            ctx.update({
                "tier": payload.get("plan", "free"),
                "auth_source": "jwt",
                "key_detected": True,
                "key_recognized": True,
                "user_id": payload.get("user_id"),
                "user_email": payload.get("email"),
                "observed_header": "Authorization: Bearer <jwt>",
            })
            return _finalize(ctx)
    except Exception:
        pass

    # ---- 5. Anonymous fallback ----
    ctx["tier"] = "anonymous"
    return _finalize(ctx)


def _finalize(ctx: dict) -> dict:
    ctx["limits"] = LIMITS.get(ctx["tier"], LIMITS["free"])
    ctx["next_action"] = _next_action(ctx)
    return ctx


def _next_action(ctx: dict) -> dict:
    if not ctx["key_detected"]:
        return {
            "code": "no_key",
            "message": "No API key or auth detected. Send it as "
                       "'Authorization: Bearer <key>' or 'X-API-Key: <key>'.",
            "fix_url": "https://dchub.cloud/docs/auth",
        }
    if not ctx["key_recognized"]:
        return {
            "code": "key_not_recognized",
            "message": "A key was sent but didn't match any record. "
                       "It may have been rotated, mistyped, or deactivated.",
            "fix_url": "https://dchub.cloud/dashboard/keys",
        }
    if ctx["tier"] in ("free", "anonymous"):
        return {
            "code": "free_tier_ok",
            "message": "Key recognized on Free tier. "
                       "Upgrade for higher limits and Enterprise tools.",
            "fix_url": "https://dchub.cloud/pricing",
        }
    return {
        "code": "ok",
        "message": f"Authenticated as {ctx['tier']}.",
    }


# ---------------------------------------------------------------------------
# Key extraction helpers (mirror your existing code)
# ---------------------------------------------------------------------------

def _extract_raw_key() -> str | None:
    x_api = request.headers.get("X-API-Key")
    if x_api:
        return x_api.strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        tail = auth[7:].strip()
        # Skip JWTs — they're not API keys.
        if tail.startswith("dchub_") or tail in _ai_wars_key_set():
            return tail
        # Could still be an AI Wars key — return it, caller decides.
        return tail
    q = request.args.get("api_key")
    if q:
        return q.strip()
    return None


def _detect_source() -> str:
    if request.headers.get("X-API-Key"):
        return "x_api_key"
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return "authorization_bearer"
    if request.args.get("api_key"):
        return "query_param"
    return "none"


def _observed_header(raw: str | None) -> str | None:
    if not raw:
        return None
    src = _detect_source()
    masked = _mask(raw)
    if src == "authorization_bearer":
        return f"Authorization: Bearer {masked}"
    if src == "x_api_key":
        return f"X-API-Key: {masked}"
    if src == "query_param":
        return f"?api_key={masked}"
    return None


_ai_wars_cache: set[str] | None = None


def _ai_wars_key_set() -> set[str]:
    global _ai_wars_cache
    if _ai_wars_cache is None:
        try:
            from main import AI_WARS_KEYS  # type: ignore
            _ai_wars_cache = set(AI_WARS_KEYS.keys())
        except Exception:
            _ai_wars_cache = set()
    return _ai_wars_cache


def _last4(s: str | None) -> str | None:
    if not s:
        return None
    return s if len(s) <= 4 else s[-4:]


def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}…{s[-4:]}"


# ---------------------------------------------------------------------------
# Flask blueprint — /api/me
# ---------------------------------------------------------------------------

me_blueprint = Blueprint("dchub_me", __name__)


@me_blueprint.route("/api/me", methods=["GET", "OPTIONS"])
def me_endpoint():
    if request.method == "OPTIONS":
        resp = jsonify({})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = \
            "Content-Type, Authorization, X-API-Key, X-Internal-Key"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return resp, 200

    ctx = resolve_auth()
    body = {
        "tier": ctx["tier"],
        "user_email": ctx["user_email"],
        "user_id": ctx["user_id"],
        "platform": ctx["platform"],
        "key_last4": ctx["key_last4"],
        "auth_source": ctx["auth_source"],
        "key_detected": ctx["key_detected"],
        "key_recognized": ctx["key_recognized"],
        "limits": ctx["limits"],
        "observed_header": ctx["observed_header"],
        "next_action": ctx["next_action"],
        "build": ME_BUILD,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp, 200


# ---------------------------------------------------------------------------
# better_upgrade_required — richer 403 body
# ---------------------------------------------------------------------------

def better_upgrade_required(tool_name: str, required_tier: str = "enterprise"):
    """
    Call this from any route/tool where the old flat `upgrade_required` 403 lived.
    Returns a Flask response object you can `return` directly.
    """
    ctx = resolve_auth()

    if not ctx["key_detected"]:
        reason = "no_key"
        fix_url = "https://dchub.cloud/docs/auth"
    elif not ctx["key_recognized"]:
        reason = "key_not_recognized"
        fix_url = "https://dchub.cloud/dashboard/keys"
    else:
        reason = "tier_insufficient"
        fix_url = "https://dchub.cloud/pricing"

    msg = {
        "no_key": (f"{tool_name} requires a {required_tier} API key. "
                   f"No key was sent — add it as 'Authorization: Bearer <key>'."),
        "key_not_recognized": (f"{tool_name} requires a {required_tier} API key. "
                               f"A key was sent but it wasn't recognized; "
                               f"check it in your dashboard."),
        "tier_insufficient": (f"{tool_name} requires {required_tier}; "
                              f"your current tier is {ctx['tier']}. Upgrade to continue."),
    }[reason]

    body = {
        "error": "upgrade_required",
        "reason": reason,
        "tool": tool_name,
        "required_tier": required_tier,
        "current_tier": ctx["tier"],
        "key_detected": ctx["key_detected"],
        "key_recognized": ctx["key_recognized"],
        "key_last4": ctx["key_last4"],
        "auth_source": ctx["auth_source"],
        "message": msg,
        "fix_url": fix_url,
        "verify_url": "https://dchub.cloud/api/me",
        # Keep the old field too so any existing clients don't break.
        "upgrade_url": "https://dchub.cloud/pricing",
    }
    resp = jsonify(body)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["WWW-Authenticate"] = f'Bearer realm="dchub", error="{reason}"'
    return resp, 403
