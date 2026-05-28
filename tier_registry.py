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
_PRO = dict(rate_limit=5000, record_cap=5000, page_cap=50, mcp_daily=10000, mcp_results=500)
TIER_LIMITS = {
    'anonymous':  dict(rate_limit=5,     record_cap=50,    page_cap=1,  mcp_daily=10,    mcp_results=5),
    'free':       dict(rate_limit=10,    record_cap=50,    page_cap=2,  mcp_daily=10,    mcp_results=5),
    'identified': dict(rate_limit=50,    record_cap=200,   page_cap=5,  mcp_daily=50,    mcp_results=25),
    'starter':    dict(rate_limit=500,   record_cap=500,   page_cap=10, mcp_daily=500,   mcp_results=50),
    'developer':  dict(rate_limit=1000,  record_cap=500,   page_cap=10, mcp_daily=1000,  mcp_results=100),
    'pro':        dict(**_PRO),
    'founding':   dict(**_PRO),  # founding == pro benefits
    'enterprise': dict(rate_limit=100000, record_cap=999999, page_cap=999, mcp_daily=100000, mcp_results=10000),
    'admin':      dict(rate_limit=999999, record_cap=999999, page_cap=999, mcp_daily=999999, mcp_results=99999),
}


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


def paid_plans():
    """Set of tier names that count as paying customers."""
    return {name for name, t in TIERS.items() if t['paid'] and name not in ('admin',)}


def as_public_dict():
    """Serializable registry for GET /api/v1/tiers (frontend mirror)."""
    return {
        'tiers': {n: {'rank': t['rank'], 'label': t['label'], 'paid': t['paid'],
                      'api_tier': t['api_tier']} for n, t in TIERS.items()},
        'limits': TIER_LIMITS,
        'rule': 'founding == pro for access and benefits',
    }
