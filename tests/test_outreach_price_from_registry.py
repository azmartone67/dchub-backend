"""The abandoned-checkout mail quoted $199/mo for a $99 product.

Three hand-typed dicts, keyed on three tiers, in a module that receives
whatever tier the checkout resolver produced -- and tier_registry defines
twelve. Measured against the registry that /pricing and checkout read:

    price   pro "$199/mo"         -> price("pro") == 99      quoted DOUBLE
    caps    100 / 1,000 / 10,000  -> 200 / 500 / 2,000       all three wrong
    .get(tier, "1,000")           -> a fabricated daily quota for the other
                                     nine tiers, whose subject also rendered
                                     "Finish your DC Hub X upgrade — —"

The mail's only job is to close a stalled checkout, and it closed it on terms
the product does not honour.

★ THE TESTS BELOW ARE PARAMETRISED OVER tier_registry, NOT OVER A LIST TYPED
HERE. A guard that hard-codes the prices it checks is the same defect as the
code it guards -- it goes green while both copies drift together. Adding a
tier to the registry automatically extends this suite; it cannot be forgotten.
"""
import ast
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import tier_registry as tr  # noqa: E402
from routes import outreach_cron as oc  # noqa: E402

_MOD = os.path.join(_ROOT, "routes", "outreach_cron.py")

# Every tier the registry actually prices -- discovered, never typed.
PRICED = [t for t in tr.TIERS
          if (lambda v: bool(v))(tr.price(t) or 0) and tr.pricing_copy(t)]


def _lead(tier, tool="analyze_site"):
    return {"id": 1, "email": "lead@example.com", "tool": tool, "tier": tier}


def test_the_registry_prices_something():
    """Floor: if PRICED were empty every parametrised test below would vacuously
    pass and this file would guard nothing."""
    assert len(PRICED) >= 3, PRICED


# ── the price ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("tier", PRICED)
def test_subject_price_equals_the_registry_price(tier):
    subject, html, text = oc._build_email(_lead(tier))
    want = "$%s/mo" % tr.price(tier)
    assert want in subject, f"{tier}: subject {subject!r} does not carry {want}"
    assert want in html and want in text


@pytest.mark.parametrize("tier", PRICED)
def test_no_other_price_appears_anywhere_in_the_mail(tier):
    """★ The actual failure: a SECOND, stale price rendered alongside the real
    one. Any $N/mo in the body must be THE registry price for this tier."""
    _, html, text = oc._build_email(_lead(tier))
    want = str(tr.price(tier))
    for blob in (html, text):
        for found in re.findall(r"\$\s?([0-9][0-9,]*)\s*/\s*mo", blob):
            assert found.replace(",", "") == want, (
                f"{tier}: mail quotes ${found}/mo but the registry says ${want}")


def test_pro_specifically_is_not_199():
    """The regression that shipped. Named so a diff cannot lose it in a loop."""
    subject, _, _ = oc._build_email(_lead("pro"))
    assert "$199" not in subject, subject
    assert "$%s/mo" % tr.price("pro") in subject


# ── the fabricated quota ──────────────────────────────────────────────

@pytest.mark.parametrize("tier", PRICED)
def test_bullets_come_from_the_registry(tier):
    _, html, _ = oc._build_email(_lead(tier))
    items = re.findall(r"<li>(.*?)</li>", html, re.S)
    for bullet in tr.pricing_copy(tier):
        assert bullet in items, f"{tier}: registry bullet {bullet!r} missing"


def test_an_unpriced_tier_is_not_mailed_at_all():
    """It used to render 'upgrade — —' and invent '1,000 calls/day'."""
    for tier in (None, "", "anonymous", "not-a-tier"):
        assert oc._build_email(_lead(tier)) is None, tier


def test_no_hand_typed_price_or_cap_map_survives():
    """★ AST, not a text slice: a dict literal whose values look like prices or
    call caps is the shape that drifted. Bound to the node so an unrelated edit
    moving the region cannot make this pass or fail by accident."""
    tree = ast.parse(open(_MOD, encoding="utf-8").read())
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if not any(k in ("pro", "developer", "starter") for k in keys):
            continue
        vals = [v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        if any(re.search(r"\$|\d,\d{3}|^\d{2,}$", v) for v in vals):
            bad.append((node.lineno, keys, vals))
    assert not bad, f"hand-typed tier price/cap map(s) still present: {bad}"


# ── claims the mail cannot back ───────────────────────────────────────

@pytest.mark.parametrize("tier", PRICED)
def test_prefilled_is_only_claimed_if_the_link_carries_an_email(tier):
    """The CTA URL has no email parameter, and it must not gain one -- putting
    a recipient's address in a query string is the wrong fix. So the claim goes."""
    _, html, text = oc._build_email(_lead(tier))
    for blob in (html, text):
        if "prefill" in blob.lower():
            assert "email=" in blob, (
                "the mail promises a prefilled checkout but the link carries "
                "no email; drop the claim rather than adding the parameter")


@pytest.mark.parametrize("tier", PRICED)
def test_the_can_spam_line_names_the_tier_the_lead_actually_started(tier):
    """It hardcoded 'Pro signup' for every recipient -- in the one sentence a
    CAN-SPAM complaint would quote, contradicting the body two paragraphs up."""
    _, html, _ = oc._build_email(_lead(tier))
    tail = html[html.index("You're receiving this because"):]
    label = tr.label(tier) or tier.title()
    assert f"{label} signup" in tail, tail[:160]


@pytest.mark.parametrize("tier", PRICED)
def test_the_tier_label_is_consistent_across_subject_and_body(tier):
    subject, html, text = oc._build_email(_lead(tier))
    label = tr.label(tier) or tier.title()
    assert label in subject and label in html and label in text
    assert "— —" not in subject, subject
