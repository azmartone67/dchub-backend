"""Internal /markets/* links must point at markets that EXIST (2026-07-28).

Facility pages emitted href="/markets/{slug(city + '-' + state)}" with no
existence check. Market slugs are METRO/CITY keyed, so `dallas` is real and
`dallas-texas` is not -- every Dallas facility page linked to a non-resolving
URL. Verified live before this fix:
    /markets/dallas       200
    /markets/dallas-texas 404
Earlier passes patched the DESTINATION (302-to-hub, then a city-state 301).
This is the LINK. A page must never link to its own 404.

pytest functions only -- no module-scope work or exit.
"""
import ast
import functools
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SEO = REPO_ROOT / "routes" / "seo_pages.py"
WANT = {"_slug", "_valid_market_slugs", "_market_slug_for", "_market_link"}


@functools.lru_cache(maxsize=1)
def _src():
    return SEO.read_text(encoding="utf-8")


def _load(known):
    """Extract the real helpers with a stubbed slug set."""
    tree = ast.parse(_src())
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in WANT)
            or (isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "").startswith("_MARKET_SLUGS"))]
    found = {n.name for n in body if isinstance(n, ast.FunctionDef)}
    assert found == WANT, "missing {}".format(WANT - found)
    g = {"re": re, "_conn": lambda: None,
         "_esc_attr": lambda x: str(x),
         "_h": lambda x: str(x)}
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<extracted>", "exec"), g)
    g["_MARKET_SLUGS_CACHE"]["slugs"] = known
    g["_MARKET_SLUGS_CACHE"]["at"] = 10 ** 12      # never expire during a test
    return g


def test_city_state_is_used_when_it_is_a_real_metro():
    g = _load({"northern-virginia", "dallas"})
    assert g["_market_slug_for"]("Northern", "Virginia") == "northern-virginia"


def test_falls_back_to_the_city_when_city_state_does_not_exist():
    """The Dallas case: dallas-texas is not a market, dallas is."""
    g = _load({"dallas", "dallas-fort-worth"})
    assert g["_market_slug_for"]("Dallas", "Texas") == "dallas"


def test_no_market_means_no_link_at_all():
    g = _load({"dallas"})
    assert g["_market_slug_for"]("Nowheresville", "Ohio") is None
    out = g["_market_link"]("Nowheresville", "Ohio", "Nowheresville, Ohio")
    assert "<a" not in out, "a page must never link to its own 404"
    assert "Nowheresville, Ohio" in out, "the label must survive as plain text"


def test_a_real_market_still_renders_an_anchor():
    g = _load({"dallas"})
    out = g["_market_link"]("Dallas", "Texas", "Dallas, Texas", cls="cta")
    assert 'href="/markets/dallas"' in out and 'class="cta"' in out


def test_cold_cache_fails_open_to_the_legacy_slug():
    """An unreachable DB must not strip navigation site-wide; the market route
    resolves city-state -> city with a 301, so a stale link still lands."""
    g = _load(None)
    assert g["_market_slug_for"]("Dallas", "Texas") == "dallas-texas"


def test_missing_city_yields_no_link():
    g = _load({"dallas"})
    assert g["_market_slug_for"]("", "Texas") is None


def test_no_unvalidated_market_href_remains_in_the_facility_templates():
    """The whole point: no emitter may build a market URL from raw city+state."""
    # ★ Strip comments first. The rationale block above the helper QUOTES the
    # old pattern verbatim, so a raw `not in` check fails on prose while the
    # real emitters are already fixed -- the inverse of the "comments satisfy
    # grep" trap. Assert on executable text only.
    code = "\n".join(ln for ln in _src().splitlines()
                     if not ln.strip().startswith("#"))
    # ★ PATTERN, not a literal. A first version asserted the exact
    # "_slug(city + '-' + state)" string; re-mutating with DOUBLE quotes slipped
    # straight through and all 8 tests stayed green. Match any concatenation of
    # city and state fed to _slug, whatever the quoting.
    bad = re.findall(r"_slug\(\s*city\s*\+", code)
    assert not bad, (
        "an unvalidated city+state market slug is still being built "
        "({} site(s)) -- route it through _market_link".format(len(bad)))


def test_the_slug_set_is_cached_not_queried_per_render():
    """A per-facility lookup would put a query on every SEO page render."""
    seg = _src().split("def _valid_market_slugs", 1)[1].split("\ndef ", 1)[0]
    seg = "\n".join(ln for ln in seg.splitlines()
                    if not ln.strip().startswith("#"))
    assert "_MARKET_SLUGS_TTL_S" in seg and "_MARKET_SLUGS_CACHE" in seg


# ── the SITEMAP was the dominant source (2026-07-28, third measurement) ──
# Sampled 14 of the 295 market URLs in /sitemap-markets.xml: TWELVE were 404
# (goodyear-az, miami-fl, tacoma-wa, charlotte-nc, richmond-va, tucson-az...).
# The sitemap's criterion (>=3 facilities for a city+state) was never the
# criterion the /markets/<slug> route serves on. Telling Google to crawl pages
# we never built is why the Not-found bucket refills and validation keeps
# failing -- every fix is undone by the next sitemap fetch.
MAIN = REPO_ROOT / "main.py"
DEEPDIVE = REPO_ROOT / "routes" / "market_deep_dive.py"


def _sitemap_market_block():
    """The city-market shard's code in main.py PLUS the SQL it runs.

    seo F6 (2026-09-02): the two queries (dated + no-first_seen fallback)
    moved to routes/market_deep_dive.py (US_CITY_MARKET_SQL[_NODATE]) so the
    /markets hub and the sitemap share ONE source. The shard must call that
    helper, and the SQL must still carry the join — both are asserted on
    the concatenation below, comments stripped."""
    src = MAIN.read_text(encoding="utf-8")
    seg = src.split("DB-driven US /markets/", 1)[1][:4000]
    assert "_us_city_rows(_mk_conn)" in seg, (
        "the sitemap shard no longer runs the shared US city-market query")
    dd = DEEPDIVE.read_text(encoding="utf-8")
    sql = dd.split("US_CITY_MARKET_SQL = ", 1)[1].split("def us_city_market_rows", 1)[0]
    return "\n".join(ln for ln in (seg + "\n" + sql).splitlines()
                     if not ln.strip().startswith("#"))


def test_sitemap_only_lists_markets_that_exist():
    seg = _sitemap_market_block()
    assert "market_power_scores" in seg, (
        "the sitemap must JOIN a real market table -- otherwise it emits URLs "
        "the route never serves")
    assert "JOIN market_power_scores" in seg


def test_sitemap_no_longer_builds_a_city_state_slug():
    seg = _sitemap_market_block()
    assert "|| '-' || LOWER(state)" not in seg, (
        "city+state slugs (miami-fl) are not market pages; emit the city slug")


def test_sitemap_fallback_query_is_guarded_too():
    """The degraded path must not re-introduce the 404s."""
    seg = _sitemap_market_block()
    assert seg.count("JOIN market_power_scores") >= 2, (
        "both the primary and the no-first_seen fallback query need the join")


def _resolver():
    tree = ast.parse(DEEPDIVE.read_text(encoding="utf-8"))
    want = {"_market_slug_without_state", "_market_exists"}
    body = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in want)
            or (isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "").startswith("_US_STATE"))]
    assert {n.name for n in body if isinstance(n, ast.FunctionDef)} == want
    g = {"_conn": lambda: None}
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    exec(compile(mod, "<extracted>", "exec"), g)
    return g


def test_state_abbreviations_are_recognised():
    """The first resolver only stripped FULL state names, so miami-fl -- the
    shape the sitemap actually emitted -- still 404'd."""
    g = _resolver()
    assert "fl" in g["_US_STATE_ABBREVS"]
    assert "az" in g["_US_STATE_ABBREVS"]
    g["_market_exists"] = lambda s: s in {"miami", "goodyear", "dallas"}
    import types
    fn = g["_market_slug_without_state"]
    fn.__globals__["_market_exists"] = g["_market_exists"]
    assert fn("miami-fl") == "miami"
    assert fn("goodyear-az") == "goodyear"
    assert fn("dallas-texas") == "dallas"


def test_unknown_market_still_404s():
    g = _resolver()
    fn = g["_market_slug_without_state"]
    fn.__globals__["_market_exists"] = lambda s: False
    assert fn("nowhere-tx") is None


def test_db_down_degrades_to_404_not_a_guess():
    g = _resolver()
    assert g["_market_exists"]("miami") is False
