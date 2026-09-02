"""
_stripe_links.py — single source of truth for Stripe Payment Link URLs.

Phase ZZZZZ-round39 (2026-05-25). Previously STRIPE_LINKS was duplicated
in routes/stripe_direct_upgrade.py + routes/checkout_email_capture.py +
mcp_gatekeeper.py + email_capture.py + usage_limit_emails.py + others —
which caused the $299 vs $199 Pro link mismatch incident. Now there is
ONE canonical map; every consumer imports from here.

To swap a Stripe link, edit ONLY this file.
"""

# ─────────────────────────────────────────────────────────────────────
# Canonical Stripe Payment Link URLs.
# Verified prices as of 2026-05-25 — re-verify in Stripe dashboard when
# changing. Each comment notes the configured price on the link.
# ─────────────────────────────────────────────────────────────────────
STRIPE_LINKS = {
    "starter":         "https://buy.stripe.com/8x2dRa5sS0x75uteGuaZi0g",  # $9/mo
    # r-reprice (2026-06-19): Developer $49→$79, Pro $199→$299 (restores the
    # pre-r38 Pro anchor), Team $499→$699. NEW checkouts only — existing
    # subscribers stay on the OLD links (Stripe never auto-migrates a
    # subscription's price), so all current paid members are grandfathered.
    # The subscription webhook is amount-agnostic (mrr from price.unit_amount,
    # tier→'paid' for any paid sub: flask_mcp_endpoints.py:2347/2407), so the
    # new amounts provision + email a key correctly.
    "developer":       "https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c",  # $49/mo (HELD 2026-06-19 owner call — builder on-ramp; the $79 link 00w28s3kK0x7f5355UaZi0k was minted then reverted/deactivated)
    "pro":             "https://buy.stripe.com/7sY7sM9J8enX7CB69YaZi0l",  # $299/mo (r-reprice; legacy $199: eVq5kE4oOfs13mleGuaZi0h)
    "team":            "https://buy.stripe.com/14AbJ2bRga7H0a98i6aZi0m",  # $699/mo, 5 seats (r-reprice; no prior link existed)
    "pro_annual":      "https://buy.stripe.com/dRm7sM6wW7Zz1edgOCaZi07",  # $1,188/yr (50% off $199/mo) - operator-provided link dRm7...07, 2026-06-04
    # r-annual50 (2026-06-26): NEW $1,794/yr one-time = 50% off the current
    # $299/mo Pro list ($3,588/yr). plink_1Tml5XJ9ey2ATcQlAMDgpMI2,
    # price_1Tml5WJ9ey2ATcQlhqdF82z1. Webhook → pro_annual + 365-day expiry.
    "pro_annual_promo": "https://buy.stripe.com/fZu14o1cCdjT5ut7e2aZi0n",  # $1,794/yr one-time (50% off $299/mo)
    # r-founder99 (2026-06-26): $99/mo Founding Member recurring, limited
    # licenses (see /api/founding-members counter). plink_1Tml5YJ9ey2ATcQlbQSMZRu4,
    # price_1Tml5XJ9ey2ATcQl0pbU4htM. Webhook → founding → ('founding','pro').
    "founding":        "https://buy.stripe.com/14A9AUcVk4Nn1edcymaZi0o",  # $99/mo (founding member)
    "metered":         "https://buy.stripe.com/9B69AU08y2FfbSR55UaZi0i",  # $10 one-time = 1,000 API calls (single one-time pack; 2026-06-25 repricing)
    "pack5":           "https://buy.stripe.com/9B69AU08y2FfbSR55UaZi0i",  # $10 one-time = 1,000 API calls (2026-06-25 repricing; env override DCHUB_PACK5_URL in mcp_conversion_plays.py)
    "enterprise":      "https://buy.stripe.com/fZueVe5sS6Vv7CB41QaZi0a",  # Custom
    "enterprise_annual": "https://buy.stripe.com/dRmdRa4oO1Bb9KJ2XMaZi0b",  # Custom annual
    # r75 (2026-05-26): partnership-specific subscription links. NOT shown
    # in public pricing — these are bespoke per landed deal. First entry:
    # NLR's Year-1 Research Seed at $3K/yr (90% off Strategic Partnership
    # list of $10K, 97% off Enterprise $100K list). Includes full API
    # surface + partnership rights from Day 1. Converts to Strategic at
    # $10K in Year 2 when NLR's dedicated DC-siting funding closes.
    "research_seed_nlr": "https://buy.stripe.com/cNi3cwaNc0x75utdCqaZi0e",  # $3,000/yr (NLR FY 2026)
}

# Tool → recommended tier mapping. Tools gated at Pro require Pro plan;
# tools gated at Developer require Developer or higher.
TOOL_TIER_MAP = {
    # Pro-gated tools (free tier blocked)
    "get_intelligence_index":  "pro",
    "compare_sites":           "pro",
    "analyze_site":            "pro",
    "get_infrastructure":      "pro",
    "get_fiber_intel":         "pro",
    "get_grid_intelligence":   "pro",
    # Developer-gated (fields truncated on free)
    "search_facilities":       "developer",
    "list_transactions":       "developer",
    "get_news":                "developer",
    "get_pipeline":            "developer",
    "rank_markets":            "developer",
    "find_alternatives":       "developer",
    "score_facility":          "developer",
    # AI capex relevance tools (r36)
    "ai_capacity_index":       "developer",
    "hyperscaler_deals":       "developer",
}

TIER_PRICE_LABEL = {
    "starter":    "$9/mo",
    "developer":  "$49/mo",
    "pro":        "$299/mo",
    "founding":   "$99/mo",
    "pro_annual": "$1,188/yr",
    "metered":    "$10 / 1,000 API calls",
    "pack5":      "$10 / 1,000 API calls (one-time)",
    "enterprise": "Custom",
    "enterprise_annual": "Custom annual",
    "research_seed_nlr": "$3,000/yr (NLR FY 2026 Research Seed)",
}

# ── the one-time pack ─────────────────────────────────────────────────
# 2026-09-02 (relay-sells-what-it-says). The $10 one-time pack is the ONLY
# thing an anonymous or merely-identified caller is ever offered by copy
# ("Unlock full data — $10 one-time" on /upgrade/h/<token>), so it is also
# the only thing an unresolvable tier may fall through to. `metered` and
# `pack5` are the same Stripe link (see STRIPE_LINKS); `metered` is the
# canonical key, `pack5` the legacy alias mcp_conversion_plays still emits.
# The USD figure is the charge on that link — cross-pinned against
# mcp_conversion_plays.PACK10_PRICE_CENTS by tests/test_relay_sells_what_it_says.py.
PACK_TIER = "metered"
ONE_TIME_TIERS = frozenset({"metered", "pack5"})
TIER_ONE_TIME_USD = {"metered": 10, "pack5": 10}

# Caller-tier words that are NOT plans. `free`/`identified`/`anon` describe
# who is asking, not what they are buying; a URL that carries one of these as
# ?tier= must never resolve to a subscription.
NON_PLAN_TIERS = frozenset({"free", "anonymous", "anon", "identified", "none", ""})


def resolve_tier(tool: str, tier_param: str, budget_hint: str = "") -> str:
    """Pick the right tier — explicit param wins, then budget hint, then tool lookup,
    then the one-time pack. r45.1 (2026-05-25): added budget hint for downsell flow.

    ★ 2026-09-02: the fall-through is PACK_TIER, never a monthly plan. For
    three months the default was 'developer' ($49/mo), and the agent→human
    relay page (routes/human_relay.py) emitted ?tier=free|identified — the
    CALLER's tier, not a plan — so its "$10 one-time" button 302'd to the
    $49/mo Developer link. 102 real human opens in 30d, 0 paid. Verified
    live 2026-09-02T00:29Z at api.dchub.cloud and the Railway origin. A
    tier we cannot name is a caller we cannot classify; the honest offer to
    an unclassified caller is the cheapest, non-recurring one.
    """
    if tier_param and tier_param.lower() in STRIPE_LINKS:
        return tier_param.lower()
    # r45.1: ?budget=tight, ?budget=cheap, ?intent=starter → starter ($9/mo)
    if budget_hint and budget_hint.lower() in ("tight", "cheap", "starter", "low"):
        return "starter"
    if tool and tool in TOOL_TIER_MAP:
        return TOOL_TIER_MAP[tool]
    return PACK_TIER


def get_stripe_url(tier: str) -> str:
    return STRIPE_LINKS.get(tier) or STRIPE_LINKS[PACK_TIER]
