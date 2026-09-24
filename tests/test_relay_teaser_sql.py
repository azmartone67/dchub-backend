"""r-relay-teaser against a real Postgres: store through the endpoint, render on the page.

The hermetic tests (tests/test_relay_teaser.py) stub get_teaser. Only a real
database shows the DDL runs, the first write wins (ON CONFLICT DO NOTHING on the
token signature), the page reads what the endpoint wrote, and an outage renders
the page without the number instead of failing it.

Set RELAYED_CHECKOUT_SQL_DSN to run it (CI's relayed-checkout lane passes the
db-parity DSN and then asserts these files did not skip).
"""
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")
flask = pytest.importorskip("flask")

DSN = os.environ.get("RELAYED_CHECKOUT_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="RELAYED_CHECKOUT_SQL_DSN not set — no Postgres to run against")
KEY = "relay-teaser-sql-test-key"


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", KEY)
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("DCHUB_HUMAN_RELAY_DISABLE", raising=False)
    from routes import human_relay, relay_teaser
    monkeypatch.setattr(human_relay, "_log_open", lambda info, token, valid: None)
    monkeypatch.setattr(relay_teaser, "_DDL_DONE", [False])
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS relay_teasers")
    conn.close()
    a = flask.Flask("relay-teaser-sql")
    a.register_blueprint(human_relay.human_relay_bp)
    return a, human_relay


def _store(client, token, label, value):
    return client.post("/api/v1/relay/teaser", headers={"X-Internal-Key": KEY},
                       json={"token": token, "label": label, "value": value})


def test_store_then_render_and_the_first_write_wins(app):
    a, relay = app
    c = a.test_client()
    tok = relay.make_relay_token("sess-sql", "get_grid_intelligence", "free")
    r1 = _store(c, tok, "constraint score", "62")
    assert r1.status_code == 200 and r1.get_json() == {"ok": True, "stored": True}
    r2 = _store(c, tok, "constraint score", "99")
    assert r2.status_code == 200 and r2.get_json() == {"ok": True, "stored": False}
    html = c.get(f"/upgrade/h/{tok}").get_data(as_text=True)
    assert "<b>constraint score: 62</b>" in html
    assert "99" not in html.split("held back", 1)[1][:60]


def test_the_row_carries_the_session_and_tool_from_the_token(app):
    a, relay = app
    tok = relay.make_relay_token("sess-row", "get_fiber_intel", "free")
    assert _store(a.test_client(), tok, "months to power", "18 months").status_code == 200
    conn = psycopg2.connect(DSN)
    with conn.cursor() as cur:
        cur.execute("SELECT session_id, tool, label, value FROM relay_teasers"
                    " WHERE token_sig = %s", (tok.rsplit(".", 1)[1],))
        assert cur.fetchone() == ("sess-row", "get_fiber_intel", "months to power", "18 months")
    conn.close()


def test_another_tokens_teaser_is_not_shown(app):
    a, relay = app
    c = a.test_client()
    t1 = relay.make_relay_token("sess-a", "rank_markets", "free")
    t2 = relay.make_relay_token("sess-b", "rank_markets", "free")
    assert _store(c, t1, "constraint score", "62").status_code == 200
    html = c.get(f"/upgrade/h/{t2}").get_data(as_text=True)
    assert "held back" not in html


def test_an_outage_renders_the_page_without_the_number(app, monkeypatch):
    a, relay = app
    monkeypatch.setenv("DATABASE_URL", "postgres://nobody@127.0.0.1:1/none?connect_timeout=1")
    c = a.test_client()
    tok = relay.make_relay_token("sess-down", "rank_markets", "free")
    r = _store(c, tok, "constraint score", "62")
    assert r.status_code == 503 and r.get_json()["error"] == "store_failed"
    html = c.get(f"/upgrade/h/{tok}").get_data(as_text=True)
    assert "held back" not in html and "Get full data — $10 one-time" in html
