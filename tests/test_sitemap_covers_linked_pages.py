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
    # round 1 — reachable from the nav snapshot vendored in this repo
    "/glossary", "/faq", "/team", "/changelog", "/hyperscaler-deals",
    "/advertise", "/ai-wars", "/announcements", "/api-docs",
    "/capacity-pipeline", "/compare", "/construction-pipeline", "/developers",
    "/intelligence", "/land-power-map", "/map", "/press", "/rankings",
    # round 2 — from the CURRENT nav (66 links, not the snapshot's 36), once
    # dchub-frontend's build-search-index.py started reporting the gap
    "/brief", "/connect-mcp", "/daily", "/dc-hub-media/", "/dcgi",
    "/hyperscalers", "/listings", "/mcp-standing", "/partners/feedback",
    "/premium", "/product", "/radar", "/receipts", "/reports/quarterly",
    "/state-of-2026", "/system-status", "/what-ais-say",
    # round 3 (2026-09-06) — the FOOTER half. Rounds 1 and 2 both derived from
    # the NAV, so pages linked only from dchub-frontend/index.html's footer were
    # never candidates: "every page the chrome links" was really "every page the
    # NAV links". Found by pruning nine thin footer links and having to prove the
    # pruned pages kept a route. Each probed 2026-09-06 through the edge: 200,
    # no redirect, no noindex, self-canonical or none, allowed by robots.txt.
    "/founders", "/ai-hub", "/ai-integrations", "/cited-by", "/data-sources",
    "/data-center-grid-constraint", "/where-to-build-data-center",
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
    # ★ Live 200s the nav links to, and inside `Disallow: /sites/` in
    # robots.txt. A sitemap entry for a robots-blocked URL is a contradiction
    # Search Console reports against the whole sitemap. Guarded below, so a
    # robots.txt change is what unblocks them — not someone re-adding the line.
    "/sites/": "robots.txt Disallow: /sites/",
    "/sites/value": "robots.txt Disallow: /sites/",
}

# Paths robots.txt refuses. Kept beside WITHHELD so the two cannot disagree.
ROBOTS_BLOCKED_PREFIXES = ("/sites/",)


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
    """THE REGRESSION GUARD. Every one of these was linked from the site's own
    chrome and missing from its own index. Three rounds: 18 from the vendored nav
    snapshot, 17 more from the current nav, 7 from the FOOTER."""
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


def test_no_robots_blocked_path_is_in_the_sitemap():
    """★ THE CONTRADICTION GUARD. Listing a URL the site's own robots.txt
    refuses is not a discovery hint — Search Console reports it as "Submitted
    URL blocked by robots.txt" and counts it against the sitemap as a whole.
    Two of the nav-linked pages found on 2026-09-05 were exactly this, and the
    eligibility check is the only reason they were not swept in with the other
    seventeen."""
    listed = _static_pages()
    blocked = sorted(p for p in listed
                     if p.startswith(ROBOTS_BLOCKED_PREFIXES))
    assert not blocked, (
        f"{blocked} are in static_pages but robots.txt disallows "
        f"{ROBOTS_BLOCKED_PREFIXES}. Either drop them, or change robots.txt "
        f"first and update ROBOTS_BLOCKED_PREFIXES in the same PR.")


def test_this_repo_does_not_pretend_to_own_robots_txt():
    """★ THE HALF OF THE INVARIANT THAT BELONGS HERE.

    The first version of this leg asserted "Disallow: /sites/" in
    static/robots.txt and FAILED — correctly. That file is not what
    dchub.cloud serves: the live robots.txt comes from the dchub-frontend repo
    (verified 2026-09-05 — the served body matches that repo's copy, and
    static/robots.txt here disallows a different set entirely, with no /sites/
    line at all). A guard reading it would have pinned this sitemap's
    behaviour to a file nobody serves — the same defect as the vendored
    frontend mirror retired in #3871.

    So the reciprocal check — "robots.txt still disallows what WITHHELD claims
    it does" — lives in dchub-frontend#1374, beside the file it reads. What is
    checkable here is that this repo does not grow a second answer: if
    static/robots.txt ever starts disallowing these prefixes, there are two
    robots.txt files making claims about the same paths and only one is
    served."""
    local = os.path.join(REPO, "static", "robots.txt")
    if not os.path.exists(local):
        return                       # nothing here to contradict anything
    with open(local, encoding="utf-8") as fh:
        body = fh.read()
    overlap = [p for p in ROBOTS_BLOCKED_PREFIXES if f"Disallow: {p}" in body]
    assert not overlap, (
        f"static/robots.txt now disallows {overlap}, which the SERVED "
        f"robots.txt (dchub-frontend) also governs. Two files answering for "
        f"the same paths and only one reaching the edge is how the vendored "
        f"mirror went stale. Keep the answer in one repo.")
