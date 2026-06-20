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
    "developer":       "https://buy.stripe.com/00w28s3kK0x7f5355UaZi0k",  # $79/mo (r-reprice; legacy $49: 7sY5kE8F4fs13ml0PEaZi0c)
    "pro":             "https://buy.stripe.com/7sY7sM9J8enX7CB69YaZi0l",  # $299/mo (r-reprice; legacy $199: eVq5kE4oOfs13mleGuaZi0h)
    "team":            "https://buy.stripe.com/14AbJ2bRga7H0a98i6aZi0m",  # $699/mo, 5 seats (r-reprice; no prior link existed)
    "pro_annual":      "https://buy.stripe.com/dRm7sM6wW7Zz1edgOCaZi07",  # $1,188/yr (50% off $199/mo) - operator-provided link dRm7...07, 2026-06-04
    "metered":         "https://buy.stripe.com/9B69AU08y2FfbSR55UaZi0i",  # $1 / 100 API calls (usage-based / pay-as-you-go)
    "pack5":           "https://buy.stripe.com/8x26oIbRg7ZzbSR7e2aZi0j",  # $5 one-time = 1,000 API credits (r-pack5, 2026-06-16; env override DCHUB_PACK5_URL in mcp_conversion_plays.py)
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
    "pro":        "$199/mo",
    "pro_annual": "$1,188/yr",
    "metered":    "$1 / 100 calls",
    "pack5":      "$5 / 1,000 calls (one-time)",
    "enterprise": "Custom",
    "enterprise_annual": "Custom annual",
    "research_seed_nlr": "$3,000/yr (NLR FY 2026 Research Seed)",
}


def resolve_tier(tool: str, tier_param: str, budget_hint: str = "") -> str:
    """Pick the right tier — explicit param wins, then budget hint, then tool lookup,
    then default. r45.1 (2026-05-25): added budget hint for downsell flow."""
    if tier_param and tier_param.lower() in STRIPE_LINKS:
        return tier_param.lower()
    # r45.1: ?budget=tight, ?budget=cheap, ?intent=starter → starter ($9/mo)
    if budget_hint and budget_hint.lower() in ("tight", "cheap", "starter", "low"):
        return "starter"
    if tool and tool in TOOL_TIER_MAP:
        return TOOL_TIER_MAP[tool]
    return "developer"


def get_stripe_url(tier: str) -> str:
    return STRIPE_LINKS.get(tier, STRIPE_LINKS["developer"])
