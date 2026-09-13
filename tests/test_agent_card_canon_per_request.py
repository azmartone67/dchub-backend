"""The A2A card resolves canon when it is SERVED, not when it is imported.

2026-09-13, measured cache-busted with and without the edge:
/.well-known/agent-card.json said "21,500+ distinct facilities" while
/api/v1/canon/phrases, /AGENTS.md and the MCP server card all said 21,800+.

AGENT_CARD is a module-level dict. canon_text() inside it ran once, at import,
when every canon cache is cold, so the pinned floor stayed on the card for the
life of the process. #4320 fixed the same defect on /connect by keeping the
template raw and resolving it where the response is built; routes/agent_a2a.py
now does the same in _card().

These tests swap canon_text for a stub that answers whatever canon "is now".
They need no DB, no network and no main.py, and every one of them fails if
_card() goes back to reading the import-time strings.
"""
import pathlib
import re

import routes.agent_a2a as agent_a2a


def _canon(facilities, deals="2,100+"):
    def canon_text(s):
        return s.replace("{canon_facilities}", facilities).replace("{canon_deals}", deals)
    return canon_text


def test_the_served_description_uses_canon_as_of_the_request(monkeypatch):
    monkeypatch.setattr(agent_a2a, "canon_text", _canon("21,800+"))
    card = agent_a2a._card()
    assert "21,800+ distinct facilities" in card["description"]
    assert card["agent"]["description"] == card["description"]


def test_a_canon_that_moves_reaches_the_next_response(monkeypatch):
    monkeypatch.setattr(agent_a2a, "canon_text", _canon("21,500+"))
    first = agent_a2a._card()["description"]
    monkeypatch.setattr(agent_a2a, "canon_text", _canon("21,800+"))
    second = agent_a2a._card()["description"]
    assert "21,500+" in first
    assert "21,800+" in second


def test_the_facility_and_deal_skills_follow_canon_too(monkeypatch):
    monkeypatch.setattr(agent_a2a, "canon_text", _canon("21,800+", deals="2,100+"))
    skills = {s["name"]: s for s in agent_a2a._card()["skills"]}
    assert "21,800+ distinct data center facilities" in skills["facility_intelligence"]["summary"]
    # A2A readers take `description`; it must carry the same live figure.
    assert skills["facility_intelligence"]["description"] == skills["facility_intelligence"]["summary"]
    assert skills["deal_flow"]["summary"].startswith("2,100+ tracked M&A deals")
    # Skills with no canon placeholder pass through untouched.
    assert skills["site_planning"]["summary"] == next(
        s["summary"] for s in agent_a2a.AGENT_CARD["skills"] if s["name"] == "site_planning")


def test_serving_never_mutates_the_shared_module_card(monkeypatch):
    before_desc = agent_a2a.AGENT_CARD["agent"]["description"]
    before_skills = [s.get("summary") for s in agent_a2a.AGENT_CARD["skills"]]
    monkeypatch.setattr(agent_a2a, "canon_text", _canon("99,999+", deals="9,999+"))
    agent_a2a._card()
    assert agent_a2a.AGENT_CARD["agent"]["description"] == before_desc
    assert [s.get("summary") for s in agent_a2a.AGENT_CARD["skills"]] == before_skills


def test_every_live_skill_names_a_skill_that_exists():
    # A renamed skill would silently fall back to its import-time summary.
    names = {s["name"] for s in agent_a2a.AGENT_CARD["skills"]}
    assert set(agent_a2a._LIVE_SKILL_SUMMARIES) <= names


def test_no_hand_typed_deal_count_on_the_card():
    src = pathlib.Path(agent_a2a.__file__).read_text()
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"\d{1,3},\d{3}\+ tracked M&A", code)
