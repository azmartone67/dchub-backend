"""r-relay-teaser: one withheld number, shown free on the human relay page.

Stored server-side against the relay token (never inside it: the token is signed,
not encrypted, and the agent holds it). Hermetic: no DB, no network. The real
Postgres round trip is tests/test_relay_teaser_sql.py.
"""
import os

import pytest

flask = pytest.importorskip("flask")


@pytest.fixture()
def relay_app(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", "test-secret")
    monkeypatch.delenv("DCHUB_HUMAN_RELAY_DISABLE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    from routes import human_relay
    monkeypatch.setattr(human_relay, "_log_open", lambda info, token, valid: None)
    app = flask.Flask("relay-teaser-test")
    app.register_blueprint(human_relay.human_relay_bp)
    return app, human_relay


@pytest.mark.parametrize("label,value", [
    ("constraint score", "62"), ("months to power", "18 months"),
    ("wholesale price", "$41.20/MWh"), ("renewable share", "36.5%"),
])
def test_well_formed_teasers_are_accepted(label, value):
    from routes.relay_teaser import valid_teaser
    assert valid_teaser(label, value)


@pytest.mark.parametrize("label,value", [
    ("", "62"), ("constraint score", ""), ("<script>", "1"), ("x", "<b>1</b>"),
    ("a" * 61, "1"), ("constraint score", "1" * 25), ("Constraint Score", "62"),
    (None, "62"), ("constraint score", 62),
])
def test_malformed_teasers_are_refused(label, value):
    from routes.relay_teaser import valid_teaser
    assert not valid_teaser(label, value)


def test_the_store_endpoint_requires_the_internal_key(relay_app):
    app, relay = relay_app
    tok = relay.make_relay_token("sess-t", "get_grid_intelligence", "free")
    r = app.test_client().post("/api/v1/relay/teaser",
                               json={"token": tok, "label": "constraint score", "value": "62"})
    assert r.status_code == 403


def test_the_store_endpoint_refuses_a_token_we_did_not_sign(relay_app):
    app, _ = relay_app
    r = app.test_client().post("/api/v1/relay/teaser",
                               headers={"X-Internal-Key": "test-secret"},
                               json={"token": "forged.0123456789abcdef0123456789abcdef",
                                     "label": "constraint score", "value": "62"})
    assert r.status_code == 400 and r.get_json()["error"] == "invalid_token"


def test_the_store_endpoint_refuses_a_malformed_teaser(relay_app):
    app, relay = relay_app
    tok = relay.make_relay_token("sess-t", "get_grid_intelligence", "free")
    r = app.test_client().post("/api/v1/relay/teaser",
                               headers={"X-Internal-Key": "test-secret"},
                               json={"token": tok, "label": "<b>x</b>", "value": "62"})
    assert r.status_code == 400 and r.get_json()["error"] == "invalid_teaser"


def test_the_page_renders_a_stored_teaser_escaped(relay_app, monkeypatch):
    app, relay = relay_app
    from routes import relay_teaser
    seen = []
    monkeypatch.setattr(relay_teaser, "get_teaser",
                        lambda token: seen.append(token) or ("wholesale price", "$41.20/MWh"))
    tok = relay.make_relay_token("sess-t", "get_grid_intelligence", "free")
    html = app.test_client().get(f"/upgrade/h/{tok}").get_data(as_text=True)
    assert seen == [tok], "the page must look the teaser up by its own token"
    assert ("One number your agent's preview held back: "
            "<b>wholesale price: $41.20/MWh</b>. The full answer has the rest.") in html
    # the teaser sits between the tool line and the pack line
    assert html.index("held back") < html.index("$10 one-time pack")


def test_the_page_is_unchanged_without_a_teaser(relay_app):
    app, relay = relay_app
    tok = relay.make_relay_token("sess-t", "get_grid_intelligence", "free")
    html = app.test_client().get(f"/upgrade/h/{tok}").get_data(as_text=True)
    assert "held back" not in html
    assert "Get full data — $10 one-time" in html


def test_an_invalid_token_never_looks_a_teaser_up(relay_app, monkeypatch):
    app, _ = relay_app
    from routes import relay_teaser
    monkeypatch.setattr(relay_teaser, "get_teaser",
                        lambda token: pytest.fail("looked up a teaser for a bad token"))
    html = app.test_client().get("/upgrade/h/junk.0123").get_data(as_text=True)
    assert "held back" not in html


def test_an_ampersand_in_a_label_is_escaped_on_the_page(relay_app, monkeypatch):
    app, relay = relay_app
    from routes import relay_teaser
    monkeypatch.setattr(relay_teaser, "get_teaser", lambda token: ("r&d spend", "12%"))
    tok = relay.make_relay_token("sess-t", "rank_markets", "free")
    html = app.test_client().get(f"/upgrade/h/{tok}").get_data(as_text=True)
    assert "<b>r&amp;d spend: 12%</b>" in html
