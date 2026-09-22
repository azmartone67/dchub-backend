"""/upgrade/h asks for marketing consent separately, and only through the
double opt-in (frontend#1534, 2026-09-22).

The issue's rule: identify + marketing_opt_in capture on /upgrade/h, CRM-only
until opt-in, and no email sends while marketing_opt_in is false.

The page's email form already captured the address for the receipt and the key
(r-identify-rung). Consent is now an extra, unticked box. Ticked: the form asks
routes/marketing_opt_in.request_opt_in, which validates the address, honours
suppression and its per-address cooldown, and sends the ONE confirmation email;
the address becomes marketable only when that link is clicked. Unticked:
nothing is requested and nothing is sent. Either way the buyer continues to
the same checkout, and a failure on the way cannot stop them.

The real route runs (human_relay_bp on a Flask app) with real signed tokens;
capture() and request_opt_in() are recorders, because their own behaviour is
pinned by tests/test_identify_rung.py and the opt-in module's tests.
"""
import pathlib
import sys

import flask
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECRET = "relay-opt-in-test-internal-key"


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", SECRET)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DCHUB_HUMAN_RELAY_DISABLE", "1")       # no relay_opens writes
    import routes.human_relay as hr
    import routes.marketing_opt_in as moi
    import routes.relay_identify as ri
    calls = {"capture": [], "opt_in": []}
    monkeypatch.setattr(ri, "capture",
                        lambda sid, email, source="relay_page", **kw:
                        calls["capture"].append((sid, email, source)) or {"ok": True})

    def opt_in(email, source="api"):
        calls["opt_in"].append((email, source))
        if calls.get("raise"):
            raise RuntimeError("opt-in store down")
        return {"ok": True, "sent": True, "reason": None}
    monkeypatch.setattr(moi, "request_opt_in", opt_in)
    app = flask.Flask("relay-opt-in")
    app.register_blueprint(hr.human_relay_bp)
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = "100.64.0.9"
    return c, hr, calls


def _token(hr, sid="sess-optin-1"):
    return hr.make_relay_token(sid, "get_grid_intelligence", "free")


def test_the_form_offers_consent_unticked(relay):
    c, hr, _ = relay
    html = c.get("/upgrade/h/" + _token(hr)).get_data(as_text=True)
    assert "name='marketing_opt_in'" in html, "the consent box is not on the page"
    box = html[html.index("name='marketing_opt_in'") - 60: html.index("name='marketing_opt_in'") + 80]
    assert "checked" not in box, "consent must never be pre-ticked"
    assert "confirm" in html.lower()


def test_ticked_asks_for_the_double_opt_in_once_and_still_checks_out(relay):
    c, hr, calls = relay
    r = c.post("/upgrade/h/" + _token(hr),
               data={"email": "human@example.com", "marketing_opt_in": "1"})
    assert r.status_code == 302
    assert calls["opt_in"] == [("human@example.com", "relay_page")]
    assert calls["capture"] == [("sess-optin-1", "human@example.com", "relay_page")]


def test_unticked_asks_for_nothing(relay):
    c, hr, calls = relay
    r = c.post("/upgrade/h/" + _token(hr), data={"email": "human@example.com"})
    assert r.status_code == 302
    assert calls["opt_in"] == []
    assert len(calls["capture"]) == 1


@pytest.mark.parametrize("value", ["0", "on", "true", ""])
def test_only_the_boxs_own_value_is_consent(relay, value):
    c, hr, calls = relay
    c.post("/upgrade/h/" + _token(hr), data={"email": "h@example.com", "marketing_opt_in": value})
    assert calls["opt_in"] == []


def test_a_failed_opt_in_request_still_reaches_checkout(relay):
    c, hr, calls = relay
    calls["raise"] = True
    r = c.post("/upgrade/h/" + _token(hr),
               data={"email": "human@example.com", "marketing_opt_in": "1"})
    assert r.status_code == 302 and r.headers["Location"]


def test_a_token_without_a_session_asks_for_nothing(relay):
    c, hr, calls = relay
    r = c.post("/upgrade/h/not-a-valid-token",
               data={"email": "human@example.com", "marketing_opt_in": "1"})
    assert r.status_code == 302
    assert calls["opt_in"] == [] and calls["capture"] == []


def test_the_page_never_writes_consent_itself():
    """Consent is set in exactly one place: the opt-in module, on a confirmed
    click. This page may only REQUEST it."""
    src = (ROOT / "routes" / "human_relay.py").read_text(encoding="utf-8")
    assert "marketing_opt_in_at" not in src
    assert "'marketing_opt_in', " not in src and "marketing_opt_in =" not in src
