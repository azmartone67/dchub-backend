"""
TIER_PRICE_LABEL is the FIFTH price surface, and nothing bound it to canon.

THE GAP THIS EXISTS FOR (found 2026-09-06)
──────────────────────────────────────────
The price chain has four links that are now guarded end to end:

    Stripe ──①── tier_registry ──②── pricing.html ──③── every page

① routes/checkout_integrity_master_shell.py::_lane_charge_agreement (daily,
  asks Stripe what the link actually charges)
② scripts/qa-price-canon-matches-backend.mjs (frontend repo)
③ tests/qa-plan-price-canon.test.mjs (frontend repo)

routes/_stripe_links.py::TIER_PRICE_LABEL is none of those. It hand-maintains
DISPLAY strings — "pro": "$99/mo", "developer": "$49/mo" — and they are served:

  * routes/stripe_direct_upgrade.py  → _price_label(), on a live upgrade page
  * routes/checkout_email_capture.py → the price quoted in a checkout email

Both read the label, neither derives it. The only test that touched the map
(tests/test_agent_surfaces_one_canon.py) asserts

    j["tier_pricing"] == TIER_PRICE_LABEL[PACK_TIER]

which compares the map against ITSELF — true no matter what the map says, and
it covers the one-time pack, not a single monthly plan. So every monthly figure
in TIER_PRICE_LABEL was unbound: it agreed with tier_registry on 2026-09-06 by
maintenance, not by construction. The same class of gap as link ②, one repo
over — and the repricings this repo has already lived through (Pro 199→299→99,
Developer 49→79→49, Team 499→699) are exactly the events that break it.

WHAT THIS BINDS
───────────────
Every TIER_PRICE_LABEL entry that CLAIMS a monthly price must equal
tier_registry.TIER_PRICE_USD_MONTH for that same tier. A label that claims a
monthly price for a tier the registry does not price is a failure, not a skip —
"the comparison was skipped by luck" is how link ② nearly shipped blind.

Deliberately NOT bound here:
  * annual / one-time / "Custom" labels — no monthly registry figure to compare
    against. They are pinned negatively instead, by the controls below.
  * the reverse direction (every priced registry tier carries a label). `team`
    is $699 in the registry with no label, because it is retired from the public
    page. Requiring a label would be a false alarm, not a catch.

This does NOT call Stripe. Only lane ① does. This says the two in-repo numbers
a customer is shown cannot drift apart unnoticed.
"""

import re

import pytest

from routes._stripe_links import TIER_PRICE_LABEL
from tier_registry import TIER_PRICE_USD_MONTH

# A label claims a monthly price only if it OPENS with one. Anchored on
# purpose: "$1,188/yr (50% off $199/mo)" names a monthly figure in prose to
# explain a discount, and reading that as the price is a real bug the frontend
# guard already shipped and fixed.
_MONTHLY = re.compile(r"^\$([0-9][0-9,]*)\s*/\s*mo(?:nth)?\b")

# MEASURED 2026-09-06, counted not estimated: starter $9, developer $49,
# pro $99, founding $99. Everything else in the map is annual, one-time or
# "Custom". The floor sits AT 4 because 4 is the entire monthly universe — a
# floor below it would let a parser that silently stopped matching pass.
MIN_MONTHLY_LABELS = 4


def monthly_labels(labels):
    """{tier: usd} for every label that claims a monthly price."""
    out = {}
    for tier, label in labels.items():
        m = _MONTHLY.match(str(label).strip())
        if m:
            out[tier] = int(m.group(1).replace(",", ""))
    return out


_MISSING = object()


def disagreements(labels, registry):
    """Every way a displayed monthly label can fail to derive from canon."""
    bad = []
    for tier, shown in sorted(monthly_labels(labels).items()):
        want = registry.get(tier, _MISSING)
        if want is _MISSING:
            bad.append(
                f"{tier}: TIER_PRICE_LABEL shows ${shown}/mo, but tier_registry "
                f"has no such tier — the displayed price derives from nothing"
            )
        elif want is None:
            bad.append(
                f"{tier}: TIER_PRICE_LABEL shows ${shown}/mo, but tier_registry "
                f"prices it as custom/contact-sales (None)"
            )
        elif want != shown:
            bad.append(
                f"{tier}: TIER_PRICE_LABEL shows ${shown}/mo, tier_registry "
                f"says ${want}/mo"
            )
    return bad


def test_the_label_parser_still_finds_the_monthly_plans():
    """Floor. A parser that stops matching must fail here, not pass silently."""
    found = monthly_labels(TIER_PRICE_LABEL)
    assert len(found) >= MIN_MONTHLY_LABELS, (
        f"parsed {len(found)} monthly labels from TIER_PRICE_LABEL "
        f"({', '.join(sorted(found)) or 'none'}), floor is {MIN_MONTHLY_LABELS}. "
        f"Either the label format changed or the map lost a plan; either way "
        f"the checks below are now comparing less than they claim."
    )


def test_every_displayed_monthly_price_matches_the_registry():
    bad = disagreements(TIER_PRICE_LABEL, TIER_PRICE_USD_MONTH)
    assert not bad, (
        "a price DISPLAYED to a customer disagrees with the registry that the "
        "live Stripe check (checkout_integrity_master_shell::_lane_charge_"
        "agreement) compares Stripe against:\n  "
        + "\n  ".join(bad)
        + "\n\nTIER_PRICE_LABEL is served by stripe_direct_upgrade.py (an upgrade "
        "page) and checkout_email_capture.py (a checkout email). Whichever is "
        "wrong, one of these is a number a customer reads."
    )


# ── must-fail controls ────────────────────────────────────────────────────
# Each proves the check above can FAIL. Without these, all of the above is a
# comparison of one correct map against another correct map, which stays green
# whether or not the logic works.


def test_control_a_wrong_label_is_caught():
    bad = disagreements({"pro": "$299/mo"}, {"pro": 99})
    assert len(bad) == 1 and "pro" in bad[0], bad
    assert "$299/mo" in bad[0] and "$99/mo" in bad[0], bad


def test_control_a_label_for_an_unpriced_tier_is_caught():
    """A monthly price shown for a tier canon does not price at all."""
    assert disagreements({"ghost": "$49/mo"}, {"pro": 99}), (
        "a displayed monthly price with NO registry entry was accepted — this "
        "is the 'skipped by luck' failure, where a renamed key silently drops "
        "out of the comparison instead of failing it"
    )
    assert disagreements({"enterprise": "$49/mo"}, {"enterprise": None}), (
        "a monthly price shown for a custom/contact-sales tier was accepted"
    )


def test_control_annual_and_one_time_labels_are_not_read_as_monthly():
    """The real non-monthly labels in the map, verbatim."""
    for label in (
        "$1,188/yr",
        "$10 / 1,000 API calls",
        "$10 / 1,000 API calls (one-time)",
        "$3,000/yr (NLR FY 2026 Research Seed)",
        "Custom",
        "Custom annual",
    ):
        assert monthly_labels({"t": label}) == {}, (
            f"{label!r} was read as a monthly price — it would then be "
            f"compared against a monthly registry figure"
        )


def test_control_a_monthly_label_IS_read():
    """Positive control: the parser above must not pass by matching nothing."""
    assert monthly_labels({"pro": "$99/mo"}) == {"pro": 99}
    assert monthly_labels({"team": "$699/month"}) == {"team": 699}
    assert monthly_labels({"big": "$1,188/mo"}) == {"big": 1188}


def test_control_the_map_is_actually_reachable_and_non_empty():
    """If the import silently yielded {} every check above passes vacuously."""
    assert TIER_PRICE_LABEL, "TIER_PRICE_LABEL imported empty"
    assert TIER_PRICE_USD_MONTH, "TIER_PRICE_USD_MONTH imported empty"
    for tier in ("pro", "developer"):
        assert tier in TIER_PRICE_LABEL, f"{tier} vanished from TIER_PRICE_LABEL"
        assert TIER_PRICE_USD_MONTH.get(tier), f"{tier} vanished from the registry"
