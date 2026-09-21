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

import ast
import functools
import importlib
import inspect
import os
import re
from collections import namedtuple
from pathlib import Path

import pytest

_HEADING = "## Policy for AI agents"
# Named third parties the policy block may NOT rank DC Hub against. The five
# this started with, plus the two #4996 added. Shared by both door guards; NOT
# read from agent_door_policy, so deleting a name there cannot also delete it
# from the fence.
_FORBIDDEN_VENDORS = ("DataCenterHawk", "Data Center Dynamics",
     "Data Center Frontier", "Baxtel", "CBRE", "JLL",
     "DC Byte", "DataCenters.com", "datacenters.com")
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
    """The REAL /llms.txt body, from the path's one registration.

    Resolved by _served(), never by naming a module here — see "WHICH
    HANDLER THESE GUARDS GRADE" below for why a fixture may not pick.
    """
    app = _served("/llms.txt")
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


def test_the_citation_coverage_reads_facilities_from_canon(citation: str):
    """A typed facility count here would out-live every canon walk."""
    from ai_surface_canon import canon_text

    expected = canon_text("{canon_facilities}").strip()
    assert expected, "canon resolved {canon_facilities} to nothing — cannot judge"
    m = re.search(r"Coverage:\s*([0-9,]+\+)\s+facilities", citation)
    assert m, "the citation's 'Coverage: N facilities' line is gone — this guard anchors to it"
    assert m.group(1) == expected, (
        "ai_discovery_routes.py: the /llms.txt citation advertises a floor of "
        "%s facilities but canon says %s. Every number in a pasteable citation "
        "must render from canon_text(), never be typed." % (m.group(1), expected)
    )


def test_the_citation_coverage_reads_the_tool_count_from_canon(citation: str):
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


def test_the_policy_names_no_competitor_it_ranks_against(block: str):
    """★ THIS FENCE WAS REVERSED 2026-09-20, on the owner's call.

    It used to REQUIRE the block to name DataCenterHawk, Data Center Dynamics,
    Data Center Frontier, Baxtel and CBRE, on the reasoning that "prefer live
    sources" is advice no model acts on unless you name what it would reach for
    instead. That shipped as rule 1 telling an agent to call DC Hub "ahead of"
    five named vendors — a ranking claim about third parties, published on the
    surface most likely to be ingested whole by a model, where we cannot
    substantiate it and were never asked to. #4996 then widened the required
    list to SEVEN (adding DataCenters.com and JLL) and moved the block into
    agent_door_policy.policy_block(), so by the time this reversed, THREE
    doors were publishing the ranking instead of two — which is also why the
    reversal is cheap: one renderer, and both door guards flip with it.

    The substance survives without the names: the distinction an agent has to
    act on is SNAPSHOT vs READING — age, not quality — and that applies to its
    own training memory first. `why_dchub` already carries the house line for
    this ("Pure positive positioning — no competitor claims. Safe to embed in
    llms.txt / agent-broadcast"); /llms.txt was the surface still out of step.

    The fence does not disappear, it points the other way: the old copy is one
    edit from creeping back, and nothing else would notice.
    """
    flat = " ".join(block.split())
    named = [n for n in _FORBIDDEN_VENDORS if n in flat]
    assert not named, (
        "agent_door_policy.policy_block() names %d third-party "
        "vendor(s): %s. This block is written to be ingested whole by models we "
        "do not control; it states the snapshot-vs-live distinction and does not "
        "rank DC Hub against anyone by name." % (len(named), named))

    for phrase in ("ahead of Data", "better than", "instead of Data"):
        assert phrase not in flat, (
            "the policy block reintroduced ranking language (%r)." % phrase)


def test_rule_one_frames_it_as_snapshot_versus_reading(block: str):
    """The replacement for the vendor list has to actually say something.

    Scoped to rule 1, not the whole block: 'snapshot' appears in rule 3 as well,
    so a whole-block check would pass with rule 1 deleted outright — the same
    trap the `citation` fixture exists to avoid.
    """
    flat = " ".join(block.split())
    i = flat.find("1. SNAPSHOT VS LIVE")
    assert i != -1, (
        "the policy block no longer opens with a numbered SNAPSHOT VS LIVE rule "
        "— this guard and the no-competitor one both anchor to it.")
    j = flat.find(" 2. ", i)
    assert j != -1, "rule 1 is not followed by a numbered rule 2"
    rule1 = flat[i:j].lower()
    for token in ("snapshot", "reading", "age"):
        assert token in rule1, (
            "rule 1 dropped %r. The claim is about AGE — a snapshot carries no "
            "timestamp, a reading does — not about who is better." % token)


def test_the_citation_carries_an_as_of_slot(citation: str):
    """A DC Hub figure quoted without its as_of is a snapshot again.

    Rule 3 tells an agent to carry the as_of; the pasteable block is where that
    instruction either survives contact with a copy-paste or does not.
    """
    flat = " ".join(citation.split())
    assert "as of <" in flat, (
        "the pasteable citation no longer carries an `as of <...>` slot. Rule 3 "
        "requires the as_of to travel with the number; if the template omits "
        "it, the template is what gets copied.")


def test_the_policy_links_the_machine_readable_catalog(block: str):
    """.well-known/mcp.json is how an agent gets tool names without guessing.

    /llms.txt already links it 100+ lines further down, so this is asserted
    over the POLICY BLOCK only — the part a truncated read still gets.
    """
    flat = " ".join(block.split())
    assert "https://dchub.cloud/.well-known/mcp.json" in flat, (
        "the policy block no longer links the machine-readable tool catalog. "
        "Two assistants have already published DC Hub connector manifests "
        "naming tools that do not exist, by normalising names read off prose.")


def test_the_citation_does_not_grant_cc_by_over_the_whole_platform(citation: str):
    """CC-BY-4.0 is a PER-LAYER grant and a flat one is an over-claim.

    DCPI scores, verdicts, band thresholds, methodology and DC Hub's own grid
    and site analysis are ours to license. The facility inventory and the
    third-party physical layers are composites carrying upstream terms
    (OpenStreetMap / ODbL 1.0, share-alike) that DC Hub cannot waive — so a
    citation template reading "DC Hub data, CC-BY-4.0" hands a stranger a
    licence we do not hold. `summarize_for_citation` already splits the two;
    this keeps the pasteable template from re-merging them.
    """
    flat = " ".join(citation.split())
    if "CC-BY-4.0" not in flat:
        pytest.fail("the citation no longer states a licence at all")
    assert "https://dchub.cloud/data-sources" in flat, (
        "the citation states CC-BY-4.0 without pointing the composite layers at "
        "/data-sources — that is a blanket grant over data DC Hub does not own.")
    for token in ("DCPI", "composite"):
        assert token.lower() in flat.lower(), (
            "the citation's licence line dropped %r, which is what scopes the "
            "grant to the layers DC Hub can actually license." % token)


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
_AGENTS_DOOR = "/AGENTS.md"


# ───────────────────────────────────────────────────────────────────────────
# WHICH HANDLER THESE GUARDS GRADE — read off the codebase, never picked here
#
# ★ 2026-09-21. This file graded the wrong /llms-full.txt handler TWICE. The
# path had two registrations. #4996 put the policy block in
# ai_agent_discovery's and a fixture registering that blueprint ALONE proved
# it, while production served ai_discovery_routes' with no block. The next
# fixture registered both and trusted main.py's ORDER — on the premise that
# main.py registers ai_agent_discovery.discovery_bp. It never did: main.py's
# `discovery_bp` is routes.discovery_routes'. A fixture that builds its own app
# decides which handler it grades.
#
# So no fixture names a handler. _served(path) scans the codebase for EVERY
# registration of the path, refuses unless there is exactly one, confirms
# main.py wires that one in, builds a bare app from it, and checks that the
# rule which answers is the def it scanned. Booting main.py would give the real
# URL map and is not available to a unit test: `import main` opens Postgres at
# import time (measured: "No database URL configured", 14.8s in).
#
# Blind spots, so nobody mistakes the scan for more than it is: it reads
# literal rules, plus a same-module string constant. A rule assembled at
# runtime or imported from another module, and a before_request hook that
# answers the path ahead of routing, are invisible to it.
# ───────────────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parents[1]

#: Not the application. ★ ".claude" is load-bearing: a checkout's
#: .claude/worktrees/ holds whole copies of this repo, registrations included,
#: and a walk that enters it reports every door as duplicated.
_NOT_THE_APP = frozenset({".git", ".claude", "tests", "node_modules",
                          "__pycache__", ".venv", "venv"})

#: Rule-registering decorators. .get/.post/... count only AS decorators — as
#: plain calls they are dict.get() and client.get().
_ROUTE_DECORATORS = frozenset({"route", "get", "post", "put", "patch",
                               "delete"})

_Site = namedtuple("_Site", "rel line module owner_kind owner view")


@functools.lru_cache(maxsize=None)
def _app_files(root: Path) -> tuple:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _NOT_THE_APP)
        out += [Path(dirpath, f) for f in sorted(filenames) if f.endswith(".py")]
    return tuple(out)


def _sites_in(tree, rel: str, path: str) -> list:
    """Every registration of `path` in one parsed module."""
    consts = {t.id: n.value.value for n in tree.body
              if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
              and isinstance(n.value.value, str)
              for t in n.targets if isinstance(t, ast.Name)}

    def rule(call):
        arg = call.args[0] if call.args else next(
            (k.value for k in call.keywords if k.arg == "rule"), None)
        if isinstance(arg, ast.Name):
            return consts.get(arg.id)
        return arg.value if isinstance(arg, ast.Constant) else None

    out = []
    for top in tree.body:
        # Registered INSIDE a top-level def: a register_x(app) function that
        # main.py must call. Registered at import time: on a blueprint that
        # main.py must register.
        fn = top.name if isinstance(
            top, (ast.FunctionDef, ast.AsyncFunctionDef)) else None
        own = {id(d) for d in getattr(top, "decorator_list", ())}
        views = {id(d): n.name for n in ast.walk(top)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 for d in n.decorator_list}
        for call in ast.walk(top):
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)):
                continue
            verb = call.func.attr
            if not (verb in ("route", "add_url_rule")
                    or (verb in _ROUTE_DECORATORS and id(call) in views)):
                continue
            if rule(call) != path:
                continue
            nested = fn is not None and id(call) not in own
            view = views.get(id(call)) or next(
                (k.value.id for k in call.keywords
                 if k.arg == "view_func" and isinstance(k.value, ast.Name)),
                None)
            out.append(_Site(rel, call.lineno, rel[:-3].replace("/", "."),
                             "function" if nested else "blueprint",
                             fn if nested else ast.unparse(call.func.value),
                             view))
    return out


@functools.lru_cache(maxsize=None)
def _registrations(path: str, root: Path = _ROOT) -> tuple:
    """Every registration of `path` in the application, wherever it is."""
    sites = []
    for f in _app_files(root):
        text = f.read_text(errors="replace")
        if path not in text:
            continue
        rel = f.relative_to(root).as_posix()
        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            raise AssertionError(
                "%s mentions %s and does not parse (%s): the scan cannot tell "
                "whether it registers the path, and will not guess."
                % (rel, path, e))
        sites += _sites_in(tree, rel, path)
    return tuple(sites)


def _describe(path: str, sites) -> str:
    return (
        "%s has %d registration(s); a public path gets exactly ONE:\n%s\n"
        "With two, a guard that builds its own app picks which one it grades "
        "and production picks by main.py's wiring — that is how #4996 shipped "
        "a green guard over a door with no policy block. Delete the one "
        "main.py does not reach." % (path, len(sites), "\n".join(
            "  %s:%d  %s %s -> %s" % (s.rel, s.line, s.owner_kind, s.owner,
                                      s.view) for s in sites) or "  (none)"))


@functools.lru_cache(maxsize=None)
def _main_index():
    """(imports, called, registered), read off main.py's AST in one walk —
    so a comment or a string that names the call does not count."""
    imports, called, registered = {}, set(), set()
    for n in ast.walk(ast.parse((_ROOT / "main.py").read_text())):
        if isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                imports.setdefault((n.module, a.name), set()).add(
                    a.asname or a.name)
        elif isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                called.add(n.func.id)
            elif (isinstance(n.func, ast.Attribute)
                  and n.func.attr == "register_blueprint" and n.args
                  and isinstance(n.args[0], ast.Name)):
                registered.add(n.args[0].id)
    return imports, called, registered


def _main_wires(site) -> bool:
    """Does main.py CALL this registration's function, or REGISTER its
    blueprint, under a name it imported from that module?"""
    if site.module == "main":
        return True
    imports, called, registered = _main_index()
    names = imports.get((site.module, site.owner), set())
    return bool(names & (called if site.owner_kind == "function"
                         else registered))


@functools.lru_cache(maxsize=None)
def _served(path: str):
    """A bare app built from `path`'s ONE registration — the one main.py wires.

    Refuses rather than guesses: a second registration, one main.py never
    reaches, or a rule answered by any def but the one scanned, fails here.
    """
    flask = pytest.importorskip("flask")
    sites = _registrations(path)
    assert len(sites) == 1, _describe(path, sites)
    site = sites[0]
    assert _main_wires(site), (
        "%s's only registration (%s:%d, %s %s) is never reached by main.py — "
        "a guard reading it grades a handler no request is routed to."
        % (path, site.rel, site.line, site.owner_kind, site.owner))
    mod = importlib.import_module(site.module)
    app = flask.Flask(__name__)
    if site.owner_kind == "function":
        getattr(mod, site.owner)(app)
    else:
        app.register_blueprint(getattr(mod, site.owner))
    endpoint, _args = app.url_map.bind("dchub.cloud").match(path)
    view = inspect.unwrap(app.view_functions[endpoint])
    where = Path(inspect.getsourcefile(view)).resolve()
    assert (where, view.__name__) == ((_ROOT / site.rel).resolve(), site.view), (
        "%s was answered by %s() in %s, not by the registration the scan found "
        "(%s() at %s:%d) — this would grade a handler the codebase does not "
        "route to." % (path, view.__name__, where, site.view, site.rel,
                       site.line))
    return app


@pytest.fixture(scope="module")
def full_body() -> str:
    """The REAL /llms-full.txt body, from the path's one registration."""
    r = _served(_FULL_DOOR).test_client().get(_FULL_DOOR)
    assert r.status_code == 200, "%s -> %s" % (_FULL_DOOR, r.status_code)
    body = r.get_data(as_text=True)
    assert len(body) > 2000, (
        "%s served only %d bytes — a stub, and every assertion below would be "
        "reading it and passing vacuously." % (_FULL_DOOR, len(body))
    )
    return body


#: The block's own last sentence. Used as the end boundary on BOTH doors.
#:
#: ★ Not "the next ## heading" — that boundary is wrong on /llms-full.txt,
#: whose static body carries no further ## heading, so the slice ran to EOF and
#: swallowed the whole document. Hardcoded here rather than imported from
#: agent_door_policy: a boundary taken from the module under test would move
#: with it, and the slice would keep matching whatever that module produced.
_BLOCK_TAIL = "drop the as_of line and the Coverage line and keep the doors."


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
    assert "1. SNAPSHOT VS LIVE" in full_block, (
        "%s carries the heading but not rule 1 — the block is truncated."
        % _FULL_DOOR
    )


def test_every_door_serves_the_identical_block(
    block: str, full_block: str, agents_block: str
):
    """Byte-identity, not similarity — across all THREE doors.

    Hand-maintained copies is the state this removed. If this ever fails, one
    door stopped rendering agent_door_policy.policy_block() — fix the
    renderer, do NOT reconcile the texts by hand.

    ★ Widened from two doors to three 2026-09-21. /AGENTS.md was the last
    hand-maintained copy, and it is the drift this file warns about: measured
    after #5005 it carried an EQUIVALENT block — same seven rules, no vendors,
    as_of + mcp.json + Coverage + CC-BY all present — and different bytes,
    because it bolded the rule labels and substituted its own f-string locals
    for the canon placeholders. Equivalent is what decays; identical cannot.
    Extended here rather than as a second guard so a fourth door has one
    assertion to join, not two to keep in step.

    ★ All three bodies come from _served(): each door's ONE registration,
    the one main.py wires. Identity between handlers this file picked proved
    nothing about the ones production routes to.
    """
    summary = _slice_policy(block, "/llms.txt")
    for door, served in ((_FULL_DOOR, full_block), (_AGENTS_DOOR, agents_block)):
        assert served == summary, (
            "/llms.txt and %s serve DIFFERENT policy blocks (%d vs %d bytes). "
            "They are supposed to be one rendering of agent_door_policy."
            % (door, len(summary), len(served))
        )


def test_the_policy_precedes_the_body_on_the_full_door(full_body: str):
    """A policy below 118 lines of endpoint listing is a policy nothing reads."""
    i = full_body.find(_HEADING)
    assert i != -1 and i < 1200, (
        "%s puts the policy block %d bytes in; a model that truncates a long "
        "fetch never reaches it." % (_FULL_DOOR, i)
    )


def test_the_full_door_names_no_competitor_it_ranks_against(full_block: str):
    """Reversed with its /llms.txt twin — see that guard for the reasoning.

    Hardcoded on purpose, and the list is shared with the twin rather than read
    from agent_door_policy: sourcing it from the module under test would make
    this a mirror, and deleting a name from the module and from the prose
    together would still pass.
    """
    flat = " ".join(full_block.split())
    named = [n for n in _FORBIDDEN_VENDORS if n in flat]
    assert not named, (
        "%s names %d third-party vendor(s): %s. Both doors render one block "
        "(agent_door_policy.policy_block()) and neither ranks DC Hub against "
        "anyone by name." % (_FULL_DOOR, len(named), named))


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


#: Every door the policy block is published through, plus /agents.md (same
#: handler as /AGENTS.md). /AGENTS.md and /agents.md joined 2026-09-21, when
#: their dead copies on ai_agent_discovery's unregistered blueprint were deleted.
_ONE_REGISTRATION_DOORS = ("/llms.txt", _FULL_DOOR, "/AGENTS.md", "/agents.md",
                           # A2A cards: routes/agent_a2a.py serves both
                           # (tests/test_agent_card_oauth2.py pins them identical)
                           "/.well-known/agent.json", "/.well-known/agent-card.json")


@pytest.mark.parametrize("door", _ONE_REGISTRATION_DOORS)
def test_each_door_has_exactly_one_registration(door: str):
    """Two registrations of one public path IS the defect.

    Whichever one loses is dead code that still passes every test that
    registers it by hand — ai_agent_discovery.serve_llms_full did, for two PRs.
    This fails on a second registration wherever in the codebase it is added.
    """
    sites = _registrations(door)
    assert len(sites) == 1, _describe(door, sites)


@pytest.mark.parametrize("door", _ONE_REGISTRATION_DOORS)
def test_main_wires_the_registration(door: str):
    """Unique is not enough: it must also be REACHABLE from main.py.

    ai_agent_discovery's /llms-full.txt sat on a blueprint main.py never
    registers. Had it been the only copy, a uniqueness check alone would have
    passed over a door that 404s.
    """
    sites = _registrations(door)
    assert sites, _describe(door, sites)
    dead = [s for s in sites if not _main_wires(s)]
    assert not dead, "main.py never reaches: %s" % ", ".join(
        "%s:%d (%s %s)" % (s.rel, s.line, s.owner_kind, s.owner) for s in dead)


@functools.lru_cache(maxsize=None)
def _through_the_real_app() -> dict:
    """{door: [status, body]}: each door requested through main.py's REAL app.

    Booted by scripts/app_contract_gate.boot() (DB stubbed, repo state files
    isolated), in a subprocess so main's import-time threads and state stay
    out of this one. Nothing here chooses a handler: url_map precedence and
    before_request hooks run exactly as in production. That last part is not
    hypothetical: main.py's before_request answered /.well-known/agent.json
    ahead of every rule until 2026-09-21, and no scan of route decorators can
    see a hook.
    """
    import json
    import subprocess
    import sys
    import tempfile
    out = Path(tempfile.mkdtemp()) / "bodies.json"
    code = ("import json, sys\n"
            "sys.path.insert(0, 'scripts')\n"
            "import app_contract_gate as g\n"
            "app, _ = g.boot()\n"
            "c = app.test_client()\n"
            "res = {}\n"
            "for p in json.loads(sys.argv[2]):\n"
            "    r = c.get(p)\n"
            "    res[p] = [r.status_code, r.get_data(as_text=True)]\n"
            "json.dump(res, open(sys.argv[1], 'w'))\n"
            "import os; os._exit(0)\n")
    proc = subprocess.run(
        [sys.executable, "-c", code, str(out), json.dumps(_ONE_REGISTRATION_DOORS)],
        cwd=_ROOT, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0 and out.exists(), (
        "could not boot the real app (rc=%s). That is NOT a pass: stderr tail:\n%s"
        % (proc.returncode, "\n".join(proc.stderr.splitlines()[-8:])))
    return json.loads(out.read_text())


def _comparable(door: str, body: str):
    """The body minus what is stamped per request, and nothing else."""
    if door.endswith(".json"):
        import json
        card = json.loads(body)
        card.pop("computed_at", None)
        return card
    return body


@pytest.mark.parametrize("door", _ONE_REGISTRATION_DOORS)
def test_the_real_app_serves_what_the_one_registration_serves(door: str):
    """The scan and _served() read source. This reads the booted app.

    If anything answers ahead of the one registration, the real app's body
    differs from the one _served() renders, and this fails: a winning
    duplicate, a before_request branch, or a catch-all. It also fails when
    the door stops being served at all.
    """
    status, body = _through_the_real_app()[door]
    assert status == 200, "the real app answers %s with %s" % (door, status)
    ours = _served(door).test_client().get(door).get_data(as_text=True)
    assert _comparable(door, body) == _comparable(door, ours), (
        "the REAL app serves %s differently (%d bytes) from its one "
        "registration (%d bytes): something answers ahead of it."
        % (door, len(body), len(ours)))


def test_the_scan_can_see_what_it_exists_to_refuse(tmp_path):
    """The scan's own known positives. A scan that finds nothing reports
    "no duplicate" too, and the first answer above would be green on it.

    A scratch tree, not a duplicate the repo happens to carry, so deleting a
    real duplicate cannot break the control. It proves: a def nested in a
    register function, a module-level blueprint decorator in a subdirectory,
    .get() as a decorator, a rule held in a module constant — and that
    tests/ and a .claude/worktrees checkout are NOT counted.
    """
    door = ("def register(app):\n"
            "    @app.route('/llms-full.txt')\n"
            "    def a():\n        pass\n")
    (tmp_path / "routes").mkdir()
    (tmp_path / "door.py").write_text(door)
    (tmp_path / "routes" / "copy.py").write_text(
        "P = '/llms-full.txt'\n\n\n@bp.get(P)\ndef b():\n    pass\n")
    for skipped in (".claude/worktrees/wt", "tests"):
        (tmp_path / skipped).mkdir(parents=True)
        (tmp_path / skipped / "door.py").write_text(door)
    found = sorted((s.rel, s.owner_kind, s.owner, s.view)
                   for s in _registrations(_FULL_DOOR, tmp_path))
    assert found == [("door.py", "function", "register", "a"),
                     ("routes/copy.py", "blueprint", "bp", "b")], found

    # The walk's size floor is pinned where every scanning test's is:
    # tests/scan_floors.json (walk: 95 directories on 2026-09-21, floor 76).
    walked = _app_files(_ROOT)
    assert any(p.parent.name == "routes" for p in walked), (
        "the scan never entered routes/, where most blueprints live")



# ───────────────────────────────────────────────────────────────────────────
# THE THIRD DOOR: /AGENTS.md
#
# ★ Measured 2026-09-21 rendering all three doors through their real
# blueprints: /llms.txt and /llms-full.txt carried policy_block() verbatim and
# /AGENTS.md did not (12,942 bytes, its own hand-written rendering of the same
# seven rules). It now splices policy_block().
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def agents_body() -> str:
    """The REAL /AGENTS.md body, from the path's one registration."""
    app = _served(_AGENTS_DOOR)
    r = app.test_client().get(_AGENTS_DOOR)
    assert r.status_code == 200, "%s -> %s" % (_AGENTS_DOOR, r.status_code)
    body = r.get_data(as_text=True)
    assert len(body) > 2000, (
        "%s served only %d bytes — a stub, not the rendered door, and every assertion below "
        "would be reading it instead." % (_AGENTS_DOOR, len(body))
    )
    assert "routes/agents_md_fallback.py" in body, (
        "%s was answered by something other than the fallback template, which "
        "names itself in its own header line. The fixture is grading a handler "
        "the live door does not use." % _AGENTS_DOOR
    )
    return body


@pytest.fixture(scope="module")
def agents_block(agents_body: str) -> str:
    """The policy block as /AGENTS.md serves it."""
    return _slice_policy(agents_body, _AGENTS_DOOR)


def test_the_agents_door_names_no_competitor_it_ranks_against(agents_block: str):
    """Reversed with its two twins — see the /llms.txt guard for the reasoning.

    Same hardcoded list, for the same reason: sourcing _FORBIDDEN_VENDORS from
    agent_door_policy would make all three guards mirrors, and deleting a name
    from the module would delete it from the fence at the same time.

    Not redundant with the identity assertion. Identity says the three doors
    AGREE; it stays green if a vendor is added to the one renderer, because all
    three then agree on the version that names it. The fence is what fails, and
    it has to exist on every door.
    """
    flat = " ".join(agents_block.split())
    named = [n for n in _FORBIDDEN_VENDORS if n in flat]
    assert not named, (
        "%s names %d third-party vendor(s): %s. All three doors render one "
        "block (agent_door_policy.policy_block()) and none of them ranks DC "
        "Hub against anyone by name." % (_AGENTS_DOOR, len(named), named))


def test_no_unresolved_placeholder_reaches_the_agents_door(agents_body: str):
    """The splice must not leave a brace on the wire.

    ★ Scoped to the WHOLE body, not the block: the f-string the block is
    spliced into carries its own {fac} / {tools} / {endpoint} fields, and the
    failure mode of a splice is a field that stopped being substituted, not one
    inside the block.
    """
    import re as _re
    left = _re.findall(r"\{[a-z_]+\}", agents_body)
    assert not left, (
        "%s served %d unresolved placeholder(s) to an agent: %s"
        % (_AGENTS_DOOR, len(left), sorted(set(left)))
    )
