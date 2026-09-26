"""/.well-known/agent-card.json must publish the MCP server version from the
same accessor as every other served card, not a hand-typed literal.

Measured 2026-09-25: the card served "version": "2.1.2" (a literal in
routes/agent_a2a.py) while the MCP server's `initialize` answered 2.12.x.
"""
import routes.agent_a2a as agent_a2a
import ai_surface_canon


def test_card_version_comes_from_the_served_version_accessor(monkeypatch):
    monkeypatch.setattr(ai_surface_canon, "resolve_server_version_cached",
                        lambda: "9.87.65")
    card = agent_a2a._card()
    assert card["version"] == "9.87.65"
    assert card["agent"]["version"] == "9.87.65"


def test_card_version_falls_back_to_the_canon_pin_never_blank(monkeypatch):
    monkeypatch.setattr(ai_surface_canon, "resolve_server_version_cached",
                        lambda: "")
    card = agent_a2a._card()
    assert card["version"] == str(ai_surface_canon.PINNED["version"])
    assert card["version"]


def test_no_version_literal_in_the_module_card():
    assert agent_a2a.AGENT_CARD["agent"]["version"] is None
