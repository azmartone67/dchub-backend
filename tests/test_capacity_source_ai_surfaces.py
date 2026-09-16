#!/usr/bin/env python3
"""Capacity Source must be RETRIEVABLE, not merely present, on every surface an
AI assistant reads.

NO NETWORK, NO DB. main.py is read by AST; nothing here imports it.

★ WHAT WAS WRONG (measured 2026-09-15, before this guard)

Capacity Source gained search by SIZE (min_kw / min_mw) and by LOCATION (region,
country, US state, metro) and a deal-registration flow. The surfaces an
assistant actually reads had not caught up, each in a different way:

    /llms.txt              one clause: "Search by size and location", no region
                           values, no callable URL, no example
    /llms-full.txt         NO Capacity Source section at all — the file an agent
                           is told to read for "comprehensive endpoint
                           documentation" did not mention the program
    /api/v1/ai-agents.json canonical_endpoints named dcpi, data, human_facing,
                           agent_concierge, vertex_ai — and nothing for the one
                           question an agent is sent here to answer
    /.well-known/mcp.json  source_capacity's description read as a browse-only
                           feed, so an agent choosing a tool picked
                           search_facilities, which answers a different question
    /api/v1/agent/cookbook a recipe for every ANALYSIS question, none for
                           acquisition
    robots.txt             `Disallow: /*?` was about to make the one URL that
                           answers "where do I find capacity in Europe" the one
                           advertised URL these crawlers were told to skip

An agent cannot cite what it cannot find, and "present somewhere in the file" is
not findable: the assistant has to see the PREDICATE (size, location), the VALUE
SET (which regions exist) and a URL it can hand to its human.

★ THE THREE PROPERTIES, asserted per surface rather than once

 1. SEARCHABILITY  every surface names the size filters, the location filters
                   and the real region values — a surface that says "searchable"
                   without naming what to search by teaches nothing.
 2. ZERO STATE     with no listing live, every live-aware surface reads as
                   ONBOARDING. Not "no capacity", not a market with nothing in
                   it, and never a claim of inventory. The owner deletes the
                   sample listings after a meeting, so this is the state the
                   crawlers will most often see.
 3. CONFIDENTIALITY  a teaser is the whole of what a crawler or an unaccepted
                   agent gets. The emitter is checked, not the prose: no site
                   address, no coordinates, no substation, no contact. A
                   provider's NAME is the one identity that can appear, and only
                   where that provider opted in (detail.provider.disclosed) —
                   which is why the copy on every surface says "opted in" rather
                   than the tidier, false "never named".

Run: python3 -m pytest tests/test_capacity_source_ai_surfaces.py -rEf
"""
from __future__ import annotations

import ast
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ── The claim set, written once and asserted against every surface ──────────
# Deliberately TOKENS, not sentences: a surface writes in its own voice (a
# markdown block, a JSON manifest, an MCP tool description, a card), so pinning
# phrasing would either break on a legitimate rewrite or force four surfaces
# into one voice. What must not vary is which predicate and which values the
# reader is told about.
SIZE_FILTERS = ("min_kw", "min_mw")
LOCATION_FILTERS = ("region", "country", "location")
REGIONS = ("north_america", "latin_america", "europe",
           "asia_pacific", "middle_east_africa")
SEARCH_URLS = (
    "https://dchub.cloud/listings?min_kw=500&region=europe",
    "https://dchub.cloud/api/v1/listings?min_mw=5&region=north_america",
    "https://dchub.cloud/api/v1/listings?location=Dallas",
)
TOOL_CHAIN = ("source_capacity", "accept_capacity_terms", "request_capacity_intro")

# Deal registration, as it actually works. Three separate facts, because a
# surface can get one right and the others wrong — and the one that shipped
# wrong was the third: two surfaces said contact "is never shared", which reads
# as "never, full stop" and is false the moment a provider accepts.
_GIVEN_TO_PROVIDER = re.compile(r"company\s+(?:name\s+)?and\s+requirement", re.I)
_ACCEPT_OR_DECLINE = re.compile(r"accepts?\s+or\s+declines?", re.I)
_ONLY_ON_ACCEPTANCE = re.compile(
    r"only\s+(?:on|after|if|when)\s+(?:the\s+provider\s+)?"
    r"(?:accept\w*|it\s+accepts)", re.I)

# Confidentiality, stated positively so a surface that simply omits the subject
# fails rather than passing by silence.
_DENIES_SITE = re.compile(r"site\s+address|the\s+site\s+is\s+never", re.I)
_DENIES_GEO = re.compile(r"coordinates", re.I)
_DENIES_SUBSTATION = re.compile(r"substation", re.I)


def _missing(text: str, tokens) -> list:
    return [t for t in tokens if t not in text]


# ── Surface 1 + 2: the llms files, served through the real route ────────────
@pytest.fixture(scope="module")
def served():
    flask = pytest.importorskip("flask")
    from ai_discovery_routes import register_discovery_routes
    app = flask.Flask(__name__)
    register_discovery_routes(app)
    client = app.test_client()
    out = {}
    for path in ("/llms.txt", "/llms-full.txt", "/robots.txt"):
        r = client.get(path)
        assert r.status_code == 200, "%s -> %s" % (path, r.status_code)
        out[path] = r.get_data(as_text=True)
    return out


def _capacity_block(body: str) -> str:
    """The Capacity Source SECTION of a served llms body.

    Sliced, not searched whole-file, so a token that happens to appear in an
    unrelated section cannot satisfy an assertion about this one.

    Anchored on the HEADING line, not on the first occurrence of the words:
    llms-full.txt wraps its headings in `====` rules, so anchoring on the raw
    phrase and ending at the next rule returned the heading alone — a one-line
    block that failed every assertion for a reason that had nothing to do with
    the surface. Both files end a section at the next `## `.
    """
    lines = body.splitlines()
    start = next((n for n, line in enumerate(lines)
                  if line.startswith("#") and "capacity source" in line.lower()), None)
    assert start is not None, "no Capacity Source heading in this surface at all"
    end = next((n for n in range(start + 1, len(lines))
                if lines[n].startswith("## ")), len(lines))
    block = "\n".join(lines[start:end])
    assert len(block) > 400, (
        "the Capacity Source section sliced to %d chars — the heading anchor "
        "moved, and every assertion below would run on almost nothing"
        % len(block))
    return block


@pytest.mark.parametrize("path", ["/llms.txt", "/llms-full.txt"])
def test_the_llms_surfaces_name_the_size_and_location_predicates(served, path):
    block = _capacity_block(served[path])
    assert not _missing(block, SIZE_FILTERS), (
        f"{path} does not name the size filters {SIZE_FILTERS}. 'searchable by "
        "size' without the parameter name is not something an agent can act on.")
    assert not _missing(block, LOCATION_FILTERS), (
        f"{path} does not name the location filters {LOCATION_FILTERS}.")
    assert not _missing(block, REGIONS), (
        f"{path} is missing region value(s) {_missing(block, REGIONS)}. An agent "
        "that cannot see the value set guesses one, and a guessed region is a "
        "400 or an empty result its human reads as 'no capacity there'.")


@pytest.mark.parametrize("path", ["/llms.txt", "/llms-full.txt"])
def test_the_llms_surfaces_name_a_callable_search_url(served, path):
    block = _capacity_block(served[path])
    assert not _missing(block, SEARCH_URLS), (
        f"{path} is missing search URL(s) {_missing(block, SEARCH_URLS)}. These "
        "are what an assistant hands its human when it is asked where to find "
        "capacity; a bare endpoint name is not a link anyone can follow.")


@pytest.mark.parametrize("path", ["/llms.txt", "/llms-full.txt"])
def test_the_llms_surfaces_name_the_whole_tool_chain(served, path):
    block = _capacity_block(served[path])
    assert not _missing(block, TOOL_CHAIN), (
        f"{path} does not name {_missing(block, TOOL_CHAIN)} — an agent that "
        "knows how to search but not how to register stops at the teaser.")


@pytest.mark.parametrize("path", ["/llms.txt", "/llms-full.txt"])
def test_the_llms_surfaces_describe_deal_registration_honestly(served, path):
    block = _capacity_block(served[path])
    assert _GIVEN_TO_PROVIDER.search(block), (
        f"{path} does not say what the provider actually receives (the buyer's "
        "company and requirement). An agent that cannot tell its human what is "
        "disclosed cannot get informed consent for the registration.")
    assert _ACCEPT_OR_DECLINE.search(block), (
        f"{path} does not say the provider accepts or declines — it reads as "
        "though registering produces an introduction.")
    assert _ONLY_ON_ACCEPTANCE.search(block), (
        f"{path} does not say identity and contacts are exchanged only on "
        "acceptance.")


@pytest.mark.parametrize("path", ["/llms.txt", "/llms-full.txt"])
def test_the_llms_surfaces_state_the_confidentiality_boundary(served, path):
    block = _capacity_block(served[path])
    for label, pat in (("the site address", _DENIES_SITE),
                       ("coordinates", _DENIES_GEO),
                       ("the substation", _DENIES_SUBSTATION)):
        assert pat.search(block), (
            f"{path} says nothing about {label}. Silence is not a boundary: an "
            "agent that does not know the field is withheld reports it missing, "
            "or goes looking for it elsewhere.")


# ── The ZERO STATE, which is what a crawler sees most of the time ───────────
# The owner deletes the sample listings after a meeting, so `live_count: 0` is
# the steady state, not an edge case. Both directions are asserted: the live
# one is the CONTROL — without it, an emitter that had lost its live branch
# entirely would pass the zero-state test for the wrong reason.
def _llms_with_summary(monkeypatch, summary):
    flask = pytest.importorskip("flask")
    import routes.exclusive_listings as el
    from ai_discovery_routes import register_discovery_routes
    monkeypatch.setattr(el, "cached_listings_summary", lambda: summary)
    app = flask.Flask(__name__)
    register_discovery_routes(app)
    return app.test_client().get("/llms.txt").get_data(as_text=True)


_ZERO_SUMMARY = {"program_status": "upcoming", "live_count": 0, "total_mw": None,
                 "markets": [], "market_count": 0, "delivery_types": {},
                 "latest_updated_at": None, "generated_at": "2026-09-15T00:00:00Z"}
_LIVE_SUMMARY = {"program_status": "live", "live_count": 2, "total_mw": 42.5,
                 "markets": [{"market": "Dallas", "state": "TX", "country": "US",
                              "count": 1, "mw": 30.0, "delivery_types": ["turnkey"]},
                             {"market": "Dublin", "state": None, "country": "IE",
                              "count": 1, "mw": 12.5, "delivery_types": ["land"]}],
                 "market_count": 2, "delivery_types": {"turnkey": 1, "land": 1},
                 "latest_updated_at": "2026-09-14T09:00:00Z",
                 "generated_at": "2026-09-15T00:00:00Z"}


def test_the_zero_state_reads_as_onboarding_not_as_an_empty_market(monkeypatch):
    body = _llms_with_summary(monkeypatch, _ZERO_SUMMARY)
    block = _capacity_block(body)
    assert "(upcoming)" in block, (
        "the Capacity Source heading dropped '(upcoming)' with nothing live — "
        "the heading is the first thing a crawler reads and it now implies "
        "inventory that does not exist.")
    assert "UPCOMING while the first listings are" in block, (
        "the program sentence no longer says the listings are being onboarded.")
    assert "Available now:" not in block, (
        "an availability line was rendered with live_count 0 — that line is a "
        "claim of inventory.")
    assert re.search(r"0\s+means\s+onboarding", block, re.I), (
        "the block does not tell a reader what live_count 0 MEANS. A zero that "
        "is not explained is read as 'this market has no capacity', which is "
        "the one conclusion it never supports.")


def test_the_live_state_still_renders_availability(monkeypatch):
    """CONTROL for the test above. An emitter whose live branch was deleted
    would pass every zero-state assertion while being permanently wrong."""
    block = _capacity_block(_llms_with_summary(monkeypatch, _LIVE_SUMMARY))
    assert "(upcoming)" not in block, "the heading kept '(upcoming)' while live"
    assert "Available now: 2 listings" in block, block[:400]
    assert "Dallas" in block and "Dublin" in block


def test_an_unreadable_summary_falls_to_onboarding(monkeypatch):
    """None is what cached_listings_summary returns when the listings cannot be
    read. It must resolve the SAFE way: an unknown state is never inventory."""
    block = _capacity_block(_llms_with_summary(monkeypatch, None))
    assert "(upcoming)" in block
    assert "Available now:" not in block


# ── robots.txt: everything we advertise, these crawlers may fetch ───────────
def test_the_capacity_search_urls_are_fetchable_by_the_assistant_crawlers(served):
    """Narrow, and named. tests/test_robots_permits_what_llms_advertises.py
    already derives this for the whole advertised set; this one fails with
    Capacity Source in the message, because /listings?... is the first
    advertised URL that sits OUTSIDE the `Allow: /api/` prefix and is therefore
    the first that a well-meaning robots edit can silently re-block."""
    Protego = pytest.importorskip(
        "protego", reason="RFC 9309 parser required; stdlib robotparser cannot see /*?").Protego
    parser = Protego.parse(served["/robots.txt"])
    blocked = [(ua, u) for ua in ("ClaudeBot", "GPTBot", "PerplexityBot",
                                  "Googlebot", "Bingbot")
               for u in SEARCH_URLS if not parser.can_fetch(u, ua)]
    assert not blocked, (
        f"{len(blocked)} crawler/URL pair(s) told not to fetch the Capacity "
        f"Source search we advertise: {blocked[:4]}. robots.txt is advisory, so "
        "this never appears as an error — only as a crawler that never asks.")


def test_the_long_tail_behind_those_urls_stays_closed(served):
    """The half that made widening safe. An end-anchored Allow permits exactly
    the advertised search; a prefix Allow would pass the test above and hand
    Bing an unbounded ?page= crawl of the same path."""
    Protego = pytest.importorskip("protego").Protego
    parser = Protego.parse(served["/robots.txt"])
    leaked = [(u, s) for u in SEARCH_URLS
              for s in ("&page=99", "&cb=1", "&offset=5000", "0")
              if parser.can_fetch(u + s, "Bingbot")]
    assert not leaked, f"advertised search leaked its long tail: {leaked[:4]}"


# ── Surface 3: /api/v1/ai-agents.json (+ the .well-known twin) ──────────────
# AST, because main.py must never be imported by a test (house rule) and because
# the block interpolates a live value, so literal_eval cannot read it.
def _main_capacity_strings() -> list:
    with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (isinstance(key, ast.Constant) and key.value == "capacity_source"
                    and isinstance(value, ast.Dict)):
                return [n.value for n in ast.walk(value)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    # Returns EMPTY rather than raising: a pytest.fail inside a module-scoped
    # fixture surfaces as an ERROR, which reads like a broken environment. A
    # missing block is a product defect, and it should report as a FAILURE with
    # the test below naming it.
    return []


@pytest.fixture(scope="module")
def manifest_strings():
    return _main_capacity_strings()


def test_the_agent_manifest_has_a_capacity_source_block(manifest_strings):
    """Floor for everything below, which loops over this set."""
    assert len(manifest_strings) >= 15, (
        f"main.py's ai-agents.json carries {len(manifest_strings)} "
        "capacity_source string(s). /api/v1/ai-agents.json (and its "
        "/.well-known twin) is the first surface an agent reads, and it names "
        "every other agent-facing endpoint — an agent asked where to FIND "
        "capacity has nothing here to follow.")


def test_the_agent_manifest_carries_the_search_contract(manifest_strings):
    blob = "\n".join(manifest_strings)
    assert not _missing(blob, SIZE_FILTERS + tuple(LOCATION_FILTERS)), (
        "ai-agents.json capacity_source omits "
        f"{_missing(blob, SIZE_FILTERS + tuple(LOCATION_FILTERS))}")
    assert not _missing(blob, REGIONS), (
        f"ai-agents.json omits region value(s) {_missing(blob, REGIONS)}")
    assert not _missing(blob, SEARCH_URLS), (
        f"ai-agents.json omits search URL(s) {_missing(blob, SEARCH_URLS)}")
    assert not _missing(blob, TOOL_CHAIN), (
        f"ai-agents.json omits tool(s) {_missing(blob, TOOL_CHAIN)}")


def test_the_agent_manifest_describes_deal_registration_honestly(manifest_strings):
    blob = "\n".join(manifest_strings)
    assert _GIVEN_TO_PROVIDER.search(blob)
    assert _ACCEPT_OR_DECLINE.search(blob)
    assert _ONLY_ON_ACCEPTANCE.search(blob)


def test_the_agent_manifest_zero_state_is_onboarding_not_absence(manifest_strings):
    """`status` is rendered from the live listing count; `status_means` is what
    a reader does with it. The onboarding wording must exist in the source and
    must not be phrased as a statement about how much capacity exists."""
    blob = "\n".join(manifest_strings)
    assert "upcoming" in blob, "the manifest has no onboarding state at all"
    assert re.search(r"onboard", blob, re.I), (
        "nothing in the manifest explains what 'upcoming' means to a reader")
    assert re.search(r"never a statement about how much capacity", blob, re.I), (
        "the manifest does not say that 'upcoming' is an onboarding state "
        "rather than a claim about available capacity — the one misreading "
        "that turns an honest zero into a false market signal")


# ── Surface 4: the MCP tool catalog behind /.well-known/mcp.json ────────────
@pytest.fixture(scope="module")
def catalog():
    from routes.mcp_tool_catalog import _merged_tools
    return {name: (summary, example)
            for name, _cat, _tier, summary, example in _merged_tools()}


def test_the_tool_catalog_teaches_search_by_size_and_location(catalog):
    assert "source_capacity" in catalog, sorted(catalog)[:10]
    summary, example = catalog["source_capacity"]
    assert not _missing(summary, SIZE_FILTERS + tuple(LOCATION_FILTERS)), (
        "source_capacity's catalog description omits "
        f"{_missing(summary, SIZE_FILTERS + tuple(LOCATION_FILTERS))} — an "
        "agent choosing between tools cannot tell this one searches at all.")
    assert not _missing(summary, REGIONS), (
        f"source_capacity omits region value(s) {_missing(summary, REGIONS)}")
    assert "min_kw" in example or "region" in example, (
        f"the published call example {example!r} does not demonstrate the "
        "search this description advertises")


def test_the_registration_tool_does_not_promise_permanent_secrecy(catalog):
    """The exact misstatement this PR removed. 'Operator contact is never
    shared' was live in two places and is false on acceptance — an agent that
    repeats it is telling its human the opposite of what the flow does."""
    summary, _ = catalog["request_capacity_intro"]
    assert not re.search(r"contact\s+is\s+never\s+shared", summary, re.I), (
        "request_capacity_intro claims contact is never shared. It is shared, "
        "on acceptance — say 'not before the provider accepts'.")
    assert _ACCEPT_OR_DECLINE.search(summary), summary[-300:]


# ── Surface 5: the agent cookbook ───────────────────────────────────────────
RECIPE_ID = "source-capacity-by-size-and-location"


@pytest.fixture(scope="module")
def cookbook():
    flask = pytest.importorskip("flask")
    from routes.agent_concierge import agent_concierge_bp
    app = flask.Flask(__name__)
    app.register_blueprint(agent_concierge_bp)
    return app.test_client()


def test_the_cookbook_serves_a_sourcing_recipe(cookbook):
    r = cookbook.get("/api/v1/agent/cookbook")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    ids = [x["id"] for x in body["recipes"]]
    assert RECIPE_ID in ids, (
        f"the cookbook has no capacity-sourcing recipe. ids={ids}")
    assert body["count"] == len(body["recipes"])


def test_the_recipe_is_an_ORDERED_tool_chain(cookbook):
    r = cookbook.get("/api/v1/agent/recipe/%s" % RECIPE_ID)
    assert r.status_code == 200, r.status_code
    recipe = r.get_json()["recipe"]
    chain = [t["tool"] for t in recipe["tools"]]
    assert chain == list(TOOL_CHAIN), (
        f"the chain is {chain}; it must be {list(TOOL_CHAIN)} — searching "
        "before terms are accepted is what the listing lock is for, and "
        "registering before terms are accepted is refused.")
    for step in recipe["tools"]:
        assert step.get("why"), f"step {step['tool']} has no `why` for the human"


def test_the_recipe_matches_the_shape_of_its_siblings(cookbook):
    """A recipe missing a field renders a broken card on /agent and breaks the
    /solve response the endpoint builds from it."""
    recipes = cookbook.get("/api/v1/agent/cookbook").get_json()["recipes"]
    mine = next(x for x in recipes if x["id"] == RECIPE_ID)
    sibling = next(x for x in recipes if x["id"] != RECIPE_ID)
    assert set(sibling) <= set(mine), (
        f"the new recipe is missing field(s) {sorted(set(sibling) - set(mine))}")
    assert mine["citation"] and "{as_of}" not in mine["citation"], (
        "the citation template was served unrendered")


def test_the_recipe_says_what_the_provider_receives(cookbook):
    recipe = cookbook.get("/api/v1/agent/recipe/%s" % RECIPE_ID).get_json()["recipe"]
    blob = recipe["sample_answer"] + " " + " ".join(t["why"] for t in recipe["tools"])
    assert _GIVEN_TO_PROVIDER.search(blob)
    assert _ACCEPT_OR_DECLINE.search(blob)
    assert re.search(r"live_count\s+is\s+0|onboard", blob, re.I), (
        "the recipe does not tell the agent what to do when nothing is live — "
        "which is the state it will meet most often")


# ── Surface 6: the /api/v1/whats-new capability card ────────────────────────
def test_the_whats_new_card_is_published_and_teaches_the_search():
    from routes.platform_updates import published_updates
    cards = published_updates(force=True).get("cards") or []
    assert cards, "no published platform cards at all"
    card = next((c for c in cards if c["id"] == "capacity-source"), None)
    assert card is not None, (
        "the Capacity Source card is not published on /api/v1/whats-new — "
        f"published ids: {[c['id'] for c in cards][:8]}")
    blob = card["title"] + " " + card["body"]
    assert re.search(r"size", blob, re.I) and re.search(r"location", blob, re.I), (
        "the card does not say the listings are searchable by size and location")
    assert re.search(r"kilowatt|megawatt", blob, re.I), (
        "the card does not name the size units a reader would search by")
    assert _ACCEPT_OR_DECLINE.search(blob), (
        "the card does not say the provider accepts or declines")
    assert "source_capacity" in blob


def test_the_mcp_packs_are_still_published_beside_it():
    """The same feed carries the /mcp/* pack cards. They are the other half of
    what a registry blurb quotes from here, and a Capacity Source edit that
    knocked one out would be invisible without this."""
    from routes.platform_updates import published_updates
    ids = {c["id"] for c in (published_updates(force=True).get("cards") or [])}
    packs = {i for i in ids if i.startswith("mcp-pack-")}
    assert len(packs) >= 5, f"only {len(packs)} MCP pack cards published: {packs}"


# ── CONFIDENTIALITY, checked at the emitter rather than in the prose ────────
# The prose tests above say the surfaces DESCRIBE the boundary. These say the
# code HOLDS it: what _teaser can publish is what a crawler and an unaccepted
# agent get, on every surface, at every tier.
_GEO_AND_CONTACT = {"latitude", "longitude", "lat", "lng", "lon",
                    "substation", "contact", "contact_email", "phone",
                    "owner_id", "site"}


def _identity_field_names():
    from routes.exclusive_listings import _IDENTITY_DETAIL_KEYS
    return set(_IDENTITY_DETAIL_KEYS) | _GEO_AND_CONTACT


def _dict_keys_in(func_name: str) -> set:
    """Every string key of every dict literal inside one function, by AST."""
    with open(os.path.join(ROOT, "routes", "exclusive_listings.py"),
              encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return {k.value for d in ast.walk(node) if isinstance(d, ast.Dict)
                    for k in d.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    pytest.fail(f"{func_name} not found in routes/exclusive_listings.py")


def test_the_teaser_publishes_no_identity_field():
    banned = _identity_field_names()
    leaked = sorted(_dict_keys_in("_teaser") & banned)
    assert not leaked, (
        f"_teaser publishes identity field(s) {leaked}. The teaser is what a "
        "crawler, an anonymous caller and an agent whose human has not been "
        "accepted all receive — there is no tier below it to hide behind.")


def test_the_identity_scan_can_actually_find_an_identity_field():
    """CONTROL. The assertion above is a set intersection; if the banned set or
    the key extraction silently came back empty it would pass on anything.
    _disclosure is the block that DOES carry these fields (released only to a
    buyer the provider accepted), so the same scan must light up there."""
    banned = _identity_field_names()
    assert len(banned) >= 10, f"the banned set collapsed to {banned}"
    found = _dict_keys_in("_disclosure") & banned
    assert found, (
        "_disclosure names no identity field, so this scanner cannot see one "
        "and the teaser assertion above proves nothing. Either the extraction "
        "broke or the disclosure block moved.")


@pytest.mark.parametrize("path", ["/llms.txt", "/llms-full.txt"])
def test_no_ai_surface_advertises_an_identity_field(served, path):
    """Field NAMES, not prose. 'never the site address' must stay sayable, so
    the scan looks for the snake_case keys a reader would try to request."""
    block = _capacity_block(served[path])
    named = sorted(t for t in ("site_address", "street_address", "postal_code",
                               "parcel_id", "site_name", "facility_name",
                               "provider_city", "latitude", "longitude")
                   if t in block)
    assert not named, (
        f"{path} advertises identity field(s) {named}. Naming a field teaches "
        "an agent to ask for it; these are never served at teaser level.")
