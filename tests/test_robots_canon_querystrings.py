"""Guard for the robots.txt assistant-crawler group (2026-08-11).

★ THE FAILURE THIS ENCODES

`Disallow: /*?` blocks every URL carrying a query string. But we instruct every
agent to cache-bust — it is in our own ship discipline, because a "verified
live" read off a cached response is not one. So the rule punished agents for
following our own instruction, and it punished exactly the ones diligent enough
to fetch:

    Meta        LIVE_CRAWL_POLICY_BLOCKED on canonical_counts and tools_url;
                could not run the published self-test at all.
    Perplexity  "could not fetch the live CACHE-BUSTED DC Hub MCP surface"
                — every round, for a week.

Neither was an HTTP failure. Both paths return 200 to a direct curl. robots.txt
is advisory, so the block lives in the crawler's own policy engine: it reads the
line and never issues the request. That is why this never appeared in our logs
as an error — it appeared as silence, and we read the silence as apathy.

★ THE PARSER MATTERS. Python's stdlib urllib.robotparser predates RFC 9309 and
does not implement `Disallow: /*?` at all — it reported these paths as ALLOWED
both before and after the fix, i.e. it cannot see the bug or the fix. Verifying
a robots change with it is a vacuous pass. Protego (Scrapy's, RFC 9309
compliant) is what the real crawlers behave like, so it is what we assert with.
"""
import subprocess

import pytest

Protego = pytest.importorskip(
    "protego", reason="RFC 9309 parser required; stdlib robotparser cannot see /*?"
).Protego


def _group(src: str) -> str:
    """The assistant-crawler group, sliced out of the served robots body."""
    return src[src.index("User-agent: GPTBot"):src.index("# Discovery files")]


def _served() -> str:
    with open("ai_discovery_routes.py", encoding="utf-8") as fh:
        return fh.read()


def _parser():
    return Protego.parse(_group(_served()))


def _can(path: str, ua: str = "meta-externalagent") -> bool:
    return _parser().can_fetch("https://dchub.cloud" + path, ua)


# ── The surfaces that MUST survive a cache-buster ──────────────────────────
CANON_WITH_QUERY = (
    "/api/v1/canon/phrases?_=123",
    "/api/v1/canon/coverage?_=123",
    "/api/v1/canon/selftest?_=123",
    "/api/v1/canon/taxonomy?_=123",
    "/.well-known/mcp.json?_=123",
    "/llms.txt?_=123",
    "/openapi.json?_=123",
)


@pytest.mark.parametrize("path", CANON_WITH_QUERY)
def test_canonical_surfaces_are_fetchable_cache_busted(path):
    assert _can(path), (
        f"{path} is blocked for assistant crawlers. We tell every agent to "
        "cache-bust; blocking the result is how Meta and Perplexity lost a week."
    )


@pytest.mark.parametrize("path", [p.split("?")[0] for p in CANON_WITH_QUERY])
def test_canonical_surfaces_are_fetchable_clean(path):
    assert _can(path)


# ── The hygiene the rule exists for MUST survive the fix ───────────────────
@pytest.mark.parametrize("path", [
    "/facilities/foo?cb=1",
    "/markets?filter=x",
    "/us-data-center-map.html?zoom=3",
])
def test_duplicate_content_hygiene_still_blocks(path):
    """`Disallow: /*?` exists to stop crawl budget draining into ?cb=/?filter=
    duplicates of rankable HTML. Widening it to those would trade one problem
    for a worse one.
    """
    assert not _can(path), f"{path} should still be blocked — this is the rule's purpose"


@pytest.mark.parametrize("path", ["/admin", "/sites/x", "/cdn-cgi/trace"])
def test_never_rankable_surfaces_still_blocked(path):
    assert not _can(path)


def test_clean_content_paths_unaffected():
    assert _can("/facilities/foo")
    assert _can("/sites/")          # the Allow: /sites/$ exception


def test_the_fix_actually_changed_behaviour():
    """Anti-vacuous guard. If HEAD and the working tree agree on these paths,
    either the fix is missing or the parser cannot see it — and every
    assertion above would pass while the crawlers stayed blocked.
    """
    before_src = subprocess.run(
        ["git", "show", "HEAD~1:ai_discovery_routes.py"],
        capture_output=True, text=True).stdout
    if not before_src:
        pytest.skip("no previous revision available in this checkout")
    before = Protego.parse(_group(before_src))
    blocked_before = [
        p for p in CANON_WITH_QUERY
        if not before.can_fetch("https://dchub.cloud" + p, "meta-externalagent")
    ]
    assert blocked_before, (
        "the previous revision already allowed these — this test proves nothing"
    )


@pytest.mark.parametrize("ua", [
    "meta-externalagent", "PerplexityBot", "Perplexity-User",
    "ClaudeBot", "GPTBot", "Googlebot", "GrokBot",
])
def test_every_agent_in_the_group_gets_the_canon_surfaces(ua):
    """A named group inherits nothing (RFC 9309), and this group is shared by
    every assistant crawler — so the fix must hold for all of them, not just
    the two that reported it.
    """
    assert _can("/api/v1/canon/selftest?_=1", ua)
