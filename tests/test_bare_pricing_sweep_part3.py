"""r-bare-pricing-sweep (2026-09-23), continuing past #5355 and #5356.

map_tier_gating._upgrade_cta is the biggest single fix in the 13-gap sweep
by caller count: 7 sites in map_tier_gating.py itself (the /api/v1/map,
Land & Power free/developer, and capacity-heatmap walls) plus
routes/mcp_tier1_tools.py's find_alternatives. It carried a raw,
unattributed buy.stripe.com link on 'free' and a bare /pricing (no even a
#fragment) on 'developer'.

A SECOND, unrelated bug rode along: mcp_tier1_tools.py's caller uses a
wider tier vocabulary (_SPEC_TIER_RANK: 'identified', 'starter', ...) than
_upgrade_cta's own switch ('anonymous'/'free'/'developer') expects, so an
'identified' or 'starter' caller got _upgrade_cta's fall-through None —
gated with NO CTA at all, worse than a bare URL. Only 'identified' and
'starter' were actually affected: they're the only two ranks below
_upgrade_cta's blind spot that _specs_visible ever lets reach this call
(anonymous/'' already matches; developer/pro+ already pass the gate).

map_tier_gating.py itself imports cleanly (no main.py cascade), so
_upgrade_cta is tested directly, not source-pinned. mcp_tier1_tools.py also
imports cleanly, but find_alternatives' own body needs a live DB deeper in,
so the tier-normalization fix is verified by extracting its real source
(same pattern test_find_alternatives_spec_gate.py already uses for
_SPEC_TIER_RANK/_specs_visible) rather than exercising the whole route.
"""
from __future__ import annotations

import ast
import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "bare-pricing-part3-test-secret")
    monkeypatch.delenv("DCHUB_GO_LINKS", raising=False)


def _decode(url: str):
    assert url.startswith("https://dchub.cloud/go/c/"), url
    token = url[len("https://dchub.cloud/go/c/"):]
    payload = token.rsplit(".", 1)[0]
    import base64
    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    return raw.split("|")


# ── map_tier_gating._upgrade_cta: exercised directly, not source-pinned ────
def test_anonymous_gets_signup_not_a_checkout():
    from map_tier_gating import _upgrade_cta
    cta = _upgrade_cta("anonymous", "the facility map")
    assert cta["action"] == "sign_up"
    assert cta["url"] == "https://dchub.cloud/signup"


def test_free_tier_gets_a_signed_developer_checkout_not_raw_stripe():
    from map_tier_gating import _upgrade_cta
    cta = _upgrade_cta("free", "map data")
    assert _decode(cta["url"])[0] == "developer"
    assert "buy.stripe.com" not in cta["url"]
    # 'checkout' is a compat alias for the SAME signed url, never a second
    # raw Stripe literal — api_response_contract caught exactly this shape
    # of break when 'checkout' was simply dropped earlier in the sweep.
    assert cta["checkout"] == cta["url"]


def test_developer_tier_gets_a_signed_pro_checkout_not_bare_pricing():
    from map_tier_gating import _upgrade_cta
    cta = _upgrade_cta("developer", "map data")
    assert _decode(cta["url"])[0] == "pro"
    assert cta["url"] != "https://dchub.cloud/pricing"


def test_pro_and_unknown_tiers_get_no_cta():
    from map_tier_gating import _upgrade_cta
    assert _upgrade_cta("pro", "map data") is None
    assert _upgrade_cta("enterprise", "map data") is None


def test_no_signing_secret_fails_open_to_bare_pricing_never_raises(monkeypatch):
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    from map_tier_gating import _upgrade_cta
    cta = _upgrade_cta("free", "map data")
    assert cta["url"] == "https://dchub.cloud/pricing"
    assert cta["checkout"] == cta["url"]  # still aliased, even in the fallback


def test_the_seven_call_sites_in_this_file_still_call_it_the_same_way():
    """Composition guard: _upgrade_cta's callers were not touched by this
    fix, only its own body — confirms the signature and call shape are
    unchanged for the map/Land&Power/heatmap handlers."""
    src = (ROOT / "map_tier_gating.py").read_text(encoding="utf-8")
    assert src.count("_upgrade_cta(") >= 8  # 7 call sites + the def itself


# ── mcp_tier1_tools.py: the tier-vocabulary fix, source-pinned ─────────────
SRC_PATH = ROOT / "routes" / "mcp_tier1_tools.py"
SRC = io.open(SRC_PATH, encoding="utf-8").read()
TREE = ast.parse(SRC)


def _func_source(name):
    for n in ast.walk(TREE):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(SRC, n)
    raise AssertionError(f"{name}() not found in {SRC_PATH.name}")


def test_find_alternatives_normalizes_identified_and_starter_before_the_cta():
    body = _func_source("find_alternatives")
    assert '_cta_tier = "free" if _tier in ("identified", "starter") else _tier' in body
    assert "_upgrade_cta(_cta_tier," in body
    # the raw _tier (identified/starter, wider than _upgrade_cta's own
    # switch) must never reach _upgrade_cta directly again
    assert "_upgrade_cta(_tier," not in body


def test_the_normalization_actually_produces_a_cta_for_both_affected_ranks():
    """_SPEC_TIER_RANK ranks identified and starter at 1, same as free — the
    mapping this fix adds must route both to the SAME real CTA free_tier
    gets, not to a different or missing one."""
    from map_tier_gating import _upgrade_cta
    free_cta = _upgrade_cta("free", "operator and capacity data")
    for wide_tier in ("identified", "starter"):
        mapped = "free" if wide_tier in ("identified", "starter") else wide_tier
        assert mapped == "free"
        cta = _upgrade_cta(mapped, "operator and capacity data")
        assert cta is not None, wide_tier
        assert cta["action"] == free_cta["action"]


def test_the_except_fallback_also_signs_instead_of_a_bare_literal():
    """The outer try/except around the _upgrade_cta import (map_tier_gating
    itself failing to import) had its own bare-pricing fallback literal —
    fixed alongside, not left as the one remaining bare URL in this file."""
    body = _func_source("find_alternatives")
    assert "checkout_url(\"developer\")" in body
    assert body.count("https://dchub.cloud/pricing#developer") == 1  # the
    # ONE survivor: the innermost fallback, only reached if BOTH the
    # map_tier_gating import AND checkout_click_tracker.checkout_url fail.
