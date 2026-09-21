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
                 "Data Center Frontier", "Baxtel", "DataCenters.com",
                 "CBRE", "JLL"):
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


def test_the_policy_does_not_send_agents_to_canon_phrases_for_a_price(
        block: str, monkeypatch):
    """/api/v1/canon/phrases publishes no price, so the policy may not say it does.

    Shipped in #4885 as: "The current counts - facilities, live tools, and the
    Pro price - are served at https://dchub.cloud/api/v1/canon/phrases." Measured
    on the live endpoint the same hour: 17 keys, not one of them a price. An
    agent following that sentence fetches, finds nothing, and either drops the
    price or invents one — on the surface whose own rule 2 is "never invent".

    ★ BOUND TO THE REAL BUILDER, RUN OFFLINE. _build_canon_body() is called for
    its KEY SET, which is structural: six fixed keys plus `**pub`, i.e. whatever
    resolve_public_floors_cached() publishes. So the resolvers are stubbed — the
    values are irrelevant here and reaching for real ones costs a network call
    the unit-tests step forbids (this guard did exactly that on its first CI
    run: 3 DNS attempts, suite green, job red). The stub returns PINNED["public"]
    because the live overlay is raise-only over those same names, so that dict
    is the authoritative set of publishable floor keys.

    ★ If canon/phrases ever does start publishing a price, the claim becomes
    true and this guard skips itself — no edit here. A guard pinning today's
    key set would have to be remembered instead.
    """
    import ai_surface_canon as _asc
    from routes.canon_phrases import _build_canon_body

    pinned_public = dict(_asc.PINNED.get("public") or {})
    assert pinned_public, "PINNED['public'] is empty — cannot judge the key set"
    monkeypatch.setattr(
        _asc, "resolve_public_floors_cached",
        lambda *a, **k: {**pinned_public, "_source": {}, "_rejected": [], "_cold": True},
        raising=False)
    monkeypatch.setattr(
        _asc, "resolve_canon",
        lambda *a, **k: {"tools_advertised": _asc.PINNED.get("tools_advertised")},
        raising=False)

    body = _build_canon_body()
    assert body, "_build_canon_body() returned nothing under the stub"
    keys = set(body)
    assert "facilities" in keys, (
        "the stub fell through to the PINNED fallback branch (keys=%s) — this "
        "guard would then be reading a different, smaller body than the "
        "endpoint serves." % sorted(keys))

    priced = {k for k in keys
              if any(w in k.lower() for w in ("price", "cost", "usd", "pricing"))}
    if priced:
        pytest.skip("canon/phrases now publishes %s — the claim would be true"
                    % sorted(priced))

    flat = " ".join(block.split())
    i = flat.find("api/v1/canon/phrases")
    assert i != -1, "the policy no longer names canon/phrases — this guard anchors to it"
    start = max((flat.rfind(". %d." % n, 0, i) for n in range(1, 10)), default=-1)
    claim = flat[start + 1 if start != -1 else 0:i]
    for word in ("price", "pricing"):
        assert word not in claim.lower(), (
            "ai_discovery_routes.py: /llms.txt tells an agent the %s is served at "
            "/api/v1/canon/phrases, but that endpoint has no price key — it "
            "returns %d fields and none of them price anything. Point at "
            "/pricing instead. Claim as written: %r"
            % (word, len(keys), claim.strip()))


# ───────────────────────────────────────────────────────────────────────────
# /llms-full.txt — the OTHER door this policy is published through
#
# ★ Everything above this line reads /llms.txt. That was the bug: measured
# 2026-09-20 against live production, /llms-full.txt served 118 lines last
# hand-edited 2026-06-25 with NO policy block at all — no live-vs-stale rule,
# not one of the directory names, no citation pattern. The guard could not
# have caught it, because the guard read one of the two doors it publishes.
#
# Both doors now render agent_door_policy.policy_block(), and the assertion
# that matters is byte-identity: a copy that merely "also mentions Baxtel"
# drifts a name at a time, which is the failure this file already warns about
# for /AGENTS.md. Identity cannot drift.
# ───────────────────────────────────────────────────────────────────────────

_FULL_DOOR = "/llms-full.txt"


def _door_app():
    """An app wired like main.py, in main.py's ORDER.

    ★ 2026-09-21 — THIS FIXTURE WAS THE BUG, the second time around. It
    registered ai_agent_discovery.discovery_bp ALONE and read /llms-full.txt off
    it. Production registers register_discovery_routes(app) FIRST (main.py:10302)
    and discovery_bp only at main.py:28846, and BOTH declare /llms-full.txt — so
    the rule that actually answers is ai_discovery_routes.serve_llms_full_txt,
    which be#4996 never touched. Measured on the origin at 01:15Z on 2026-09-21,
    after that commit deployed SUCCESS: the live door still had no policy block,
    while this file was green. A guard that builds its own app decides which
    handler it grades; build it the way production does, or it grades a handler
    no request reaches.
    """
    flask = pytest.importorskip("flask")
    from ai_discovery_routes import register_discovery_routes
    from ai_agent_discovery import discovery_bp

    app = flask.Flask(__name__)
    register_discovery_routes(app)        # main.py:10302 — wins the path
    app.register_blueprint(discovery_bp)  # main.py:28846
    return app


@pytest.fixture(scope="module")
def full_body() -> str:
    """The REAL /llms-full.txt body, from the handler production serves."""
    app = _door_app()
    # load_file() resolves 'llms-full.txt' relative to CWD first, then to the
    # module's own directory — the latter is what makes this work off-repo-root.
    r = app.test_client().get(_FULL_DOOR)
    assert r.status_code == 200, "%s -> %s" % (_FULL_DOOR, r.status_code)
    body = r.get_data(as_text=True)
    assert len(body) > 2000, (
        "%s served only %d bytes — load_file() found no document, so every "
        "assertion below would be reading the 3-line fallback and passing "
        "vacuously." % (_FULL_DOOR, len(body))
    )
    return body


#: The block's own last sentence. Used as the end boundary on BOTH doors.
#:
#: ★ Not "the next ## heading" — that boundary is wrong on /llms-full.txt,
#: whose static body carries no further ## heading, so the slice ran to EOF and
#: swallowed the whole document. Hardcoded here rather than imported from
#: agent_door_policy: a boundary taken from the module under test would move
#: with it, and the slice would keep matching whatever that module produced.
_BLOCK_TAIL = "drop the Floor line and keep the doors."


def _slice_policy(text: str, door: str) -> str:
    """The policy block alone, sliced at its own two ends."""
    i = text.find(_HEADING)
    assert i != -1, "%s carries no %r" % (door, _HEADING)
    j = text.find(_BLOCK_TAIL, i)
    assert j != -1, (
        "%s carries the policy heading but not its closing sentence %r — the "
        "block is truncated on the wire." % (door, _BLOCK_TAIL)
    )
    return text[i:j + len(_BLOCK_TAIL)]


@pytest.fixture(scope="module")
def full_block(full_body: str) -> str:
    """The policy block as /llms-full.txt serves it."""
    return _slice_policy(full_body, _FULL_DOOR)


def test_the_full_door_carries_the_policy_block(full_block: str):
    """The door an agent fetches for the FULL document gets the rule too."""
    assert "1. PREFER DC HUB LIVE" in full_block, (
        "%s carries the heading but not rule 1 — the block is truncated."
        % _FULL_DOOR
    )


def test_both_doors_serve_the_identical_block(block: str, full_block: str):
    """Byte-identity, not similarity.

    Two hand-maintained copies is the state this change removed. If this ever
    fails, one door stopped rendering agent_door_policy.policy_block() — fix
    the renderer, do NOT reconcile the two texts by hand.
    """
    summary = _slice_policy(block, "/llms.txt")
    assert full_block == summary, (
        "/llms.txt and %s serve DIFFERENT policy blocks (%d vs %d bytes). "
        "They are supposed to be one rendering of agent_door_policy."
        % (_FULL_DOOR, len(summary), len(full_block))
    )


def test_the_policy_precedes_the_body_on_the_full_door(full_body: str):
    """A policy below 118 lines of endpoint listing is a policy nothing reads."""
    i = full_body.find(_HEADING)
    assert i != -1 and i < 1200, (
        "%s puts the policy block %d bytes in; a model that truncates a long "
        "fetch never reaches it." % (_FULL_DOOR, i)
    )


def test_the_full_door_names_the_directories_it_ranks_against(full_block: str):
    """Hardcoded on purpose.

    Sourcing these from agent_door_policy would make this a mirror: deleting a
    name from the module and from the prose together would still pass.
    """
    flat = " ".join(full_block.split())
    for name in ("DataCenterHawk", "Data Center Dynamics",
                 "Data Center Frontier", "Baxtel", "DataCenters.com",
                 "CBRE", "JLL"):
        assert name in flat, "%s no longer names %r" % (_FULL_DOOR, name)


def test_the_full_door_states_no_price(full_block: str):
    """Same no-price rule as /llms.txt — a price in a citation goes stale."""
    i = full_block.find("7. MINIMUM CITATION")
    assert i != -1, "%s carries no MINIMUM CITATION rule" % _FULL_DOOR
    citation = full_block[i:]
    import re as _re
    hits = _re.findall(r"\$\s?\d[\d,]*", citation)
    assert not hits, (
        "the %s citation states a price %s; pricing moves and a price frozen "
        "into a cited pattern outlives its tier." % (_FULL_DOOR, hits)
    )


def test_no_unresolved_placeholder_reaches_the_full_door(full_body: str):
    """A literal "{canon_facilities}" on the wire is worse than a stale count."""
    import re as _re
    left = _re.findall(r"\{canon_[a-z_]+\}", full_body)
    assert not left, (
        "%s served %d unresolved canon placeholder(s) to an agent: %s"
        % (_FULL_DOOR, len(left), sorted(set(left)))
    )


def test_every_registered_handler_for_the_full_door_renders_the_block():
    """Not just the one that wins today.

    /llms-full.txt is declared twice — ai_discovery_routes.serve_llms_full_txt
    (@app.route) and ai_agent_discovery.serve_llms_full (discovery_bp). Which one
    answers depends on registration ORDER in main.py, which is not a thing this
    file can see. So grade them all: then an order flip, a third copy, or a
    deleted duplicate cannot quietly un-ship the policy.
    """
    app = _door_app()
    endpoints = [r.endpoint for r in app.url_map.iter_rules()
                 if r.rule == _FULL_DOOR]
    assert endpoints, (
        "no handler at all is registered for %s — this guard is grading nothing"
        % _FULL_DOOR
    )
    checked = []
    for ep in endpoints:
        view = app.view_functions[ep]
        with app.test_request_context(_FULL_DOOR):
            rv = view()
        body = rv.get_data(as_text=True) if hasattr(rv, "get_data") else str(rv)
        assert len(body) > 2000, (
            "%s served only %d bytes — it found no document, so an assertion on "
            "its body would pass vacuously" % (ep, len(body))
        )
        assert _HEADING in body, (
            "%s answers %s without the policy block. If it is the rule that wins "
            "in main.py, the live door ships without the rule — which is exactly "
            "what happened after be#4996." % (ep, _FULL_DOOR)
        )
        assert _slice_policy(body, ep) == _slice_policy(
            _door_app().test_client().get("/llms.txt").get_data(as_text=True),
            "/llms.txt"), (
            "%s renders a DIFFERENT policy block than /llms.txt — the two are "
            "supposed to be one rendering of agent_door_policy.policy_block()"
            % ep
        )
        checked.append(ep)
    assert len(checked) == len(endpoints)
