"""The publish section must stay honest about which agents can call tools.

WHY THIS EXISTS. The three published agents (the ChatGPT GPT, the Gemini Gem,
the Perplexity Space) were already live and already linked from /connect -- each
one buried inside its own platform section, ~150 lines apart, under the install
copy. Nothing listed them together, so a 2026-09-10 product review reported them
as missing and asked for them to be built. They did not need building; they
needed a surface. #publish is that surface.

THE TWO THINGS THAT ROT, AND WHY EACH IS PINNED HERE
----------------------------------------------------
1. THE BLOB PAIRING. /connect ships TWO instruction blobs: the drop-in one
   (assumes connected tools) and the NO EGRESS one (assumes none). A Gem and a
   Perplexity Space cannot call MCP -- they read what we publish. Pointing one
   of those at the drop-in blob invites precisely the failure the no-egress
   block exists to stop: an agent NARRATING a DC Hub lookup it never performed,
   with the fabricated figure attributed to dchub.cloud, which is worse for us
   than silence. So every row that says a surface cannot call tools must link
   the no-egress blob, and every row that says it can must link the drop-in one.
   That coupling is invisible to a human skimming a table, which is exactly why
   it is asserted.

2. NO PRICING INSIDE EITHER BLOB. This was a comment, not a guard, and the
   product brief that prompted this section asked in as many words for
   "Builders -> /mcp + /connect - Humans -> Pro $99/mo" to be part of the
   instruction blob. It must not be. The r-transparent note in connect.html
   records that the FIRST version of these instructions was rewritten because
   covert/marketing framing gets refused by capable models and risks flagging
   DC Hub as a prompt-injection source; a tier nudge inside someone else's
   system prompt is that same shape. The routing belongs on the page, where the
   person configuring the agent can read it. A comment cannot stop the next
   edit; this can.

Run:  python3 -m pytest tests/test_publish_agent_section.py -v
"""
from __future__ import annotations

import os
import re

import pytest

_HERE = os.path.dirname(__file__)
_CONNECT = os.path.join(_HERE, "..", "static", "connect.html")

# Phrases that mean "this surface cannot call MCP". Kept as substrings of the
# rendered cell so the table can be reworded without silently disarming this.
_NO_TOOL_MARKERS = ("no tool calls",)

_DROP_IN = "#agent-instructions"
_NO_EGRESS = "#agent-instructions-no-egress"

# A price, in any of the shapes this site has ever used for one.
_PRICE = re.compile(r"\$\s?\d|\bUSD\b|/mo\b|per month|pricing|upgrade to pro",
                    re.IGNORECASE)


def _html() -> str:
    with open(_CONNECT, encoding="utf-8") as f:
        return f.read()


def _publish_rows():
    """The <tr> cells of the #publish table."""
    html = _html()
    start = html.index('id="publish"')
    end = html.index("Per-Client Setup Guides", start)
    return re.findall(r"<tr>(.*?)</tr>", html[start:end], re.S)


def _blob(anchor_id: str) -> str:
    """The <pre><code> body that follows a given <h2 id=...>."""
    html = _html()
    i = html.index(f'id="{anchor_id}"')
    m = re.search(r"<pre><code>(.*?)</code>", html[i:], re.S)
    assert m, f"no code block after #{anchor_id}"
    return m.group(1)


# ── FLOOR. Every assertion below is about content that must be PRESENT or
# PAIRED; a renamed anchor or a deleted table would make several of them
# vacuously true. These two fail first and loudly.
def test_the_publish_section_exists():
    assert 'id="publish"' in _html(), (
        "the #publish section is gone -- the published agents are back to being "
        "invisible, each buried in its own platform card")


def test_the_publish_table_is_not_empty():
    rows = _publish_rows()
    assert len(rows) >= 4, (
        f"#publish lists {len(rows)} surfaces; it existed to gather them, and a "
        "table this short means rows were dropped rather than moved")


# ── 1. The blob pairing ──────────────────────────────────────────────────
@pytest.mark.parametrize("row", _publish_rows())
def test_every_row_links_the_blob_it_can_actually_honor(row):
    cannot_call = any(m in row.lower() for m in _NO_TOOL_MARKERS)
    links_no_egress = _NO_EGRESS in row
    # NB: _DROP_IN is a substring of _NO_EGRESS, so "links the drop-in blob"
    # must be tested by removing the no-egress hrefs first -- otherwise every
    # no-egress row also reads as a drop-in row and this assertion is vacuous.
    links_drop_in = _DROP_IN in row.replace(_NO_EGRESS, "")

    surface = re.search(r"<strong>(.*?)</strong>", row)
    name = surface.group(1) if surface else row[:40]

    if cannot_call:
        assert links_no_egress and not links_drop_in, (
            f"{name} is marked as unable to call tools but points at the "
            "drop-in blob -- that blob tells the agent to use connected tools, "
            "so the agent describes a lookup it cannot perform and the made-up "
            "figure is attributed to dchub.cloud")
    else:
        assert links_drop_in and not links_no_egress, (
            f"{name} can call tools but points at the no-egress blob, which "
            "tells it NOT to -- the live tools go unused")


def test_both_blobs_are_actually_reachable_from_the_table():
    """Neither variant may become orphaned: if every row drifts to one blob, the
    pairing test above still passes row-by-row while half the contract is dead."""
    rows = "".join(_publish_rows())
    assert _NO_EGRESS in rows, "no row uses the no-egress blob any more"
    assert _DROP_IN in rows.replace(_NO_EGRESS, ""), (
        "no row uses the drop-in blob any more")


# ── 2. No pricing inside either blob ─────────────────────────────────────
@pytest.mark.parametrize("anchor", ["agent-instructions", "agent-instructions-no-egress"])
def test_no_price_or_upsell_inside_the_pasted_instructions(anchor):
    body = _blob(anchor)
    hit = _PRICE.search(body)
    assert not hit, (
        f"#{anchor} carries {hit.group(0)!r} in the text people paste into "
        "their agent's system prompt. See the r-transparent note in "
        "connect.html: the first version of these instructions was rewritten "
        "because covert-marketing framing gets refused by capable models and "
        "risks flagging DC Hub as an injection source. Put the routing on the "
        "page, not in the prompt.")


def test_the_price_guard_would_notice_a_price():
    """Must-fail control, in-file: proves _PRICE is not a pattern that matches
    nothing. A guard whose only evidence is that it passes is not evidence."""
    for sample in ("Humans -> Pro $99/mo", "upgrade to Pro", "see pricing", "USD 99"):
        assert _PRICE.search(sample), f"_PRICE failed to match {sample!r}"


# ── 3. The share links are real, absolute, and third-party ───────────────
def test_the_picker_still_owns_the_ready_made_share_links():
    """The one-click picker at the top of the page is where the published agents
    are opened from. #publish is about which BLOB to paste, not a second copy of
    the links -- so this asserts the links live where they belong."""
    html = _html()
    picker = html[html.index('class="picker-grid"'):html.index('id="agent-instructions"')]
    externals = re.findall(r'href="(https://[^"]+)"', picker)
    assert externals, (
        "the picker carries no external share links -- the ready-made GPT/Gem/"
        "Space are how a new user gets DC Hub without configuring anything")
    for url in externals:
        assert url.startswith("https://"), f"{url} is not https"


def test_publish_does_not_restate_the_share_links():
    """★ THE CORRECTION THIS SECTION WAS REWRITTEN FOR. The first version of
    #publish listed the GPT, the Gem and the Space again -- a third copy of
    links the picker already carries twice. Three copies of a share URL is three
    places to leave a dead one when an agent is re-published. #publish answers
    "which blob", the picker answers "open one"."""
    rows = "".join(_publish_rows())
    dupes = re.findall(r'href="(https://(?:chatgpt|gemini|www\.perplexity)[^"]*)"', rows)
    assert not dupes, (
        f"#publish restates share links the picker already owns: {dupes}")
