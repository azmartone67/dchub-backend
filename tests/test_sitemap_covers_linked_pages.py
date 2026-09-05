"""Pages the site links to must be in the sitemap, because the sitemap IS the
site index.

★ THE DEFECT (2026-09-05). /wiki calls itself "every page on DC Hub" and the
palette calls itself the site search. Both are GENERATED FROM main.py's
`static_pages`. That list is hand-typed, so pages the global nav and footer
link to on every page of the site — /glossary, /faq, /team, /changelog,
/api-docs, /map, /press, /rankings and 10 more — were live (200) and completely
unfindable: absent from the sitemap, absent from the search index, absent from
the index that claims to list everything.

★ WHY A HAND-TYPED LIST KEEPS LOSING. The palette had exactly this bug and was
fixed on 2026-09-04 by generating from the sitemap instead of its own typed
catalog of 23 pages. That moved the hand-typing one level up rather than
removing it. `static_pages` is that catalog.

★ WHY THE NAV-DERIVED CHECK IS NOT IN THIS FILE. The obvious guard is "every
link in dchub-frontend/js/dchub-nav.js must be listed". It cannot live here:
the dchub-frontend/ copy vendored into THIS repo is STALE — it has no
wiki.html at all, and its dchub-nav.js hashes differently from the file live
on dchub.cloud (which matches the dchub-frontend repo). A guard reading it
would be reading a file nothing serves, and would report on a nav that no
longer exists. That reconciliation belongs where the nav is authoritative and
the published sitemap is already in hand: dchub-frontend's
scripts/build-search-index.py. What IS hermetic here is the list's own
integrity, which is what this file asserts.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, "main.py")

# The 18 that were live-but-unfindable on 2026-09-05. Each verified through the
# edge that day: 200, zero redirects, self-canonical or no canonical tag, no
# noindex. Pinned by name so an edit to static_pages cannot drop them back out.
RESTORED = (
    "/glossary", "/faq", "/team", "/changelog", "/hyperscaler-deals",
    "/advertise", "/ai-wars", "/announcements", "/api-docs",
    "/capacity-pipeline", "/compare", "/construction-pipeline", "/developers",
    "/intelligence", "/land-power-map", "/map", "/press", "/rankings",
)

# Deliberately NOT listed, and why. Each was linked from the site chrome and
# checked live on 2026-09-05 — omission here is a decision, not an oversight.
WITHHELD = {
    "/login": "auth surface — never indexable",
    "/dashboard": "serves <meta name=robots content=noindex>",
    "/architecture": "route retired in #3837; a static twin still answers 200",
    "/assets": "301 -> /database, which is listed",
    "/research/grid-intelligence": "301 -> /grid-intelligence, which is listed",
    "/integrations": "self-canonicals to /integrations/mcp, which is listed",
}


def _static_pages():
    """The paths main.py puts in the static sitemap shard."""
    with open(MAIN, encoding="utf-8") as fh:
        src = fh.read()
    i = src.index("    static_pages = [")
    j = src.index("\n    ]", i)
    return re.findall(r"\(\s*'(/[^']*)'", src[i:j])


def test_the_list_is_actually_parsed():
    """★ NON-VACUITY. static_pages is scraped with a regex from a file this
    test does not own. If the literal is renamed or reformatted the regex
    matches nothing and every assertion below passes on an empty list. This one
    fails first, loudly."""
    paths = _static_pages()
    assert len(paths) >= 130, (
        f"only {len(paths)} sitemap paths parsed out of main.py — the "
        f"static_pages literal moved or changed shape, and this guard is blind")
    assert "/" in paths and "/pricing" in paths, "parsed something, but not the list"


def test_the_restored_pages_are_listed():
    """THE REGRESSION GUARD. These 18 were linked from the site's own chrome
    and missing from its own index."""
    listed = set(_static_pages())
    missing = [p for p in RESTORED if p not in listed]
    assert not missing, (
        f"{len(missing)} page(s) dropped back out of the sitemap: {missing}. "
        f"They are linked from the nav/footer, so /wiki and the search palette "
        f"go blind to them again.")


def test_withheld_pages_are_not_listed():
    """The other half of the decision. A page cannot be both declared
    unindexable and shipped in the sitemap — whichever statement is stale will
    be the one trusted later."""
    listed = set(_static_pages())
    contradicted = sorted(p for p in WITHHELD if p in listed)
    assert not contradicted, (
        f"{contradicted} are in static_pages but WITHHELD says they must not "
        f"be indexed ({ {p: WITHHELD[p] for p in contradicted} }). Drop the "
        f"sitemap entry, or drop the WITHHELD entry with the reason it changed.")


def test_no_path_is_listed_twice():
    """A duplicated <loc> is a soft error Google reports against the whole
    sitemap, and a hand-typed list of 139 is exactly where one appears."""
    paths = _static_pages()
    dupes = sorted({p for p in paths if paths.count(p) > 1})
    assert not dupes, f"listed more than once in static_pages: {dupes}"


def test_every_listed_path_is_a_rooted_relative_path():
    """A sitemap builds absolute URLs by concatenation, so a path missing its
    leading slash or carrying a scheme silently emits a broken <loc>."""
    bad = [p for p in _static_pages()
           if not p.startswith("/") or p.startswith("//") or "://" in p
           or " " in p or p.endswith(("?", "#"))]
    assert not bad, f"malformed sitemap path(s): {bad}"
