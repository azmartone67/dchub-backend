"""r-filter-boundary (2026-09-04) — "91.6% lost" was not a loss.

The public /ai handoff card printed, as its headline conclusion:

    Biggest leak: Paywall hit → Relay minted —
    357 reached paywall hit, 30 reached relay minted (91.6% lost).

A rung of LEAK_LADDER divides two stage counts. That is a conversion rate only
when both stages are drawn from the SAME population. These two are not:

  paywall_hit    mcp_upgrade_signals, written by server.mjs signalPaywall() —
                 posts UNCONDITIONALLY, no bot gate at all
  relay_minted   mcp_high_intent_sessions, written by trackPaidHit() — gated
                 TWICE: isBotOrInternalCtx() client-side AND
                 _is_non_human_client() server-side, the latter answering
                 HTTP 200 with {"skipped": ...} while the caller only logs on
                 !resp.ok, so a deliberate drop leaves no trace

Both gates deliberately exclude internal tags, QA harnesses and raw scripting
UAs — of 123 claims minted in a 30-day audit, ~93 were raw scripts with no
human to click. So most of that rung's "loss" is traffic correctly filtered
out of the second stage while still counted in the first.

★ The rung is still REPORTED. Suppressing the largest arithmetic drop would
hide a real number and is the opposite failure. It now carries
same_population=false and the reason, so a reader — or a partner quoting the
card — cannot mistake it for 91.6% of prospects giving up.

WHAT THIS GUARD PINS:
  · the boundary rung declares itself, and the inner rungs stay clean;
  · the declaration is DERIVED from the ladder, not restated per call site;
  · a rung's percentage and its comparability travel together in one dict.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from routes.handoff_definition import (  # noqa: E402
    LEAK_LADDER,
    biggest_leak,
    biggest_leak_detail,
)

_LIVE = {"paywall_hit": 357, "high_intent": 32, "relay_minted": 30,
         "human_acted": 0, "identified": 0, "paid_attributed": 0, "redeemed": 0}


def test_the_paywall_rung_declares_it_is_not_a_conversion_rate():
    d = biggest_leak_detail(_LIVE)
    assert d["label"] == "paywall→relay_mint", "the live shape picks this rung"
    assert d["lost_pct"] == 91.6, "the number is still published, not suppressed"
    assert d["same_population"] is False, (
        "the rung that spans the bot-gate boundary is being published as a "
        "plain conversion rate — this is the line the /ai card headlines")
    basis = d["population_basis"] or ""
    assert "NOT a conversion rate" in basis
    for token in ("signalPaywall", "trackPaidHit", "skipped"):
        assert token in basis, (
            "the basis must name the mechanism (%s) so a reader can check it "
            "rather than take it on faith" % token)


def test_inner_rungs_are_comparable_and_say_so():
    """relay_minted → human_acted → identified → paid all sit inside
    mcp_high_intent_sessions, so their percentages ARE conversion rates and
    must not be hedged."""
    inner = {"paywall_hit": 100, "relay_minted": 90, "human_acted": 0,
             "identified": 0, "paid_attributed": 0}
    d = biggest_leak_detail(inner)
    assert d["label"] == "relay_mint→human_acted"
    assert d["same_population"] is True
    assert d["population_basis"] is None, (
        "an inner rung must not carry a boundary caveat — hedging a real "
        "conversion rate is the opposite error and hides a real leak")


def test_every_rung_declares_its_comparability():
    """No rung may be silent about it: a missing declaration reads as
    'comparable' to any consumer that checks."""
    assert all(len(r) == 4 for r in LEAK_LADDER), (
        "a LEAK_LADDER rung has no population slot — add one rather than "
        "letting it default to comparable")
    for src, dst, label, boundary in LEAK_LADDER:
        steps = {src: 100, dst: 1}
        d = biggest_leak_detail(steps)
        assert "same_population" in d and "population_basis" in d
        assert isinstance(d["same_population"], bool)


def test_exactly_one_rung_is_a_boundary_and_it_is_the_paywall_one():
    flagged = [(s, dd) for s, dd, _l, b in LEAK_LADDER if b is not None]
    assert flagged == [("paywall_hit", "relay_minted")], (
        "the population boundary is between the ungated paywall signal and "
        "the double-gated high-intent table; flagging others hides real "
        "leaks, flagging none restores the false headline. Got: %r" % flagged)


def test_label_and_detail_stay_one_writer():
    """biggest_leak() must keep deriving from biggest_leak_detail(); two walks
    of one ladder are two writers of one definition."""
    assert biggest_leak(_LIVE) == biggest_leak_detail(_LIVE)["label"]


def test_a_zero_upstream_rung_is_not_reported_as_a_loss():
    """Unchanged behaviour, re-pinned: 0 -> 0 is not '100% lost'."""
    d = biggest_leak_detail({"paywall_hit": 0, "relay_minted": 0,
                             "human_acted": 0, "identified": 0,
                             "paid_attributed": 0})
    assert d["lost_pct"] is None
