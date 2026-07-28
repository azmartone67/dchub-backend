"""anchor_intents.py — THE canonical anchor intents (2026-07-28).

These six strings are published verbatim on 13 /for/<platform> pages, llms.txt,
llms-full.txt, AGENTS.md, all six /integrations pages, and in the starter pack
every connecting agent receives on its first tool call. Agents copy them
literally. They are not documentation — they are a public API written in
English, and Perplexity named the consequence exactly:

    "your English-language examples have become operational contracts —
     every example line now needs the same change control you'd apply to
     a public API."

★★ WHY THIS FILE EXISTS. Until now the list lived in THREE independent
transcriptions — dchub-frontend/scripts/heal-front-door.mjs, this repo's
_FRONT_DOOR_HTML, and dchub-mcp-server's _STARTER_PACK — and they had already
diverged: the gateway carried five of the six, so "which ISO has the shortest
time-to-power right now" was published on every page but never reached a single
agent. Nothing detected that, because each copy was internally consistent.

★ The fix is NOT a fourth copy with a fourth test. Perplexity, when I was
tempted: "I would not add a third independently maintained copy; that would
recreate the same problem one layer up." So this module is the single
publication point and the other surfaces DERIVE from it:

    routes/anchor_intents.py                    <- canonical (here)
      ├─ _FRONT_DOOR_HTML (integrations_landing) renders from ANCHORS
      ├─ /.well-known/mcp.json                   publishes `anchor_intents`
      └─ dchub-frontend heal-front-door.mjs      FETCHES the manifest,
                                                 fail-closed to its baked copy

★ SEMANTIC OWNERSHIP STAYS IN THE GATEWAY. Whether an intent actually routes to
its declared recipe can only be tested by the planner, which lives in
dchub-mcp-server (test/anchor-contract.test.mjs). This file owns PUBLICATION;
that test owns MEANING. Splitting them is deliberate — the alternative was
publishing from a repo that cannot verify the claim it publishes.

CHANGE CONTROL: editing an intent string here changes a published contract.
Bump nothing, but expect the gateway's anchor-contract test to fail if the new
wording no longer routes to the declared recipe — that failure is the feature.
"""
from __future__ import annotations

import hashlib
import json

# recipe → the MCP prompt/slash-command the intent corresponds to.
# intent → the exact string an agent should pass through as `execute_plan(intent=...)`.
# Order is the real decision flow (Perplexity): broad screening → grid reality
# check → comparison → site verification → cross-domain overlap → timing.
ANCHORS = (
    {"recipe": "market_selection",
     "intent": "rank markets for a 200 MW AI campus"},
    {"recipe": "grid_and_queue",
     "intent": "how much power is available in ERCOT for a 100 MW data center"},
    {"recipe": "compare_markets",
     "intent": "compare Dallas vs Phoenix for a GPU training cluster"},
    {"recipe": "site_analysis",
     "intent": "find 100 MW of buildable capacity near Ashburn"},
    {"recipe": "fiber_power_pairing",
     "intent": "where do fiber density and grid headroom overlap in Atlanta"},
    {"recipe": "grid_and_queue",
     "intent": "which ISO has the shortest time-to-power right now"},
)

INTENTS = tuple(a["intent"] for a in ANCHORS)


def contract_hash() -> str:
    """Stable hash of the published contract (Perplexity, 07-28).

    Lets a consumer detect drift without diffing six strings: fetch the payload,
    compare the hash to the one it last derived from, re-derive only on change.
    Hashes the ORDERED (recipe, intent) pairs — order is part of the contract,
    because the sequence is the decision flow agents are meant to follow.
    """
    canon = json.dumps([[a["recipe"], a["intent"]] for a in ANCHORS],
                       separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def anchor_payload() -> dict:
    """The shape published in /.well-known/mcp.json for downstream consumers.

    `source` is not decoration: a consumer that finds this list stale needs to
    know which file to edit, and the whole point of this module is that there is
    exactly one answer.
    """
    return {
        "contract_hash": contract_hash(),
        "count": len(ANCHORS),
        "call": "execute_plan",
        "param": "intent",
        "note": ("Each intent is one execute_plan call. Pass it through unchanged. "
                 "These are the canonical published examples — the same strings "
                 "rendered on /for/*, llms.txt, AGENTS.md and /integrations/*."),
        "source": "dchub-backend routes/anchor_intents.py",
        "anchors": [dict(a) for a in ANCHORS],
    }


def render_anchor_list_html() -> str:
    """The <li> list used inside _FRONT_DOOR_HTML.

    Recipe name first, question second — Perplexity, 2026-07-28: a client that
    truncates a long instruction block clips the TAIL, so whatever leads the
    line is what survives. Leading with the question taught the paraphrase and
    lost the action.
    """
    from html import escape as _esc
    return "\n".join(
        f'    <li><b>{_esc(a["recipe"])}</b> &mdash; '
        f'<code>execute_plan(intent="{_esc(a["intent"])}")</code></li>'
        for a in ANCHORS
    )
