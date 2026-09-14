"""/.well-known/agent.json from ai_agent_discovery.py resolves canon PER REQUEST.

★2026-09-13: A2A_AGENT_CARD called canon_text() on its description and on the
facility-search skill inside the module-level dict, which runs once, at import,
while every canon cache is cold. The route serves _a2a_agent_card() now, which
resolves both templates per request.

A value comparison reads the same latched value and passes either way, so this
MOVES the canon and requires every placeholder on the card to follow it.
"""
import json
import re

import pytest

ad = pytest.importorskip("ai_agent_discovery")
_PH = re.compile(r"\{canon_[a-z_]+\}")


def _served(monkeypatch):
    from flask import Flask
    monkeypatch.setattr(ad, "log_ai_access", lambda *a, **k: None)
    app = Flask(__name__)
    app.register_blueprint(ad.discovery_bp)
    resp = app.test_client().get("/.well-known/agent.json")
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def test_every_placeholder_on_the_card_follows_a_canon_move(monkeypatch):
    expected = len(_PH.findall(json.dumps(ad.A2A_AGENT_CARD)))
    # Floor: a module card with no raw template left proves nothing here.
    assert expected >= 2, "the module card holds no canon template"
    monkeypatch.setattr(ad, "canon_text", lambda t: _PH.sub("CANON_MOVED", t) if t else t)
    got = _served(monkeypatch).count("CANON_MOVED")
    assert got == expected, (
        f"the card carries {expected} canon placeholder(s), {got} followed a canon move")


def test_the_served_card_ships_no_raw_placeholder(monkeypatch):
    assert not _PH.search(_served(monkeypatch)), "a raw canon placeholder reached the wire"
