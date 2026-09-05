"""The Grok on-ramp must end in a VERIFIED TOOL CALL, not in a pasted URL.

Grok is the platform that looks like a one-day trial in the 7-day retention
read: the connector gets added, the page says "then ask Grok anything", and
nothing ever proves a tool ran. Claude comes back because the connector stays
in the loop; Grok's on-ramp stopped one step short of a repeatable call.

Three surfaces describe the same install and they used to disagree:

    /connect            static/connect.html          "no code, ~30 seconds"
    /connect-mcp        (dchub-frontend)             paid tier named, key-to-persist
    /integrations/grok  routes/integrations_landing  the honest one

These tests pin, on the two BACKEND surfaces, the three facts a Grok user has
to have and that only one page used to carry:

  1. the paid-tier gate — /connect is where people bounce, and they never
     reach the page that mentioned it;
  2. a literal first prompt plus the tool name to look for, so "installed"
     means "a tool ran" and not "a URL was pasted";
  3. connect_url, not the raw key — measured on Grok, a minted key made
     exactly ONE call (the claim) and was never presented again, because the
     client rebuilds the session on every tool call. `claim_free_key` returns
     `connect_url` (server.mjs `_connectUrl` → https://dchub.cloud/mcp?apiKey=…)
     and that URL is the only durable object on a URL-box client.

Assertions are anchored to the Grok SECTION, never to the whole page — a
substring found anywhere in a 40 KB document proves nothing about the card a
reader is looking at. See feedback: vacuous substring assertions.

Pure functions: no DB, no network, and never imports main (tests/ must not).
"""
import os
import re

import pytest

il = pytest.importorskip("routes.integrations_landing")

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The first prompt is a LITERAL both surfaces must carry byte-for-byte. It is
# not decorative: it is the pass/fail test the reader runs, and paraphrasing it
# is how "ask Grok anything" comes back.
FIRST_PROMPT = "Use DC Hub. Which US grid has the most headroom right now?"

# get_grid_scoreboard is the deliberate first tool: keyless (verified live
# 2026-09-05, anonymous tools/call → ok:true) and cheap, so the test cannot
# fail for the one reason a new user cannot fix — not holding a key yet.
FIRST_TOOL = "get_grid_scoreboard"


def _connect_html() -> str:
    with open(os.path.join(_REPO, "static", "connect.html"), encoding="utf-8") as f:
        return f.read()


def _flat(html: str) -> str:
    """Collapse whitespace runs the way a browser does.

    Without this the tests read SOURCE LAYOUT rather than rendered copy: a
    sentence that happens to wrap between "not" and "attached" would fail a
    check on text every reader sees on one line.
    """
    return re.sub(r"\s+", " ", html)


def _grok_card() -> str:
    """Just the Grok platform-card from /connect — not the whole page."""
    html = _connect_html()
    start = html.index("<h3>Grok (xAI)</h3>")
    end = html.index("</div>", start)
    card = html[start:end]
    assert 0 < len(card) < 6000, f"Grok card slice looks wrong ({len(card)} chars)"
    return _flat(card)


def _grok_steps() -> str:
    """The consumer step list on /integrations/grok — not the whole recipe."""
    html = il.GROK_RECIPE_HTML
    start = html.index("Connect in Grok (consumer)")
    end = html.index("</ol>", start)
    return _flat(html[start:end])


# ── 1. the paid-tier gate, on the page where people actually bounce ──────────

def test_connect_names_the_paid_tier_gate():
    assert "paid Grok tier" in _grok_card()


def test_integrations_grok_names_the_paid_tier_gate():
    assert "paid Grok tier" in _grok_steps()


# ── 2. install is finished by a TOOL CALL, not by a paste ────────────────────

@pytest.mark.parametrize("name,section", [
    ("connect", _grok_card),
    ("integrations/grok", _grok_steps),
])
def test_first_message_is_a_literal_prompt_naming_the_tool_to_expect(name, section):
    html = section()
    assert FIRST_PROMPT in html, f"{name}: the verbatim first prompt is missing"
    assert FIRST_TOOL in html, f"{name}: does not say which tool should fire"
    # The negative half is the point: the reader must be told what FAILURE
    # looks like, or they cannot tell an attached connector from a hallucination.
    assert "not attached" in html, f"{name}: no failure signal for the first call"


def test_connect_does_not_sell_grok_as_a_frictionless_thirty_second_install():
    """The old lede — "no code, about 30 seconds" + "ask Grok anything" — is
    what produced installs that never made a second call. Both are banned."""
    card = _grok_card()
    assert "30 seconds" not in card
    assert "ask Grok anything" not in card


# ── 3. connect_url is the durable object on Grok, never the bare key ─────────

@pytest.mark.parametrize("name,section", [
    ("connect", _grok_card),
    ("integrations/grok", _grok_steps),
])
def test_persistence_is_the_connect_url(name, section):
    html = section()
    assert "connect_url" in html, f"{name}: does not name connect_url"
    assert "claim_free_key" in html, f"{name}: does not say where connect_url comes from"


def test_integrations_grok_rate_guidance_does_not_call_the_bare_key_durable():
    """`claim_free_key` alone is NOT persistence on Grok. The rate-guidance
    pane used to say the key made "the connector recognised next session",
    which is the opposite of what was measured there."""
    html = il.GROK_RECIPE_HTML
    assert "durable key so the connector is recognised next" not in html


def test_grok_custom_instructions_tell_the_agent_to_relay_the_url():
    """The copy-paste custom-instructions block is what the MODEL follows. If
    it says only "call claim_free_key and continue", the agent pockets a key
    it cannot keep and the human never sees the URL that would have worked."""
    html = il.GROK_RECIPE_HTML
    start = html.index("Grok custom instructions")
    block = html[start:html.index("</pre>", start)]
    assert "connect_url" in block
