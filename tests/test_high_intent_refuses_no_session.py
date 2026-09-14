"""r-hi-needs-session (2026-09-14): the placeholder "no-session" is not a session.

The mcp-server sends session_id="no-session" for a tools/call with no Mcp-Session-Id.
Both high-intent endpoints keyed mcp_high_intent_sessions on it, so every sessionless
caller shared one (session, tool) row. Read on Neon 2026-09-14: 17 such rows, 15 claims
minted on them between 07-26 and 09-13, 14 of them auto-redeemed within seconds.

These drive the real routes through Flask's test client and need no database: _conn is
replaced by a recorder. The control proves a real session id reaches that recorder, so a
refusal cannot pass by failing earlier (auth, validation, a missing route).
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flask import Flask  # noqa: E402

import routes.mcp_high_intent_claim as hi  # noqa: E402

KEY = "test-internal-key-for-high-intent"
HEADERS = {"X-Internal-Key": KEY}
REAL_SID = "e6f1c0de-1234-4aaa-9999-abcdef012345"
TOOL = "get_tax_incentives"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", KEY)
    monkeypatch.delenv("DCHUB_ADMIN_KEY", raising=False)
    opened = []

    def recorder():
        opened.append(True)
        return None  # the real _conn's answer when no DSN is set: the route stops at no_db

    monkeypatch.setattr(hi, "_conn", recorder)
    app = Flask(__name__)
    app.register_blueprint(hi.mcp_high_intent_claim_bp)
    test_client = app.test_client()
    test_client.opened = opened
    return test_client


def _track(client, sid):
    return client.post("/api/v1/mcp/track-paid-hit", headers=HEADERS,
                       json={"session_id": sid, "tool": TOOL,
                             "user_agent": "node", "mcp_client": "mcp"})


def _mint(client, sid):
    return client.get("/api/v1/mcp/should-mint-claim", headers=HEADERS,
                      query_string={"session_id": sid, "tool": TOOL, "variant": "generic"})


def test_track_paid_hit_counts_nothing_for_the_placeholder(client):
    r = _track(client, "no-session")
    assert r.status_code == 200
    body = r.get_json()
    assert body["skipped"] == "no_session"
    assert body["count"] == 0
    assert body["is_high_intent"] is False
    assert client.opened == []


def test_should_mint_claim_mints_nothing_for_the_placeholder(client):
    r = _mint(client, "no-session")
    assert r.status_code == 200
    body = r.get_json()
    assert body["should_mint"] is False
    assert body["reason"] == "no_session"
    assert "claim_url" not in body
    assert client.opened == []


@pytest.mark.parametrize("spelling", ["NO-SESSION", "No-Session", " no-session "])
def test_the_placeholder_is_refused_whatever_its_case_or_padding(client, spelling):
    assert _track(client, spelling).get_json().get("skipped") == "no_session"
    assert _mint(client, spelling).get_json().get("reason") == "no_session"
    assert client.opened == []


def test_control_a_real_session_still_reaches_the_database(client):
    assert _track(client, REAL_SID).status_code == 503
    assert _mint(client, REAL_SID).get_json() == {"should_mint": False, "error": "no_db"}
    assert len(client.opened) == 2


def test_an_id_that_only_contains_the_placeholder_is_a_session(client):
    for sid in ("no-session-2", "sess-no-session"):
        _track(client, sid)
        _mint(client, sid)
    assert len(client.opened) == 4


def test_auth_is_still_checked_before_the_placeholder(client):
    r = client.post("/api/v1/mcp/track-paid-hit", json={"session_id": "no-session", "tool": TOOL})
    assert r.status_code == 403
    r = client.get("/api/v1/mcp/should-mint-claim",
                   query_string={"session_id": "no-session", "tool": TOOL})
    assert r.status_code == 403
    assert client.opened == []
