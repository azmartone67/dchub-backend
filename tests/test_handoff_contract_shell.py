"""Handoff Contract Shell #43 (2026-07-29) — lane guards.

The finding this shell exists for is a token with two owners. Two endpoints
consume the same SINGLE-USE claim:

    POST /claim/<token>                    the human email form
    POST /api/v1/mcp/high-intent/redeem    "binds the trial key with NO human
                                            page-open"  ← ours, gateway-called

The second wins every race because it is automatic and sub-second (median
0.85s), and once it fires the human URL returns 410 Gone. So the handoff funnel
measures something our own architecture forecloses.

I first called that "arbitrage", implying agents were gaming us. They are not —
we built the bypass deliberately. The tests below therefore guard the CONTRACT
framing, not a blame framing, and they guard the two remedies that would be
wrong: adding friction (which already failed at 0.08% conversion) and
duplicating a check that another shell already owns.

Run:  python3 -m pytest tests/test_handoff_contract_shell.py -v
"""
from __future__ import annotations

import inspect

from routes import handoff_contract_shell as hcs


def test_both_token_consumers_are_named():
    """The defect is only legible if both owners of the token are written down.
    A reader who sees only the human form concludes the link is unpersuasive."""
    assert "claim/<token>" in hcs.HUMAN_CONSUMER
    assert "high-intent/redeem" in hcs.AGENT_CONSUMER
    src = inspect.getsource(hcs)
    assert "NO human page-open" in src, (
        "the agent endpoint's own stated contract is no longer quoted — that quote "
        "is the evidence the bypass is intentional rather than abuse")


def test_the_machine_threshold_is_imported_not_restated():
    """One threshold, one origin. Restating 5s here would let the two modules
    disagree silently — the defect class that put a 404 in the Glama writer."""
    src = inspect.getsource(hcs)
    assert "from routes.relay_conversion_watch import MACHINE_REDEEM_SECONDS" in src, (
        "the redemption threshold is no longer imported from the watch that "
        "measures it")
    assert isinstance(hcs.MACHINE_REDEEM_SECONDS, (int, float))


def test_lane_two_does_not_prescribe_friction():
    """Auto-redeem exists because friction already failed: 7,839 paywall signals
    produced 6 conversions and agents bounced. A shell that quietly recommends
    re-adding a wall would trade a measurable handoff for measurable adoption."""
    src = inspect.getsource(hcs._lane_token_contract)
    assert "7,839" in src, "the evidence against friction is no longer recorded"
    assert "TWO ARTIFACTS" in src or "two artifacts" in src.lower(), (
        "the two-artifact remedy is gone — without it the obvious reading of this "
        "lane is 'add a wall'")


def test_lane_one_reads_the_served_page_not_a_local_file():
    """My first version opened ~/dchub-frontend/ai.html from disk: the backend
    host has no frontend checkout, so the lane would be permanently
    INDETERMINATE in production, and locally it read a stale pre-merge copy and
    reported FAILED for a fix that had already shipped."""
    src = inspect.getsource(hcs._lane_dashboard_honesty)
    # Assert the BEHAVIOUR, not a mention. The first version of this test
    # searched for the path string and tripped on the docstring that explains the
    # old bug — flagging the explanation as the bug. Grep-for-a-string is the
    # weakest form of guard and it fails in exactly this way.
    body = src.split('"""')[2] if src.count('"""') >= 2 else src
    assert "requests.get" in body, "lane 1 no longer fetches the live page"
    assert "open(" not in body, (
        "lane 1 reads a file again — wrong surface, and invisible in production "
        "where the backend host has no frontend checkout")
    assert "worstDrop(" in body, "lane 1 no longer checks for the derived cliff"


def test_published_figures_are_checked_in_both_directions():
    """Checking only upward misses what actually happened: canon moved to 15,000+
    mid-session, stranding both the homepage and the registry copy BELOW it."""
    src = inspect.getsource(hcs._lane_published_claims)
    assert "over" in src and "under" in src, "one drift direction is unchecked"
    assert "_canonical_numbers" in src, "canon is hardcoded again instead of read"
    # Only over-claiming is credibility-critical; under-claiming must not be.
    assert "critical=bool(over)" in src, "the critical direction changed"
    assert "critical=False" in src, "under-claiming became critical — it is not"


def test_rank_claim_is_not_duplicated_here():
    """Shell #42 owns it. A second copy of one check is exactly how the Glama
    reader and writer drifted into a 404."""
    src = inspect.getsource(hcs._lane_published_claims)
    assert "shell #42" in src, "the single-owner note for the rank claim is gone"
    assert "UNVERIFIED" in src


def test_unreadable_is_never_a_pass():
    assert hcs._lane_verdict([hcs._check("x", "n", None, "unreadable")]) == "INDETERMINATE"
    assert hcs._lane_verdict([]) == "INDETERMINATE"
    assert hcs._lane_verdict([hcs._check("a", "n", True, "ok"),
                              hcs._check("b", "n", None, "?")]) == "INDETERMINATE"


def test_three_lanes_in_order():
    assert [k for k, _, _ in hcs.LANES] == [
        "dashboard_honesty", "token_contract", "published_claims"]
