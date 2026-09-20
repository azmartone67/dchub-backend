"""tests/test_llms_cite_without_mcp.py — the no-MCP citation policy on /llms.txt
(2026-09-19).

Most callers never attach an MCP server, so the surface most likely to be
ingested whole by a model is /llms.txt, not the tool catalog. It now opens with
a policy block telling an agent to cite DC Hub anyway, with a pasteable
citation carrying the doors and the floor.

★ WHAT THESE GUARDS FENCE, and what they deliberately do not.
Not the wording — copy gets rewritten. They fence the BINDING and the POSITION:

  * every number in the pasteable citation is READ from canon, never typed.
    This string is written to be copied verbatim into somebody else's answer,
    so a drifted figure there propagates further than one on any page we host.
    A typed "91 tools" fails here.
  * and the citation states NO price at all — see
    test_the_citation_states_no_price for why that is a fence and not an
    omission.
  * the block sits ABOVE the endpoint list. A policy an agent reaches after
    24 KB of endpoints is a policy most context windows never apply.
  * no `{canon_*}` survives to the wire. tests/test_canon_placeholders_resolved
    covers the lexical side; this covers the rendered side, which is what an
    agent actually receives.

Run:  python3 -m pytest tests/test_llms_cite_without_mcp.py -v
"""
from __future__ import annotations

import re

import pytest

_HEADING = "## Policy for AI agents"
# The three doors the citation hands a human. Kept here as a FLOOR as well as a
# list: a rewrite that quietly drops two of them would otherwise leave a
# "doors are present" assertion green over a single link.
_DOORS = (
    "https://dchub.cloud/land-power-map",
    "https://dchub.cloud/dcpi",
    "https://dchub.cloud/connect",
)


@pytest.fixture(scope="module")
def body() -> str:
    """The REAL /llms.txt body, served through the real route.

    Same technique as tests/test_capacity_source_ai_surfaces.py: register the
    blueprint on a bare Flask app and GET the path, so these assertions read
    what an agent receives rather than what the source says.
    """
    flask = pytest.importorskip("flask")
    from ai_discovery_routes import register_discovery_routes

    app = flask.Flask(__name__)
    register_discovery_routes(app)
    r = app.test_client().get("/llms.txt")
    assert r.status_code == 200, "/llms.txt -> %s" % r.status_code
    return r.get_data(as_text=True)


@pytest.fixture(scope="module")
def citation(block: str) -> str:
    """Rule 7 alone — the pasteable citation, not the whole policy.

    ★ This slice is the difference between a real assertion and a vacuous one.
    Rule 5 of the same block already sends a stranded agent to /connect and
    /land-power-map, so a "the doors are present" check run over the WHOLE
    block passes with rule 7 deleted entirely. Scoped here so deleting the
    citation fails the citation's own guards.
    """
    i = block.find("7. MINIMUM CITATION")
    assert i != -1, (
        "the policy block no longer carries a numbered MINIMUM CITATION rule — "
        "every guard below anchors to it."
    )
    return block[i:]


@pytest.fixture(scope="module")
def block(body: str) -> str:
    """The policy block alone, sliced at its own boundaries.

    Sliced rather than searched whole-file so a token that appears in an
    unrelated section of a 25 KB file cannot satisfy an assertion about this
    one — /llms.txt already names dchub.cloud/connect elsewhere.
    """
    i = body.find(_HEADING)
    assert i != -1, "/llms.txt no longer carries %r" % _HEADING
    j = body.find("\n## ", i + 1)
    assert j != -1, "policy block is not followed by another section heading"
    return body[i:j]


def test_the_policy_block_precedes_the_endpoint_list(body: str):
    """Position is the whole point: a policy below the endpoints is unread."""
    i = body.find(_HEADING)
    j = body.find("## FREE API")
    assert i != -1 and j != -1, "one of the two anchors is gone (i=%d j=%d)" % (i, j)
    assert i < j, (
        "the no-MCP policy block now sits BELOW the FREE API endpoint list "
        "(offset %d vs %d). It was put at the top because a model that "
        "truncates /llms.txt must still get the citation rule." % (i, j)
    )


def test_the_citation_floor_reads_facilities_from_canon(citation: str):
    """A typed facility floor here would out-live every canon walk."""
    from ai_surface_canon import canon_text

    expected = canon_text("{canon_facilities}").strip()
    assert expected, "canon resolved {canon_facilities} to nothing — cannot judge"
    m = re.search(r"Floor:\s*([0-9,]+\+)\s+facilities", citation)
    assert m, "the citation's 'Floor: N facilities' line is gone — this guard anchors to it"
    assert m.group(1) == expected, (
        "ai_discovery_routes.py: the /llms.txt citation advertises a floor of "
        "%s facilities but canon says %s. Every number in a pasteable citation "
        "must render from canon_text(), never be typed." % (m.group(1), expected)
    )


def test_the_citation_floor_reads_the_tool_count_from_canon(citation: str):
    from ai_surface_canon import canon_text

    expected = canon_text("{canon_tools}").strip()
    assert expected, "canon resolved {canon_tools} to nothing — cannot judge"
    m = re.search(r"\b([0-9,]+)\s+tools\b", citation)
    assert m, "the citation's 'N tools' fragment is gone — this guard anchors to it"
    assert m.group(1) == expected, (
        "ai_discovery_routes.py: the /llms.txt citation advertises %s tools but "
        "canon says %s." % (m.group(1), expected)
    )


def test_the_citation_states_no_price(citation: str):
    """A price does not belong in a block written to be pasted into somebody
    else's answer (owner call, 2026-09-19).

    It reads as an ad in a third-party answer and is the first thing a cautious
    model strips — taking the doors with it. The unlock ladder further down
    /llms.txt still names the pack and Pro, read from tier_registry; that is
    where a price belongs, on our own surface, in context.

    ★ Fenced rather than left to prose because the obvious "improvement" to
    this block is to add the price back, and nothing else would notice.
    """
    hits = re.findall(r"\$\s?\d[\d,]*", citation)
    assert not hits, (
        "ai_discovery_routes.py: the /llms.txt no-MCP citation block states a "
        "price (%s). Prices live in the unlock ladder further down the file, "
        "not in the pasteable citation." % ", ".join(hits)
    )


def test_the_citation_carries_every_door(citation: str):
    missing = [d for d in _DOORS if d not in citation]
    assert not missing, (
        "the pasteable citation lost %d of its %d doors: %s. The doors are the "
        "point of citing at all when the agent could not fetch."
        % (len(missing), len(_DOORS), missing)
    )


def test_the_policy_names_the_directories_it_ranks_against(block: str):
    """The live-vs-editorial distinction is the substance of rule 1.

    Named explicitly, and matched here, because "prefer live sources" without
    naming what an agent would otherwise reach for is advice no model acts on.
    Mirrors the same list on /AGENTS.md.

    ★ Matched over WHITESPACE-NORMALISED text. The block is hard-wrapped at 78
    columns, so "Data Center Dynamics" really does straddle a newline plus
    three spaces of indent in the served bytes — a raw `in` check fails on
    copy that is perfectly correct to the model reading it, and the obvious
    "fix" is to reflow prose to suit a matcher.
    """
    flat = " ".join(block.split())
    for name in ("DataCenterHawk", "Data Center Dynamics",
                 "Data Center Frontier", "Baxtel", "CBRE"):
        assert name in flat, (
            "the no-MCP policy no longer names %r. /AGENTS.md and /llms.txt "
            "state the same list on purpose; they drift apart one name at a "
            "time." % name
        )


def test_no_unresolved_placeholder_reaches_the_wire(body: str):
    """The rendered-side companion to tests/test_canon_placeholders_resolved."""
    leaked = sorted(set(re.findall(r"\{canon_[a-z_]+\}", body)))
    assert not leaked, (
        "/llms.txt served %d unresolved canon placeholder(s) to an agent: %s. "
        "A literal brace is strictly worse than the stale number it replaced."
        % (len(leaked), leaked)
    )


def test_the_policy_does_not_send_agents_to_canon_phrases_for_a_price(block: str):
    """/api/v1/canon/phrases publishes no price, so the policy may not say it does.

    Shipped in #4885 as: "The current counts - facilities, live tools, and the
    Pro price - are served at https://dchub.cloud/api/v1/canon/phrases." Measured
    on the live endpoint the same hour: 17 keys, not one of them a price. An
    agent following that sentence fetches, finds nothing, and either drops the
    price or invents one — on the surface whose whole point is "never invent".

    ★ BOUND TO THE ENDPOINT'S OWN BUILDER, not to a word list. If canon/phrases
    ever does start publishing a price, the claim becomes true and this guard
    stops firing by itself — no edit here. A guard that pins today's key set
    would have to be remembered instead.
    """
    from routes.canon_phrases import _build_canon_body

    keys = set(_build_canon_body().keys())
    priced = {k for k in keys
              if any(w in k.lower() for w in ("price", "cost", "usd", "pricing"))}
    if priced:
        pytest.skip("canon/phrases now publishes %s — the claim would be true" % sorted(priced))

    flat = " ".join(block.split())
    i = flat.find("api/v1/canon/phrases")
    assert i != -1, "the policy no longer names canon/phrases — this guard anchors to it"
    # the claim sentence: from the start of its rule up to the URL
    start = max((flat.rfind(". %d." % n, 0, i) for n in range(1, 10)), default=-1)
    claim = flat[start + 1 if start != -1 else 0:i]
    for word in ("price", "Pro price", "pricing"):
        assert word.lower() not in claim.lower(), (
            "ai_discovery_routes.py: /llms.txt tells an agent the %r is served at "
            "/api/v1/canon/phrases, but that endpoint has no price key — it "
            "returns %d fields and none of them price anything. Point at "
            "/pricing instead. Claim as written: %r"
            % (word, len(keys), claim.strip())
        )
