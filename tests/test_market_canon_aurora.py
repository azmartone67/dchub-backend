"""Guards for the aurora name-twin canon fix (r-aurora-canon, 2026-08-02).

Same mechanism as r-portland-canon (see test_market_canon_portland.py),
INVERTED outcome — which is the whole reason this file exists separately.

The incident: _load_markets_dynamic groups by (LOWER(city), city, state), so
Aurora IL (22 fac / 158 MW, Chicago metro) and Aurora CO (12 fac / 51 MW,
Denver metro) were BOTH emitted under slug 'aurora'. The scoring loop does
`UPDATE ... WHERE market_slug=%s` per market, so only the market written LAST
survives; the loader orders facility_count DESC, so the SMALLER city always
writes last and takes the slug. That is why the single live market_power_scores
row said state=CO while 76% of the MW behind it (158 of 209) is Illinois, and
why /markets/aurora rendered "29 facilities, 209 MW" — the UNION of both
Auroras — with CyrusOne, an Illinois operator, as its top operator.

Why the outcome inverts vs Portland: bare 'portland' was RETIRED because a
hardcoded 'portland-or' row already owned Oregon, so the bare slug became an
alias. Aurora has no hardcoded twin and no corroborating surface at all (no
curated /markets/aurora page, no main.py vocab entry, and main.MARKET_ALIASES
claims 'Aurora' for BOTH 'chicago' and 'denver'). Owner decision: bare 'aurora'
MEANS ILLINOIS and stays a real published market; Colorado is minted
'aurora-co' / 'Aurora, CO'. So the Portland-shaped retirement machinery must
NOT be applied here — pinned below, because applying it "for symmetry" would
orphan the market this fix just canonicalized.

Source-level + pure imports only; never imports main or routes/* (routes/dcpi
builds MARKETS at import time, which needs a DB — house rule).
"""

import ast

from tests import _market_canon_consts as _canon_consts
import pathlib
import re

from util.market_aliases import (
    DCPI_METRO_ALIASES,
    REDUNDANT_TWIN_SLUGS,
    canonical_slug,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _src(rel):
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _tree(rel):
    return ast.parse(_src(rel))


def _norm(s):
    return re.sub(r"\s+", " ", s)


def _assigned_literal(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return ast.literal_eval(node.value)
    raise AssertionError(f"no module-level literal assignment for {name}")


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _dis():
    return _assigned_literal(_tree("routes/dcpi.py"),
                             "_CITY_MARKET_DISAMBIGUATION")


# ── routes/dcpi.py: Colorado is minted state-suffixed, Illinois keeps bare ──
def test_city_market_disambiguation_covers_aurora_colorado():
    assert _dis()[("aurora", "CO")] == ("aurora-co", "Aurora, CO")


def test_illinois_keeps_the_bare_aurora_slug():
    """The owner decision is directional: the MINORITY market (CO) is the one
    that gets state-suffixed. An ("aurora","IL") entry would mean Illinois had
    been suffixed too, leaving bare 'aurora' unminted — the indexed
    /markets/aurora and /dcpi/aurora URLs would go to a market that no longer
    exists."""
    dis = _dis()
    assert ("aurora", "IL") not in dis, (
        "Illinois was state-suffixed as well — bare 'aurora' is then minted "
        "by nobody and the live indexed URLs strand"
    )


# ── the INVERSION: bare 'aurora' is a real market, not a retired twin ───────
def test_bare_aurora_is_not_aliased_away():
    """Portland's bare slug became an alias for a hardcoded Oregon row. Aurora
    has no hardcoded twin — bare 'aurora' IS the Illinois market. An alias
    entry here would redirect a real published market onto a slug that has no
    market_power_scores row at all."""
    assert canonical_slug("aurora") == "", (
        "bare 'aurora' picked up a DCPI_METRO_ALIASES entry — it is the "
        "canonical Illinois market, not an alias for anything"
    )
    assert "aurora" not in DCPI_METRO_ALIASES
    assert "aurora-co" not in DCPI_METRO_ALIASES


def test_neither_aurora_is_a_retired_twin():
    """r-twin-unpublish retires REDUNDANT_TWIN_SLUGS on every recompute.
    'aurora' (Illinois) and 'aurora-co' (Colorado) are two DIFFERENT real
    markets, so retiring either unpublishes live data."""
    assert "aurora" not in REDUNDANT_TWIN_SLUGS
    assert "aurora-co" not in REDUNDANT_TWIN_SLUGS


def test_aurora_pages_do_not_301():
    """Both markets get their own indexable /markets URL. A redirect here
    would collapse two distinct metros (Chicago-metro and Denver-metro) onto
    one page — the exact conflation this fix removes."""
    # r-market-canon-split (2026-09-05): MARKETS_CANONICAL_REDIRECT is derived
    # from util.market_aliases now, so there is no literal to eval.
    redirects = _canon_consts.canonical_redirect()
    assert "aurora" not in redirects
    assert "aurora-co" not in redirects


# ── generic guards: adding ONE table entry must remain sufficient ───────────
def test_no_disambiguated_slug_is_also_retired_or_aliased():
    """Cross-table coherence for EVERY entry, not just Aurora's: a slug the
    loader is told to mint must not simultaneously be retired by
    r-twin-unpublish or aliased onto some other market. Either combination
    mints a row on every recompute and then immediately throws it away."""
    for (bare, state), (slug, name) in _dis().items():
        assert slug != bare and slug and name, (bare, state)
        assert slug not in REDUNDANT_TWIN_SLUGS, (
            f"{slug} is minted by the disambiguation AND retired as a twin"
        )
        assert slug not in DCPI_METRO_ALIASES, (
            f"{slug} is minted by the disambiguation AND aliased away"
        )


def test_self_heal_iterates_the_whole_disambiguation_table():
    """The recompute self-heal must LOOP over _CITY_MARKET_DISAMBIGUATION, not
    hardcode a slug. This is precisely what made Aurora a one-entry fix — if
    the loop is ever specialized back to 'portland', a new entry silently
    stops self-healing and leaves the stale bare row in place forever."""
    fn = _func(_tree("routes/dcpi.py"), "recompute_all_scores")
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)
             and any(isinstance(x, ast.Name)
                     and x.id == "_CITY_MARKET_DISAMBIGUATION"
                     for x in ast.walk(n.iter))]
    assert loops, (
        "recompute self-heal no longer iterates _CITY_MARKET_DISAMBIGUATION "
        "— added entries will not rename their pre-existing bare row"
    )


def test_disambiguation_is_applied_before_the_iso_resolve():
    """The slug-keyed ISO lookup must see the FINAL slug. Applied after,
    resolve_iso() would be handed the bare form and could hand a Denver-metro
    market a PJM grid (or vice versa) off the Chicago-metro slug."""
    src = _src("routes/dcpi.py")
    fn = _func(ast.parse(src), "_load_markets_dynamic")
    seg = ast.get_source_segment(src, fn)
    assert "_CITY_MARKET_DISAMBIGUATION" in seg and "_resolve_iso" in seg
    assert seg.index("_CITY_MARKET_DISAMBIGUATION") < seg.index("_resolve_iso("), (
        "the ISO resolve now runs before the slug override — a disambiguated "
        "market can be handed the bare slug's grid"
    )


def test_both_loader_mint_branches_apply_the_disambiguation():
    """_load_markets_dynamic mints in TWO live shapes (dict + tuple). One
    un-patched branch re-mints the bare Colorado row on the next boot."""
    fn = _func(_tree("routes/dcpi.py"), "_load_markets_dynamic")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "_CITY_MARKET_DISAMBIGUATION"]
    assert len(calls) >= 2, (
        "fewer than 2 mint branches consult _CITY_MARKET_DISAMBIGUATION — "
        "the un-patched branch will re-mint the bare Colorado 'aurora' row"
    )
