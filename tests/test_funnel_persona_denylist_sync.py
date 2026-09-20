"""Two denylists for one population (2026-09-20) — a name cannot be ours in
mcp_calls_deloop and real demand on the funnel board.

mcp_calls_deloop.INTERNAL_PLATFORM_VALUES is the CURATED list of platform tags
that are ours or a third-party catalogue crawler. It carries an explicit
"DELIBERATELY NOT EXCLUDED" section, so it is a decided list, not a guess.

routes/schema_repair's funnel view computes is_synthetic / is_registry_probe
over the SAME strings and had never been taught 9 of them. 'acme-siting-agent'
and 'reviewer-sim' are the QA judge fleet's personas — self-traffic to one
layer, unconverted demand to the other, on the board that sets priorities.

★ 'acme-siting-agent' is also the client name our own live verification
procedures send. A probe named to look like a real agent to the gate looks
like one to the funnel too.

CI-SAFETY: source-level, no network, no DB.
"""
import ast
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _like_re(pattern):
    """Translate a SQL LIKE pattern to a regex.

    ★ NOT re.escape(p).replace(r"\\%", ".*"). re.escape does not escape '%',
    so that spelling silently leaves every wildcard literal and the emulation
    matches nothing — which reads as "the view does not cover this name" for
    EVERY name. That mistake produced a 24-name gap list here before
    test_like_translation_is_not_vacuous caught it; the real gap was 9.
    """
    return "".join(".*" if c == "%" else "." if c == "_" else re.escape(c)
                   for c in pattern)


def _vocabulary(src, start, end):
    """The IN(...) literals and LIKE patterns of one CASE expression."""
    block = src[src.index(start):src.index(end)]
    exact = set()
    for m in re.finditer(r"IN \(([^)]*)\)", block, re.S):
        exact |= {x.strip().strip("'") for x in m.group(1).split(",")
                  if x.strip().startswith("'")}
    return exact, [m.group(1) for m in re.finditer(r"LIKE '([^']+)'", block)]


@pytest.fixture(scope="module")
def view_src():
    with open(os.path.join(ROOT, "routes", "schema_repair.py"),
              encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def classify(view_src):
    s_exact, s_like = _vocabulary(
        view_src, "WHEN s.self_traffic IS TRUE", "END AS is_synthetic")
    r_exact, r_like = _vocabulary(
        view_src, "-- r-self-traffic (2026-08-17): third-party CATALOG",
        "END AS is_registry_probe")

    def _hit(name, exact, likes):
        n = (name or "").lower()
        return n in exact or any(re.fullmatch(_like_re(p), n) for p in likes)

    def _classify(name):
        n = (name or "").lower()
        if len(n) <= 2 or _hit(n, s_exact, s_like):
            return "synthetic"
        if _hit(n, r_exact, r_like):
            return "registry_probe"
        return None
    return _classify


@pytest.fixture(scope="module")
def curated():
    with open(os.path.join(ROOT, "mcp_calls_deloop.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "INTERNAL_PLATFORM_VALUES"):
            return [e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    pytest.fail("INTERNAL_PLATFORM_VALUES not found in mcp_calls_deloop")


# ── the emulation must be able to report a miss AND a hit ────────────

def test_like_translation_is_not_vacuous():
    """The guard below is only as good as this translator. A translator that
    matches nothing makes every name look uncovered; one that matches
    everything makes the whole file pass vacuously. Pin both directions."""
    assert re.fullmatch(_like_re("%catalog-sync%"), "catalog-sync")
    assert re.fullmatch(_like_re("dchub-%"), "dchub-internal")
    assert re.fullmatch(_like_re("%-probe"), "fabrique-noauth-probe")
    assert not re.fullmatch(_like_re("dchub-%"), "acme-siting-agent")
    assert not re.fullmatch(_like_re("mcphub%"), "manifest-mirror")


def test_classifier_reads_a_real_vocabulary(classify):
    """If the CASE blocks failed to parse, every assertion below would pass by
    returning None for everything — or fail for everything. Anchor on names the
    view has carried for weeks."""
    assert classify("dchub-selfheal") == "synthetic"
    assert classify("smithery-gateway") == "registry_probe"
    assert classify("claude") is None


# ── the fix: our own personas are ours on BOTH boards ────────────────

@pytest.mark.parametrize("persona", ["acme-siting-agent", "reviewer-sim"])
def test_qa_judge_personas_are_synthetic(classify, persona):
    """server.mjs _INTERNAL_SELF_TAG does not catch either name — neither
    contains dchub/verify/probe/test/harness/... — so detectPlatformFromInit
    MINTS the raw name as a platform and it reaches mcp_client verbatim.
    is_synthetic is the only thing standing between that and the board."""
    assert classify(persona) == "synthetic"


@pytest.mark.parametrize("crawler", [
    "mcp-gateway-registry", "manifest-mirror", "watchdog",
    "mcpqueen-grader", "mcpexplorerbot", "mcpindex-trust",
    "mcp-rugpull-research",
])
def test_third_party_crawlers_are_registry_probes_not_synthetic(classify, crawler):
    """These are NOT ours, so they must not be folded into is_synthetic —
    mcp_funnel_real deliberately still contains registry probes ("reported, not
    silently subtracted"), and blanket-excluding a gateway deletes real demand.
    Pinning the class, not just "is excluded", is what keeps that distinction."""
    assert classify(crawler) == "registry_probe"


# ── and it must not drift back ───────────────────────────────────────

# server.mjs _INTERNAL_SELF_TAG rewrites these to 'dchub-internal' BEFORE they
# reach mcp_client, so the view never sees the raw string. Frozen here rather
# than waved through: a name that LEAVES that regex in the mcp-server repo
# becomes demand on this board silently, and this list is where someone looking
# at that incident will find the coupling named.
_REWRITTEN_BY_MCP_SERVER = frozenset({
    "capwall2", "clawith", "dbg", "dchubhealer", "f5r", "final", "fix2-v2",
    "full", "mcp-vouch", "raw", "rev", "value-harness", "vinline",
})


def test_no_curated_name_reads_as_real_demand(classify, curated):
    """The invariant. Every name mcp_calls_deloop has decided is not real
    demand must be classified by the funnel view too — or be an explicitly
    frozen cross-repo case above. A new persona added to the curated list
    fails here instead of quietly inflating the leakage board."""
    invisible = sorted(n for n in curated
                       if classify(n) is None
                       and n.lower() not in _REWRITTEN_BY_MCP_SERVER)
    assert not invisible, (
        "curated as not-real-demand in mcp_calls_deloop, but the funnel view "
        f"publishes them as real callers: {invisible}")


def test_frozen_rewrite_set_has_no_dead_entries(curated):
    """A frozen exemption list rots into a blanket waiver. Every entry must
    still be a name the curated list actually carries."""
    stale = sorted(n for n in _REWRITTEN_BY_MCP_SERVER
                   if n not in {c.lower() for c in curated})
    assert not stale, f"exempted names no longer in the curated list: {stale}"


# Port of dchub-mcp-server server.mjs _INTERNAL_SELF_TAG (L1786). It is a
# MIRROR across repos and cannot detect a change made over there — but without
# it the exemption set above is an unguarded escape hatch, and the cheapest way
# to silence the invariant is to add a name to it rather than teach the view.
_SELF_TAG_FAMILY = re.compile(
    r"(dchub|verify|probe|audit|harness|test|check|diag|sweep|selfheal"
    r"|canary|smoke|regression)")
_SELF_TAG_EXACT = re.compile(
    r"^(clawith|value-harness|dbg|raw|full|f5r|fv|rev|final|vinline|qa"
    r"|qa-mozilla|fix2-v2|mcp-vouch|capwall2|pipeline_mcp|dchubhealer)$")


def _rewritten_by_mcp_server(name):
    safe = re.sub(r"[^a-z0-9_-]", "", (name or "").lower())[:40]
    return bool(safe) and bool(
        len(safe) <= 2 or _SELF_TAG_FAMILY.search(safe)
        or _SELF_TAG_EXACT.match(safe))


def test_every_exempted_name_is_actually_rewritten():
    """The exemption is "the mcp-server rewrites this before it reaches
    mcp_client" — so each entry must satisfy that rule. Without this, adding a
    new persona to the exemption set silences
    test_no_curated_name_reads_as_real_demand while the persona goes on being
    published as real demand. A mutation run caught exactly that: dropping
    'acme-siting-agent' and 'reviewer-sim' into this set left all 13 tests
    green."""
    not_rewritten = sorted(n for n in _REWRITTEN_BY_MCP_SERVER
                           if not _rewritten_by_mcp_server(n))
    assert not not_rewritten, (
        "exempted as 'rewritten by the mcp-server', but _INTERNAL_SELF_TAG "
        f"does not match: {not_rewritten} — teach the view instead")


def test_no_exemption_is_dead(classify):
    """An exemption is only justified while the view does NOT classify the
    name. Four entries in the first draft of this set ('yellowmcp-health',
    'agentpulse', 'mcpscoringengine', 'catalog-sync') were already matched by
    is_synthetic / is_registry_probe — exempting them said the view could not
    see names it saw perfectly well, which is how an exemption list grows into
    a waiver nobody reads."""
    dead = sorted(n for n in _REWRITTEN_BY_MCP_SERVER if classify(n) is not None)
    assert not dead, f"exempted but already classified by the view: {dead}"


def test_the_rewrite_port_can_say_no():
    """A port that returns True for everything turns the test above vacuous."""
    assert _rewritten_by_mcp_server("dchub-regression-test")
    assert _rewritten_by_mcp_server("clawith")
    assert not _rewritten_by_mcp_server("acme-siting-agent")
    assert not _rewritten_by_mcp_server("reviewer-sim")
    assert not _rewritten_by_mcp_server("claude")
