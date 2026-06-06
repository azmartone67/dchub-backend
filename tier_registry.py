"""
tier_registry.py — THE canonical source of truth for subscription tiers.
(r43-H, 2026-05-27)

WHY THIS EXISTS
---------------
Tier definitions were hand-copied into ~17 separate maps across the
backend, frontend, and MCP worker (rank maps, label maps, rate-limit
maps, paid-plan sets, plan→role maps). They drifted independently, which
is how a paying *founding* member (Carl Braun) ended up classified as
free: several maps either omitted 'founding' or ranked it below 'pro'.

This module is the ONE place tiers are defined. New gating/display/limit
code should import from here. The legacy scattered maps are validated
against this registry by tests/test_tier_consistency.py — if any of them
drifts (omits a tier, ranks founding below pro, gives founding non-pro
benefits), that test fails in CI before it can ship.

KEY BUSINESS RULE: founding === pro for BOTH access and benefits.
Founding is a premium early-adopter cohort mapped to the pro api tier.
"""

# Ordinal rank — higher = more access. A user satisfies a gate when their
# rank >= the required tier's rank. founding shares pro's rank.
TIERS = {
    'anonymous':  {'rank': -1, 'label': 'Anonymous',  'paid': False, 'api_tier': 'anonymous'},
    'anon':       {'rank': -1, 'label': 'Anonymous',  'paid': False, 'api_tier': 'anonymous'},
    'free':       {'rank': 0,  'label': 'Free',       'paid': False, 'api_tier': 'free'},
    'identified': {'rank': 1,  'label': 'Identified', 'paid': False, 'api_tier': 'identified'},
    'starter':    {'rank': 2,  'label': 'Starter',    'paid': True,  'api_tier': 'starter'},
    'developer':  {'rank': 3,  'label': 'Developer',  'paid': True,  'api_tier': 'developer'},
    'pro':        {'rank': 4,  'label': 'Pro',        'paid': True,  'api_tier': 'pro'},
    'founding':   {'rank': 4,  'label': 'Founding',   'paid': True,  'api_tier': 'pro'},   # == pro
    'enterprise': {'rank': 5,  'label': 'Enterprise', 'paid': True,  'api_tier': 'enterprise'},
    'research_seed': {'rank': 5, 'label': 'Research', 'paid': True,  'api_tier': 'enterprise'},
    'admin':      {'rank': 99, 'label': 'Admin',      'paid': True,  'api_tier': 'admin'},
}

# Per-day benefit limits. founding mirrors pro exactly.
# (rate_limit = API calls/day, record_cap = unique records/day,
#  page_cap = max pages/query, mcp_daily = MCP calls/day, mcp_results)
_PRO = dict(rate_limit=5000, record_cap=5000, page_cap=50, mcp_daily=2000, mcp_results=500)
TIER_LIMITS = {
    'anonymous':  dict(rate_limit=5,     record_cap=50,    page_cap=1,  mcp_daily=10,    mcp_results=5),
    'free':       dict(rate_limit=10,    record_cap=50,    page_cap=2,  mcp_daily=10,    mcp_results=5),
    'identified': dict(rate_limit=50,    record_cap=200,   page_cap=5,  mcp_daily=50,    mcp_results=25),
    'starter':    dict(rate_limit=500,   record_cap=500,   page_cap=10, mcp_daily=200,   mcp_results=50),
    'developer':  dict(rate_limit=1000,  record_cap=500,   page_cap=10, mcp_daily=500,  mcp_results=100),
    'pro':        dict(**_PRO),
    'founding':   dict(**_PRO),  # founding == pro benefits
    'enterprise': dict(rate_limit=100000, record_cap=999999, page_cap=999, mcp_daily=100000, mcp_results=10000),
    'research_seed': dict(rate_limit=100000, record_cap=999999, page_cap=999, mcp_daily=100000, mcp_results=10000),  # NLR == enterprise
    'admin':      dict(rate_limit=999999, record_cap=999999, page_cap=999, mcp_daily=999999, mcp_results=99999),
}


# ─────────────────────────────────────────────────────────────────────
# Canonical DISPLAY pricing (r-price-unify, 2026-06-02).
# Before this, the same $9 was quoted as both "Starter/10,000/day" and
# "Developer/500/day", and $49 as both "Developer" and "Pro", across 5+
# surfaces (server.mjs paywall, auto_trial.py, redeem pages) because no
# canonical price/quota map existed. THIS is now the source of truth for
# the dollar price; routes/_stripe_links.py is the source of truth for the
# Stripe Payment Link URL (the price the customer is actually charged on
# that link). Keep the two in sync — every consumer should read price from
# here + link from _stripe_links instead of hardcoding. Canonical calls/day
# per tier = TIER_LIMITS[tier]['mcp_daily'] (free 10 · starter 200 ·
# developer 500 · pro 2000 · enterprise 100000).
# ─────────────────────────────────────────────────────────────────────
TIER_PRICE_USD_MONTH = {
    'anonymous':  0,
    'anon':       0,
    'free':       0,
    'identified': 0,
    'starter':    9,
    'developer':  49,
    'pro':        199,
    'founding':   199,    # == pro
    'enterprise': None,   # custom / contact sales
    'research_seed': None,
    'admin':      None,
}


# ─────────────────────────────────────────────────────────────────────
# Per-tier surface unlocks (r-market-brief, 2026-06-06).
# Source of truth for which subscription unlocks which premium surface.
# Consumers read this to drive paywall copy + the "Includes…" pricing copy.
# ─────────────────────────────────────────────────────────────────────
TIER_FEATURES = {
    'anonymous':  {'market_brief': 'teaser'},
    'anon':       {'market_brief': 'teaser'},
    'free':       {'market_brief': 'teaser'},
    'identified': {'market_brief': 'teaser'},
    'starter':    {'market_brief': 'teaser'},
    'developer':  {'market_brief': 'teaser'},
    # PRO + above unlock the full 9-section brief for ALL markets.
    'pro':        {'market_brief': 'full'},
    'founding':   {'market_brief': 'full'},   # == pro
    'enterprise': {'market_brief': 'full+white_label'},
    'research_seed': {'market_brief': 'full+white_label'},
    'admin':      {'market_brief': 'full+white_label'},
}

# Public-facing pricing copy bullets. The pricing page reads this to render
# the "Includes…" list under each plan card; keeping the copy here keeps
# tier benefits + price + marketing copy in ONE source of truth.
TIER_PRICING_COPY = {
    'starter':    ['10× the free quota', 'API access', 'Email support'],
    'developer':  ['500 MCP calls/day', 'All public datasets',
                   'Developer Slack channel'],
    'pro':        ['2,000 MCP calls/day', 'Market Brief for all markets',
                   'Live deal flow + operator footprint',
                   'Priority email support'],
    'founding':   ['2,000 MCP calls/day', 'Market Brief for all markets',
                   'Founding-member badge', 'Direct line to the team'],
    'enterprise': ['Unlimited MCP', 'White-labeled Market Brief',
                   'Dedicated account manager', 'Custom SLA'],
}


def features(tier):
    """Per-tier feature unlock map (e.g. market_brief = teaser|full|full+white_label)."""
    return TIER_FEATURES.get(_norm(tier), TIER_FEATURES['free'])


def pricing_copy(tier):
    """Bulleted 'Includes…' list shown on the pricing card for `tier`."""
    return TIER_PRICING_COPY.get(_norm(tier), [])


def _norm(name):
    return (name or 'free').strip().lower()


def rank(tier):
    """Ordinal rank for a tier name (case-insensitive). Unknown → free."""
    return TIERS.get(_norm(tier), TIERS['free'])['rank']


def satisfies(user_tier, required_tier):
    """True iff user_tier grants at least required_tier's access."""
    return rank(user_tier) >= rank(required_tier)


def is_paid(tier):
    return TIERS.get(_norm(tier), {}).get('paid', False)


def label(tier):
    return TIERS.get(_norm(tier), TIERS['free'])['label']


def api_tier(tier):
    """The effective API access tier (e.g. founding → 'pro')."""
    return TIERS.get(_norm(tier), TIERS['free'])['api_tier']


def limits(tier):
    return TIER_LIMITS.get(_norm(tier), TIER_LIMITS['free'])


def price(tier):
    """Canonical monthly USD price for a tier (None = custom/contact). Unknown → 0."""
    return TIER_PRICE_USD_MONTH.get(_norm(tier), 0)


def calls_per_day(tier):
    """Canonical MCP calls/day quota for a tier (the number to quote on the paywall)."""
    return limits(tier).get('mcp_daily', 0)


def _stripe_link(tier):
    """Canonical Stripe Payment Link URL for a tier, or None. Lazy import so a
    consumer importing tier_registry never depends on routes/ being on the
    path at import time (tier_registry loads very early + widely)."""
    try:
        from routes._stripe_links import STRIPE_LINKS
        return STRIPE_LINKS.get(_norm(tier))
    except Exception:
        return None


def paid_plans():
    """Set of tier names that count as paying customers."""
    return {name for name, t in TIERS.items() if t['paid'] and name not in ('admin',)}


def as_public_dict():
    """Serializable registry for GET /api/v1/tiers (frontend mirror).

    r-price-unify (2026-06-02): now includes the canonical price_usd_month,
    calls_per_day (mcp_daily), and stripe_link per tier so every surface
    (frontend pricing, MCP paywall copy, redeem pages) can read ONE source
    instead of hardcoding contradictory numbers."""
    return {
        'tiers': {n: {'rank': t['rank'], 'label': t['label'], 'paid': t['paid'],
                      'api_tier': t['api_tier'],
                      'price_usd_month': TIER_PRICE_USD_MONTH.get(n),
                      'calls_per_day': TIER_LIMITS.get(n, {}).get('mcp_daily'),
                      'stripe_link': _stripe_link(n),
                      'features': TIER_FEATURES.get(n, {}),
                      'includes': TIER_PRICING_COPY.get(n, [])}
                  for n, t in TIERS.items()},
        'limits': TIER_LIMITS,
        'pricing': {n: TIER_PRICE_USD_MONTH.get(n) for n in TIERS},
        'features': TIER_FEATURES,
        'rule': 'founding == pro for access and benefits',
        'price_note': 'price_usd_month: starter 9 · developer 49 · pro 199 · enterprise custom. calls_per_day = mcp_daily.',
    }
