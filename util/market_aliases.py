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


# ═══════════════════════════════════════════════════════════════════════
# IDENTIFIER RESOLUTION — r-markets-api-ident (2026-09-05)
# ═══════════════════════════════════════════════════════════════════════
# /api/v1/markets/<id> resolved against main.MARKET_ALIASES — a curated,
# hand-written, US-only dict of 34 keys — while /api/v1/markets PUBLISHES
# 132 markets built from three sources (curated + US auto-discovered +
# international auto-discovered). The detail route served a strict SUBSET
# of what its own list route advertises, so following the list's
# `cite_url_template` produced 404s. Measured live through the edge,
# cache-busted 2026-09-05, on the five ids the anonymous tier shows:
#
#     listed id `northern virginia`  -> /api/v1/markets/... 200
#     listed id `london-gb`          -> 404
#     listed id `singapore-sg`       -> 404
#     listed id `tokyo-jp`           -> 404
#     listed id `amsterdam-nl`       -> 404
#
# Four of the five ids we publish 404 on our own detail route, and the
# 404 body's remediation text ("Call rank_markets (or GET /api/v1/markets)
# for the full list") hands the agent exactly those ids. A closed loop of
# bad advice.
#
# ★ THE REPORTED SYMPTOM NAMED THE WRONG AXIS. The defect arrived as
# "multi-word markets 404, single-word ones work" (Santa Clara / Ludwigshafen
# Am Rhein failing, Ashburn passing). Word count is not the discriminator —
# measured live the same day:
#     /api/v1/markets/San Antonio       200   multi-word, works
#     /api/v1/markets/Northern Virginia 200   multi-word, works
#     /api/v1/markets/Salt Lake City    200   multi-word, works
#     /api/v1/markets/Frankfurt         404   SINGLE-word, fails
#     /api/v1/markets/Tucson            404   SINGLE-word, fails
#     /api/v1/markets/Boardman          404   SINGLE-word, fails
# The real discriminator is membership in the curated dict. `.lower()
# .replace('-', ' ')` already handled case and hyphens, so every curated
# market answered in BOTH spellings and every non-curated one 404'd in both.
# The sample happened to pair multi-word markets with non-curated ones.
#
# The three sources spell their ids three different ways, which is why a
# single normalisation is needed rather than a per-source special case:
#     curated             'northern virginia'          (spaces)
#     US auto-discovered  'santa-clara'                (hyphens)
#     international       'ludwigshafen-am-rhein-de'   (hyphens + country)
# An agent writing the place name by hand produces none of those three. It
# writes `Ludwigshafen Am Rhein` — which is exactly the market's published
# `name`, so NAME is indexed alongside `id` rather than trying to guess the
# country suffix off a bare city.

import re as _re
import unicodedata as _ud

#: Anything a human or a slug generator uses to join words. Collapsing them
#: all to one space is what makes 'Santa Clara', 'santa-clara' and
#: 'santa_clara' one key rather than three.
_IDENT_SEPARATORS = _re.compile(r"[\s\-_,./]+")


def normalize_market_key(raw: str | None) -> str:
    """Fold any spelling of a market identifier onto one comparison key.

    Case, separator style and accents are all spellings of the same market,
    never distinctions between markets. Accent folding matters because the
    international slug builder lowercases the DB's city verbatim
    ('sao-paulo-br' from 'São Paulo'), while an agent types the unaccented
    form — without folding, one of the two spellings 404s.
    """
    s = _ud.normalize("NFKD", str(raw or ""))
    s = "".join(ch for ch in s if not _ud.combining(ch))
    return _IDENT_SEPARATORS.sub(" ", s.lower()).strip()


def build_identifier_index(markets) -> dict:
    """Map every published spelling of every market onto its record.

    Both `id` and `name` are indexed: the id is what our own links and
    `cite_url_template` emit, the name is what an agent writes by hand.

    CURATED WINS. A curated market and an auto-discovered one can normalise
    to the same key (both a curated 'columbus' and a discovered 'Columbus,
    OH' fold to `columbus`), and the curated row carries the hand-checked
    multi-city definition. `auto_discovered` is falsey on curated rows, so
    sorting on it puts them first, and first-writer-wins keeps them.

    Cities are deliberately NOT indexed. 'Aurora' belongs to both `chicago`
    and `denver`, so a city key would resolve to whichever market happened
    to sort first — an arbitrary answer is worse than an honest 404.
    """
    index: dict = {}
    for m in sorted(markets or (), key=lambda r: bool(r.get("auto_discovered"))):
        for raw in (m.get("id"), m.get("name")):
            key = normalize_market_key(raw)
            if key and key not in index:
                index[key] = m
    return index


def resolve_market_identifier(raw: str | None, markets):
    """Published market for any spelling of `raw`, or None.

    Accepts slug, display name and any casing. Returns the market record
    from `markets` itself, so a caller can never resolve to a market the
    list route does not publish.
    """
    key = normalize_market_key(raw)
    if not key:
        return None
    index = build_identifier_index(markets)
    hit = index.get(key)
    if hit is not None:
        return hit
    # FALLBACK ONLY — never ahead of a direct hit. DCPI_METRO_ALIASES maps
    # 'northern-virginia' -> 'ashburn', but `northern virginia` is ALSO a
    # curated market in its own right with a wider city set (Ashburn,
    # Loudoun, Sterling, Reston, Herndon, Manassas, Prince William,
    # Leesburg) than 'ashburn' (Ashburn, Loudoun). Consulting the alias map
    # first would silently re-point a market that already resolves and shrink
    # its published facility count. It runs only once a direct match fails,
    # which is where it earns its keep: 'bay-area', 'sv', 'south-bay' and the
    # retired twins ('columbus-oh', 'the-dalles-or') have no record of their
    # own and would otherwise 404.
    alias = canonical_slug(key.replace(" ", "-"))
    if alias:
        return index.get(normalize_market_key(alias))
    return None


def market_scope_sql(country, state):
    """Country/state guard for a resolved market's facility queries.

    Lives here rather than in main.py so it can be EXECUTED by a test —
    tests/ must not import main (the green-main convention), and a guard
    that can only read this predicate out of the AST cannot catch a wrong
    predicate, only a missing one.

    The guard used to be a hardcoded US literal, which was correct only
    because the only markets that resolved were the 34 curated US ones. Now
    that international markets resolve, a US-only guard would return zero
    facilities for every one of them — a 200 reading 0 MW, which is worse
    than the 404 it replaced. State narrows a US auto-discovered market to
    the (city, state) group the list route actually published, so
    `/api/v1/markets/<city>` cannot silently sum two same-named cities in
    different states.
    """
    if country and str(country).upper() not in ('US', 'USA'):
        return "AND country = %s", [country]
    guard = ("AND (country = 'US' OR country = 'USA' "
             "OR country IS NULL OR country = '')")
    if state:
        return guard + " AND UPPER(state) = %s", [str(state).upper()]
    return guard, []
