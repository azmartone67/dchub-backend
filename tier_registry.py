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
    # MCP Funnel Round 3 Unlock 3 (2026-06-07): team accounts. Sits
    # between Pro (rank 4) and Enterprise (rank 5); api_tier='pro' so
    # team members get Pro-equivalent gates. The multi-seat economics
    # live in routes/team_accounts.py — this entry is only for tier
    # rank/label propagation.
    'team':       {'rank': 4,  'label': 'Team',       'paid': True,  'api_tier': 'pro'},
    'enterprise': {'rank': 5,  'label': 'Enterprise', 'paid': True,  'api_tier': 'enterprise'},
    'research_seed': {'rank': 5, 'label': 'Research', 'paid': True,  'api_tier': 'enterprise'},
    'admin':      {'rank': 99, 'label': 'Admin',      'paid': True,  'api_tier': 'admin'},
}

# Per-day benefit limits. founding mirrors pro exactly.
# (rate_limit = API calls/day, record_cap = unique records/day,
#  page_cap = max pages/query, mcp_daily = MCP calls/day, mcp_results)
_PRO = dict(rate_limit=5000, record_cap=5000, page_cap=50, mcp_daily=2000, mcp_results=500)
TIER_LIMITS = {
    # ★ mcp_daily 10 -> 5 (2026-08-03). anonymous and free BOTH sat at 10
    # MCP calls/day, so claiming a free key bought an agent literally nothing
    # on the surface agents actually use — which is why 30d produced 3
    # identified callers. This restores a real first rung (anon 5 -> free 10
    # -> identified 50 -> starter 200) and mirrors anonymous's own REST
    # rate_limit=5, which was already half of free's 10. It TIGHTENS the
    # anonymous tier rather than loosening free.
    'anonymous':  dict(rate_limit=5,     record_cap=50,    page_cap=1,  mcp_daily=5,     mcp_results=5),
    'free':       dict(rate_limit=10,    record_cap=50,    page_cap=2,  mcp_daily=10,    mcp_results=5),
    'identified': dict(rate_limit=50,    record_cap=200,   page_cap=5,  mcp_daily=50,    mcp_results=25),
    'starter':    dict(rate_limit=500,   record_cap=500,   page_cap=10, mcp_daily=200,   mcp_results=50),
    'developer':  dict(rate_limit=1000,  record_cap=500,   page_cap=10, mcp_daily=500,  mcp_results=100),
    'pro':        dict(**_PRO),
    'team':       dict(**_PRO),  # team members get Pro-equivalent daily caps
    'founding':   dict(**_PRO),  # founding == pro benefits
    'enterprise': dict(rate_limit=100000, record_cap=999999, page_cap=999, mcp_daily=100000, mcp_results=10000),
    'research_seed': dict(rate_limit=100000, record_cap=999999, page_cap=999, mcp_daily=100000, mcp_results=10000),  # NLR == enterprise
    'admin':      dict(rate_limit=999999, record_cap=999999, page_cap=999, mcp_daily=999999, mcp_results=99999),
}

# ── Monthly quota arithmetic (monthly-quota phase 2, 2026-08-06) ──────
# One month = 30 x the canonical per-day number. Lives HERE, next to
# TIER_LIMITS, because both the enforcing gate (monthly_quota.py, which
# imports this) and the public pricing copy below multiply by it — a
# second copy of "30" is exactly how display and quota start disagreeing.
MCP_DAYS_PER_MONTH = 30


def calls_per_month(tier):
    """Canonical MCP calls/MONTH for a tier — the number to quote on a PAID
    paywall. Paid ceilings are enforced per month (monthly_quota.py); their
    per-day caps were never enforced on the /mcp path. free and identified
    are still gated per DAY, so quote those with calls_per_day() instead."""
    return limits(tier).get('mcp_daily', 0) * MCP_DAYS_PER_MONTH


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
    # r-reprice (2026-06-19): Developer $49→$79, Pro $199→$299 (restores the
    # pre-r38 Pro anchor), Team $499→$699. Justified by the deepened grid layer
    # (live multi-ISO + Phoenix/Vegas/PacNW BAs, fixed compare_isos/scoreboard,
    # real DCGI gas-cost + gas-to-grid economics). DISPLAY price only; the
    # customer is charged by the Stripe link in routes/_stripe_links.py — both
    # updated together. Existing subscribers grandfathered on their old links.
    # ── r-price-collapse (2026-09-05, owner call) ────────────────────
    # MEASURED over 90d of Stripe checkout sessions: every self-serve price
    # ABOVE $99 closed at 0% — $299 Pro 17 opens/0 paid, $699 Team 8/0,
    # $1,188 annual 24/0, $1,794 annual promo 6/0, $199 legacy 16/0. That is
    # 76 checkout opens above $99 with zero sales. $99 closed 8 of 43 (18.6%)
    # — the best rate on the page — and is 10 of 16 live subscriptions.
    # Self-serve has a ceiling and it sits at $99; anything worth more than
    # that needs a human, which is what ENTERPRISE_FROM_USD_YEAR is for.
    # So Pro IS $99 now. `founding` collapses into it (they were already the
    # same tier — see the founding==pro rule below); the separate
    # founding SKU and its scarcity counter are retired.
    'starter':    9,      # retired from the public page (2.6% close); tier kept
                          # for the 2 grandfathered subs + ?budget=tight downsell.
    'developer':  49,     # HELD: the builder/agent on-ramp (6.1% close).
    'pro':        99,     # was 299 (r-reprice 2026-06-19), was 199 before that.
                          # Charged truth is the r-founder99 $99/mo link, which
                          # is now STRIPE_LINKS['pro'] — see _stripe_links.py.
    'team':       699,    # retired from the public page: 8 checkout opens, 0
                          # sales, 0 subscribers ever. Tier kept so an existing
                          # link cannot 404; Teams are sold via the enterprise
                          # lane now.
    # founding DECOUPLED from pro at r-reprice: founding members must NOT see
    # the new Pro price. founding still == pro for ACCESS/rank/benefits (TIERS
    # + TIER_LIMITS below). Charged truth is $99/mo — the r-founder99
    # (2026-06-26) 'founding' Stripe link in routes/_stripe_links.py, which
    # canonical_funnel.PLAN_MONTHLY_USD and the webhook amount-band both
    # mirror. The previous 199 here was a stale display value that made
    # /api/v1/tiers quote 2x what founding members are actually charged.
    'founding':   99,     # == pro. LEGACY ALIAS as of r-price-collapse: kept so
                          # the 10 existing founding subs, their keys and every
                          # ?tier=founding URL keep resolving. New buyers are
                          # sold 'pro' at the same $99 on the same Stripe link.
    'enterprise': None,   # custom / contact sales — see ENTERPRISE_FROM_USD_YEAR
    'research_seed': None,
    'admin':      None,
}

# ─────────────────────────────────────────────────────────────────────
# The enterprise anchor (r-price-collapse, 2026-09-05).
#
# The public page needs ONE number above $99 so the product is not read as
# a $99 product. This is the "from" price for a seat-and-scope deal sold by
# a human — NOT a self-serve SKU, and deliberately NOT in STRIPE_LINKS:
# nothing at this price may ever be closable on a payment link, because 90d
# of data says a payment link closes exactly 0% above $99.
#
# Anchored against the category: comparable single-purpose tools quote
# ~$20K/yr (site/power screening) and ~$5K/yr (fiber lookup); DC Hub covers
# both surfaces plus grid telemetry, DCPI and M&A in one licence, so $12K
# sits under the incumbent while pricing the union of two of them.
# ─────────────────────────────────────────────────────────────────────
ENTERPRISE_FROM_USD_YEAR = 12000


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
    'team':       {'market_brief': 'full'},   # team members get Pro features
    'founding':   {'market_brief': 'full'},   # == pro
    'enterprise': {'market_brief': 'full+white_label'},
    'research_seed': {'market_brief': 'full+white_label'},
    'admin':      {'market_brief': 'full+white_label'},
}

# Public-facing pricing copy bullets. The pricing page reads this to render
# the "Includes…" list under each plan card; keeping the copy here keeps
# tier benefits + price + marketing copy in ONE source of truth.
#
# ★2026-08-06 (monthly-quota phase 2): PAID quotas are quoted per MONTH.
# The per-day figure these bullets used to carry was never enforced on the
# /mcp path (verified 2026-07-30) — monthly is the ceiling that will
# actually exist, and it is the number the gate uses. Every quantity below
# is COMPUTED from TIER_LIMITS x MCP_DAYS_PER_MONTH, so a repriced tier
# updates the pricing card and the quota together or not at all.
# free/identified copy stays per-DAY on purpose: those gates are real and
# still daily, so a monthly figure there would over-promise.
def _mcp_monthly_copy(tier, suffix=''):
    return f"{TIER_LIMITS[tier]['mcp_daily'] * MCP_DAYS_PER_MONTH:,} MCP calls/month{suffix}"


TIER_PRICING_COPY = {
    'starter':    [_mcp_monthly_copy('starter'), 'API access', 'Email support'],
    'developer':  [_mcp_monthly_copy('developer'), 'All public datasets',
                   'Developer Slack channel'],
    'pro':        [_mcp_monthly_copy('pro'), 'Market Brief for all markets',
                   'Live deal flow + operator footprint',
                   'Priority email support'],
    'team':       ['5 seats included', _mcp_monthly_copy('team', ' shared'),
                   'Per-seat usage attribution',
                   'Owner-controlled invite + remove',
                   'Everything in Pro'],
    'founding':   [_mcp_monthly_copy('founding'), 'Market Brief for all markets',
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


# ── annual options (brain-ascension #28 wave 2, 2026-07-25) ───────────
# The annual SKUs were charged via routes/_stripe_links.py but INVISIBLE to
# this registry, so /api/v1/tiers (the frontend/paywall mirror) could never
# render an annual toggle. Display-only and ADDITIVE: keyed by the base tier
# it upgrades; nothing here changes rank/access/limits (founding==pro rule
# and test_tier_consistency are untouched). Keep amounts in lock-step with
# _stripe_links.py comments.
#
# ★ r-price-collapse (2026-09-05): PRO ANNUAL IS WITHDRAWN, and the reason is
#   arithmetic, not taste. Both annual SKUs were priced off the $299 monthly
#   list. Against a $99 list they stop making sense the instant the price
#   changes:
#       pro_annual        $1,188/yr = 12 x $99 exactly  -> a 0% "discount"
#       pro_annual_promo  $1,794/yr                     -> 51% MORE than
#                                                          paying monthly
#   Selling either one next to a $99/mo button is an offer that punishes the
#   buyer for taking it. Neither has ever sold (0 of 24 and 0 of 6 checkout
#   opens in 90d), so withdrawing them costs nothing measurable.
#   The links in _stripe_links.py are LEFT INTACT so any annual sub already
#   provisioned keeps renewing and any live URL keeps resolving — they are
#   simply no longer advertised.
#   TO RESTORE a real annual: mint a $990/yr link (2 months free on $99) and
#   put it back here. That is an owner action in Stripe; nothing in code
#   blocks it.
ANNUAL_OPTIONS = {
    'enterprise': {
        'annual_usd_year': None,        # custom / contact sales
        'annual_link_key': 'enterprise_annual',
        'note': 'custom annual — contact sales',
    },
}


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
                      # ★ Resolve ALIASES through api_tier (2026-08-03). 'anon'
                      # is an alias of 'anonymous' and has no TIER_LIMITS entry
                      # of its own, so this published "anon calls/day = null" —
                      # which reads on /api/v1/tiers as an UNCAPPED anonymous
                      # tier. It is not uncapped (TIERS['anon'].api_tier =
                      # 'anonymous', 5/day), but the published ladder said
                      # otherwise, and the ladder is what agents and buyers
                      # read. Same for 'founding'/'team' -> pro.
                      'calls_per_day': (TIER_LIMITS.get(n)
                                        or TIER_LIMITS.get(t['api_tier'], {})
                                        ).get('mcp_daily'),
                      # ★2026-08-06: the PAID ceiling that is actually
                      # enforced (monthly_quota.py). Published alongside
                      # calls_per_day, not instead of it — free/identified
                      # are still gated per day. Aliases resolve through
                      # api_tier for the same reason calls_per_day does.
                      'calls_per_month': (
                          lambda d: (d.get('mcp_daily') * MCP_DAYS_PER_MONTH)
                          if d.get('mcp_daily') is not None else None
                      )(TIER_LIMITS.get(n) or TIER_LIMITS.get(t['api_tier'], {})),
                      'stripe_link': _stripe_link(n),
                      'annual': ANNUAL_OPTIONS.get(n),
                      'features': TIER_FEATURES.get(n, {}),
                      'includes': TIER_PRICING_COPY.get(n, [])}
                  for n, t in TIERS.items()},
        'limits': TIER_LIMITS,
        'pricing': {n: TIER_PRICE_USD_MONTH.get(n) for n in TIERS},
        'annual_options': ANNUAL_OPTIONS,
        'features': TIER_FEATURES,
        'rule': 'founding == pro for access and benefits',
        # ★ r-price-collapse follow-up (2026-09-05): this sentence used to TYPE
        #   its own price list ("pro 299 · team 699") right beside the
        #   machine-readable `pricing` map in the SAME response. When Pro moved
        #   to 99 the map updated and the sentence did not, so /api/v1/tiers
        #   published two different prices for one tier in one payload — and
        #   the prose is the half an LLM is most likely to quote. DERIVED now,
        #   so it cannot drift again.
        'price_note': ('price_usd_month: '
                       + ' · '.join(
                           f"{_t} {TIER_PRICE_USD_MONTH[_t]}"
                           for _t in ('starter', 'developer', 'pro', 'team')
                       )
                       + ' · enterprise custom. calls_per_day = mcp_daily; '
                       f'calls_per_month = mcp_daily x {MCP_DAYS_PER_MONTH} — quote PAID '
                       'tiers monthly (that is the enforced ceiling) and free/identified '
                       'daily (those gates are still per-day).'),
    }
