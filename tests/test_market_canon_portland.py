"""Guards for the portland/portland-or name-twin canon fix (r-portland-canon,
2026-08-02).

The incident: market_power_scores carried TWO published rows named
'Portland' — bare 'portland' (Portland, MAINE; minted by the dynamic
LOWER(city) loader) and 'portland-or' (Portland, OREGON; hardcoded). Two
consequences, both pinned here:

  1. _gather_market_facts resolved by slug OR name ordered on computed_at
     alone, so generate_for_market('portland') landed on whichever twin the
     recompute wrote LAST — and then upserted the brief under facts["slug"],
     i.e. the OTHER market's row. The nightly cron targeting 'portland'
     rewrote 'portland-or' forever; the requested page's brief could never
     refresh (st.-louis class, 702a7bd0).
  2. Every other surface (main.py market vocab, curated /markets/portland =
     Portland-Hillsboro, market-brief seeds) treats bare 'portland' as
     OREGON, so a Maine row under that slug was mislabeled everywhere it
     appeared. Maine is now minted 'portland-me' / 'Portland, ME'
     (_CITY_MARKET_DISAMBIGUATION) with a recompute self-heal rename.

Also pinned: the /markets flagship-staleness fix — cron_rotate must reach the
sitemapped metro-canon pages (northern-virginia, silicon-valley) whose mps
rows are deliberately retired, and must measure staleness against the PAGE
row generation actually writes, or the portland-or slot pins NULLS FIRST
forever (guard-starvation class).

Source-level + pure imports only; never imports main or routes/* (routes/dcpi
builds MARKETS at import time, which needs a DB — house rule).
"""

import ast
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
    """The ast.literal_eval of `name = <literal>` at module level (plain or
    annotated assignment — MARKET_ALIAS uses `: dict[str, str]`)."""
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


def _str_constants(node):
    return [c.value for c in ast.walk(node)
            if isinstance(c, ast.Constant) and isinstance(c.value, str)]


# ── util/market_aliases: bare 'portland' is an Oregon alias ─────────────
def test_bare_portland_aliases_to_portland_or():
    assert canonical_slug("portland") == "portland-or"
    assert canonical_slug("portland-hillsboro") == "portland-or"


def test_bare_portland_is_a_retired_twin():
    """A resurrected bare-'portland' row (hand insert, orphan re-adopt) is
    junk by definition — every consumer reads the slug as Oregon — so the
    recompute's r-twin-unpublish must retire it."""
    assert "portland" in REDUNDANT_TWIN_SLUGS
    # and the alias target must not itself be retired (no twin -> twin chain)
    assert DCPI_METRO_ALIASES["portland"] not in REDUNDANT_TWIN_SLUGS


# ── routes/dcpi.py: Maine is minted disambiguated, and self-heals ───────
def test_city_market_disambiguation_covers_portland_maine():
    dis = _assigned_literal(_tree("routes/dcpi.py"),
                            "_CITY_MARKET_DISAMBIGUATION")
    assert dis[("portland", "ME")] == ("portland-me", "Portland, ME")
    # every disambiguated slug must differ from the bare form it replaces,
    # or the override is a no-op that leaves the collision in place
    for (bare, _state), (slug, name) in dis.items():
        assert slug != bare and slug and name


def test_both_loader_mint_branches_apply_the_disambiguation():
    """_load_markets_dynamic mints market tuples in TWO live shapes (dict +
    tuple). The st.-louis period-slug fix had to patch both; so does this —
    one un-patched branch re-mints the bare Maine twin on the next boot."""
    fn = _func(_tree("routes/dcpi.py"), "_load_markets_dynamic")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
             and isinstance(n.func.value, ast.Name)
             and n.func.value.id == "_CITY_MARKET_DISAMBIGUATION"]
    assert len(calls) >= 2, (
        "fewer than 2 mint branches consult _CITY_MARKET_DISAMBIGUATION — "
        "the un-patched branch will re-mint the bare 'portland' Maine row"
    )


def test_recompute_self_heals_the_bare_maine_row():
    """The rename must be guarded both ways (st.-louis 'never orphan'
    discipline): RENAME when the disambiguated row does not exist yet,
    DELETE the bare row when it does."""
    consts = [_norm(s) for s in _str_constants(_tree("routes/dcpi.py"))]
    assert any("UPDATE market_power_scores" in s
               and "SET market_slug = %s, market_name = %s" in s
               for s in consts), "self-heal RENAME statement missing"
    assert any("SELECT 1 FROM market_power_scores WHERE market_slug = %s" in s
               for s in consts), "self-heal existence guard missing"
    assert any("DELETE FROM market_power_scores" in s
               and "market_slug = %s AND state = %s" in s
               for s in consts), "self-heal DELETE-when-twin-exists missing"


# ── routes/market_deep_dive.py: deterministic resolve, page-slug persist ─
def test_facts_resolver_prefers_exact_slug_over_name_match():
    fn = _func(_tree("routes/market_deep_dive.py"), "_gather_market_facts")
    sqls = [_norm(s) for s in _str_constants(fn) if "market_power_scores" in s]
    assert any(
        "ORDER BY (LOWER(market_slug) = LOWER(%s)) DESC" in s for s in sqls
    ), (
        "_gather_market_facts orders on computed_at alone again — with a "
        "name-twin (two markets both named 'Portland') the resolution "
        "depends on recompute order, not on the slug that was asked for"
    )


def test_generate_persists_under_the_requested_page_slug():
    """Both INSERTs (neutral placeholder + real upsert) must key on
    page_slug, never facts["slug"] — facts["slug"] is whichever row the
    resolver landed on, which for a name-twin is the OTHER market."""
    fn = _func(_tree("routes/market_deep_dive.py"), "generate_for_market")
    inserts = []
    for call in ast.walk(fn):
        if not (isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "execute" and call.args):
            continue
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                and "INSERT INTO market_deep_dives" in first.value:
            inserts.append(call)
    assert len(inserts) == 2, f"expected 2 brief INSERTs, found {len(inserts)}"
    for call in inserts:
        params = call.args[1]
        assert isinstance(params, ast.Tuple), ast.dump(call)
        slug_param = params.elts[0]
        assert isinstance(slug_param, ast.Name) and slug_param.id == "page_slug", (
            "a market_deep_dives INSERT keys on something other than "
            "page_slug: " + ast.dump(slug_param)
        )


def test_page_canon_and_redirects_are_consistent():
    tree = _tree("routes/market_deep_dive.py")
    page_canon = _assigned_literal(tree, "MARKETS_DEEP_DIVE_PAGE_CANON")
    redirects = _assigned_literal(tree, "MARKETS_CANONICAL_REDIRECT")

    assert page_canon.get("portland-or") == "portland"
    # a slug whose generation is re-keyed must also 301 on /markets, or two
    # URLs serve the same brief (duplicate content, the r43-H class)
    for src_slug, dst in page_canon.items():
        assert redirects.get(src_slug) == dst

    # every retired twin's /markets URL must consolidate onto the recorded
    # canon (util/market_aliases) — EXCEPT the metro-canon flagships, whose
    # /markets page IS the sitemapped canonical and must keep serving 200
    flagship = {"northern-virginia", "silicon-valley"}
    for twin in REDUNDANT_TWIN_SLUGS - flagship - {"portland"}:
        assert redirects.get(twin) == canonical_slug(twin), (
            f"/markets/{twin} does not 301 to its canonical page"
        )
    for slug in flagship:
        assert slug not in redirects, f"flagship {slug} must not redirect"
    # bare 'portland' is the Oregon flagship PAGE, not a redirect source
    assert "portland" not in redirects
    # no redirect chains: a target is never itself a source
    chained = sorted(set(redirects.values()) & set(redirects))
    assert not chained, f"redirect chain via: {chained}"


def test_read_deep_dive_canonicalizes_the_slug():
    fn = _func(_tree("routes/market_deep_dive.py"), "read_deep_dive")
    reads_canon = any(
        isinstance(n, ast.Attribute) and n.attr == "get"
        and isinstance(n.value, ast.Name)
        and n.value.id == "MARKETS_DEEP_DIVE_PAGE_CANON"
        for n in ast.walk(fn))
    assert reads_canon, (
        "read_deep_dive no longer canonicalizes — API reads and facility-"
        "page splices keyed 'portland-or' go dark once the row is 'portland'"
    )


# ── cron rotation: flagships reachable, staleness measured on page row ──
def test_cron_reaches_the_retired_flagship_metros():
    tree = _tree("routes/market_deep_dive.py")
    assert set(_assigned_literal(tree, "_CRON_FLAGSHIP_METRO_SLUGS")) == \
        {"northern-virginia", "silicon-valley"}
    fn = _func(tree, "cron_rotate")
    sqls = [_norm(s) for s in _str_constants(fn) if "market_power_scores" in s]
    assert any("published = true" in s and "OR market_slug = ANY(%s)" in s
               for s in sqls), (
        "cron_rotate selects published=true only — the sitemapped flagship "
        "metro briefs (published=false by design) can never rotate again"
    )


def test_cron_staleness_join_uses_the_page_canon():
    """Joining mps 'portland-or' to mdd 'portland-or' reads NULL forever once
    the brief lives under 'portland' — the slot pins NULLS FIRST every run
    and starves the rotation (measured live for the guard-placeholder class,
    2026-08-02)."""
    fn = _func(_tree("routes/market_deep_dive.py"), "cron_rotate")
    src_seg = ast.get_source_segment(_src("routes/market_deep_dive.py"), fn)
    assert "MARKETS_DEEP_DIVE_PAGE_CANON" in src_seg
    assert "USING (market_slug)" not in src_seg, (
        "cron staleness join reverted to USING (market_slug) — re-keyed "
        "briefs read as never-generated and pin the rotation head"
    )


# ── market_brief + sitemap surfaces ─────────────────────────────────────
def test_market_brief_alias_steers_portland_at_oregon():
    alias = _assigned_literal(_tree("routes/market_brief.py"), "MARKET_ALIAS")
    assert alias.get("portland") == "portland-or", (
        "/markets/portland/brief hero lookup falls back to name-matching "
        "'Portland' — with the Maine twin that is nondeterministic"
    )


def test_sitemap_city_shard_skips_markets_redirect_slugs():
    """A sitemap must never list a URL that redirects. The city-markets
    shard joins on mps slugs, which include twins that /markets now 301s
    ('ashburn', 'washington' both clear the >=3-facilities city join)."""
    # seo F6 (2026-09-02): the filter moved to
    # routes/market_deep_dive.listable_market_slug so the /markets hub and
    # the shard skip the same slugs. The shard must route every DB slug
    # through it, and it must still consult the redirect map.
    src = _src("main.py")
    i = src.index("sitemap city-state markets DB fetch failed")
    seg = "\n".join(ln for ln in src[max(0, i - 8000):i].splitlines()
                    if not ln.strip().startswith("#"))
    assert "_listable_market(_mslug, _seen_market_slugs)" in seg, (
        "city-markets sitemap shard no longer filters slugs through the "
        "shared listable_market_slug — redirect URLs re-enter the sitemap"
    )
    dd = _src("routes/market_deep_dive.py")
    fn = dd.split("def listable_market_slug", 1)[1].split("\ndef ", 1)[0]
    fn = "\n".join(ln for ln in fn.splitlines()
                   if not ln.strip().startswith("#"))
    assert "s in MARKETS_CANONICAL_REDIRECT" in fn, (
        "listable_market_slug no longer consults the /markets redirect map"
    )
