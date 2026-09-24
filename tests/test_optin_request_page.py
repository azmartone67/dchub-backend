"""The paywall opt-in link and the page it opens (r-optin-link-dead, 2026-09-24).

Measured live 2026-09-24: mcp_gatekeeper._optin_cta_block linked to
/api/v1/marketing/opt-in/request (404, no route), and the real
/api/v1/opt-in/request was POST-only (405 to a click). No human could opt in
through the paywall; opt_in_consents held 4 rows ever, none confirmed.

These tests drive the real blueprint through a Flask test client, with the
sender stubbed. The first one is the guard that would have caught the 404: the
CTA's own URL must resolve to a GET route on the blueprint that serves it.
"""
from urllib.parse import urlparse

import pytest

flask = pytest.importorskip("flask")

import routes.marketing_opt_in as moi  # noqa: E402


@pytest.fixture()
def env(monkeypatch):
    calls = []
    result = {"v": {"ok": True, "sent": True, "reason": None}}

    def fake(email, source="api"):
        calls.append((email, source))
        return result["v"]

    monkeypatch.setattr(moi, "request_opt_in", fake)
    app = flask.Flask("optin-page-test")
    app.register_blueprint(moi.marketing_opt_in_bp)
    return {"client": app.test_client(), "app": app, "calls": calls, "result": result}


def _cta_url(monkeypatch):
    import mcp_gatekeeper as g
    monkeypatch.setenv("OPTIN_CTA_ENABLED", "true")
    cta = g._optin_cta_block("get_grid_intelligence", g.Tier.FREE, "dch_live_secret")
    assert cta is not None
    return cta["optin_url"]


def test_the_cta_link_resolves_to_a_get_route_that_renders_the_form(env, monkeypatch):
    url = _cta_url(monkeypatch)
    u = urlparse(url)
    assert u.netloc == "dchub.cloud"
    endpoint, _ = env["app"].url_map.bind("dchub.cloud").match(u.path, method="GET")
    assert endpoint == "marketing_opt_in.opt_in_request"
    r = env["client"].get(u.path + "?" + u.query)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "<form" in html and "name=\"email\"" in html
    assert 'value="paywall_optin_cta"' in html
    assert "dch_live_secret" not in url


def test_opening_the_page_sends_nothing(env):
    env["client"].get("/api/v1/opt-in/request?source=paywall_optin_cta")
    assert env["calls"] == [], "a link preview must not be able to mail anyone"


@pytest.mark.parametrize("raw", ["<script>alert(1)</script>", '"><img src=x>', "has space", ""])
def test_an_odd_source_falls_back_and_is_never_reflected(env, raw):
    html = env["client"].get("/api/v1/opt-in/request", query_string={"source": raw}).get_data(as_text=True)
    assert 'value="optin_request"' in html
    if raw:
        assert raw not in html


def test_the_form_post_requests_the_double_opt_in_and_answers_in_html(env):
    r = env["client"].post("/api/v1/opt-in/request",
                           data={"email": " Buyer@Example.org ", "source": "paywall_optin_cta"})
    assert r.mimetype == "text/html"
    assert env["calls"] == [("buyer@example.org", "paywall_optin_cta")]


def test_the_form_reply_does_not_reveal_whether_mail_went_out(env):
    a = env["client"].post("/api/v1/opt-in/request", data={"email": "x@example.org"}).get_data()
    env["result"]["v"] = {"ok": True, "sent": False, "reason": "suppressed"}
    b = env["client"].post("/api/v1/opt-in/request", data={"email": "x@example.org"}).get_data()
    assert a == b


def test_an_empty_form_sends_nothing(env):
    env["client"].post("/api/v1/opt-in/request", data={"email": "", "source": "x"})
    assert env["calls"] == []


def test_the_json_contract_is_unchanged(env):
    r = env["client"].post("/api/v1/opt-in/request",
                           json={"email": "a@example.org", "source": "api_client"})
    body = r.get_json()
    assert r.mimetype == "application/json"
    assert set(body) >= {"ok", "sent", "reason", "message"}
    assert env["calls"] == [("a@example.org", "api_client")]
