"""End-to-end guard: a referral click must actually reach Stripe as a
partner-attributed conversion.

The pure-function tests in test_partner_attribution_ref.py prove the ref logic.
They do NOT prove the wiring — that /r/<partner> really sets the cookie, that
the cookie carries the right flags, or that /pricing/upgrade reads it back. The
whole failure this change addresses was a break in exactly that kind of seam:
clicks were logged and payments were recorded, and nothing joined them.

These build a minimal Flask app from the two blueprints directly. main.py is
never imported (it opens DB pools and registers ~200 blueprints).
"""
import os

import pytest

flask = pytest.importorskip("flask")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DCHUB_REFERRAL_PARTNERS", "data-center-signals")
    # No DATABASE_URL → _log_click's write is swallowed by design; the redirect
    # and the cookie must still happen. That is the point of the fixture: a
    # logging outage must not silently cost a partner their attribution.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)

    from routes.partnership_click_tracker import partnership_click_bp
    from routes.stripe_direct_upgrade import stripe_direct_bp

    app = flask.Flask(__name__)
    app.register_blueprint(partnership_click_bp)
    app.register_blueprint(stripe_direct_bp)
    app.testing = True
    return app.test_client()


def _cookie_header(resp):
    return "; ".join(resp.headers.getlist("Set-Cookie"))


# ── /r/<partner> stamps the cookie ────────────────────────────────────────

def test_referral_sets_cookie_for_allowlisted_partner(client):
    resp = client.get("/r/data-center-signals")
    assert resp.status_code == 302
    assert "dchub_partner_ref=data-center-signals" in _cookie_header(resp)


def test_referral_cookie_carries_the_right_flags(client):
    """HttpOnly because only the server reads it. SameSite=Lax because it must
    survive a top-level GET arriving from the partner's own site — Strict
    would drop it on exactly the navigation that matters."""
    cookie = _cookie_header(client.get("/r/data-center-signals"))
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=Lax" in cookie
    assert "Path=/" in cookie


def test_unknown_partner_gets_no_cookie(client):
    """Still redirects (and is logged), but cannot become commissionable."""
    resp = client.get("/r/acme-affiliate")
    assert resp.status_code == 302
    assert "dchub_partner_ref" not in _cookie_header(resp)


def test_referral_survives_a_logging_outage(client):
    """DB is absent in this fixture. The cookie must still be set — otherwise
    a partner loses attribution whenever our click table is unavailable."""
    resp = client.get("/r/data-center-signals")
    assert "dchub_partner_ref=data-center-signals" in _cookie_header(resp)


# ── ?to= must not become an open redirect ─────────────────────────────────

def test_deep_link_stays_on_our_own_host(client):
    resp = client.get("/r/data-center-signals?to=/markets/ashburn")
    assert resp.headers["Location"] == "https://dchub.cloud/markets/ashburn"


@pytest.mark.parametrize("evil", [
    "https://evil.example.com",
    "//evil.example.com",
    "/\\evil.example.com",
    "http://evil.example.com/x",
])
def test_open_redirect_is_refused(client, evil):
    resp = client.get("/r/data-center-signals", query_string={"to": evil})
    assert resp.headers["Location"] == "https://dchub.cloud/"


# ── the cookie reaches Stripe ─────────────────────────────────────────────

def test_cookie_produces_a_partner_client_reference_id(client):
    client.set_cookie("dchub_partner_ref", "data-center-signals")
    resp = client.get("/upgrade?tier=developer&direct=1")
    assert resp.status_code == 302
    assert "web__partner__data-center-signals" in resp.headers["Location"]


def test_explicit_surface_still_wins_over_the_cookie(client):
    client.set_cookie("dchub_partner_ref", "data-center-signals")
    resp = client.get("/upgrade?tier=developer&direct=1&surface=market&ref=ashburn")
    loc = resp.headers["Location"]
    assert "web__market__ashburn" in loc
    assert "partner" not in loc


def test_forged_cookie_earns_nothing(client):
    """A visitor setting the cookie by hand to a slug we never issued must not
    manufacture a commissionable conversion."""
    client.set_cookie("dchub_partner_ref", "acme-affiliate")
    resp = client.get("/upgrade?tier=developer&direct=1")
    loc = resp.headers["Location"]
    assert "acme-affiliate" not in loc
    assert "web__partner__" not in loc


def test_no_cookie_leaves_the_legacy_shape_untouched(client):
    """Regression guard: with no referral in play the URL must be exactly what
    it was before this change."""
    resp = client.get("/upgrade?tier=developer&direct=1")
    assert "mcp%3Atool%3Dnone%3Aref%3Dpaywall" in resp.headers["Location"]


def test_partner_carries_through_the_email_capture_hand_off(client):
    """The default /pricing/upgrade path forwards to the email-capture form;
    the partner must survive that leg too, not just ?direct=1."""
    client.set_cookie("dchub_partner_ref", "data-center-signals")
    resp = client.get("/pricing/upgrade?tier=developer")
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert "/pricing/checkout/start" in loc
    assert "surface=partner" in loc
    assert "data-center-signals" in loc


def test_json_paywall_link_is_attributed_too(client):
    client.set_cookie("dchub_partner_ref", "data-center-signals")
    resp = client.get("/api/v1/paywall/checkout?tier=developer")
    assert "web__partner__data-center-signals" in resp.get_json()["checkout_url"]
