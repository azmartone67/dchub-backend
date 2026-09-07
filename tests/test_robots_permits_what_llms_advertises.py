#!/usr/bin/env python3
"""tests/test_robots_permits_what_llms_advertises.py — everything we advertise,
we must permit.

NO NETWORK, NO DB.

★ THE FAILURE THIS ENCODES (measured 2026-09-06)

llms.txt and llms-full.txt exist to hand an agent worked, callable examples.
Every one of those examples carries a query string. robots.txt's named crawler
groups carry `Disallow: /*?`. So we published a machine surface and, in the same
breath, told the machines not to fetch it:

    PerplexityBot   12 of 57 advertised llms.txt URLs DISALLOWED
    Perplexity-User 12          (same set)
    GPTBot          12          (same set)
    ClaudeBot       12          (same set)
    Googlebot       12          (same set)
    Bingbot         14          — and this one is Copilot's ONLY surface

The first five closed on 2026-09-06 (#4052). Bingbot stayed open one more day
because the trade looked forced: reopening /api/ to it in August was made safe
by keeping `Disallow: /*?`, and this file recorded that robots.txt "cannot
express the examples but not the long tail". That was wrong — an END-ANCHORED
Allow expresses exactly one URL — and it closed on 2026-09-07 with the long-tail
hygiene fully intact. See the Bingbot block in robots.txt for the measurement.

This is the SECOND time. On 2026-08-11 the same rule blocked the cache-busted
canon surfaces; Meta reported LIVE_CRAWL_POLICY_BLOCKED and Perplexity said it
"could not fetch the live CACHE-BUSTED DC Hub MCP surface" every round for a
week. That fix added six `Allow:` lines for the DISCOVERY files and stopped
there — it never covered the DATA API those files spend their length
advertising. Six hand-maintained exceptions then fell behind the catalog, which
is the whole reason this guard derives its assertions instead of listing them.

robots.txt is advisory: the block lives in the crawler's own policy engine, so
it never appears in our logs as an error. It appears as a crawler going quiet,
and we read that as apathy — twice.

★ THE CONTRACT
    For every crawler group that is not deliberately closed to /api/,
    every URL llms.txt or llms-full.txt advertises must be fetchable.

Both sides are DERIVED from the served bodies. Add an endpoint to llms.txt and
this guard starts requiring it; close a group to /api/ on purpose (PetalBot is
paced that way) and it drops out of scope on its own. Nothing to keep in sync.

★ THE PARSER MATTERS. Python's stdlib urllib.robotparser predates RFC 9309 and
does not implement `Disallow: /*?` at all — it reports these paths ALLOWED both
before and after the fix, i.e. it can see neither the bug nor the fix. Protego
(Scrapy's, RFC 9309) is what real crawlers behave like, so it is what we assert
with. See test_ci_installs_protego below: that dependency was absent from CI,
which is why the 2026-08-11 guard has been a silent skip since the day it landed.
"""
import os
import re
import sys

import pytest

Protego = pytest.importorskip(
    "protego", reason="RFC 9309 parser required; stdlib robotparser cannot see /*?"
).Protego

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "ai_discovery_routes.py")
sys.path.insert(0, ROOT)


def _src() -> str:
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


def _robots_body(src: str) -> str:
    """The robots.txt literal exactly as served — the whole file, not one group.

    Slicing a single group would parse something no crawler ever sees; group
    precedence (RFC 9309: one most-specific group, inheriting nothing) is part
    of what is under test.
    """
    i = src.index("def serve_robots_txt(")
    j = src.index('content = """', i) + len('content = """')
    return src[j:src.index('"""', j)]


def _llms_template(src: str, fn: str) -> str:
    """One llms template, INDENTED python comments dropped.

    Only indented `#` lines are python comments here; a `#` at column 0 is
    markdown inside the served literal, so stripping by `lstrip()` — as the
    older llms guards do — would also delete real headings and any URL on them.
    """
    a = src.index("def %s(" % fn)
    b = src.index("\n    @app.route(", a + 10)
    return "\n".join(
        line for line in src[a:b].splitlines()
        if not (line[:1].isspace() and line.lstrip().startswith("#"))
    )


_URL = re.compile(r"https://dchub\.cloud/[^\s)\]\"'<>]*")


def _advertised(src: str) -> set:
    return {
        m.rstrip(".,;:")
        for fn in ("serve_llms_txt", "serve_llms_full_txt")
        for m in _URL.findall(_llms_template(src, fn))
    }


def _groups(body: str) -> list:
    """[{uas, rules}] — a User-agent run, then the directives it owns.

    A non-UA directive TERMINATES the run (RFC 9309), which is why
    Content-Signal placement matters elsewhere in this file; parse it the same
    way a crawler does rather than assuming.
    """
    groups, cur, in_run = [], None, False
    for raw in body.splitlines():
        line = raw.split("#")[0].strip()
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            if not in_run:
                cur = {"uas": [], "rules": []}
                groups.append(cur)
                in_run = True
            cur["uas"].append(val)
        elif key in ("allow", "disallow"):
            in_run = False
            if cur is not None:
                cur["rules"].append("%s: %s" % (key, val))
    return groups


# ── The one crawler this contract deliberately does NOT cover ──────────────
# Bingbot is excluded, and the exclusion is bound to the guard that owns the
# other side of the argument rather than left as a bare name here.
#
# WHY. /api/ was reopened to Bingbot on 2026-08-31 (Copilot crawls as this UA
# and has no other surface). The precondition that made reopening safe was
# keeping `Disallow: /*?`: the 2026-07-28 measurement found 1 in 3 sampled
# /api/ paths 404ing, and that junk lives behind query strings — ?page=99,
# ?offset=5000. Bing had been reporting "limited crawl capacity" since June, so
# unlike the assistant crawlers its budget is the binding constraint.
#
# ★★★ 2026-09-07 — RESOLVED, AND THE PREMISE WAS THE BUG. This block used to
# read: "robots.txt cannot express the examples but not the long tail: `Allow:
# /api/v1/facilities` unblocks ?q=Virginia and ?page=99 alike." True of a PREFIX
# Allow, false of an END-ANCHORED one:
#
#     Allow: /api/v1/facilities*q=Virginia*country=US$
#
# permits that one URL and nothing else. Bingbot went 14 -> 0 blocked with 0 of
# 68 adversarial probes leaking and no change to any other crawler, so the trade
# this exclusion was granted for did not exist. The ownership test below said
# what to do when that happens — "delete the EXCLUDED_UAS entry so this contract
# starts covering it" — so the entry is gone and Bingbot is in scope.
#
# The MECHANISM stays. Deliberately empty is not the same as unused: the next
# crawler we pace for a reason still needs its reason bound to a live guard, and
# an empty dict must not make the tests below pass by having nothing to check.
EXCLUDED_UAS = {}


def _in_scope_uas(body: str) -> list:
    """Named groups that are not deliberately closed to parameterized /api/.

    `*` is out of scope by design — an unnamed crawler gets the stricter policy,
    and the fix for that is to NAME the partner (see the partner-parity note in
    robots.txt), not to widen the wildcard. PetalBot carries `Disallow: /api/`
    on purpose (11.73k requests, 0 referrals, paced 2026-09-04) and drops out
    here on its own, without this file naming it. Bingbot is IN scope as of
    2026-09-07: its group says neither `Disallow: /api/` nor a bare `Allow:
    /api/`, and its 14 advertised URLs are permitted by end-anchored Allow lines
    instead — which is exactly why the mutation control below has to strip both
    forms, not just the prefix one.
    """
    return [
        ua
        for g in _groups(body)
        for ua in g["uas"]
        if ua != "*"
        and ua not in EXCLUDED_UAS
        and "disallow: /api/" not in g["rules"]
    ]


SRC_TEXT = _src()
BODY = _robots_body(SRC_TEXT)
ADVERTISED = sorted(_advertised(SRC_TEXT))
WITH_QUERY = [u for u in ADVERTISED if "?" in u]
IN_SCOPE = _in_scope_uas(BODY)


# ── Floors: a derived guard that derives NOTHING is a silent green ──────────
# Every assertion below is a loop over a derived set. If a slice anchor moves
# and a set comes back empty, all of them pass vacuously. These are the floors
# that turn that into a red. They are deliberately well under the measured
# values (62 / 14 / 31 on 2026-09-06) so ordinary edits do not trip them.
def test_the_derived_sets_are_not_empty():
    assert len(ADVERTISED) >= 40, (
        f"only {len(ADVERTISED)} URLs parsed out of the llms templates — the "
        "slice anchors in _llms_template probably moved. Every assertion in "
        "this file loops over this set and would pass on an empty one."
    )
    assert len(WITH_QUERY) >= 10, (
        f"only {len(WITH_QUERY)} advertised URLs carry a query string. That is "
        "the exact class `Disallow: /*?` blocks, so without them this guard "
        "cannot see the defect it exists for."
    )
    assert len(IN_SCOPE) >= 20, (
        f"only {len(IN_SCOPE)} in-scope crawler UAs — _robots_body or _groups "
        "is not reading the served body."
    )


# ── The contract ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("ua", _in_scope_uas(_robots_body(_src())))
def test_every_in_scope_crawler_can_fetch_everything_we_advertise(ua):
    parser = Protego.parse(BODY)
    blocked = [u for u in ADVERTISED if not parser.can_fetch(u, ua)]
    assert not blocked, (
        f"{ua} is told not to fetch {len(blocked)} URL(s) that llms.txt or "
        f"llms-full.txt advertises to it: {blocked[:5]}. We publish these as "
        "worked examples; blocking them is how Perplexity and Meta each lost a "
        "week, and it never surfaces as an error — only as silence."
    )


# ── The hygiene the /*? rule exists for MUST survive the widening ──────────
@pytest.mark.parametrize("ua", ["PerplexityBot", "Bingbot", "GPTBot", "ClaudeBot"])
@pytest.mark.parametrize("path", [
    "/facilities/foo?cb=1",
    "/markets?filter=x",
    "/us-data-center-map.html?zoom=3",
])
def test_duplicate_content_hygiene_still_blocks(ua, path):
    """`Disallow: /*?` exists to stop crawl budget draining into ?cb=/?filter=
    duplicates of RANKABLE HTML. Widening /api/ must not touch that — trading
    the duplicate-content problem for the crawl-budget one is not a fix.
    """
    assert not Protego.parse(BODY).can_fetch("https://dchub.cloud" + path, ua), (
        f"{path} became fetchable for {ua} — the widening was too broad"
    )


@pytest.mark.parametrize("ua", ["PerplexityBot", "Bingbot"])
@pytest.mark.parametrize("path", [
    "/api/admin/x", "/api/admin/x?y=1",
    "/api/v1/admin/x", "/api/v1/admin/x?y=1",
    "/api/auth/token", "/api/auth/token?x=1",
    "/api/stripe/webhook", "/api/stripe/webhook?x=1",
    "/admin", "/sites/x", "/cdn-cgi/trace",
])
def test_sensitive_and_never_rankable_surfaces_stay_shut(ua, path):
    """`Allow: /api/` would otherwise expose the admin/auth/billing prefixes in
    their ?-carrying form. Note the CLEAN forms were already crawlable by these
    groups before this change — `Disallow: /admin` never matched /api/admin —
    so these assertions close a pre-existing hole as well as guarding the new
    line.
    """
    assert not Protego.parse(BODY).can_fetch("https://dchub.cloud" + path, ua)


def test_clean_content_paths_unaffected():
    parser = Protego.parse(BODY)
    assert parser.can_fetch("https://dchub.cloud/facilities/foo", "PerplexityBot")
    assert parser.can_fetch("https://dchub.cloud/sites/", "PerplexityBot")


def test_each_exclusion_is_still_owned_by_a_live_guard():
    """An exception with no owner is just a hole.

    ★ NOT parametrized, on purpose. `@parametrize` over an empty EXCLUDED_UAS
    collects ZERO tests and reports green — the same silent-green shape the
    Floors section above exists to prevent. As a plain loop with an explicit
    empty branch, emptiness has to assert something positive instead.

    Each EXCLUDED_UAS entry names the guard asserting the OPPOSITE — that this
    crawler must keep refusing the query-string long tail. If that guard is ever
    deleted, the decision behind the exclusion has changed, and this fails so
    the contract gets widened deliberately instead of the gap quietly outliving
    its reason.
    """
    if not EXCLUDED_UAS:
        # The empty case must still assert something. Bingbot was the only entry
        # this dict ever held; if it silently fell back OUT of scope, the
        # exclusion would be back without anyone deciding it.
        assert "Bingbot" in IN_SCOPE, (
            "EXCLUDED_UAS is empty, so every named crawler should be covered — "
            f"but Bingbot is not in scope. IN_SCOPE={IN_SCOPE}. Either its group "
            "grew a `Disallow: /api/`, or it vanished from robots.txt."
        )
        return

    for ua, (path, func) in sorted(EXCLUDED_UAS.items()):
        full = os.path.join(ROOT, path)
        assert os.path.exists(full), (
            f"{ua} is excluded from the advertised-URL contract on the authority "
            f"of {path}, which no longer exists. Re-decide the exclusion."
        )
        with open(full, encoding="utf-8") as fh:
            assert "def %s(" % func in fh.read(), (
                f"{path}::{func} is gone. It is the reason {ua} is excluded "
                "here — if that policy was reversed, delete the EXCLUDED_UAS "
                "entry so this contract starts covering it."
            )


def test_bingbot_gets_the_examples_without_the_long_tail():
    """The property that replaced the exclusion, asserted from both sides.

    Bingbot's 14 URLs are permitted by END-ANCHORED Allow lines, not by opening
    /api/. So the contract above (everything advertised is fetchable) is only
    half of it — the half that made the August reopen safe is that EXTENDING an
    advertised URL must still be refused. A prefix Allow would pass the first
    half and fail this one, which is the whole reason this test exists.
    """
    parser = Protego.parse(BODY)

    for u in WITH_QUERY:
        assert parser.can_fetch(u, "Bingbot") is True, (
            f"Bingbot cannot fetch advertised {u} — its end-anchored Allow line "
            "is missing or no longer matches. Derivation is "
            "re.sub(r'[^A-Za-z0-9/._~=-]', '*', path) + '$' — a whitelist, "
            "because a literal `,` fails to match just as `&` does."
        )

    for u in WITH_QUERY:
        for suffix in ("&page=99", "&cb=1", "&offset=5000", "0"):
            probe = u + suffix
            assert parser.can_fetch(probe, "Bingbot") is False, (
                f"Bingbot regained {probe}. An advertised example leaked its "
                "pagination long tail — the Allow line lost its `$` anchor, or "
                "was widened to a prefix."
            )

    for probe in ("https://dchub.cloud/api/v1/facilities?page=99",
                  "https://dchub.cloud/api/v1/facilities?q=Virginia",
                  "https://dchub.cloud/api/news?limit=1000",
                  "https://dchub.cloud/api/v1/search?q=junk&offset=5000"):
        assert parser.can_fetch(probe, "Bingbot") is False, (
            f"Bingbot regained {probe} — the crawl-budget sink that closing "
            "/api/ in July was meant to drain."
        )

    # ★ MEASURED RESIDUAL, recorded rather than discovered later. `*` spans
    #   separators, so PREPENDING a parameter keeps the advertised tail at the
    #   end and is allowed. Bing crawls what it discovers and nothing links this
    #   form, so it is bounded by discoverability, not by the pattern. Asserted
    #   as EXPECTED so that if a future construction closes it, this test fails
    #   and the comment gets deleted with it instead of going stale.
    prepended = "https://dchub.cloud/api/v1/facilities?page=99&q=Virginia&country=US"
    assert parser.can_fetch(prepended, "Bingbot") is True, (
        "the prepended-parameter residual is now closed — good. Delete this "
        "assertion and the RESIDUAL note in robots.txt, which no longer applies."
    )


def test_petalbot_stays_out_of_scope_without_being_named_here():
    """PetalBot is paced deliberately (`Disallow: /api/`). It must fall out of
    scope because of what its own group says, not because this file lists an
    exception — otherwise pacing a crawler later would silently start failing
    the contract, or worse, quietly widen it.
    """
    assert "PetalBot" not in IN_SCOPE
    assert any("PetalBot" in g["uas"] for g in _groups(BODY)), \
        "PetalBot vanished from robots.txt entirely — check the group survived"


# ── Anti-vacuous: prove the Allow lines carry the behaviour ────────────────
def test_removing_the_api_allow_lines_reblocks_everything():
    """Mutation control. Without it every assertion above would still pass if
    `Disallow: /*?` were deleted outright (which "fixes" the contract by
    abandoning the duplicate-content hygiene), or if Protego could not see `/*?`
    at all — the stdlib parser cannot, and reports these allowed either way.

    Mutating the body in memory needs no git, no history and no network, so
    unlike a `git show HEAD~1` control it can neither expire nor skip.

    ★ TWO mechanisms now carry this, and the mutation has to strip BOTH.
    The assistant group is unblocked by a prefix `Allow: /api/`; Bingbot is
    unblocked by end-anchored `Allow: /api/...$` lines and has no prefix Allow
    at all. Removing only the prefix form would leave Bingbot fetchable and this
    control would fail for the wrong reason — reporting a leak where the design
    changed.
    """
    prefix = "Allow: /api/"
    anchored = [
        l.strip() for l in BODY.splitlines()
        if l.strip().startswith("Allow: /api/") and l.strip().endswith("$")
    ]
    assert prefix in BODY, (
        "`Allow: /api/` is not in the served robots body — either the fix was "
        "reverted or this guard drifted from the emitter. Either way it was "
        "about to test nothing."
    )
    assert len(anchored) >= 10, (
        f"only {len(anchored)} end-anchored `Allow: /api/...$` lines in the "
        "served body, but Bingbot's 14 advertised URLs depend on them. Either "
        "they were removed or the emitter changed shape."
    )

    drop = {prefix, *anchored}
    mutated = "\n".join(l for l in BODY.splitlines() if l.strip() not in drop)
    assert mutated != BODY, "mutation did not apply — the red below proves nothing"
    # ★ Compare LINE SETS, not substrings: "Allow: /api/" is a substring of
    #   "Allow: /api/v1/canon/", so `gone not in mutated` would fail on a
    #   mutation that applied perfectly.
    remaining = {l.strip() for l in mutated.splitlines()}
    for gone in drop:
        assert gone not in remaining, f"mutation missed {gone!r}"

    before = Protego.parse(mutated)
    still = [
        (ua, u) for ua in IN_SCOPE for u in WITH_QUERY
        if before.can_fetch(u, ua)
    ]
    assert not still, (
        f"removing the /api/ Allow lines left {len(still)} advertised "
        f"query-string URLs fetchable (e.g. {still[:3]}), so those lines are not "
        "what unblocks them. Most likely `Disallow: /*?` was dropped from a "
        "group instead."
    )


# ── The guard that keeps THIS guard runnable ───────────────────────────────
def test_ci_installs_protego():
    """★ Every protego-based assertion in this repo is behind importorskip, and
    protego was in NO workflow and NOT in requirements.txt — so
    test_robots_canon_querystrings.py and test_robots_crawl_hygiene.py have been
    SKIPPING in CI since the day they were written, reporting green while the
    surface they guard drifted. A missing dependency is 'could not run', which
    is not 'ran and passed'.

    This test deliberately does NOT importorskip: it is a plain string check so
    it runs everywhere, including on a machine without protego, and fails loudly
    if the install line is ever trimmed.
    """
    wf = os.path.join(ROOT, ".github", "workflows", "pre-merge.yml")
    assert os.path.exists(wf), wf
    with open(wf, encoding="utf-8") as fh:
        text = fh.read()
    assert "pytest" in text, "wrong workflow — no pytest install step found"
    assert "protego" in text.lower(), (
        "protego is not installed by pre-merge.yml, so every robots guard in "
        "tests/ silently skips there. Add it to the pip install line."
    )
