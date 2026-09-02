"""GUARD — a health endpoint must be able to say "I am sick" THROUGH THE EDGE.

WHAT WAS MEASURED (2026-09-02, ENTSO-E Transparency maintenance outage)

    railway .../api/v1/iso/eu/health -> 503   1.6s   (correct: the feed IS dead)
    edge    .../api/v1/iso/eu/health -> 200  19-39s
            x-dc-hub-backend: render   x-dc-hub-failover: true
            x-dc-worker-version: 4.9.48

Bodies BYTE-IDENTICAL — only the status differed, so a body diff does NOT
catch this.

worker.js STEP 2 returns Railway's response only when `resp.status < 500`. A
deliberate 503 falls through to STEP 2.5, which asks the STALE Render build
(IS_FAILOVER=true) the same question and ships its 200 because `< 400` passes.
#3568 had just made that route `200 if live_feed_ok else 503`, for the stated
reason that "every monitor that reads a status code saw green" through a 50h
outage. It deployed, it is correct at the origin, and the edge converted it
back to 200 one hop later.

★★★ FIXED IN THE WRONG WORKER FIRST — the SECOND time this exact mistake has
been made, in the opposite direction. dchub-frontend#1303 put the guard in the
PAGES worker (_worker.js, v4.67.x) and changed NOTHING here, because
api.dchub.cloud is served by THIS zone worker — the measured response said
x-dc-worker-version 4.9.48. The mirror image is already written down in
dchub-frontend/tests/qa-failover-render-2xx-only.test.mjs ("THIS BUG WAS FIXED
IN THE WRONG WORKER FIRST"), where the backend worker got a fix Pages needed.

    ★ READ x-dc-worker-version BEFORE choosing the file:
        4.9.x   = dchub-backend/worker.js   (api.dchub.cloud zone routes)
        4.6x.x  = dchub-frontend/_worker.js (Pages)

THE CONTRACT
  1. The exemption exists at all (anti-vacuous — fail loud if removed).
  2. It matches EXACTLY, never by prefix.
  3. It runs BEFORE the Render failover AND before the KV-stale step — KV
     would serve a cached 200 from when the feed was healthy, the same lie by
     another route.
  4. It keeps the `resp &&` null guard: a timeout has no verdict to preserve.

MUST-FAIL — executed 2026-09-02, each mutation confirmed applied on disk:
    baseline                                        exit=0  7 passed
    M1 remove the exemption entirely (anti-vacuous) exit=1  3 failed
    M2 drop the route from the Set                  exit=1  1 failed
    M3 prefix match instead of Set.has              exit=1  1 failed
    M4 move it AFTER the Render failover            exit=1  1 failed
    M5 drop the `resp &&` null guard                exit=1  3 failed
    M6 verdict becomes cacheable                    exit=1  1 failed
    M7 WORKER_VERSION left unbumped                 exit=1  1 failed

NO NETWORK: reads worker.js as text.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "worker.js").read_text(encoding="utf-8")


def test_the_exemption_still_exists():
    """Anti-vacuous. Every ordering assertion below is vacuously true if the
    block is gone, so anchor on its existence first."""
    assert "const VERDICT_ROUTES = new Set([" in SRC, (
        "the verdict-route exemption is gone from worker.js — /api/v1/iso/eu/health "
        "503 is laundered into the stale Render build's 200. If it was removed on "
        "purpose, delete this test in the SAME commit rather than letting it pass "
        "by finding nothing to check"
    )
    assert re.search(r"const isVerdictRoute = ", SRC), "the predicate is gone"


def test_the_measured_route_is_listed():
    m = re.search(r"const VERDICT_ROUTES = new Set\(\[([\s\S]*?)\]\);", SRC)
    assert m, "VERDICT_ROUTES set not parseable"
    routes = re.findall(r"'([^']+)'", m.group(1))
    assert "/api/v1/iso/eu/health" in routes, (
        "the one route with live evidence is not listed; routes=%r" % routes)


def test_matching_is_exact_not_prefix():
    """A prefix would suppress failover for genuinely broken routes — the exact
    failure the ladder exists to prevent. /api/v1/iso/eu/snapshot is the live
    control: it legitimately failed over during the same outage."""
    m = re.search(r"const isVerdictRoute = ([^;]+);", SRC)
    body = m.group(1)
    assert ".has(" in body, "predicate no longer uses Set.has — %s" % body
    # f-string, not %s: regression_lint's [url-format-typo] rule blocks a
    # literal %s on a line carrying a URL path, because that shape is the
    # psycopg2/URL formatting bug it exists to catch. The rule is right about
    # the shape even when this instance is only a message.
    assert "startsWith" not in body, (
        "prefix matching would swallow /api/v1/iso/eu/healthz and "
        f"/api/v1/iso/eu/health/deep: {body}")


def test_it_short_circuits_before_failover_AND_before_kv_stale():
    """The ordering is the whole fix. Anchors are asserted to appear exactly
    once so the index comparison is structural, not a substring that happens
    to be present somewhere."""
    guard = "if (resp && isVerdictRoute(pathname)) {"
    failover = "// STEP 2.5: Render failover"
    kv_stale = "// STEP 3: Stale KV"
    for name, anchor in (("guard", guard), ("STEP 2.5", failover), ("STEP 3", kv_stale)):
        assert SRC.count(anchor) == 1, (
            "anchor %r appears %d times, expected exactly 1 — the ordering "
            "assertions below stop being meaningful" % (name, SRC.count(anchor)))
    assert SRC.index(guard) < SRC.index(failover), (
        "the exemption runs AFTER the Render failover — Render answers first "
        "and its stale 200 ships, which is the entire bug")
    assert SRC.index(guard) < SRC.index(kv_stale), (
        "the exemption runs AFTER the KV-stale step, which would serve a cached "
        "200 from when the feed was healthy — the same lie by another route")


def test_a_timeout_still_takes_the_normal_ladder():
    """`resp &&` is load-bearing: a null resp means the primary timed out, so
    there is no verdict to preserve and failover/KV must still run."""
    assert "if (resp && isVerdictRoute(pathname)) {" in SRC, (
        "the null-response guard is gone — a primary TIMEOUT would short-circuit "
        "to nothing instead of failing over")


def test_the_verdict_response_is_not_cached():
    """A cached verdict is a stale verdict: the next caller must get a fresh
    one, or the fix reintroduces the problem with a different mechanism."""
    block = re.search(
        r"if \(resp && isVerdictRoute\(pathname\)\) \{([\s\S]{0,900}?)\n    \}", SRC)
    assert block, "verdict block not parseable"
    assert "no-store" in block.group(1), (
        "the verdict response is cacheable; a cached 200 outlives the outage")


def test_worker_version_was_bumped_so_the_deploy_is_verifiable():
    """x-dc-worker-version is how this change is confirmed live from outside —
    the same header that revealed the fix had gone into the wrong worker."""
    m = re.search(r"const WORKER_VERSION = '([^']+)'", SRC)
    assert m, "WORKER_VERSION missing"
    assert m.group(1) != "4.9.48-anon-callable-flag", (
        "WORKER_VERSION is unchanged from the version measured serving the bug; "
        "a deploy cannot be told apart from a non-deploy")
