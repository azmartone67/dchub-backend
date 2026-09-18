"""
Every price in a market-page CTA must BE canon — not merely sit next to canon.

WHAT THIS EXISTS FOR (2026-09-17)
─────────────────────────────────
/markets/<slug> — 253 published pages, served by routes/market_deep_dive.py —
carried a CTA that quoted exactly one price, the Developer monthly rate, and
neither the Pro plan nor the one-time pack. The approved offer reached no
market page. Fixing that put a THIRD price surface on those pages, so it needs
binding to the producers the same way links ①-③ of the price chain are:

    Stripe ──①── tier_registry ──②── pricing.html ──③── every page

① routes/checkout_integrity_master_shell.py::_lane_charge_agreement
② scripts/qa-price-canon-matches-backend.mjs   (frontend repo)
③ tests/qa-plan-price-canon.test.mjs           (frontend repo)

THE MUTATION THAT TAUGHT THIS ITS SHAPE
───────────────────────────────────────
The sibling guard in the frontend repo (tests/qa-market-cta-canon.test.mjs)
first asserted that the canon price was PRESENT in the block. A mutation that
repriced Pro at $199 SURVIVED it, because the canon price was still present —
in the button underneath. Presence is not absence.

So this asserts the complement: EVERY monthly figure the block emits IS canon,
and every one-time figure IS the pack. test_no_canon_price_elsewhere_excuses_a
_wrong_one is the control that keeps it that way — it plants a canon price in
the same page and a wrong one in the block, and demands a failure.

THREE PAINTERS, NOT ONE
───────────────────────
market_short_html, _render_deep_dive_body and _render_neutral_market_page all
serve /markets/<slug>. The first two are the ones a healthy page hits; the
third and the shell fire exactly when the market's data DEGRADES — which is
when a missing offer would be least noticed. A guard that checked only the
happy painter would pass while the offer silently vanished from every degraded
page, so every painter is asserted here by name.

DELIBERATELY NOT BOUND HERE
───────────────────────────
* Facility pages (22,100+). Out of scope by owner decision; the assertion that
  they stay out lives in test_agent_handoff_offer_is_market_only below, which
  fails if a future edit widens _OFFER_KINDS without a deliberate change here.
* The Developer and Enterprise tiers. The block does not quote them, and a
  guard that demanded them would block a copy change nobody asked for.
* competitive.html / database.html (frontend repo) still sell the retired
  Starter tier as live copy. That is an owner decision, logged, not an
  engineer's drive-by — and not reachable from this repo.
"""
import re

import pytest

from tier_registry import TIER_PRICE_USD_MONTH
from routes.mcp_conversion_plays import PACK10_PRICE_CENTS, PACK10_CREDITS
from routes.market_deep_dive import _market_offer_html, _offer_num
from routes.seo_agent_alternates import _agent_handoff_html, _OFFER_KINDS

# Canon, read from the producers. Never typed.
PRO = float(TIER_PRICE_USD_MONTH["pro"])
PACK = PACK10_PRICE_CENTS / 100.0

# Retired by r-price-collapse 2026-09-05. Read by name so a live copy string
# can be checked against it without the registry key being treated as canon.
RETIRED_STARTER = float(TIER_PRICE_USD_MONTH.get("starter") or 0)

MONTHLY = re.compile(r"\$([0-9][0-9,]*(?:\.[0-9]{2})?)\s*/\s*mo")
ANY_DOLLAR = re.compile(r"\$([0-9][0-9,]*(?:\.[0-9]{2})?)")


def _num(s):
    return float(s.replace(",", ""))


def _blocks():
    """One rendered CTA per painter-shaped call, labelled."""
    return {
        "deep-dive (real facts)": _market_offer_html(
            "austin", "Austin", ref="market-deep-dive",
            facility_count=47, total_mw=1240.4, dcpi=62),
        "deep-dive (other market)": _market_offer_html(
            "ashburn", "Ashburn", ref="market-deep-dive",
            facility_count=131, total_mw=3890, dcpi=94),
        "brief-guard (no counts by design)": _market_offer_html(
            "reno", "Reno", ref="market-brief-guard"),
        "shell (em-dash placeholders)": _market_offer_html(
            "akron", "Akron", ref="market",
            facility_count="—", total_mw="—"),
    }


@pytest.mark.parametrize("label", list(_blocks()))
def test_every_monthly_price_in_the_block_is_canon(label):
    """Not 'canon appears' — every /mo figure emitted IS the Pro price."""
    block = _blocks()[label]
    found = [_num(m) for m in MONTHLY.findall(block)]
    assert found, f"{label}: block quotes no monthly price at all"
    for v in found:
        assert v == PRO, (
            f"{label}: block quotes ${v:g}/mo; canon Pro is ${PRO:g}/mo "
            f"(tier_registry.TIER_PRICE_USD_MONTH['pro'])")


@pytest.mark.parametrize("label", list(_blocks()))
def test_every_dollar_figure_is_canon_pro_or_canon_pack(label):
    """The block emits exactly two kinds of number. Anything else is drift."""
    block = _blocks()[label]
    for v in (_num(m) for m in ANY_DOLLAR.findall(block)):
        assert v in (PRO, PACK), (
            f"{label}: block quotes ${v:g}, which is neither canon Pro "
            f"(${PRO:g}) nor the canon pack (${PACK:g})")


@pytest.mark.parametrize("label", list(_blocks()))
def test_block_quotes_the_pack_and_its_real_credit_count(label):
    block = _blocks()[label]
    pack_s = ("$%d" % PACK) if float(PACK).is_integer() else ("$%.2f" % PACK)
    assert pack_s in block, f"{label}: pack price {pack_s} missing"
    assert format(int(PACK10_CREDITS), ",") in block, (
        f"{label}: pack credit count {PACK10_CREDITS:,} missing — a pack "
        f"price with no call count is not an offer")


@pytest.mark.parametrize("label", list(_blocks()))
def test_block_carries_both_approved_destinations(label):
    block = _blocks()[label]
    for url in ("https://dchub.cloud/pricing", "https://dchub.cloud/connect"):
        assert url in block, f"{label}: {url} missing from the CTA"


@pytest.mark.parametrize("label", list(_blocks()))
def test_retired_starter_tier_never_reaches_a_market_page(label):
    block = _blocks()[label]
    if not RETIRED_STARTER:
        pytest.skip("tier_registry no longer carries a starter price")
    for v in (_num(m) for m in ANY_DOLLAR.findall(block)):
        assert v != RETIRED_STARTER, (
            f"{label}: block quotes ${v:g}, the Starter tier retired by "
            f"r-price-collapse on 2026-09-05")


def test_no_canon_price_elsewhere_excuses_a_wrong_one():
    """THE CONTROL. The frontend sibling shipped blind without this.

    A page carrying canon Pro in one place and a wrong price in the CTA must
    FAIL. If this test ever passes by finding canon somewhere on the page,
    the assertions above have regressed to presence checks.
    """
    good = _market_offer_html("austin", "Austin", ref="market-deep-dive",
                              facility_count=47)
    wrong = float(PRO) + 100.0
    page = (f'<p>Plans start at ${PRO:g}/mo.</p>'
            + good.replace(f"${PRO:g}/mo", f"${wrong:g}/mo"))

    assert f"${PRO:g}/mo" in page, "control is malformed: canon absent"
    monthly = [_num(m) for m in MONTHLY.findall(page)]
    assert wrong in monthly, "control is malformed: the wrong price is absent"
    assert not all(v == PRO for v in monthly), (
        "a wrong price went undetected on a page that also carries canon — "
        "the block assertions have decayed into presence checks")


def test_copy_varies_by_market_not_stamped_boilerplate():
    """Approval was conditioned on variation being real. Two markets with
    different measured facts must not render the same bytes."""
    b = _blocks()
    a, c = b["deep-dive (real facts)"], b["deep-dive (other market)"]
    assert a != c, "two markets rendered identical CTA bytes"
    for fact in ("47 tracked facilities", "1,240 MW", "DCPI 62/100"):
        assert fact in a, f"measured fact missing from the copy: {fact}"
    assert "131 tracked facilities" in c


def test_unknown_counts_are_dropped_not_printed_as_zero():
    """A painter with no counts must say less, never claim zero."""
    for v in (None, "—", "", "n/a", 0, -3, float("nan"), True, False):
        assert _offer_num(v) is None, f"_offer_num({v!r}) should be None"
    assert _offer_num("1,240") == 1240.0
    assert _offer_num(47) == 47.0

    degraded = _blocks()["shell (em-dash placeholders)"]
    for bad in ("0 tracked", "0 MW", "DCPI 0/100", "— tracked"):
        assert bad not in degraded, f"degraded page fabricated: {bad}"


def test_singular_plural_is_not_a_grammar_bug_on_one_facility():
    one = _market_offer_html("provo", "Provo", ref="market", facility_count=1)
    assert "1 tracked facility" in one and "facilities" not in one


def test_market_name_is_escaped():
    out = _market_offer_html("x", 'Bad <script>"', ref="market")
    assert "<script>" not in out and "&lt;script&gt;" in out


# ── the offer must reach EVERY painter of /markets/<slug> ────────────────
# These three functions all serve that path. Asserting the builder is called
# from each one is what stops the offer from silently dropping off the two
# that fire when a market's data degrades.
def test_all_three_market_painters_call_the_one_builder():
    import inspect
    from routes import market_deep_dive as m

    for fn_name in ("_render_deep_dive_body", "_render_neutral_market_page",
                    "market_short_html"):
        srcfn = inspect.getsource(getattr(m, fn_name))
        assert "_market_offer_html(" in srcfn, (
            f"{fn_name} serves /markets/<slug> and emits no offer — the CTA "
            f"would vanish on every page this painter answers")


def test_no_painter_kept_a_hand_written_price():
    """A second, unbound price string in these renderers is the drift this
    change removed. The builder is the only place a price may appear."""
    import inspect
    from routes import market_deep_dive as m

    for fn_name in ("_render_deep_dive_body", "_render_neutral_market_page",
                    "market_short_html"):
        srcfn = inspect.getsource(getattr(m, fn_name))
        assert not ANY_DOLLAR.search(srcfn), (
            f"{fn_name} contains a hand-written dollar figure; prices must "
            f"come from _market_offer_html, which derives them")


# ── facility pages stay untouched ────────────────────────────────────────
def test_agent_handoff_offer_is_market_only():
    """Owner decision: the offer goes in the market tree's handoff line and
    nowhere else. This fails if _OFFER_KINDS is widened."""
    assert tuple(_OFFER_KINDS) == ("market",), (
        f"_OFFER_KINDS is {tuple(_OFFER_KINDS)}; widening it puts the offer "
        f"on page kinds the owner ruled out (facility pages: 22,100+)")

    market = _agent_handoff_html("market", "austin")
    assert "https://dchub.cloud/pricing" in market
    assert "https://dchub.cloud/connect" in market
    for v in (_num(x) for x in MONTHLY.findall(market)):
        assert v == PRO, f"handoff quotes ${v:g}/mo, canon is ${PRO:g}/mo"

    for kind in ("facility", "grid", "deals", "capacity"):
        line = _agent_handoff_html(kind, "some-slug")
        assert "/pricing" not in line and "/connect" not in line, (
            f"{kind} handoff carries an offer; only the market tree may")
        assert not ANY_DOLLAR.search(line), f"{kind} handoff quotes a price"
