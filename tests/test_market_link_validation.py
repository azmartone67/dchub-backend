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
