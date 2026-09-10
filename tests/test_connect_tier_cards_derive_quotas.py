"""/connect's tier cards must derive their quotas, and name the lane they quote.

MEASURED LIVE 2026-09-10 on https://dchub.cloud/connect:

    Free        10        calls/day     canon mcp_daily 10        correct
    Pro         10,000    calls/day     canon mcp_daily 2,000     5x OVER-claim
    Enterprise  100,000   calls/day     canon mcp_daily 100,000   correct

Three hand-typed numbers, two of them right, which is the worst version of this
bug: the card that was wrong sat between two that were correct, so nothing about
the block looked stale. It is the page that tells AI agents what they get, and
it promised five times the quota the gate enforces.

★ WHY THE LABEL IS PART OF THE CONTRACT. Pro's lanes DISAGREE -- mcp_daily is
2,000 and the REST rate_limit is 5,000 -- which is exactly why ai_surface_canon
deliberately publishes no {canon_pro_calls}, on the same rule that refuses
{canon_developer_calls}: a surface must not silently pick a winner between two
true numbers. The resolution is not to omit the number, it is to SAY WHICH ONE.
So the placeholder is {canon_pro_mcp_calls} and the card must read "MCP
calls/day". A future edit that shortens that back to a bare "calls/day"
re-creates the ambiguity while leaving the digits right, so the label is
asserted here alongside the value.

Run:  python3 -m pytest tests/test_connect_tier_cards_derive_quotas.py -v
"""
from __future__ import annotations

import os
import re

import pytest

import ai_surface_canon as asc
import tier_registry

_HERE = os.path.dirname(__file__)
_CONNECT = os.path.join(_HERE, "..", "static", "connect.html")

# The quota this page has advertised for Pro and must never advertise again.
_RETIRED_PRO_QUOTA = "10,000"


def _html() -> str:
    with open(_CONNECT, encoding="utf-8") as f:
        return f.read()


def _card(tier: str) -> str:
    html = _html()
    i = html.index(f'class="tier-card {tier}"')
    return html[i:html.index("</div>", html.index("tier-desc", i))]


def _rendered(tier: str) -> str:
    return asc.canon_text(_card(tier))


# ── FLOOR: the block must exist, or every "X is absent" below is vacuous.
def test_the_tier_grid_is_here():
    html = _html()
    assert 'class="tier-grid"' in html
    for tier in ("free", "pro", "enterprise"):
        assert f'class="tier-card {tier}"' in html, f"{tier} card is gone"


# ── The value is derived, not typed ──────────────────────────────────────
def test_pro_card_is_a_placeholder_not_a_literal():
    """The point is not that the number reads 2,000 today -- it is that no digit
    was typed. A literal that happens to be correct is the bug one reprice away."""
    assert "{canon_pro_mcp_calls}" in _card("pro"), (
        "/connect's Pro tier card hand-types its quota again; derive it from "
        "tier_registry via {canon_pro_mcp_calls}")


def test_free_card_is_a_placeholder_not_a_literal():
    assert "{canon_free_calls}" in _card("free")


def test_pro_card_renders_the_registry_quota():
    want = f"{tier_registry.calls_per_day('pro'):,}"
    assert want in _rendered("pro"), (
        f"Pro card renders {_rendered('pro')!r}, which does not carry the "
        f"canonical MCP quota {want}")


def test_the_retired_over_claim_cannot_return():
    rendered = _rendered("pro")
    if _RETIRED_PRO_QUOTA == f"{tier_registry.calls_per_day('pro'):,}":
        pytest.skip("10,000 is the canonical quota again; not drift")
    assert _RETIRED_PRO_QUOTA not in rendered, (
        f"/connect advertises the retired Pro quota {_RETIRED_PRO_QUOTA} — it "
        "promised 5x what the gate enforces")


# ── The label names the lane ─────────────────────────────────────────────
@pytest.mark.parametrize("tier", ["free", "pro"])
def test_the_card_says_which_lane_it_quotes(tier):
    """A bare 'calls/day' beside a number picked from two disagreeing lanes is
    the ambiguity ai_surface_canon refuses to publish. Saying 'MCP' is what
    earns the number the right to be on the page."""
    desc = _rendered(tier)
    assert "MCP calls/day" in desc, (
        f"the {tier} card reads a bare 'calls/day'; Pro's MCP and REST quotas "
        "differ, so the lane has to be named")


def test_no_placeholder_survives_into_the_rendered_cards():
    """The failure canon_text calls worse than a stale number: shipping a literal
    '{canon_...}' to an agent reading the tier table."""
    for tier in ("free", "pro", "enterprise"):
        leaked = re.findall(r"\{canon_[a-z_]+\}", _rendered(tier))
        assert not leaked, f"{tier} card shipped raw placeholders: {leaked}"


def test_no_placeholder_survives_anywhere_ON_THE_WHOLE_PAGE():
    """★ SCOPED TO THE PAGE, not the cards, because the card-scoped test above
    missed a real one. Writing this change, a canon token was typed into an HTML
    COMMENT while explaining which placeholder deliberately does not exist -- and
    an HTML comment is served bytes. The card assertions were all green; only
    rendering the whole file found it.

    A comment is not a safe place to name a placeholder, and 'it is only in a
    comment' is the reasoning that ships one."""
    rendered = asc.canon_text(_html())
    leaked = sorted(set(re.findall(r"\{canon_[a-z_]+\}", rendered)))
    assert not leaked, (
        f"/connect ships unresolved canon tokens: {leaked}. If one is inside a "
        "comment, it is still in the response an agent reads — describe the "
        "placeholder in prose instead of writing the token.")
