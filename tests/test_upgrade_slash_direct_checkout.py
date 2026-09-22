"""/upgrade/ (trailing slash) is the direct-checkout revenue path.

The edge forwards it to this app, and dchub-frontend's
scripts/qa-critical-smoke.mjs check (c) fails unless it answers a 3xx to
Stripe or checkout. Until 2026-09-21 the only rule matching it was
stripe_direct_upgrade's "/upgrade" with strict_slashes=False. #5127 deleted that
rule as the shadowed loser of bare /upgrade (pair_code wins that one), and
production answered /upgrade/ with a 404 (measured 2026-09-22).

The app is built from the two blueprints that contest the path, registered in
main.py's order (pair_code first), so bare /upgrade goes to its real winner.
main.py is never imported (it opens DB pools and registers ~200 blueprints).
The booted app's shadow check is app-contract-gate's.
"""
import os
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

flask = pytest.importorskip("flask")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import app_contract_gate as gate  # noqa: E402  (stdlib-only at import time)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)

    from routes.pair_code import pair_code_bp
    from routes.stripe_direct_upgrade import stripe_direct_bp

    a = flask.Flask(__name__)
    a.register_blueprint(pair_code_bp)
    a.register_blueprint(stripe_direct_bp)
    a.testing = True
    return a


def _endpoint(app, path):
    return app.url_map.bind("dchub.cloud").match(path, method="GET")[0]


def test_upgrade_slash_is_served_by_the_direct_checkout_view(app):
    assert _endpoint(app, "/upgrade/") == "stripe_direct_upgrade.upgrade_redirect"


def test_bare_upgrade_is_still_pair_codes(app):
    # A strict "/upgrade/" rule must not 308 bare /upgrade into itself.
    assert _endpoint(app, "/upgrade") == "pair_code.upgrade_redirect"


@pytest.mark.parametrize("qs,tier", [("", None), ("?tier=pro", "pro")],
                         ids=["no_params", "tier_pro"])
def test_upgrade_slash_302s_straight_to_stripe(app, qs, tier):
    from routes._stripe_links import STRIPE_LINKS

    resp = app.test_client().get("/upgrade/" + qs)
    assert resp.status_code == 302, resp.status_code
    loc = resp.headers["Location"]
    parts = urlsplit(loc)
    assert parts.netloc == "buy.stripe.com", loc
    assert "client_reference_id" in parse_qs(parts.query), loc
    if tier:
        assert loc.startswith(STRIPE_LINKS[tier]), loc


def test_the_slash_rule_is_not_a_shadowed_duplicate(app):
    assert gate.shadowed(app) == {}
