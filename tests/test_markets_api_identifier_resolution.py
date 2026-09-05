"""r-markets-api-ident (2026-09-05) — /api/v1/markets/<id> served a strict
SUBSET of the markets /api/v1/markets publishes.

THE BUG, measured live through the edge and cache-busted on 2026-09-05. The
list route publishes 132 markets and hands out `cite_url_template:
https://dchub.cloud/markets/{id}`. Four of the five ids it shows an anonymous
caller 404'd on the detail route that lists them:

    listed id `northern virginia`  -> /api/v1/markets/... 200
    listed id `london-gb`          -> 404
    listed id `singapore-sg`       -> 404
    listed id `tokyo-jp`           -> 404
    listed id `amsterdam-nl`       -> 404

The 404 body's own remediation text said "Call rank_markets (or GET
/api/v1/markets) for the full list" — which hands the agent exactly those
ids. A closed loop of bad advice. Found in origin HTTP logs: the single
AI-crawler 4xx in an 850-request sample was
`GET /api/v1/markets/Ludwigshafen Am Rhein` -> 404.

★ THE REPORTED SYMPTOM NAMED THE WRONG AXIS, which is why this file pins the
right one. It arrived as "multi-word markets 404, single-word ones work".
Word count is not the discriminator — measured the same day:

    /api/v1/markets/San Antonio       200   multi-word, works
    /api/v1/markets/Northern Virginia 200   multi-word, works
    /api/v1/markets/Salt Lake City    200   multi-word, works
    /api/v1/markets/Frankfurt         404   SINGLE-word, fails
    /api/v1/markets/Tucson            404   SINGLE-word, fails
    /api/v1/markets/Boardman          404   SINGLE-word, fails

`.lower().replace('-', ' ')` already handled case and hyphens, so every
CURATED market answered in both spellings and every non-curated one 404'd in
both. The real discriminator was membership in main.MARKET_ALIASES, a
hand-written dict of 34 US keys. A guard written to the reported symptom
(multi-word markets in both spellings) would have passed on the unfixed code
for San Antonio and stayed green for Frankfurt, so these cases carry BOTH
axes: word count and curated-ness, crossed.

WHAT IS GUARDED
  * test_resolves_* — the resolver itself, executed against a universe shaped
    exactly like build_market_universe's output.
  * test_the_detail_route_resolves_through_the_published_universe — read out
    of main.py's AST, because tests/ must not import main (green-main
    convention). This is the one that goes red if the resolver call is
    deleted from the route and the curated-dict gate comes back.
"""
import ast
import os

import pytest

from util.market_aliases import (  # noqa: E402
    build_identifier_index,
    market_scope_sql,
    normalize_market_key,
    resolve_market_identifier,
)

MAIN_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


# ---------------------------------------------------------------------------
# A universe shaped exactly like build_market_universe() returns: curated rows
# carry space-separated ids and no `auto_discovered`; US auto rows carry a
# hyphenated city slug + state; international rows carry '<city>-<country>'.
# ---------------------------------------------------------------------------
UNIVERSE = [
    # ── curated (main.MARKET_ALIASES) ──
    {"id": "ashburn", "name": "Ashburn",
     "cities": ["Ashburn", "Loudoun"]},
    {"id": "northern virginia", "name": "Northern Virginia",
     "cities": ["Ashburn", "Loudoun", "Sterling", "Reston"]},
    {"id": "san antonio", "name": "San Antonio", "cities": ["San Antonio"]},
    {"id": "salt lake city", "name": "Salt Lake City",
     "cities": ["Salt Lake City", "West Valley"]},
    {"id": "silicon valley", "name": "Silicon Valley",
     "cities": ["San Jose", "Santa Clara", "Sunnyvale"]},
    # ── US auto-discovered ──
    {"id": "santa-clara", "name": "Santa Clara", "cities": ["Santa Clara"],
     "auto_discovered": True, "state": "CA", "country": "US"},
    {"id": "tucson", "name": "Tucson", "cities": ["Tucson"],
     "auto_discovered": True, "state": "AZ", "country": "US"},
    {"id": "boardman", "name": "Boardman", "cities": ["Boardman"],
     "auto_discovered": True, "state": "OR", "country": "US"},
    # ── international auto-discovered ──
    {"id": "ludwigshafen-am-rhein-de", "name": "Ludwigshafen Am Rhein",
     "cities": ["Ludwigshafen Am Rhein"], "auto_discovered": True,
     "international": True, "country": "DE"},
    {"id": "frankfurt-de", "name": "Frankfurt", "cities": ["Frankfurt"],
     "auto_discovered": True, "international": True, "country": "DE"},
    {"id": "london-gb", "name": "London", "cities": ["London"],
     "auto_discovered": True, "international": True, "country": "GB"},
    {"id": "sao-paulo-br", "name": "São Paulo", "cities": ["São Paulo"],
     "auto_discovered": True, "international": True, "country": "BR"},
]


def test_the_universe_fixture_is_not_empty():
    """Every parametrised case below resolves against UNIVERSE. If it were
    empty or mis-shaped they would all fail for the wrong reason, or a
    negative-case test would pass vacuously."""
    assert len(UNIVERSE) == 12
    assert all(m.get("id") and m.get("name") and m.get("cities")
               for m in UNIVERSE)
    # Both id spellings must be present, or the crossing below is not a
    # crossing.
    assert any(" " in m["id"] for m in UNIVERSE)
    assert any("-" in m["id"] for m in UNIVERSE)


# ---------------------------------------------------------------------------
# The reported axis (word count) crossed with the real one (curated-ness).
# Every case is given in BOTH spellings a machine reasonably guesses: the
# human-readable display name and the hyphenated slug, each in the casing an
# assistant writes and in lowercase.
# ---------------------------------------------------------------------------
CASES = [
    # (label, spellings, expected canonical id)
    ("multi-word / curated",
     ["Northern Virginia", "northern virginia",
      "Northern-Virginia", "northern-virginia"], "northern virginia"),
    ("multi-word / curated",
     ["San Antonio", "san antonio", "San-Antonio", "san-antonio"],
     "san antonio"),
    ("multi-word / curated",
     ["Salt Lake City", "salt lake city", "Salt-Lake-City",
      "salt-lake-city"], "salt lake city"),
    ("multi-word / US auto-discovered",
     ["Santa Clara", "santa clara", "Santa-Clara", "santa-clara"],
     "santa-clara"),
    ("multi-word / international",
     ["Ludwigshafen Am Rhein", "ludwigshafen am rhein",
      "Ludwigshafen-Am-Rhein", "ludwigshafen-am-rhein",
      "ludwigshafen-am-rhein-de"], "ludwigshafen-am-rhein-de"),
    # ★ SINGLE-word non-curated — the cases the reported "multi-word" framing
    # would have left 404ing.
    ("single-word / US auto-discovered",
     ["Tucson", "tucson", "TUCSON"], "tucson"),
    ("single-word / US auto-discovered",
     ["Boardman", "boardman"], "boardman"),
    ("single-word / international",
     ["Frankfurt", "frankfurt", "frankfurt-de"], "frankfurt-de"),
    ("single-word / international",
     ["London", "london", "london-gb"], "london-gb"),
    # Accent folding: the slug builder lowercases the DB's city verbatim, so
    # the published id keeps the diacritic while an agent types it plain.
    ("multi-word / international / accented",
     ["Sao Paulo", "sao-paulo", "São Paulo", "sao paulo"], "sao-paulo-br"),
]

_PARAMS = [
    pytest.param(spelling, expected, id=f"{label}::{spelling}")
    for label, spellings, expected in CASES
    for spelling in spellings
]


@pytest.mark.parametrize("spelling,expected_id", _PARAMS)
def test_resolves_every_spelling_to_the_published_id(spelling, expected_id):
    """Slug, display name and any casing all reach the same published row."""
    hit = resolve_market_identifier(spelling, UNIVERSE)
    assert hit is not None, f"{spelling!r} did not resolve (this is the 404)"
    assert hit["id"] == expected_id


def test_single_word_and_multi_word_markets_fail_and_pass_together():
    """The axis pin. Before the fix these two groups behaved differently only
    because of curated-ness; word count never separated them and must not
    start to."""
    single = [resolve_market_identifier(s, UNIVERSE)
              for s in ("Tucson", "Frankfurt", "Boardman", "London")]
    multi = [resolve_market_identifier(s, UNIVERSE)
             for s in ("Santa Clara", "Ludwigshafen Am Rhein",
                       "San Antonio", "Salt Lake City")]
    assert all(h is not None for h in single), "single-word markets 404"
    assert all(h is not None for h in multi), "multi-word markets 404"


def test_an_unknown_market_still_404s():
    """The resolver must not become a yes-machine — an honest 404 is the
    correct answer for a market we do not publish, and every positive case
    above would be worthless if everything resolved."""
    for junk in ("Zzzz Not A Real Market", "", None, "   ", "-"):
        assert resolve_market_identifier(junk, UNIVERSE) is None


def test_a_market_can_only_resolve_to_a_row_the_list_publishes():
    """The subset bug, inverted. Whatever comes back is one of the rows that
    were passed in, so the detail route cannot serve a market the list route
    does not."""
    ids = {m["id"] for m in UNIVERSE}
    for _, spellings, _expected in CASES:
        for spelling in spellings:
            hit = resolve_market_identifier(spelling, UNIVERSE)
            assert hit is not None and hit["id"] in ids
    # ...and with nothing published, nothing resolves.
    assert resolve_market_identifier("Ashburn", []) is None


def test_curated_wins_a_normalised_collision():
    """A curated row and an auto-discovered one can fold to the same key. The
    curated row carries the hand-checked multi-city definition, so it must
    win — otherwise this change would silently shrink a market's facility
    count while 'fixing' its 404."""
    universe = [
        {"id": "columbus-oh", "name": "Columbus", "cities": ["Columbus"],
         "auto_discovered": True, "state": "OH", "country": "US"},
        {"id": "columbus", "name": "Columbus",
         "cities": ["Columbus", "New Albany", "Dublin", "Westerville"]},
    ]
    hit = resolve_market_identifier("Columbus", universe)
    assert hit["id"] == "columbus"
    assert len(hit["cities"]) == 4


def test_an_alias_never_outranks_a_market_that_resolves_directly():
    """DCPI_METRO_ALIASES maps 'northern-virginia' -> 'ashburn', but
    `northern virginia` is a curated market in its own right with a wider
    city set. Consulting the alias map ahead of a direct hit would re-point a
    market that already worked and shrink what it publishes."""
    hit = resolve_market_identifier("northern-virginia", UNIVERSE)
    assert hit["id"] == "northern virginia"
    assert len(hit["cities"]) == 4


def test_an_alias_with_no_row_of_its_own_still_resolves():
    """'silicon-valley' is curated here; 'bay-area' and 'sv' have no record
    at all and only reach a market through the alias fallback."""
    for alias in ("bay-area", "Bay Area", "sv", "south-bay"):
        hit = resolve_market_identifier(alias, UNIVERSE)
        assert hit is not None, alias
        assert hit["id"] == "santa-clara"


def test_normalisation_folds_only_spelling_not_identity():
    """Separator style, case and accents are spellings; distinct words are
    not."""
    assert (normalize_market_key("Santa Clara")
            == normalize_market_key("santa-clara")
            == normalize_market_key("santa_clara")
            == "santa clara")
    assert normalize_market_key("São Paulo") == "sao paulo"
    assert normalize_market_key("  Salt  Lake  City ") == "salt lake city"
    # Different markets must not collide.
    assert normalize_market_key("columbus") != normalize_market_key("columbia")
    assert (normalize_market_key("frankfurt")
            != normalize_market_key("frankfurt-de"))


def test_cities_are_not_indexed():
    """'Aurora' is a city of both chicago and denver. Indexing cities would
    resolve it to whichever sorted first — an arbitrary answer dressed as a
    200. An honest 404 is correct."""
    index = build_identifier_index(UNIVERSE)
    assert "loudoun" not in index
    assert "sunnyvale" not in index
    assert resolve_market_identifier("Sunnyvale", UNIVERSE) is None


# ---------------------------------------------------------------------------
# The scope guard. Executed, not read — a US-only literal against an
# international market returns a 200 that reads 0 MW, which is worse than the
# 404 it replaced.
# ---------------------------------------------------------------------------
def test_an_international_market_is_scoped_to_its_own_country():
    sql, params = market_scope_sql("DE", None)
    assert params == ["DE"]
    assert "country = %s" in sql
    assert "'US'" not in sql, "a US literal here returns 0 rows for Frankfurt"


def test_a_us_market_keeps_the_iso_collision_guard():
    """AZ is both Arizona and Azerbaijan; dropping the US guard re-opens that."""
    sql, params = market_scope_sql(None, None)
    assert params == []
    assert "country = 'US'" in sql
    sql_usa, params_usa = market_scope_sql("USA", None)
    assert params_usa == [] and "country = 'US'" in sql_usa


def test_a_us_market_with_a_state_is_scoped_to_that_state():
    """Two same-named cities in different states are two rows in the list
    route's (city, state) grouping, so the detail route must not sum them."""
    sql, params = market_scope_sql("US", "OR")
    assert params == ["OR"]
    assert "UPPER(state) = %s" in sql
    assert "country = 'US'" in sql


def test_every_scope_guard_has_one_placeholder_per_param():
    """psycopg2 binds positionally; a guard whose %s count and param count
    disagree raises a binding error that names no table or column."""
    for country, state in [(None, None), ("US", None), ("USA", "VA"),
                           ("DE", None), ("GB", "XX"), ("US", "OR")]:
        sql, params = market_scope_sql(country, state)
        assert sql.count("%s") == len(params), (country, state, sql)


# ---------------------------------------------------------------------------
# The route. Read out of the AST — tests/ must not import main.
# ---------------------------------------------------------------------------
def _function_ast(name):
    tree = ast.parse(open(MAIN_PY, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in main.py")


def _calls(node):
    return {
        n.func.id if isinstance(n.func, ast.Name) else
        (n.func.attr if isinstance(n.func, ast.Attribute) else "")
        for n in ast.walk(node) if isinstance(n, ast.Call)
    }


def test_the_detail_route_resolves_through_the_published_universe():
    """★ THE MUTATION TARGET. Deleting the resolver call from
    get_market_stats — the whole of this fix — turns this red.

    Read from the AST rather than by grep: a `resolve_market_identifier`
    mentioned only in a comment or a docstring is invisible here, which is
    the point. A grep version passes on a route that merely describes the
    fix."""
    calls = _calls(_function_ast("get_market_stats"))
    assert "resolve_market_identifier" in calls, (
        "get_market_stats no longer resolves the identifier — it is back to "
        "matching MARKET_ALIASES, which 404s every non-curated market")
    assert "build_market_universe" in calls, (
        "get_market_stats no longer reads the published universe, so it can "
        "drift back into serving a subset of what /api/v1/markets lists")


def test_the_list_route_reads_the_same_universe_builder():
    """Anti-drift: one builder, both routes. Two builders is how the detail
    route came to publish 34 markets while the list published 132."""
    assert "build_market_universe" in _calls(_function_ast("list_markets"))


def test_the_404_body_samples_ids_that_actually_resolve():
    """The 404 tells the agent to fetch the market list. It used to sample
    MARKET_ALIASES while pointing at a list route returning different ids, so
    following the advice produced another 404."""
    node = _function_ast("get_market_stats")
    src = ast.get_source_segment(open(MAIN_PY, encoding="utf-8").read(), node)
    assert "_valid = sorted(\n                    str(m.get('id'))" in src, (
        "the 404 sample is no longer built from the published universe")


# ---------------------------------------------------------------------------
# /api/v1/markets/compare — same subset defect, plus a substring bleed the
# detail route had already fixed.
#
# Measured live 2026-09-05, cache-busted:
#     ?markets=london-gb,tokyo-jp   -> 404   (ids /api/v1/markets publishes)
#     ?markets=Santa Clara,Ashburn  -> 404
#     ?markets=reno,ashburn         -> 200, reno = 43 facilities
#     /api/v1/markets/reno          -> 200, reno = 22 facilities
#
# Compare matched `city ILIKE '%reno%'` with NO country predicate at all, so
# it counted Grenoble and every other namesake on earth — r43-H fixed exactly
# this in get_market_stats on 2026-05-27 and never reached compare. Giving
# compare the resolver WITHOUT the predicate would have spread that bleed to
# every international market it can now reach, so both move together.
# ---------------------------------------------------------------------------
def _compare_src():
    src = open(MAIN_PY, encoding="utf-8").read()
    seg = ast.get_source_segment(src, _function_ast("compare_markets"))
    assert seg and len(seg) > 1500, "compare_markets slice looks wrong"
    # Read the RIGHT thing: these anchor the slice to the function whose
    # predicate this test is about, so the assertions below cannot pass on an
    # empty or mis-sliced string.
    assert "markets_param" in seg and "comparison.append" in seg
    return seg


def test_compare_resolves_through_the_published_universe():
    """★ MUTATION TARGET. Deleting the resolver from compare_markets puts it
    back to 34 curated markets while /api/v1/markets lists 132."""
    calls = _calls(_function_ast("compare_markets"))
    assert "resolve_market_identifier" in calls, (
        "compare_markets no longer resolves identifiers — london-gb, "
        "santa-clara and every other published non-curated id 404 again")
    assert "build_market_universe" in calls


def test_compare_does_not_substring_match_city_names():
    """The bleed. `city ILIKE '%reno%'` matched Grenoble; live it published
    reno at 43 facilities against the detail route's 22."""
    seg = _compare_src()
    assert "f'%{city}%'" not in seg and 'f"%{city}%"' not in seg, (
        "compare_markets is substring-matching city names again — this "
        "double-counts every namesake city on earth")
    assert "LOWER(city) = LOWER(%s)" in seg, (
        "compare_markets lost the exact city match")


def test_compare_scopes_every_facility_query_by_country():
    """Compare carried no country predicate at all. With international
    markets now reachable, an unscoped query is how 'london' starts counting
    London, Ontario."""
    seg = _compare_src()
    assert "market_scope_sql" in seg, "compare_markets has no country scope"
    # Both facility queries — the metrics aggregate and the top-providers
    # roll-up — must carry it, not just the first.
    assert seg.count("{_scope_sql}") >= 2, (
        f"only {seg.count('{_scope_sql}')} of compare's 2 facility queries "
        "carry the scope guard")


def test_compare_keeps_the_callers_raw_spelling_for_resolution():
    """The eager `.lower().replace('-', ' ')` destroyed the hyphenated ids the
    list route publishes: 'london-gb' arrived as 'london gb', which matches
    nothing."""
    seg = _compare_src()
    assert "[m.strip() for m in markets_param.split(',')]" in seg, (
        "compare_markets is normalising identifiers before resolving them, "
        "which breaks the '<city>-<country>' ids /api/v1/markets publishes")
