"""Phase TT-1 (2026-05-15) — Single tier resolver.

Before this module, DC Hub had FIVE different functions answering
"what tier is this caller?":

  1. mcp_gatekeeper.resolve_tier(api_key)             — MCP tool path
  2. mcp_gatekeeper._resolve_from_db_hash(api_key)    — DB lookup helper
  3. map_tier_gating._detect_caller_tier(jwt_fn)      — web map paywall
  4. api_tier_gating.get_user_key_from_request()      — v2 endpoints
  5. land_power_usage_limiter._get_user_plan_from_request(app)  — L&P

Each took different inputs, returned different shapes, and applied
different fallback rules. End result: the SAME API key could resolve
to different tiers on different endpoints — a real bug source that
showed up as `/api/v1/energy/summary` gating at DEVELOPER while the
matching MCP `get_energy_prices` tool gated at IDENTIFIED (PR #185
fixed energy specifically; this PR fixes the root pattern).

This module exposes ONE function:

  get_auth_context(request) -> AuthContext

…which tries every auth scheme in a defined priority order and returns
a unified AuthContext. The 5 legacy functions become thin compatibility
shims that delegate to this resolver, so existing callers keep working
while new code can use the canonical function. Follow-up PRs (Phase TT-2+)
will migrate callers one at a time and eventually retire the shims.

Priority order:
  1. X-Internal-Key   → internal (full access, no rate limit)
  2. X-Admin-Key      → internal (same as above; admin == internal)
  3. X-API-Key        → resolve via mcp_gatekeeper.resolve_tier()
  4. Authorization: Bearer <key> → same as X-API-Key
  5. JWT cookie       → resolve via map_tier_gating._detect_caller_tier()
  6. (no auth)        → anonymous

Failure-mode: any single resolver throwing returns anonymous + logs.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Optional


# Tier names used everywhere downstream. Match the strings in
# mcp_gatekeeper.Tier (lowercase) so existing comparisons keep working.
TIER_ANONYMOUS  = "anonymous"
TIER_FREE       = "free"
TIER_IDENTIFIED = "identified"
TIER_DEVELOPER  = "developer"
TIER_PRO        = "pro"
TIER_ENTERPRISE = "enterprise"
TIER_FOUNDING   = "founding"
TIER_INTERNAL   = "internal"

# Numeric rank for "is X >= Y?" comparisons. Higher = more access.
_TIER_RANK = {
    TIER_ANONYMOUS:  0,
    TIER_FREE:       1,
    TIER_IDENTIFIED: 2,
    TIER_DEVELOPER:  3,
    TIER_PRO:        4,
    TIER_ENTERPRISE: 5,
    TIER_FOUNDING:   5,    # founding == enterprise pricing-wise
    TIER_INTERNAL:   99,   # always satisfies any check
}


@dataclass(frozen=True)
class AuthContext:
    """The complete authentication picture for a request.

    Frozen so callers can't accidentally mutate after resolution. All
    fields are best-effort — anonymous callers get tier='anonymous',
    everything else None.
    """
    tier:     str              # one of TIER_* constants
    user_id:  Optional[str]    # user.id from DB if known
    email:    Optional[str]    # user.email if known
    api_key:  Optional[str]    # the raw key value (do NOT log)
    source:   str              # "x-api-key" | "jwt" | "admin-key" | "internal" | "anonymous"

    def is_at_least(self, required: str) -> bool:
        """Returns True if this caller's tier >= required tier."""
        return _TIER_RANK.get(self.tier, 0) >= _TIER_RANK.get(required, 0)

    def is_paid(self) -> bool:
        """Convenience: True for developer/pro/enterprise/founding/internal."""
        return self.is_at_least(TIER_DEVELOPER)

    def is_identified(self) -> bool:
        """True for any tier >= identified (email-known callers)."""
        return self.is_at_least(TIER_IDENTIFIED)


_ANONYMOUS = AuthContext(
    tier=TIER_ANONYMOUS, user_id=None, email=None,
    api_key=None, source="anonymous",
)


# ── public API ─────────────────────────────────────────────────────

def get_auth_context(request=None) -> AuthContext:
    """The single source of truth for "what tier is this caller?".

    Pass a Flask `request` proxy. Returns AuthContext. Never raises —
    on any internal failure, returns anonymous + writes a warning to
    stderr so the regression surfaces in Railway logs.

    Priority order tried:
      X-Internal-Key → X-Admin-Key → X-API-Key → Bearer → JWT cookie → anon
    """
    if request is None:
        try:
            from flask import request as _req
            request = _req
        except Exception:
            return _ANONYMOUS

    try:
        headers = request.headers
    except Exception:
        return _ANONYMOUS

    # 1. Internal key — used by cron jobs + internal-to-internal calls
    internal = (headers.get("X-Internal-Key") or "").strip()
    if internal and _is_valid_internal_key(internal):
        return AuthContext(
            tier=TIER_INTERNAL, user_id=None, email=None,
            api_key=None, source="internal",
        )

    # 2. Admin key — operator-only endpoints. Same access level as internal.
    admin = (headers.get("X-Admin-Key") or "").strip()
    if admin and _is_valid_admin_key(admin):
        return AuthContext(
            tier=TIER_INTERNAL, user_id=None, email=None,
            api_key=None, source="admin-key",
        )

    # 3. X-API-Key — the primary tier-determining path
    api_key = (headers.get("X-API-Key") or "").strip()
    if not api_key:
        # Fall back to `Authorization: Bearer <key>` for clients that prefer it
        auth_header = headers.get("Authorization") or ""
        if auth_header.lower().startswith("bearer "):
            api_key = auth_header[7:].strip()

    if api_key:
        ctx = _resolve_via_mcp_gatekeeper(api_key)
        if ctx is not None:
            return ctx

    # 4. JWT cookie — used by the logged-in web UI
    ctx = _resolve_via_jwt_cookie(request)
    if ctx is not None:
        return ctx

    return _ANONYMOUS


# ── private resolvers ──────────────────────────────────────────────

def _is_valid_internal_key(provided: str) -> bool:
    """Check provided key against the configured DCHUB_INTERNAL_KEY env var."""
    expected = (os.environ.get("DCHUB_INTERNAL_KEY")
                or os.environ.get("INTERNAL_KEY") or "").strip()
    if not expected:
        return False
    return _const_time_eq(provided, expected)


def _is_valid_admin_key(provided: str) -> bool:
    """Check provided key against DCHUB_ADMIN_KEY / ADMIN_KEY env var."""
    expected = (os.environ.get("DCHUB_ADMIN_KEY")
                or os.environ.get("ADMIN_KEY") or "").strip()
    if not expected:
        return False
    return _const_time_eq(provided, expected)


def _const_time_eq(a: str, b: str) -> bool:
    """Constant-time string compare to deter timing attacks. Falls back
    to == if hmac unavailable (shouldn't ever happen on stdlib)."""
    try:
        import hmac
        return hmac.compare_digest(a, b)
    except Exception:
        return a == b


def _resolve_via_mcp_gatekeeper(api_key: str) -> Optional[AuthContext]:
    """Use mcp_gatekeeper.resolve_tier() — already the canonical path
    for X-API-Key resolution. Returns None if resolution fails so the
    caller can fall through to anonymous (rather than getting locked
    out of free tier on a transient DB hiccup)."""
    try:
        from mcp_gatekeeper import resolve_tier, Tier
        tier_obj = resolve_tier(api_key)
        if tier_obj is None:
            return None
        # mcp_gatekeeper.Tier is an IntEnum; map name → lowercase string
        tier_name = tier_obj.name.lower() if hasattr(tier_obj, "name") else str(tier_obj).lower()
        return AuthContext(
            tier=tier_name,
            user_id=None,   # mcp_gatekeeper doesn't surface user_id
            email=None,     # likewise email — DB join needed
            api_key=api_key,
            source="x-api-key",
        )
    except Exception as e:
        print(f"[auth_context] mcp_gatekeeper resolve failed: {e}",
              file=sys.stderr)
        return None


def _resolve_via_jwt_cookie(request) -> Optional[AuthContext]:
    """Use map_tier_gating._detect_caller_tier() — handles JWT cookies
    for the logged-in web UI. Returns None if no JWT or invalid."""
    try:
        from map_tier_gating import _detect_caller_tier
        def _decode_jwt(_t):
            try:
                import jwt as _jwt_mod
                secret = os.environ.get("JWT_SECRET") or ""
                if not secret:
                    return None
                return _jwt_mod.decode(_t, secret, algorithms=["HS256"])
            except Exception:
                return None
        tier_str, _meta = _detect_caller_tier(decode_jwt_func=_decode_jwt)
        tier_str = (tier_str or "").lower()
        if tier_str and tier_str != TIER_ANONYMOUS:
            return AuthContext(
                tier=tier_str, user_id=None, email=None,
                api_key=None, source="jwt",
            )
        return None
    except Exception as e:
        print(f"[auth_context] JWT resolve failed: {e}", file=sys.stderr)
        return None


# ── Flask blueprint with a diagnostic endpoint ─────────────────────

try:
    from flask import Blueprint, jsonify, request as _flask_request

    auth_context_bp = Blueprint("auth_context", __name__)

    @auth_context_bp.get("/api/v1/whoami")
    def whoami():
        """Diagnostic — returns the auth context for the current
        request. Useful for verifying tier resolution from CLI:
            curl -H 'X-API-Key: ...' https://dchub.cloud/api/v1/whoami
        Never returns secrets — api_key is stripped from the response."""
        ctx = get_auth_context(_flask_request)
        return jsonify({
            "tier":   ctx.tier,
            "source": ctx.source,
            "is_identified": ctx.is_identified(),
            "is_paid":       ctx.is_paid(),
            "user_id": ctx.user_id,
            "email":   ctx.email,
            # NOTE: api_key intentionally NOT returned
        })
except ImportError:
    auth_context_bp = None  # tests can still import the resolver
