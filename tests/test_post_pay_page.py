"""/upgrade/h/done — the post-pay page (r-post-pay-identify, 2026-09-24).

Owner decision 2026-09-21: /upgrade/h moves AFTER payment. The checkout webhook
already binds the buyer's email, so this page asks for one thing only: explicit
marketing consent, through the double opt-in, for the address the PAID session
carries. These tests drive the real blueprint through a Flask test client with
the DB lookup and the opt-in sender stubbed; the lookup's SQL is executed
against Postgres in tests/test_paid_attributed_relayed_checkout_sql.py.
"""
import pytest

flask = pytest.importorskip("flask")

from routes import human_relay  # noqa: E402

CS = "cs_live_a1B2c3D4e5F6g7H8"
BUYER = "buyer.person@example.org"


@pytest.fixture()
def env(monkeypatch):
    state = {"paid": {"ref_kind": "pack_key", "email": BUYER},
             "lookups": [], "optins": [], "optin_result": {"ok": True, "sent": True}}

    def fake_paid(cs):
        state["lookups"].append(cs)
        return state["paid"]

    def fake_optin(email, source="api"):
        state["optins"].append((email, source))
        return state["optin_result"]

    monkeypatch.setattr(human_relay, "_paid_checkout", fake_paid)
    state["trial"] = None
    state["trial_lookups"] = []

    def fake_trial(cs):
        state["trial_lookups"].append(cs)
        return state["trial"]

    monkeypatch.setattr(human_relay, "_trial_checkout", fake_trial)
    import routes.marketing_opt_in as moi
    monkeypatch.setattr(moi, "request_opt_in", fake_optin)
    app = flask.Flask("post-pay-test")
    app.register_blueprint(human_relay.human_relay_bp)
    state["client"] = app.test_client()
    state["app"] = app
    return state


def test_done_reaches_the_post_pay_handler_not_the_token_route(env):
    adapter = env["app"].url_map.bind("dchub.cloud")
    assert adapter.match("/upgrade/h/done")[0] == "human_relay.post_pay_page"
    assert adapter.match("/upgrade/h/abc.def")[0] == "human_relay.relay_page"


def test_paid_page_masks_the_email_and_offers_an_unticked_opt_in(env):
    r = env["client"].get("/upgrade/h/done?cs=" + CS)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert "b***@example.org" in html
    assert BUYER not in html, "the full address must never be rendered"
    assert "name='marketing_opt_in'" in html and "checked" not in html
    assert "noindex" in html and "http-equiv='refresh'" not in html
    assert "API key your agent already uses" in html
    assert env["lookups"] == [CS]


def test_ticked_opt_in_uses_the_server_side_email_not_the_form(env):
    r = env["client"].post("/upgrade/h/done", data={
        "cs": CS, "marketing_opt_in": "1", "email": "attacker@evil.example"})
    assert r.status_code == 200
    assert env["optins"] == [(BUYER, "post_pay_page")]


def test_unticked_sends_nothing(env):
    env["client"].post("/upgrade/h/done", data={"cs": CS})
    assert env["optins"] == []


def test_reply_does_not_reveal_whether_the_confirmation_went_out(env):
    sent = env["client"].post("/upgrade/h/done", data={"cs": CS, "marketing_opt_in": "1"})
    env["optin_result"] = {"ok": True, "sent": False, "reason": "suppressed"}
    refused = env["client"].post("/upgrade/h/done", data={"cs": CS, "marketing_opt_in": "1"})
    assert sent.get_data() == refused.get_data()


def test_unrecorded_payment_retries_briefly_then_stops(env):
    env["paid"] = None
    first = env["client"].get("/upgrade/h/done?cs=" + CS).get_data(as_text=True)
    assert "r=1" in first and "http-equiv='refresh'" in first
    assert "marketing_opt_in" not in first
    last = env["client"].get("/upgrade/h/done?cs=%s&r=3" % CS).get_data(as_text=True)
    assert "http-equiv='refresh'" not in last
    assert "Payment received" in last


def test_a_malformed_cs_is_never_looked_up(env):
    for bad in ("", "cs_live_x", "pi_live_abcdefghijk", CS + "'--", "cs_live_" + "a" * 300):
        html = env["client"].get("/upgrade/h/done", query_string={"cs": bad}).get_data(as_text=True)
        assert "http-equiv='refresh'" not in html, bad
    env["client"].post("/upgrade/h/done", data={"cs": "nope", "marketing_opt_in": "1"})
    assert env["lookups"] == [] and env["optins"] == []


def test_paid_session_without_an_email_shows_no_form(env):
    env["paid"] = {"ref_kind": "session", "email": ""}
    html = env["client"].get("/upgrade/h/done?cs=" + CS).get_data(as_text=True)
    assert "marketing_opt_in" not in html and "receipt comes from Stripe" in html
    env["client"].post("/upgrade/h/done", data={"cs": CS, "marketing_opt_in": "1"})
    assert env["optins"] == []


def test_mask_email():
    assert human_relay._mask_email("azmartone@gmail.com") == "a***@gmail.com"
    assert human_relay._mask_email("noatsign") == ""
    assert human_relay._mask_email("") == ""


# ── r-pro-trial7: a $0 free-trial checkout is never "paid" ────────────────

TRIAL = {"email": BUYER, "trial_end": 1759363200, "price": "$99/mo"}  # 2025-10-02 UTC


def test_trial_checkout_shows_trial_started_not_payment_received(env):
    env["paid"] = None
    env["trial"] = dict(TRIAL)
    r = env["client"].get("/upgrade/h/done?cs=" + CS)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert "Your Pro trial has started" in html
    assert "Nothing was charged today" in html
    assert "$99/mo on October 2, 2025" in html
    assert "Payment received" not in html and "payment received" not in html
    assert "http-equiv='refresh'" not in html
    assert "b***@example.org" in html and BUYER not in html
    assert "name='marketing_opt_in'" in html and "checked" not in html
    assert env["trial_lookups"] == [CS]


def test_trial_opt_in_uses_the_stripe_side_email(env):
    env["paid"] = None
    env["trial"] = dict(TRIAL)
    env["client"].post("/upgrade/h/done", data={
        "cs": CS, "marketing_opt_in": "1", "email": "attacker@example.com"})
    assert env["optins"] == [(BUYER, "post_pay_page")]


def test_paid_session_never_asks_stripe(env):
    env["client"].get("/upgrade/h/done?cs=" + CS)
    assert env["trial_lookups"] == []


def test_malformed_cs_never_asks_stripe(env):
    env["paid"] = None
    env["client"].get("/upgrade/h/done?cs=not-a-session")
    assert env["trial_lookups"] == []


class _Obj(dict):
    pass


def _fake_stripe(monkeypatch, sess):
    import sys
    import types
    calls = []
    mod = types.ModuleType("stripe")

    class _Session:
        @staticmethod
        def retrieve(cs, **kw):
            calls.append((cs, kw))
            if isinstance(sess, Exception):
                raise sess
            return sess

    mod.checkout = types.SimpleNamespace(Session=_Session)
    monkeypatch.setitem(sys.modules, "stripe", mod)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    return calls


def _trial_sess(**over):
    sub = _Obj(status="trialing", trial_end=1759363200,
               items=_Obj(data=[_Obj(price=_Obj(unit_amount=9900,
                                                recurring=_Obj(interval="month")))]))
    s = _Obj(status="complete", mode="subscription",
             payment_status="no_payment_required", subscription=sub,
             customer_details=_Obj(email="Buyer.Person@Example.org"))
    s.update(over)
    return s


def test_trial_lookup_reads_a_real_trial_session(monkeypatch):
    calls = _fake_stripe(monkeypatch, _trial_sess())
    got = human_relay._trial_checkout(CS)
    assert got == {"email": BUYER, "trial_end": 1759363200, "price": "$99/mo"}
    assert calls[0][0] == CS and calls[0][1]["expand"] == ["subscription"]


@pytest.mark.parametrize("over", [
    {"payment_status": "paid"},
    {"status": "open"},
    {"mode": "payment"},
    {"subscription": "sub_unexpanded"},
    {"subscription": _Obj(status="active", trial_end=None)},
], ids=["paid", "open", "one_time", "unexpanded", "not_trialing"])
def test_trial_lookup_refuses_anything_but_a_trialing_trial(monkeypatch, over):
    _fake_stripe(monkeypatch, _trial_sess(**over))
    assert human_relay._trial_checkout(CS) is None


def test_trial_lookup_never_raises_and_needs_a_key(monkeypatch):
    _fake_stripe(monkeypatch, RuntimeError("stripe down"))
    assert human_relay._trial_checkout(CS) is None
    monkeypatch.delenv("STRIPE_SECRET_KEY")
    assert human_relay._trial_checkout(CS) is None
