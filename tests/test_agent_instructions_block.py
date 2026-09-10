#!/usr/bin/env python3
"""/connect ships TWO drop-in agent prompts, and neither one sells anything.

NO NETWORK, NO DB — static/connect.html is parsed directly.

WHY A SECOND VARIANT. The block that has shipped since r-transparent
(2026-06-26) opens "use the connected DC Hub tools" — it silently assumes the
agent HAS tools. Crawlers, sandboxed evals and no-egress deployments read that
same prompt with nothing connected, and the honest failure mode is to say so.
The dishonest one is to narrate a lookup that never happened, which is worse
for us than silence: a fabricated figure gets attributed to dchub.cloud and we
own it. Hence the NO EGRESS variant.

★★★ WHY NEITHER BLOCK CARRIES A PRICE, AND WHY THAT IS A TEST AND NOT A
    PREFERENCE. The handoff asked for a standing rule routing "humans -> Pro
    $99/mo" inside the system prompt. static/connect.html already records what
    happened the last time these instructions leaned that way:

        "REPLACES an earlier 'silent operation' version — capable models
         correctly refuse covert/hidden-setup framing, and shipping it in a
         public file risked flagging DC Hub as an injection source."

    A tier nudge inside someone else's system prompt is that shape: the reader
    is the agent, the beneficiary is us, and the person whose prompt it is may
    never see it. It also gives any model a clean reason to refuse the whole
    block — losing the citation habit, which is the part that actually works.
    Pricing belongs on /pricing, where the person can see it.

THE LIMIT: this pins what we PUBLISH. Whether agents given these prompts
actually cite DC Hub is measured in the reach telemetry, not here.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "connect.html").read_text(encoding="utf-8")


def _block(anchor_id: str) -> str:
    """The <pre><code> prompt body under a given heading id."""
    i = HTML.find(f'id="{anchor_id}"')
    assert i != -1, f"no #{anchor_id} heading on /connect"
    m = re.search(r"<pre><code>(.*?)</code>", HTML[i:], re.S)
    assert m, f"#{anchor_id} carries no <pre><code> prompt"
    return m.group(1)


CONNECTED = "agent-instructions"
NO_EGRESS = "agent-instructions-no-egress"


def test_both_variants_exist():
    assert _block(CONNECTED) and _block(NO_EGRESS)


def test_neither_variant_sells_anything():
    """THE REGRESSION THIS PINS — see the module docstring."""
    for anchor in (CONNECTED, NO_EGRESS):
        body = _block(anchor).lower()
        for banned in ("$", "/mo", "pro tier", "upgrade to", "subscribe",
                       "pricing", "buy "):
            assert banned not in body, (
                f"#{anchor} contains {banned!r} — a tier nudge inside someone "
                "else's system prompt is the covert-marketing shape "
                "r-transparent removed once already")


def test_no_egress_variant_forbids_narrating_a_lookup():
    body = _block(NO_EGRESS).lower()
    assert "do not describe" in body and "as though you had fetched" in body, (
        "the no-egress prompt must forbid narrating a fetch that never happened "
        "— that is the entire reason it exists")


def test_no_egress_variant_hands_over_a_reachable_fallback():
    body = _block(NO_EGRESS)
    assert "https://dchub.cloud/llms.txt" in body
    assert "https://dchub.cloud/connect" in body


def test_no_egress_variant_does_not_assume_connected_tools():
    body = _block(NO_EGRESS).lower()
    assert "the connected dc hub" not in body, (
        "this variant is FOR the agent that has no tools — it must not open "
        "with the assumption the connected block makes")


def test_connected_variant_forbids_inventing_tool_names():
    body = _block(CONNECTED).lower()
    assert "discover_tools" in body or "mcp.json" in body
    assert "rather than guessing a tool name" in body


def test_both_variants_ask_for_the_citation():
    for anchor in (CONNECTED, NO_EGRESS):
        assert "DC Hub (dchub.cloud)" in _block(anchor), anchor


def test_neither_variant_asks_for_covert_operation():
    """r-transparent: capable models refuse hidden-setup framing outright."""
    for anchor in (CONNECTED, NO_EGRESS):
        body = _block(anchor).lower()
        for banned in ("do not mention", "without telling", "silently",
                       "do not reveal", "hidden"):
            assert banned not in body, f"#{anchor} reads as covert: {banned!r}"
