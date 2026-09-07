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

# The exception lines the 2026-08-11 fix added, verbatim from the emitter. They
# are what the anti-vacuous guard below deletes to prove they are load-bearing,
# so they must stay in sync with ai_discovery_routes.py — the guard fails loudly
# if they drift rather than quietly testing nothing.
#
# 2026-09-06: + `Allow: /api/`. The canon paths are now unblocked by TWO lines —
# their own prefix and the broader /api/ one added when the same `/*?` rule was
# found blocking the data API that llms.txt advertises (see
# test_robots_permits_what_llms_advertises.py). With only the specific lines
# stripped, `Allow: /api/` still unblocks /api/v1/canon/*, the mutation below
# changes nothing for four of the seven paths, and the control stops proving
# anything. Stripping both restores it: the assertion is "some Allow line
# carries this, not the absence of `Disallow: /*?`", and that needs every line
# that could carry it.
CANON_ALLOW_LINES = (
    "Allow: /api/v1/canon/",
    "Allow: /.well-known/",
    "Allow: /llms.txt",
    "Allow: /llms-full.txt",
    "Allow: /openapi.json",
    "Allow: /api/",
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
    """Anti-vacuous guard: delete the Allow lines from the served body and the
    block must come back. Without this, every assertion above would still pass
    if `Disallow: /*?` were dropped entirely, or if the parser could not see it
    — green while the crawlers stayed blocked.

    ★ 2026-08-11 — this guard used to diff against `git show HEAD~1:`, and that
    was wrong in two directions at once:

      STALE. `HEAD~1` is a moving reference. It named the pre-fix revision on
      exactly one commit — 38cc0375, the fix itself (and on its PR merge ref,
      where HEAD~1 is main-before-the-PR). Four commits later HEAD~1 already
      contained the Allow lines, `blocked_before` came back empty, and the
      guard failed on origin/main while the fix was intact and live. A guard
      that expires is worse than none: it spends the next reader's time on a
      regression that never happened.

      VACUOUS. When `git show` returned nothing it called `pytest.skip`. Under
      a shallow checkout that is a silent pass — the one outcome an
      anti-vacuous guard must never produce.

    Both failure modes came from reaching outside the working tree for the
    "before" state. Mutating the served body in memory needs no git, no
    history and no network, so it can neither expire nor skip.
    """
    group = _group(_served())

    # 1. The mutation must actually apply. If these lines drifted out of the
    #    emitter, step 2 would "pass" against a body it never changed.
    missing = [ln for ln in CANON_ALLOW_LINES if ln not in group]
    assert not missing, (
        f"not in the served crawler group: {missing}. Either the fix was reverted "
        "(check ai_discovery_routes.py) or CANON_ALLOW_LINES drifted from the "
        "emitter — either way this guard was about to test nothing."
    )
    mutated = "\n".join(
        ln for ln in group.splitlines() if ln.strip() not in CANON_ALLOW_LINES
    )
    assert mutated != group, "mutation did not apply — the red below proves nothing"

    # 2. Stripped of them, `Disallow: /*?` must reclaim every canon surface.
    #    That is the proof the Allow lines carry the behaviour, and that Protego
    #    sees `/*?` at all (stdlib robotparser does not, and reports these
    #    allowed either way).
    before = Protego.parse(mutated)
    still_allowed = [
        p for p in CANON_WITH_QUERY
        if before.can_fetch("https://dchub.cloud" + p, "meta-externalagent")
    ]
    assert not still_allowed, (
        f"removing the Allow lines left {still_allowed} fetchable, so they are not "
        "what unblocks these paths. Most likely `Disallow: /*?` was deleted from "
        "the crawler group instead — which allows them by abandoning the "
        "duplicate-content hygiene the rule exists for."
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
