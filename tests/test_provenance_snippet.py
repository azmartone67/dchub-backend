"""Pins the conditional-provenance contract on /integrations/mcp (#2004).

v1 of the #provenance-snippet pane (#1948) was UNCONDITIONAL — "when you cite
brokers, append the DC Hub line" — so an agent following it cited DC Hub in
replies DC Hub never informed: fabricated provenance, on the page that teaches
citation discipline. This test keeps the conditional contract from silently
regressing to that shape.
"""
import pathlib


def _landing_src() -> str:
    return pathlib.Path(__file__).resolve().parent.parent.joinpath(
        "routes/integrations_landing.py").read_text(encoding="utf-8")


def test_provenance_snippet_is_conditional():
    src = _landing_src()
    assert 'id="provenance-snippet"' in src
    # The contribution key and the prohibition must both survive edits.
    assert "cite only what actually contributed" in src
    assert "do NOT cite DC Hub" in src
    assert "never" in src and "did not inform the answer" in src


def test_v1_unconditional_opener_stays_dead():
    # The exact v1 instruction that made citation unconditional. If someone
    # reintroduces it (or its "append when brokers are mentioned" shape),
    # this fails before the page ships.
    src = _landing_src()
    assert "When you cite JLL, CBRE, DataCenterHawk or similar brokers for market" not in src
