"""The API-key branch must not short-circuit on a FREE answer.

r-whoami-promote (2026-09-09). get_auth_context step 3 read:

    ctx = _resolve_via_mcp_gatekeeper(api_key)
    if ctx is not None:
        return ctx

which LOOKS like a fallthrough and is actually terminal: that resolver never
returns None — mcp_gatekeeper.resolve_tier returns Tier.FREE. So for any
request carrying an X-API-Key, every resolver below was unreachable.

Measured consequence: lbthrall@gmail.com paid $99 and /api/v1/whoami answered
{"tier":"free","is_paid":false} for his working key — byte-identical to the
answer for a garbage key. The endpoint customers are told to verify with was
the one endpoint that lied.
"""
import os
import re

import pytest

from routes.auth_context import (
    AuthContext, TIER_FREE, TIER_PRO, TIER_ANONYMOUS, TIER_DEVELOPER,
    get_auth_context,
)
import routes.auth_context as ac


class _Req:
    def __init__(self, key=None):
        self.headers = {"X-API-Key": key} if key else {}
        self.cookies = {}


def _ctx(tier, source="x-api-key"):
    return AuthContext(tier=tier, user_id=None, email=None,
                       api_key=None, source=source)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Neutralise the resolvers this test does not drive."""
    monkeypatch.setattr(ac, "_resolve_via_jwt_cookie", lambda r: None)
    monkeypatch.setattr(ac, "_is_valid_internal_key", lambda k: False)
    monkeypatch.setattr(ac, "_is_valid_admin_key", lambda k: False)


def test_free_from_gatekeeper_does_not_short_circuit(monkeypatch):
    """THE regression. A FREE gatekeeper answer must not end resolution."""
    monkeypatch.setattr(ac, "_resolve_via_mcp_gatekeeper", lambda k: _ctx(TIER_FREE))
    monkeypatch.setattr(ac, "_resolve_via_canonical", lambda r: _ctx(TIER_PRO))

    out = get_auth_context(_Req("dch_live_deadbeef"))
    assert out.tier == TIER_PRO, (
        "a FREE answer from mcp_gatekeeper short-circuited the canonical "
        "resolver — this is exactly the bug that reported a paying customer "
        "as free tier"
    )
    assert out.is_paid() is True


def test_merge_is_promote_only(monkeypatch):
    """The canonical resolver may RAISE a tier, never lower one — so a DB
    hiccup inside it cannot demote a caller the gatekeeper already trusted.

    ★ The tiers here are chosen to REACH the merge. An earlier version used
    gatekeeper=PRO / canonical=FREE and passed against a deliberately broken
    merge: PRO clears the `> FREE` branch and returns before the merge line
    ever executes, so the test proved nothing. The gatekeeper answer must be
    exactly FREE — the one value that falls through — and the canonical answer
    must be strictly BELOW it for a demote to be observable at all."""
    monkeypatch.setattr(ac, "_resolve_via_mcp_gatekeeper", lambda k: _ctx(TIER_FREE))
    monkeypatch.setattr(ac, "_resolve_via_canonical", lambda r: _ctx(TIER_ANONYMOUS))

    out = get_auth_context(_Req("dch_live_someclaimedfreekey"))
    assert out.tier == TIER_FREE, (
        "the canonical resolver DEMOTED a caller from free to anonymous. "
        "The merge must only promote: a transient DB failure inside it would "
        "otherwise strip access from callers the gatekeeper already resolved."
    )


def test_canonical_failure_keeps_the_gatekeeper_answer(monkeypatch):
    """None from the canonical resolver must not strand the caller."""
    monkeypatch.setattr(ac, "_resolve_via_mcp_gatekeeper", lambda k: _ctx(TIER_DEVELOPER))
    monkeypatch.setattr(ac, "_resolve_via_canonical", lambda r: None)

    out = get_auth_context(_Req("dchub_dev_x"))
    assert out.tier == TIER_DEVELOPER


def test_no_key_still_resolves_anonymous(monkeypatch):
    monkeypatch.setattr(ac, "_resolve_via_mcp_gatekeeper", lambda k: _ctx(TIER_PRO))
    monkeypatch.setattr(ac, "_resolve_via_canonical", lambda r: _ctx(TIER_PRO))
    assert get_auth_context(_Req()).tier == TIER_ANONYMOUS


# ── the security guard ───────────────────────────────────────────────────
def test_dch_live_is_never_mapped_to_a_paid_tier_by_prefix():
    """★ dch_live_ is a MINT CHANNEL, not a tier.

    flask_mcp_endpoints.py mints FREE claim_free_key keys with the same
    prefix as PAID Stripe keys. Any `startswith("dch_live_") -> PRO` rule
    therefore grants paid data to every free key ever claimed. Entitlement
    must come from the DB row, never from this prefix.
    """
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(repo, "mcp_gatekeeper.py"), encoding="utf-8").read()

    # Floor: prove the scan can see the prefix table at all.
    assert 'startswith("dchub_pro_")' in src, (
        "the tier-prefix table moved — this guard is aimed at a dead target"
    )

    live_prefix = "dch_" + "live_"
    for line in src.splitlines():
        code = line.split("#", 1)[0]          # ignore comments
        if live_prefix in code and "Tier." in code:
            assert not re.search(r"Tier\.(PRO|DEVELOPER|ENTERPRISE|STARTER)", code), (
                f"mcp_gatekeeper maps the {live_prefix} mint-channel prefix to a "
                f"paid tier — free claim_free_key keys share that prefix:\n  {line.strip()}"
            )
