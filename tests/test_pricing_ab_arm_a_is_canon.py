"""Arm A prices from canon (2026-09-02, finding 4f).

MEASURED 2026-09-02T00:26Z at the edge: /api/v1/pricing/ab-cohort →
{"ab_active":false,"display_price":"$199","price_usd":199,
 "stripe_url":"…/7sY7sM9J8enX7CB69YaZi0l"} — a $199 label over the $299
Pro payment link, with the kill switch engaged. There has been no $199
price since r-reprice (06-19); the legacy link was retired 08-22. The
literal `_ARM_A_PRICE_USD = 199` (r73) simply outlived both. The control
arm now reads tier_registry.price('pro'), the number /pricing and the
checkout-integrity shell read.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_arm_a_is_the_canonical_pro_price():
    import tier_registry
    from routes import pricing_ab
    assert pricing_ab._ARM_A_PRICE_USD == tier_registry.price("pro") == 299
    assert pricing_ab._ARM_A_PRICE_USD != 199
    assert pricing_ab._canon_pro_price_usd() == 299


def test_the_fail_safe_arm_prices_from_canon_when_the_ab_is_off(monkeypatch):
    import tier_registry
    from routes import pricing_ab
    monkeypatch.setattr(pricing_ab, "_ab_active", lambda: False)
    info = pricing_ab.get_displayed_pro_price(None)
    assert info["ab_active"] is False and info["cohort"] == "A"
    assert info["price_usd"] == tier_registry.price("pro")


def test_the_cohort_endpoint_labels_the_link_it_serves(monkeypatch):
    flask = pytest.importorskip("flask")
    from routes import pricing_ab
    from routes._stripe_links import STRIPE_LINKS
    monkeypatch.setenv("PRICING_AB_DISABLE", "1")
    app = flask.Flask("ab-cohort-test")
    app.register_blueprint(pricing_ab.pricing_ab_bp)
    body = app.test_client().get("/api/v1/pricing/ab-cohort").get_json()
    assert body["ab_active"] is False
    assert body["price_usd"] == 299 and body["display_price"] == "$299"
    assert body["stripe_url"] == STRIPE_LINKS["pro"]


def test_no_price_literal_is_typed_into_the_module():
    src = open(os.path.join(ROOT, "routes", "pricing_ab.py"), encoding="utf-8").read()
    assert "_ARM_A_PRICE_USD = _canon_pro_price_usd()" in src
    assert "_ARM_A_PRICE_USD = 199" not in src and "_ARM_A_PRICE_USD = 299" not in src
