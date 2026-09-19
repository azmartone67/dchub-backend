"""util/tier_gate.py's REST upgrade CTA must quote the REST limits, from canon.

WHY THIS EXISTS. The CTA typed its own quotas:

    "Upgrade to Developer ($49/mo) to unlock {teaser} + 2000 calls/day
     + 100 results/call."
    "Upgrade to Pro ($199/mo) to unlock {teaser} + 10k calls/day
     + 500 results/call."

2000 and 10k were the REST rate limits when this module was written (Phase
GG, 2026-05-15). Canon has said 1,000 and 5,000 since — so the pitch offered
a paying REST caller twice the calls they would actually get. The prices were
fixed in #4797; the quotas outlived that sweep because a price guard looks
for dollars.

★ WHICH CANON. This is the trap the fix turned on, so it is pinned here:
util/tier_gate is a REST soft paywall (its own docstring: "brings the MCP
teaser pattern to REST"). So calls/day is TIER_LIMITS['rate_limit'] — NOT
mcp_daily, which is the /mcp path's separate quota and would read 500/2,000
here. The two are easy to confuse and only one is right for this surface.

Results-per-call comes from api_tier_gating.TIER_SEARCH_LIMITS, the map that
actually enforces search/list width, which is deliberately not in
tier_registry (its own scale, no TIER_LIMITS column).

WHAT IT PINS. The rendered CTA — not the source. A test that greps for the
literal would pass on a string nobody renders.
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tier_registry  # noqa: E402
from api_tier_gating import TIER_SEARCH_LIMITS  # noqa: E402
from util.tier_gate import Tier, _build_cta  # noqa: E402

_TEASER = "the full dataset"


def _cta(current, required):
    return _build_cta(current, required, _TEASER)


def test_developer_cta_quotes_the_rest_quota_not_the_mcp_one():
    msg = _cta(Tier.IDENTIFIED, Tier.DEVELOPER)
    rest = tier_registry.limits("developer")["rate_limit"]
    mcp = tier_registry.limits("developer")["mcp_daily"]
    assert f"{rest:,} calls/day" in msg, (
        "developer CTA does not quote the REST rate_limit (%s): %r" % (rest, msg))
    assert rest != mcp, "fixture is vacuous — the two quotas are equal"
    assert f"{mcp:,} calls/day" not in msg, (
        "developer CTA quotes mcp_daily (%s) on a REST surface: %r" % (mcp, msg))


def test_pro_cta_quotes_the_rest_quota_not_the_mcp_one():
    msg = _cta(Tier.DEVELOPER, Tier.PRO)
    rest = tier_registry.limits("pro")["rate_limit"]
    mcp = tier_registry.limits("pro")["mcp_daily"]
    assert f"{rest:,} calls/day" in msg, (
        "pro CTA does not quote the REST rate_limit (%s): %r" % (rest, msg))
    assert rest != mcp, "fixture is vacuous — the two quotas are equal"
    assert f"{mcp:,} calls/day" not in msg, (
        "pro CTA quotes mcp_daily (%s) on a REST surface: %r" % (mcp, msg))


def test_results_per_call_matches_the_map_that_enforces_it():
    for tier, key in ((Tier.DEVELOPER, "developer"), (Tier.PRO, "pro")):
        current = Tier.IDENTIFIED if tier is Tier.DEVELOPER else Tier.DEVELOPER
        msg = _cta(current, tier)
        assert f"{TIER_SEARCH_LIMITS[key]:,} results/call" in msg, (
            "%s CTA does not quote TIER_SEARCH_LIMITS[%r] = %s: %r"
            % (key, key, TIER_SEARCH_LIMITS[key], msg))


def test_the_anonymous_cta_prices_the_tier_it_names():
    """It typed "$49/mo" regardless of `required`, so an endpoint needing PRO
    invited the caller to "upgrade to Pro ($49/mo)". $49 is a real price —
    just not Pro's — so a canon-vs-literal price check could never see it."""
    for required, key in ((Tier.DEVELOPER, "developer"), (Tier.PRO, "pro")):
        msg = _cta(Tier.ANONYMOUS, required)
        want = tier_registry.price_display(key)
        assert want in msg, "anonymous->%s CTA omits %s: %r" % (key, want, msg)
        wrong = [tier_registry.price_display(k)
                 for k in ("starter", "developer", "pro")
                 if tier_registry.price_display(k) != want]
        for w in wrong:
            assert w not in msg, (
                "anonymous->%s CTA also quotes %s — the price is not bound to "
                "the tier it names: %r" % (key, w, msg))


def test_no_quota_or_price_is_typed_into_the_cta_source():
    """Derivation, not a corrected literal: the CTA builder must contain no
    bare calls/day, results/call or $N figure of its own."""
    import ast
    import inspect
    from util import tier_gate

    # ★ Read the STRING CONSTANTS, not the raw source. inspect.getsource
    # hands back comments too, and the comment above the anonymous branch
    # explains the bug by quoting "$49" — which made the first version of
    # this assertion fail on its own explanation.
    tree = ast.parse(inspect.getsource(tier_gate._build_cta).lstrip())
    typed = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            typed += re.findall(r"\b[\d,]+k?\s*(?:calls/day|results/call)", node.value)
            typed += re.findall(r"\$\s?[\d,]+", node.value)
    assert not typed, (
        "util/tier_gate._build_cta types a quota or price instead of reading "
        "canon: %s" % typed)


def test_a_moved_canon_reaches_the_rendered_cta():
    """Behaviour: move the REST limit and the CTA must follow."""
    original = dict(tier_registry.TIER_LIMITS["pro"])
    try:
        tier_registry.TIER_LIMITS["pro"] = dict(original, rate_limit=7777)
        msg = _cta(Tier.DEVELOPER, Tier.PRO)
        assert "7,777 calls/day" in msg, (
            "the CTA did not follow a moved rate_limit — it is not derived: %r" % msg)
    finally:
        tier_registry.TIER_LIMITS["pro"] = original
    assert tier_registry.limits("pro")["rate_limit"] == original["rate_limit"]
