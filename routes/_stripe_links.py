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
    "developer":       "https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c",  # $49/mo
    "pro":             "https://buy.stripe.com/eVq5kE4oOfs13mleGuaZi0h",  # $199/mo (new r38, replaces $299/$2990 link)
    "pro_annual":      "https://buy.stripe.com/6oU00k6wW7ZzcWV9maaZi03",  # ~$2,000/yr (~17% off)
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
    "pro_annual": "$2,000/yr",
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
