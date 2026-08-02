"""market_aliases.py — redundant DCPI market slug → canonical slug.

r-twin-unpublish (2026-07-28). Lifted out of routes/dcpi.py so it can be
imported WITHOUT side effects. routes/dcpi.py builds `MARKETS` at module
level (a live DB query), so anything wanting just this map had to either
pay for that import or hand-copy the table — and a hand-copied shared map
is exactly what produced the six divergent state→ISO maps fixed the same
day in [util/iso_taxonomy.py]. dchub_self_heal.py needs these keys to avoid
re-publishing retired twins, hence the move.

Two distinct jobs, both keyed slug → canonical:

1. FRIENDLY ALIASES that never had a row of their own ('nova', 'dfw',
   'sv'). These exist so an inbound link resolves; nothing to retire.

2. REDUNDANT TWINS (r-twin-dedup, 2026-07-19) — the same market defined
   under two slugs, a legacy state-suffixed form plus the canonical
   bare-city form. Both rows existed; the merge kept both, so rankings
   showed duplicates. These slugs are dropped from the scoring universe,
   which means NO recompute chunk can ever reach their rows: on 2026-07-28
   all seven were still `published = true` and frozen at 2026-07-19 with
   `iso_type` NULL, while their canonical twins were current. Retiring them
   is what `_retire_alias_twins` in routes/dcpi.py does on every recompute.
"""

from __future__ import annotations

DCPI_METRO_ALIASES = {
    # ── Friendly metro aliases (no row of their own) ───────────────────
    # Northern Virginia cluster → Ashburn
    'northern-virginia': 'ashburn',
    'n-virginia':        'ashburn',
    'nova':              'ashburn',
    # Dallas-Fort Worth → Dallas
    'dallas-fort-worth': 'dallas',
    'dallas-ft-worth':   'dallas',
    'dfw':               'dallas',
    # Silicon Valley → Santa Clara (r47.43 — was 404'ing)
    'silicon-valley':    'santa-clara',
    'sv':                'santa-clara',
    'bay-area':          'santa-clara',
    'sf-bay-area':       'santa-clara',
    'south-bay':         'santa-clara',
    # Portland → Portland, OR (r-portland-canon 2026-08-02). The bare
    # 'portland' slug is OREGON on every other surface (main.py market
    # vocab, the curated /markets/portland page = Portland-Hillsboro, the
    # market-brief seeds) — but the dynamic loader minted a bare-'portland'
    # market_power_scores row for Portland MAINE (city rule: LOWER(city),
    # no state), a DIFFERENT market that merely shares the display name
    # 'Portland'. That name collision cross-wired the deep-dive resolver
    # (generate_for_market('portland') wrote portland-or's row forever).
    # Maine now lives under 'portland-me' / 'Portland, ME'
    # (routes/dcpi.py _CITY_MARKET_DISAMBIGUATION), and bare 'portland'
    # is a friendly alias for the hardcoded Oregon row.
    'portland':          'portland-or',
    'portland-hillsboro': 'portland-or',
    # ── Redundant twins (r-twin-dedup 2026-07-19) ──────────────────────
    # Canonical picks are reference-informed: 'dc' (68 press refs, the
    # intentional 'Washington, DC' market) beats the bare 'washington'
    # dynamic dupe; the others canonicalize to bare-city.
    'cheyenne-wy':       'cheyenne',
    'columbus-oh':       'columbus',
    'the-dalles-or':     'the-dalles',
    'washington':        'dc',
}

#: The subset that had a duplicate row of its own and must stay unpublished.
#: `northern-virginia`, `dallas-fort-worth` and `silicon-valley` are here as
#: well as above — they are friendly metro names that ALSO acquired their own
#: market_power_scores row from the dynamic loader, which is how three
#: high-traffic slugs ended up serving 9-day-stale scores.
REDUNDANT_TWIN_SLUGS = frozenset({
    'northern-virginia', 'dallas-fort-worth', 'silicon-valley',
    'cheyenne-wy', 'columbus-oh', 'the-dalles-or', 'washington',
    # 'portland' (r-portland-canon 2026-08-02): belt-and-braces. The Maine
    # row was RENAMED to portland-me (not just unpublished), and the loader
    # disambiguation keeps a bare-'portland' row from being re-minted. If
    # one ever resurrects anyway (hand insert, orphan re-adopt), it is junk
    # by definition — every consumer treats bare 'portland' as an Oregon
    # alias — so retire it while the canonical portland-or row is published.
    'portland',
})


def canonical_slug(slug: str | None) -> str:
    """Canonical market slug for an alias, or '' if the slug is already one."""
    return DCPI_METRO_ALIASES.get((slug or "").lower().strip(), "")
