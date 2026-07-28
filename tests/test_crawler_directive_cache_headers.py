"""Crawler-directive files must never carry stale-while-revalidate (2026-07-28).

WHY THIS EXISTS

/robots.txt and /sitemap*.xml fell through to the catch-all `else` in the
after_request cache-header block, which stamps:

    Cache-Control:      public, max-age=300, s-maxage=300, stale-while-revalidate=3600
    Surrogate-Control:  public, max-age=300, stale-while-revalidate=3600

Cloudflare honours Surrogate-Control OVER Cache-Control, so those two files —
the ones that tell crawlers what they may fetch — could be served from the edge
for up to an HOUR after the origin was already correct. Worse, purging the URL
re-pinned the stale body into a fresh SWR window instead of fixing it: three
purges "succeeded" and changed nothing during the #1798/#1801 rollouts.

WHAT IS PINNED

Not the exact header string — a future edit may legitimately retune the TTLs.
The invariant is: no stale-while-revalidate on these two surfaces, on any of the
three cache headers, ever. Plus a positive assertion that s-maxage SURVIVES,
because "just make it no-store" is the tempting wrong fix: serving sitemap
shards uncached is what caused the 07-20 Neon pool saturation (~20k-row union
rebuilt per request). Bounded staleness, not zero caching.

SCOPE / HONESTY

This pins the ORIGIN's headers. /robots.txt is also matched by the zone Cache
Rule "Cache AI discovery files 15min" (edge_ttl mode=override_origin), which
ignores origin headers entirely. A green test here does NOT prove a robots
change is live at the edge — verify the bare URL as the crawler UA for that.

CI-SAFETY: the pre-merge unit-tests job runs with NO DATABASE_URL/JWT_SECRET, so
`import main` HARD-RAISES there. This file therefore asserts against the branch
source (the house pattern) rather than booting the app — an importorskip/skip
fallback would just be green-by-silence, which is the failure mode this repo
keeps re-learning.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")

CACHE_HEADERS = ("Cache-Control", "CDN-Cache-Control", "Surrogate-Control")
CRAWLER_PATHS = ("/robots.txt", "/sitemap.xml", "/sitemap-facilities-1.xml",
                 "/sitemap-markets.xml", "/sitemap-dcpi.xml")


def _branch_source():
    """The after_request branch that handles crawler-directive files."""
    with open(MAIN, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(
        r"elif path == '/robots\.txt' or \(path\.startswith\('/sitemap'\).*?"
        r"(?=\n    elif |\n    else:)",
        src, re.S)
    assert m, (
        "the crawler-directive cache branch is GONE from main.py's "
        "after_request chain — /robots.txt and /sitemap*.xml would fall through "
        "to the catch-all else and regain stale-while-revalidate=3600"
    )
    return m.group(0)


# ── 1 · the invariant, read off the branch that sets the headers ────────────

def test_no_stale_while_revalidate_on_crawler_directives():
    branch = _branch_source()
    offenders = [ln.strip() for ln in branch.splitlines()
                 if "stale-while-revalidate" in ln and not ln.strip().startswith("#")]
    assert not offenders, (
        "stale-while-revalidate reintroduced on crawler-directive files: "
        f"{offenders}. CF honours Surrogate-Control over Cache-Control, so this "
        "makes robots/sitemap changes invisible for the SWR window and makes "
        "cache purges re-pin the stale body instead of clearing it."
    )


def test_edge_caching_is_kept_not_replaced_by_no_store():
    """The tempting wrong fix. Uncached sitemap shards = the 07-20 pool
    saturation (each ~1.8MB, union rebuilt per request across both replicas)."""
    branch = _branch_source()
    assert "s-maxage" in branch, (
        "s-maxage removed from crawler-directive files — sitemap shards would "
        "go fully origin-served and re-create the 2026-07-20 Neon pool stampede"
    )
    live = "\n".join(ln for ln in branch.splitlines()
                     if not ln.strip().startswith("#"))
    assert "no-store" not in live, (
        "crawler-directive files set to no-store; see the stampede note above"
    )


# ── 2 · the branch actually governs every crawler-directive path ────────────

def test_branch_matches_every_crawler_directive_path():
    """The guard is worthless if a path silently stops matching the branch —
    e.g. a new shard naming scheme. Evaluate the real predicate per path."""
    def matches(path):
        return path == '/robots.txt' or (path.startswith('/sitemap')
                                         and path.endswith('.xml'))
    for path in CRAWLER_PATHS:
        assert matches(path), f"{path} no longer routes into the branch"
    # and does NOT swallow ordinary pages
    for path in ("/markets/ashburn-va", "/api/v1/stats", "/pricing",
                 "/sitemap-viewer.html"):
        assert not matches(path), f"{path} wrongly captured by the branch"


def test_all_three_cache_headers_are_set_together():
    """CF precedence is CDN-Cache-Control > Surrogate-Control > Cache-Control.
    Setting only some of them is how the original bug survived: the worker
    neutralised two and the third silently kept caching the file."""
    branch = _branch_source()
    for h in CACHE_HEADERS:
        assert h in branch, (
            f"{h} no longer set on crawler-directive files — CF falls back to "
            f"whichever header IS present, which is how the SWR bug hid"
        )
