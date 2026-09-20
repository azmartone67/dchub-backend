#!/usr/bin/env python3
"""An anonymous buyer must still be traceable to the signal that sold them.

NO NETWORK, NO DB.

paid_signal_attribution_30d read attribution_rate_pct = 0.0. The cause was not
the bridge lanes — it was that the only thing stamping identity onto a
checkout, the pair code, was minted `if user_id:`. Anonymous callers are the
majority of paywall volume, so their Stripe links carried utm_* and nothing
else, and every lane failed by construction.

Measured live 2026-09-19, anonymous, on /api/v1/pipeline:

    recommended_upgrade_url -> buy.stripe.com/...?utm_source=mcp_paywall
                               &utm_tool=pipeline          (no reference)

Two independent defects, one test file:
  1. the pair code was not minted without a user_id
  2. `recommended_upgrade_url` — the field we name "recommended", the cheapest
     unblock, the one the human_message leads with — was the last raw Stripe
     link, so the sale left DC Hub entirely. Its sibling
     `one_click_upgrade_url` had been re-routed through /checkout/start in
     FF+16 and this one was missed.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ANON_ID = "anon:sess-abc123"


@pytest.fixture
def paywall(monkeypatch):
    """Real builder, real URL helpers. Only the two IDENTITY lookups are
    stubbed — both otherwise need a request-scoped DB."""
    import mcp_signal_canonical
    import routes.pair_code as pair_code
    import utils.paywall_response as pr

    minted = {}

    def _fake_code(identity, tool_name=None, **kw):
        minted["identity"] = identity
        minted["tool"] = tool_name
        return {"code": "DCM-TEST", "expires_at": None}

    monkeypatch.setattr(pair_code, "get_or_create_code", _fake_code)
    monkeypatch.setattr(mcp_signal_canonical, "_compute_caller_id",
                        lambda **kw: ANON_ID)
    return pr, minted


def _build(pr):
    from flask import Flask
    app = Flask(__name__)
    with app.test_request_context("/api/v1/pipeline",
                                  headers={"User-Agent": "test-agent"}):
        return pr.build_paywall_response(tool_name="pipeline", user_id=None)


def test_anonymous_caller_still_mints_a_pair_code(paywall):
    """The fix. Without it `minted` stays empty for every anonymous caller."""
    pr, minted = paywall
    _build(pr)
    assert minted.get("identity") == ANON_ID, (
        "no pair code was minted for an anonymous caller — its checkout will "
        "reach Stripe with no client_reference_id and cannot be attributed"
    )


def test_the_pair_identity_is_the_same_one_signals_are_stamped_with(paywall):
    """A code minted against a DIFFERENT identity would stamp the checkout and
    still never join: the caller_bridge lane matches mcp_upgrade_signals
    .caller_id, which mcp_signal_canonical._compute_caller_id produces."""
    pr, minted = paywall
    _build(pr)
    assert minted["identity"] == ANON_ID


def test_the_recommended_cta_keeps_the_buyer_on_dchub(paywall):
    """It is the CTA an agent surfaces; a raw Stripe link takes the sale
    out of DC Hub and leaves nothing to bridge."""
    pr, _ = paywall
    out = _build(pr)
    rec = out.get("recommended_upgrade_url", "")
    assert rec.startswith("https://dchub.cloud/"), (
        f"recommended_upgrade_url still leaves DC Hub: {rec}"
    )
    assert "buy.stripe.com" not in rec


def test_the_direct_stripe_link_is_preserved_not_deleted(paywall):
    """Same convention as one_click_upgrade_url_direct_stripe — clients that
    deliberately want the raw link keep it."""
    pr, _ = paywall
    out = _build(pr)
    direct = out.get("recommended_upgrade_url_direct_stripe", "")
    assert "buy.stripe.com" in direct, (
        "the raw Stripe link was dropped rather than moved aside"
    )


def test_the_recommended_product_did_not_change(paywall):
    """Re-routing must not silently upsell: starter stays starter."""
    pr, _ = paywall
    out = _build(pr)
    assert out.get("recommended_upgrade_tier") == "starter"
    assert "tier=starter" in out.get("recommended_upgrade_url", "")
