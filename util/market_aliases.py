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


def resolve_market_list(raw_markets, universe, curated=None):
    """Partition requested identifiers into (resolved, unresolved).

    r-markets-api-ident (2026-09-05). Pure, and therefore EXECUTABLE by a
    test — which is the whole reason it exists as a function instead of a
    loop inlined in main.generate_market_pdf. The property it carries is
    "nothing is dropped in silence", and a source-level guard cannot prove
    that: asserting the word "unresolved" appears in the builder stays green
    when `unresolved.append(...)` is replaced by `pass`, because the word
    survives in the declaration one line above. That mutation survived a
    substring-based version of this guard, so the check moved here where a
    test can watch the list actually fill.

    `curated` is the no-DB fast path (main.MARKET_ALIASES); anything not in
    it resolves against the published `universe`. Every input appears in
    exactly one of the two outputs — that total is the invariant.
    """
    curated = curated or {}
    resolved, unresolved = [], []
    for raw in (raw_markets or ()):
        raw = str(raw)
        key = raw.lower().replace('-', ' ')
        if key in curated:
            resolved.append({'id': key,
                             'name': key.replace('_', ' ').title(),
                             'cities': list(curated[key])})
            continue
        hit = resolve_market_identifier(raw, universe)
        if hit is None or not (hit.get('cities') or []):
            unresolved.append(raw)
        else:
            resolved.append(hit)
    return resolved, unresolved


#: Rendered when a requested market is not tracked. Named so a test can
#: assert on the emitted line rather than on the template's presence in the
#: source — an `if False:` around the render leaves the template in place.
COVERAGE_GAP_PREFIX = "Not covered: "


def report_coverage_lines(resolved, unresolved):
    """The header lines a market report must carry, given what it resolved.

    Returns the "Markets:" line and, when anything failed to resolve, the
    "Not covered:" line naming it. The builder used to join the CALLER's
    list into the title and silently omit the bodies it could not produce,
    so a Pro customer's PDF asserted coverage it did not have. Building both
    lines from the SAME partition is what makes that unrepresentable.
    """
    lines = ["Markets: " + (", ".join(m.get('name') or str(m.get('id'))
                                      for m in (resolved or [])) or "none")]
    if unresolved:
        lines.append(
            COVERAGE_GAP_PREFIX + ", ".join(str(u) for u in unresolved)
            + " — no market by that name is tracked by DC Hub. "
              "See https://dchub.cloud/api/v1/markets for every market ID.")
    return lines


# ═══════════════════════════════════════════════════════════════════════
# FACILITY MARKET CANONICALISATION — r-market-alias-split (2026-09-18)
# ═══════════════════════════════════════════════════════════════════════
# `discovered_facilities.market` is free text from ingestion, not a slug
# vocabulary. 2,316 distinct values reach an operator brief's top-5 table,
# and the same metro arrives under several spellings, so a
# `GROUP BY COALESCE(market, city)` renders one metro as two rows.
# Measured live against production 2026-09-18, with the route's own
# predicate (`is_duplicate = 0`):
#
#   Equinix          'Frankfurt'          20 facilities  240.0 MW
#   Equinix          'Frankfurt Am Main'   4 facilities   36.0 MW
#   Digital Realty   'Frankfurt'          17 facilities   80.0 MW
#   Digital Realty   'Frankfurt Am Main'  12 facilities  132.0 MW
#   Digital Realty   'Frankfurt am Main'   1 facility      0.0 MW
#
# Digital Realty's Frankfurt position is 30 facilities / 212 MW published
# as THREE rows, two of which differ only in the case of "am". Each row's
# `share_pct` is computed against the operator's whole footprint, so every
# one of them understates the metro, and each mints a different
# `market_slug` — two links to two different market pages for one metro.
#
# ★ NO DERIVED RULE IS SAFE HERE, which is why this map is hand-curated.
# A leading-whole-token containment rule (the shape
# `feedback_equality_match_misses_the_metro_alias` prescribes for slug
# MATCHING) folds `Frankfurt` into `Frankfurt Am Main` correctly, but the
# same rule, measured over the published values on 2026-09-18, also folds:
#
#   'Colorado'  + 'Colorado Springs'   — a state and a city inside it
#   'Mexico'    + 'Mexico City'        — a country and its capital
#   'Porto'(PT) + 'Porto Alegre'(BR)   — different countries
#   'Santiago'(DO) + 'Santiago De Chile'(CL) + 'Santiago de Cali'(CO)
#   'Texas' + 'Texas Regional'         — 'Texas Regional' is a state
#                                        rollup, not a metro at all
#
# Gating the rule on country does not save it: Colorado/Colorado Springs
# and Mexico/Mexico City are both same-country. So containment is used
# only to FIND candidates for review; nothing folds without an entry here.
#
#: Normalised market key → canonical DCPI market slug. Every target was
#: confirmed `published = true` in market_power_scores on 2026-09-18 — an
#: alias pointing at an unpublished slug would move a market's facilities
#: onto a page that renders empty, which is worse than the split.
FACILITY_MARKET_ALIASES: dict[str, str] = {
    # Frankfurt — the reported split. 'am Main' is the city's full legal
    # name (Frankfurt am Main); DE ingestion emits both forms.
    'frankfurt am main':        'frankfurt',
    # Washington DC — four spellings, and bare 'washington' is already a
    # retired twin above (-> 'dc'). 'Area' is an ingestion suffix.
    'washington dc':            'dc',
    'washington dc area':       'dc',
    'washington d c area':      'dc',
    # Metro names that carry a second city or a suburb.
    'minneapolis st paul':      'minneapolis',
    'raleigh durham':           'raleigh',
    'sydney olympic park':      'sydney',      # suburb of Sydney
    'piscataway township':      'piscataway',
    # Administrative subdivisions of one metro (Jakarta's five kota).
    'jakarta selatan':          'jakarta',
    'jakarta utara':            'jakarta',
    'jakarta timur':            'jakarta',
    'jakarta pusat':            'jakarta',
    'jakarta barat':            'jakarta',
    # Ward-level Japanese addresses that landed in the market column.
    'osaka shi kita ku':        'osaka',
    # Country/region suffixes on a city that is already its own market.
    'hong kong sar':            'hong kong',
    'quincy wa':                'quincy',
    'birmingham al':            'birmingham',
}


def market_group_key(raw: str | None) -> str:
    """Grouping key for a raw `discovered_facilities.market` value.

    Folds the two things that are spellings rather than distinctions —
    case/separator/accent (via `normalize_market_key`, which is what makes
    'São Paulo' and 'Sao Paulo' one market) and a curated metro alias —
    then applies DCPI_METRO_ALIASES so the key matches the slug the rest of
    DC Hub resolves through.

    ★ FOLD BEFORE YOU LIMIT. Callers must apply this to the FULL market
    list and take their top N afterwards. Folding the rows that survived a
    `LIMIT 5` turns two of the five into one and returns four markets — and
    a metro whose two halves each rank 6th and 7th never rises to the top 5
    at all, which is the defect this exists to fix.

    Returns '' for a blank input so a caller can drop it.
    """
    key = normalize_market_key(raw)
    if not key:
        return ''
    key = FACILITY_MARKET_ALIASES.get(key, key)
    slug = key.replace(' ', '-')
    return DCPI_METRO_ALIASES.get(slug, slug)


def canonical_market_slug(raw: str | None, published_slugs) -> str:
    """Published market slug for `raw`, or '' when none is published.

    `published_slugs` is the set of `market_power_scores.market_slug` values
    with `published = true` — the markets that actually have a page. The
    empty return is the point: `/markets/<slug>/brief` answers **HTTP 200
    for any slug at all**, rendering a shell whose only market-specific text
    is the slug echoed into the <h1>. Measured live 2026-09-18:

        /markets/frankfurt/brief          <title>Frankfurt Market Brief · DC Hub</title>
        /markets/frankfurt-am-main/brief  <title>Market Brief · DC Hub</title>
        /markets/las-cruces/brief         <title>Market Brief · DC Hub</title>
        /markets/pennsylvania/brief       <title>Market Brief · DC Hub</title>

    so a link cannot be validated by its status code, and a caller that
    emits `/markets/<any slug>/brief` cannot tell a market page from a dead
    one. Of the 2,267 distinct slugs the operator briefs linked to on
    2026-09-18, 2,002 (88.3%) had no published row — 35,696 MW of linked
    capacity pointing at empty shells. Resolving against the published set
    is what makes the link honest; the caller suppresses the link on ''.
    """
    key = market_group_key(raw)
    if not key:
        return ''
    return key if key in (published_slugs or ()) else ''


def fold_market_rows(rows, sort_idx: int = 1):
    """Collapse spelling/alias variants of one metro into a single row.

    `rows` are `(market_label, *numeric)` — every position after the label
    is summed, so the same function serves a `(m, n)` chip list and a
    `(m, n, mw, mw_n)` concentration table without a second copy drifting
    from this one. (A hand-copied shared map is what produced the six
    divergent state→ISO maps this module's header describes.)

    ★ FOLD BEFORE YOU LIMIT. Apply this to the FULL market list and take
    the top N afterwards. Folding the rows that survived a `LIMIT 5` takes
    the top five SPELLINGS: two of them collapse into one and four markets
    come back, and a metro whose halves rank 6th and 7th never enters the
    table at all — which is the defect this exists to fix.

    The label kept is the spelling carrying the most rows (the one a reader
    is most likely to recognise), with the alphabetically-first spelling
    breaking ties so input order cannot decide the rendered name.

    `sort_idx` NAMES the ranking column and is deliberately not inferred.
    An earlier draft ranked by the last numeric column, which for the
    concentration table's `(m, n, mw, mw_n)` shape is `mw_n` — the sparse-MW
    DENOMINATOR added by #4714, not the MW itself. That silently reordered
    the table by how many rows reported capacity rather than by capacity:
    a 10 MW market with 9 reporting rows outranked a 500 MW market with 1.
    Callers pass 2 for MW, 1 (the default) for a facility count.
    """
    merged: dict = {}
    for r in rows or ():
        label = r[0]
        key = market_group_key(label) or (label or "")
        nums = []
        for v in r[1:]:
            try:
                nums.append(float(v or 0))
            except (TypeError, ValueError):
                nums.append(0.0)
        slot = merged.get(key)
        if slot is None:
            merged[key] = [nums, {(label or ""): nums[0] if nums else 0.0}]
            continue
        slot[0] = [a + b for a, b in zip(slot[0], nums)]
        spell = slot[1]
        spell[label or ""] = spell.get(label or "", 0.0) + (nums[0] if nums else 0.0)
    out = []
    for key, (nums, spellings) in merged.items():
        label = min(spellings.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        out.append((label, *nums))
    if not 1 <= sort_idx < max((len(r) for r in out), default=2):
        raise ValueError(f"sort_idx {sort_idx} is not a numeric column")
    out.sort(key=lambda r: (-(r[sort_idx] or 0), -(r[1] or 0), r[0]))
    return out
