"""r-bare-pricing-sweep (2026-09-23), continuing past #5355/#5356/#5370.

mcp_gatekeeper.py is a whole second, previously-undocumented URL-minting
system, parallel to routes/checkout_click_tracker.py — 10 call sites handing
out raw buy.stripe.com links or bare https://dchub.cloud/pricing?utm=...
across rate-limit messages, the main upgrade_required paywall payload, and
the value-unlock block attached to every successful free/identified/
developer response.

_canonical_link(tier_key, ref='') is the single shared root: it used to be
a raw STRIPE_LINKS.get(tier_key) lookup, and now signs through
routes.checkout_click_tracker.checkout_url() instead — fixing every caller
(_developer_cta, _pack5_cta, _cta_gated's stripe_direct, _STRIPE_BUY_NOW) in
one change. A few call sites bypassed _canonical_link entirely with their
own hardcoded literal (usage_url) or a bare PRICING_URL+utm string
(_cta_truncated, _cta_redacted, _value_unlock_block, the two _gate()
payload fields, the rate-limited response, the per-response _meta field) —
each fixed individually, all routing through _canonical_link so there is
still exactly ONE source of truth.

Two cases deliberately left bare, documented in the source:
  - the "free signup" unlock_hint (FREE tier, rows hidden): points at
    /signup, which costs nothing — there is no payment to sign/attribute.
  - _cta_gated's "Or compare plans:" link, paired with the now-signed
    "One-click upgrade" stripe_direct CTA on the same line — informational
    navigation, not a checkout.
  - required==Tier.IDENTIFIED in the main paywall payload: identified is a
    FREE claim_free_key resolution, not a purchase — _canonical_link has
    nothing to sign (no 'identified' key in STRIPE_LINKS) and correctly
    falls back to informational pricing, same reasoning as pjm_node_lmp.py
    earlier in this sweep.

mcp_gatekeeper.py imports cleanly on its own (confirmed: no main.py
cascade) — the lightweight CTA builders below are exercised directly.
_gate() and _cta_gated() additionally lazy-import routes.auto_trial /
routes.pair_code / routes.email_capture, which DO pull in main.py's full
side-effect chain, so those two are source-pinned instead (verified
manually against a live call during development — see the PR description
for the actual signed output).
"""
from __future__ import annotations

import ast
import base64
import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_PATH = ROOT / "mcp_gatekeeper.py"
SRC = io.open(SRC_PATH, encoding="utf-8").read()
TREE = ast.parse(SRC)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "bare-pricing-part4-test-secret")
    monkeypatch.delenv("DCHUB_GO_LINKS", raising=False)


def _decode(url: str):
    assert url.startswith("https://dchub.cloud/go/c/"), url
    token = url[len("https://dchub.cloud/go/c/"):]
    payload = token.rsplit(".", 1)[0]
    raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    return raw.split("|")


def _func_source(name):
    for n in ast.walk(TREE):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(SRC, n)
    raise AssertionError(f"{name}() not found in {SRC_PATH.name}")


# ── _canonical_link: the shared root, exercised directly ───────────────────
def test_canonical_link_signs_instead_of_raw_stripe_lookup():
    from mcp_gatekeeper import _canonical_link
    for plan in ("developer", "pro", "starter", "enterprise"):
        url = _canonical_link(plan)
        assert _decode(url)[0] == plan
        assert "buy.stripe.com" not in url


def test_pack5_is_spelled_metered_on_the_wire_not_a_second_product_name():
    """STRIPE_LINKS has both 'pack5' and 'metered' as aliases for the same
    Stripe link; every other consumer in this codebase (checkout_url,
    _pack_led_ladder, the MCP server) spells the $10 pack 'metered' — this
    mapping keeps mcp_checkout_clicks.plan consistent with that, not a
    third spelling."""
    from mcp_gatekeeper import _canonical_link
    url = _canonical_link("pack5")
    assert _decode(url)[0] == "metered"


def test_no_signing_secret_fails_open_to_bare_pricing_never_raises(monkeypatch):
    monkeypatch.delenv("DCHUB_INTERNAL_KEY", raising=False)
    from mcp_gatekeeper import _canonical_link
    assert _canonical_link("developer") == "https://dchub.cloud/pricing"


# ── rate-limiter CTAs: real functional tests ────────────────────────────────
def test_developer_and_pack_ctas_carry_signed_links_not_raw_stripe():
    from mcp_gatekeeper import _developer_cta, _pack5_cta
    dev, pack = _developer_cta(), _pack5_cta()
    assert "buy.stripe.com" not in dev
    assert "buy.stripe.com" not in pack
    assert "https://dchub.cloud/go/c/" in dev
    assert "https://dchub.cloud/go/c/" in pack


# ── the truncated/redacted CTAs ─────────────────────────────────────────────
def test_truncated_and_redacted_ctas_sign_developer():
    from mcp_gatekeeper import _cta_truncated, _cta_redacted
    for cta in (_cta_truncated(1, 10), _cta_redacted("rank_markets")):
        assert "https://dchub.cloud/pricing?" not in cta
        url = cta.rsplit("→ ", 1)[-1].strip()
        assert _decode(url)[0] == "developer"


# ── _value_unlock_block: real functional tests, all three tier branches ────
def test_value_unlock_block_signs_developer_for_free_and_identified():
    from mcp_gatekeeper import _value_unlock_block, Tier
    for tier in (Tier.FREE, Tier.IDENTIFIED):
        block = _value_unlock_block("get_market_intel", tier, 5, 1, 10)
        assert _decode(block["upgrade_url"])[0] == "developer"


def test_value_unlock_block_signs_pro_for_developer_tier():
    """DEVELOPER-tier callers are recommended PRO, not sold Developer
    again — the fix tracks recommended_tier per branch, not a flat default."""
    from mcp_gatekeeper import _value_unlock_block, Tier
    block = _value_unlock_block("get_market_intel", Tier.DEVELOPER, 100, 50, 200)
    assert _decode(block["upgrade_url"])[0] == "pro"
    assert "PRO" in block["recommended_tier"]


def test_the_free_signup_hint_is_deliberately_left_bare():
    """Documented exception: signup is free, so there is no payment to sign
    or attribute — this must stay pointing at bare /pricing (informational),
    not silently regress to a $-checkout link it doesn't need."""
    from mcp_gatekeeper import _value_unlock_block, Tier
    block = _value_unlock_block("get_market_intel", Tier.FREE, 5, 1, 10)
    assert "https://dchub.cloud/pricing?utm_source=mcp&utm_tool=" in block["unlock_hint"]
    assert "free signup" in block["unlock_hint"]


# ── _gate()/_cta_gated(): source-pinned (both lazy-import main.py's cascade) ──
def test_cta_gated_signs_the_one_click_upgrade_stripe_link():
    body = _func_source("_cta_gated")
    assert 'stripe_direct = _canonical_link("developer")' in body
    assert 'stripe_direct = _canonical_link("pro")' in body
    assert "buy.stripe.com" not in body


def test_cta_gated_compare_plans_link_stays_bare_on_purpose():
    """Paired with the now-signed stripe_direct on the same response —
    informational "see all plans" navigation, not a checkout CTA."""
    body = _func_source("_cta_gated")
    assert "Or compare plans: {url}" in body


def test_gate_signs_stripe_buy_now_and_usage_url():
    body = _func_source("_gate")
    assert '_STRIPE_BUY_NOW = {k: _canonical_link(k)' in body
    assert '"usage_url": _attribute(_canonical_link("pack5"))' in body
    assert "buy.stripe.com" not in body


def test_gate_upgrade_url_reuses_the_already_signed_buy_now_url():
    body = _func_source("_gate")
    assert '"upgrade_url": _buy_now_url or _canonical_link(_required_name)' in body


def test_gate_rate_limited_response_signs_developer():
    body = _func_source("_gate")
    assert '"upgrade_url": _canonical_link(\'developer\'),' in body


def test_finalize_meta_upgrade_url_signs_developer():
    body = _func_source("_finalize")
    assert "data[\"_meta\"][\"upgrade_url\"] = _canonical_link('developer')" in body


def test_no_bare_pricing_literal_survives_as_a_live_field_value():
    """The only two PRICING_URL-with-utm literals left in the file are the
    documented, deliberate exceptions (free signup, compare-plans nav) —
    count them so a THIRD one silently reappearing fails this test."""
    hits = [l for l in SRC.split("\n") if "PRICING_URL}?utm_source" in l]
    assert len(hits) == 2, hits
