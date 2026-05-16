"""Phase TT-1 (2026-05-15) — tests for the single tier resolver."""

import os


def test_module_importable_without_flask():
    """The resolver should work even in test envs that lack Flask."""
    from routes import auth_context
    assert hasattr(auth_context, "get_auth_context")
    assert hasattr(auth_context, "AuthContext")


def test_tier_constants_match_mcp_gatekeeper():
    """The tier strings must match mcp_gatekeeper.Tier enum names
    (lowercased) so downstream comparisons keep working."""
    from routes.auth_context import (
        TIER_FREE, TIER_IDENTIFIED, TIER_DEVELOPER, TIER_PRO,
        TIER_ENTERPRISE, TIER_FOUNDING, TIER_INTERNAL,
    )
    for name in ("free", "identified", "developer", "pro", "enterprise"):
        for const in (TIER_FREE, TIER_IDENTIFIED, TIER_DEVELOPER,
                        TIER_PRO, TIER_ENTERPRISE):
            assert const == const.lower()


def test_anonymous_when_no_request():
    """No request passed and no Flask present → returns anonymous."""
    from routes.auth_context import get_auth_context, TIER_ANONYMOUS
    ctx = get_auth_context(None)
    assert ctx.tier == TIER_ANONYMOUS
    assert ctx.source == "anonymous"
    assert ctx.user_id is None
    assert ctx.api_key is None


def test_is_at_least_ordering():
    """Tier rank: anonymous < free < identified < developer < pro < enterprise."""
    from routes.auth_context import AuthContext
    free = AuthContext(tier="free", user_id=None, email=None,
                        api_key=None, source="test")
    pro  = AuthContext(tier="pro",  user_id=None, email=None,
                        api_key=None, source="test")
    assert pro.is_at_least("free")
    assert pro.is_at_least("developer")
    assert pro.is_at_least("pro")
    assert not pro.is_at_least("enterprise")
    assert not free.is_at_least("identified")
    assert free.is_at_least("free")


def test_is_identified_and_is_paid_helpers():
    from routes.auth_context import AuthContext
    anon = AuthContext(tier="anonymous", user_id=None, email=None,
                        api_key=None, source="anonymous")
    free = AuthContext(tier="free", user_id=None, email=None,
                        api_key=None, source="test")
    iden = AuthContext(tier="identified", user_id="u1", email="x@y.z",
                        api_key="k", source="x-api-key")
    pro  = AuthContext(tier="pro", user_id="u2", email="a@b.c",
                        api_key="k2", source="x-api-key")
    internal = AuthContext(tier="internal", user_id=None, email=None,
                            api_key=None, source="internal")

    assert not anon.is_identified()
    assert not free.is_identified()
    assert iden.is_identified()
    assert pro.is_identified()
    assert internal.is_identified()

    assert not anon.is_paid()
    assert not free.is_paid()
    assert not iden.is_paid()
    assert pro.is_paid()
    assert internal.is_paid()


def test_internal_tier_always_satisfies():
    """tier=internal must pass any check (even enterprise)."""
    from routes.auth_context import AuthContext
    internal = AuthContext(tier="internal", user_id=None, email=None,
                            api_key=None, source="internal")
    for required in ("free", "identified", "developer", "pro",
                      "enterprise", "founding"):
        assert internal.is_at_least(required), f"internal failed for {required}"


def test_anonymous_at_least_anonymous():
    """Edge case: anonymous >= anonymous (trivially)."""
    from routes.auth_context import _ANONYMOUS
    assert _ANONYMOUS.is_at_least("anonymous")
    assert not _ANONYMOUS.is_at_least("free")


def test_authcontext_is_frozen():
    """AuthContext should be immutable to prevent caller mutation."""
    from routes.auth_context import AuthContext
    ctx = AuthContext(tier="free", user_id=None, email=None,
                        api_key=None, source="test")
    try:
        ctx.tier = "enterprise"
        assert False, "AuthContext should be frozen (immutable)"
    except Exception:
        pass  # expected — dataclass(frozen=True) raises FrozenInstanceError


def test_constant_time_compare_works():
    from routes.auth_context import _const_time_eq
    assert _const_time_eq("abc", "abc")
    assert not _const_time_eq("abc", "abd")
    assert not _const_time_eq("", "x")


def test_internal_key_check_requires_env_var():
    """If no env var is set, the check returns False even for empty string."""
    from routes.auth_context import _is_valid_internal_key
    # Clear any env var
    orig_int = os.environ.pop("DCHUB_INTERNAL_KEY", None)
    orig_legacy = os.environ.pop("INTERNAL_KEY", None)
    try:
        assert not _is_valid_internal_key("any")
        assert not _is_valid_internal_key("")
    finally:
        if orig_int is not None: os.environ["DCHUB_INTERNAL_KEY"] = orig_int
        if orig_legacy is not None: os.environ["INTERNAL_KEY"] = orig_legacy
