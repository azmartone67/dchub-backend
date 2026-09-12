#!/usr/bin/env python3
"""served_slugs — a whole list resolved past the profile route's own 301s, by
the route's own rules. NO NETWORK, NO DB.

MEASURED LIVE 2026-09-11 18:21Z (dchub.cloud, cache-busted, redirects NOT
followed) on every distinct dchub_url GET /api/v1/carriers/<id>/facilities
returned for carriers 642 265 355 858 403 404 227 823 61 399:

    678 URLs    619 x 200    59 x 301 (8.7%)    every 301 ONE hop to a 200
    30 of the 301s on integer discovered ids and 29 on hex facilities ids:
      /facilities/equinix-inc-equinix-am4-amsterdam-science-park-457de6cc
        301 -> /facilities/equinix-equinix-am4-amsterdam-science-park-0a9c12c9
      /facilities/telehouse-global-data-centers-telehouse-london-docklands-south-1535329c
        301 -> /facilities/telehouse-telehouse-london-docklands-south-c0145e6c

Each was a ROW's frozen slug that the page answers from a duplicate twin with
the case-B 301 to its keeper (facility_profile_page._twin_redirect_target).

WHAT THIS FILE PINS
  1. PARITY. For every fixture, served_slugs (the batch every list calls) and
     resolve_final_slug (the resolver behind the site's own /facility/<id> 301)
     give the same answer from the same rows — and it is the answer written in
     EXPECTED, so the two walks cannot agree on a wrong one and pass.
  2. ONE COPY OF THE RULES. Only _twin_redirect_target reads slug_rows or asks
     _same_physical_site, across the route and every emitter in EMITTERS, and
     each resolves its list in ONE served_slugs call that no loop repeats.
  3. THE STATEMENTS ARE THE PAGE'S. Both sides run against a recording
     connection and are compared lookup by lookup: table, columns, key, ORDER
     BY. The column list is load-bearing — `facilities` has no is_duplicate
     column live, and naming it is what makes the page's lookup on it raise.
  4. ROUNDS, NOT ROWS. 351 slugs (euNetworks' count) cost one batch per hop.
  5. FAIL OPEN. Any failure hands back the slugs it was given.

THE LIMIT: tests/_served_slug_world.py answers unambiguous fixtures only. The
page's ORDER BY among rows sharing a slug, the lookup that raises live and the
real parameter coercions run in tests/test_served_slugs_sql_parity.py, against
Postgres, in the db-parity job.

Run:  python3 -m pytest tests/test_served_slugs_batch.py -v
"""
import ast
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.facility_profile_page as fpp  # noqa: E402
import routes.facility_slug_freeze as fsf  # noqa: E402
from routes.facility_slug import stable_hash8  # noqa: E402
from routes.facility_slug_freeze import build_canonical_slug  # noqa: E402
from tests._served_slug_world import World  # noqa: E402

KEEPER = "equinix-equinix-am4-amsterdam-science-park-0a9c12c9"
TWIN = "equinix-inc-equinix-am4-amsterdam-science-park-457de6cc"
FR5_KEEPER = "equinix-equinix-fr5-frankfurt-kleyerstrasse-3366f937"
FR5_TWIN = "equinix-equinix-fr5-ad94b281"
REBUILT = build_canonical_slug("Iron Mountain", "Iron Mountain LON-3")
FROZEN_DOUBLED = ("iron-mountain-iron-mountain-lon-3-"
                  + stable_hash8("Iron Mountain", "Iron Mountain LON-3"))
UNFROZEN_BUILD = build_canonical_slug("Switch", "Switch Tahoe Reno 2")
H_OVH = stable_hash8("OVH", "OVH RBX-8")
ALIAS_TO_TWIN = "equinix-am4-amsterdam-0badf00d"

_SITE = {"address": "Science Park 610", "latitude": 52.3564, "longitude": 4.9531}


def _row(fid, slug, dup=None, **over):
    row = dict(id=fid, name=f"Fixture Site {fid}", provider="Fixture Operator",
               city="Amsterdam", state="", country="NL", market="Amsterdam",
               power_mw=10.0, status="operational", is_duplicate=0,
               duplicate_of_id=dup, canonical_slug=slug, **_SITE)
    row.update(over)
    return row


DISCOVERED = [
    _row(101, KEEPER),
    _row(102, TWIN, dup=101),
    # FR5, the live shape: the twin has the street, the keeper none, 12 m apart
    _row(103, FR5_KEEPER, address=None, latitude=50.09885, longitude=8.632004),
    _row(104, FR5_TWIN, dup=103, address="Kleyerstraße", latitude=50.0988,
         longitude=8.632103),
    # two buildings the dedup job merely grouped: different street addresses
    _row(105, "equinix-equinix-fr2-5b1d0001", address="Hanauer Landstrasse 300"),
    _row(106, "equinix-inc-equinix-fr2-5b1d0002", dup=105,
         address="Kleyerstrasse 90"),
    # no addresses, keeper 25 km away
    _row(107, "cloudhq-cloudhq-lc3-5b1d0003", address=None, latitude=50.2,
         longitude=8.9),
    _row(108, "unknown-cloudhq-lc3-5b1d0004", dup=107, address=None,
         latitude=50.0, longitude=8.6),
    # no addresses and no coordinates
    _row(109, "databank-databank-bos1-5b1d0005", address=None, latitude=None,
         longitude=None),
    _row(110, "databank-ltd-databank-bos1-5b1d0006", dup=109, address=None,
         latitude=None, longitude=None),
    # a keeper that points onward (and is itself a twin of 101)
    _row(111, "qts-qts-ric1-5b1d0007", dup=101),
    _row(112, "qts-realty-trust-inc-qts-ric1-5b1d0008", dup=111),
    # a keeper whose frozen slug two rows wear
    _row(113, "digital-realty-digital-realty-ams1-5b1d0009"),
    _row(114, "digital-realty-digital-realty-ams1-5b1d0009"),
    _row(115, "digital-realty-trust-digital-realty-ams1-5b1d000a", dup=113),
    # a suppressed keeper, a keeper with a blank slug, a pointer at nothing
    _row(116, "coresite-coresite-sv3-5b1d000b", is_duplicate=1),
    _row(117, "coresite-inc-coresite-sv3-5b1d000c", dup=116),
    _row(118, ""),
    _row(119, "switch-ltd-switch-reno-5b1d000d", dup=118),
    _row(120, "ragingwire-ntt-va2-5b1d000e", dup=99999),
    # an unfrozen twin: its link is today's build, which the page finds by hash8
    _row(121, None, dup=101, provider="Switch", name="Switch Tahoe Reno 2"),
    # case A: frozen with the doubled prefix; today's build differs, same hash8
    _row(122, FROZEN_DOUBLED, provider="Iron Mountain", name="Iron Mountain LON-3"),
]
FACILITIES = [
    # legacy-only, reachable by hash8 under another name-part
    _row("c0ffee00c0ffee00", "ovh-rbx-8-" + H_OVH, provider="OVH", name="OVH RBX-8"),
]
ALIASES = {
    ALIAS_TO_TWIN: TWIN,                              # alias -> twin -> keeper
    "cycle-a-00000001": "cycle-b-00000002",
    "cycle-b-00000002": "cycle-a-00000001",
    "long-a-0000000a": "long-b-0000000b",
    "long-b-0000000b": "long-c-0000000c",
    "long-c-0000000c": "long-d-0000000d",
    "long-d-0000000d": "long-e-0000000e",
    "self-alias-0000000f": "self-alias-0000000f",
    "not-a-facility-slug": KEEPER,                    # no 8-character tail
}


def _stays(slug):
    return {slug: slug}


# The answer for every fixture, written down. This table is the oracle.
EXPECTED = {
    KEEPER: KEEPER,
    TWIN: KEEPER,
    FR5_TWIN: FR5_KEEPER,
    **_stays("equinix-inc-equinix-fr2-5b1d0002"),               # different streets
    **_stays("unknown-cloudhq-lc3-5b1d0004"),                   # 25 km apart
    **_stays("databank-ltd-databank-bos1-5b1d0006"),            # nothing to compare
    "qts-qts-ric1-5b1d0007": KEEPER,                            # a twin of 101 itself
    **_stays("qts-realty-trust-inc-qts-ric1-5b1d0008"),         # keeper points onward
    **_stays("digital-realty-trust-digital-realty-ams1-5b1d000a"),  # shared slug
    **_stays("coresite-inc-coresite-sv3-5b1d000c"),             # suppressed keeper
    **_stays("switch-ltd-switch-reno-5b1d000d"),                # blank-slug keeper
    **_stays("ragingwire-ntt-va2-5b1d000e"),                    # pointer at nothing
    UNFROZEN_BUILD: KEEPER,
    REBUILT: FROZEN_DOUBLED,                                    # case A, by hash8
    "ovhcloud-ovh-rbx-8-" + H_OVH: "ovh-rbx-8-" + H_OVH,        # legacy, by hash8
    ALIAS_TO_TWIN: KEEPER,                                      # two hops
    **_stays("cycle-a-00000001"),
    **_stays("long-a-0000000a"),                                # four hops
    **_stays("long-b-0000000b"),                                # three: unconfirmed
    "long-c-0000000c": "long-e-0000000e",                       # two hops
    **_stays("self-alias-0000000f"),
    "not-a-facility-slug": KEEPER,
    **_stays("nothing-matches-deadbeef"),
    **_stays(TWIN[:-8] + TWIN[-8:].upper()),                    # hash8 is lower-case
}


# ── controls ────────────────────────────────────────────────────────────────
def test_the_route_module_is_the_real_one():
    assert (pathlib.Path(fpp.__file__).resolve()
            == (ROOT / "routes" / "facility_profile_page.py").resolve())


def test_the_fixtures_reproduce_the_live_defect():
    """A twin's own slug 301s to its keeper, and today's build of a
    doubled-prefix row is an alias of it. Were either false, the parity below
    would be agreement about nothing."""
    world = World(DISCOVERED, FACILITIES, ALIASES)
    twin = world.page_row(TWIN)
    assert twin["id"] == 102 and twin["canonical_slug"] == TWIN
    assert fpp._twin_redirect_target(twin, TWIN, keeper_row=world.keeper) == KEEPER
    keeper = world.page_row(KEEPER)
    assert fpp._twin_redirect_target(keeper, KEEPER, keeper_row=world.keeper) is None
    assert REBUILT != FROZEN_DOUBLED
    assert REBUILT.rsplit("-", 1)[1] == FROZEN_DOUBLED.rsplit("-", 1)[1]
    assert UNFROZEN_BUILD.rsplit("-", 1)[1] == stable_hash8("Switch", "Switch Tahoe Reno 2")


def test_the_world_refuses_what_it_does_not_implement():
    shared = World([_row(1, "shared-slug-00000001"), _row(2, "shared-slug-00000001")])
    with pytest.raises(AssertionError, match="ambiguous fixture"):
        shared.page_row("shared-slug-00000001")
    legacy = dict(_row("abc", "legacy-row-0000abcd"), duplicate_of_id="101")
    assert World(facilities=[legacy]).page_row("legacy-row-0000abcd") is None, (
        "the world found a `facilities` row by frozen slug — live, the page's "
        "lookup on that table raises (it has no is_duplicate column)")
    assert World(facilities=[legacy], facilities_has_is_duplicate=True
                 ).page_row("legacy-row-0000abcd")["duplicate_of_id"] == "101"
    by_hash = World(facilities=[dict(FACILITIES[0], duplicate_of_id="101")]
                    ).page_row("ovhcloud-ovh-rbx-8-" + H_OVH)
    assert by_hash["_src_table"] == "facilities"
    assert by_hash["duplicate_of_id"] is None, (
        "the page selects NULL AS duplicate_of_id from facilities by hash8")


# ── 1. parity ───────────────────────────────────────────────────────────────
def _resolve_both(world, slugs):
    with pytest.MonkeyPatch.context() as mp:
        world.install_per_slug(mp, fpp)
        per_request = {s: fpp.resolve_final_slug(s) for s in slugs}
    with pytest.MonkeyPatch.context() as mp:
        world.install_batch(mp, fpp)
        batch = fpp.served_slugs(slugs)
    return per_request, batch


def test_the_batch_lands_where_the_page_resolver_lands_on_every_fixture():
    world = World(DISCOVERED, FACILITIES, ALIASES)
    per_request, batch = _resolve_both(world, list(EXPECTED))
    assert not world.refused, world.refused
    assert set(batch) == set(EXPECTED)
    disagree = {s: (per_request[s], batch[s]) for s in EXPECTED
                if per_request[s] != batch[s]}
    assert not disagree, (
        "served_slugs and resolve_final_slug answer differently from the same "
        f"rows, as (per-request, batch): {disagree}")
    wrong = {s: (batch[s], EXPECTED[s]) for s in EXPECTED if batch[s] != EXPECTED[s]}
    assert not wrong, f"both resolvers agree on a wrong answer, as (got, expected): {wrong}"
    assert world.closed == 1, "served_slugs did not hand its connection back"


# ── 4. rounds, not rows ─────────────────────────────────────────────────────
def test_a_long_list_costs_rounds_not_rows(monkeypatch):
    plain = [_row(1000 + i, f"fixture-operator-site-{i}-{i:08x}") for i in range(300)]
    twins = [_row(5000 + i, f"fixture-operator-inc-site-{i}-{0x70000000 + i:08x}",
                  dup=1000 + i) for i in range(51)]
    slugs = [r["canonical_slug"] for r in plain + twins]
    assert len(set(slugs)) == 351               # euNetworks' distinct URLs
    world = World(plain + twins).install_batch(monkeypatch, fpp)
    got = fpp.served_slugs(slugs)
    assert not world.refused, world.refused
    # hop 1 reads all 351 slugs and the 51 keepers; hop 2 confirms the keepers
    assert world.rounds == {"page_rows": 2, "keeper_rows": 1, "alias_targets": 0}, (
        f"{world.rounds} — a batch per hop, never a lookup per slug")
    assert all(got[twins[i]["canonical_slug"]] == plain[i]["canonical_slug"]
               for i in range(51))
    assert all(got[p["canonical_slug"]] == p["canonical_slug"] for p in plain)


# ── 5. fail open ────────────────────────────────────────────────────────────
def _main_with(monkeypatch, **attrs):
    main = types.ModuleType("main")
    for name, value in attrs.items():
        setattr(main, name, value)
    monkeypatch.setitem(sys.modules, "main", main)


def _pool_down():
    raise RuntimeError("pool down")


def _boom(*_a, **_k):
    raise RuntimeError("statement timeout")


@pytest.mark.parametrize("attrs", [{}, {"get_read_db": lambda: None},
                                   {"get_read_db": _pool_down}],
                         ids=["no-get_read_db", "no-connection", "raises"])
def test_no_database_hands_back_the_slugs(monkeypatch, attrs):
    _main_with(monkeypatch, **attrs)
    assert fpp.served_slugs([TWIN, KEEPER, TWIN]) == {TWIN: TWIN, KEEPER: KEEPER}


def test_a_row_lookup_that_raises_hands_back_every_slug(monkeypatch):
    world = World(DISCOVERED, FACILITIES, ALIASES).install_batch(monkeypatch, fpp)
    monkeypatch.setattr(fpp, "_batch_page_rows", _boom)
    assert fpp.served_slugs([TWIN, FR5_TWIN]) == {TWIN: TWIN, FR5_TWIN: FR5_TWIN}
    assert world.closed == 1, "the connection was not handed back after a failure"


def test_a_keeper_lookup_that_raises_stops_only_the_keeper_hops(monkeypatch):
    World(DISCOVERED, FACILITIES, ALIASES).install_batch(monkeypatch, fpp)
    monkeypatch.setattr(fpp, "_batch_keeper_rows", _boom)
    assert fpp.served_slugs([TWIN, REBUILT]) == {TWIN: TWIN, REBUILT: FROZEN_DOUBLED}


def test_an_alias_lookup_that_raises_stops_only_the_alias_hops(monkeypatch):
    World(DISCOVERED, FACILITIES, ALIASES).install_batch(monkeypatch, fpp)
    monkeypatch.setattr(fpp, "_batch_alias_targets", _boom)
    assert fpp.served_slugs([ALIAS_TO_TWIN, TWIN]) == {ALIAS_TO_TWIN: ALIAS_TO_TWIN,
                                                        TWIN: KEEPER}


def test_a_decision_that_raises_hands_back_only_that_slug(monkeypatch):
    World(DISCOVERED, FACILITIES, ALIASES).install_batch(monkeypatch, fpp)
    real = fpp._twin_redirect_target

    def flaky(fac, slug, keeper_row=None):
        if slug == FR5_TWIN:
            raise RuntimeError("unparseable coordinates")
        return real(fac, slug, keeper_row=keeper_row)
    monkeypatch.setattr(fpp, "_twin_redirect_target", flaky)
    assert fpp.served_slugs([TWIN, FR5_TWIN]) == {TWIN: KEEPER, FR5_TWIN: FR5_TWIN}


def test_nothing_to_resolve_opens_nothing(monkeypatch):
    opened = []
    _main_with(monkeypatch, get_read_db=lambda: opened.append("conn"))
    assert fpp.served_slugs([]) == {}
    assert fpp.served_slugs(None) == {}
    assert fpp.served_slugs(["", None, "   "]) == {}
    assert opened == []


@pytest.mark.parametrize("dup, key", [(42, 42), ("42", 42), (" 42 ", 42),
                                      ("+42", 42), ("7fb2abba31f0f7b4", None),
                                      ("", None), (None, None), (True, None)])
def test_a_pointer_is_keyed_the_way_the_keeper_lookup_binds_it(dup, key):
    """`k.id = %s` coerces a digit string to an integer and raises on a hex id.
    The real binds are compared against Postgres in the parity test."""
    assert fpp._twin_key(dup) == key


# ── 2. one copy of the rules ────────────────────────────────────────────────
EMITTERS = {
    "carrier_facility_ingestion.py": ("_facility_slugs",),
    "facilities_hub.py": ("_render_listing",),
    "routes/dcpi.py": ("_dcpi_facility_list_html",),
    "routes/indexnow.py": ("_served_facility_urls",),
    "routes/market_deep_dive.py": ("_market_facility_links_html",),
    "routes/mcp_tier1_tools.py": ("_facility_page_urls",),
    "routes/seo_pages.py": ("facility_page",),
}


def _tree(rel):
    return ast.parse((ROOT / rel).read_text(encoding="utf-8"))


def _parents(tree):
    return {child: node for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)}


def _enclosing_def(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None


def _callee(call):
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _one_def(tree, name):
    hits = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(hits) == 1, f"expected one def {name}(), found {len(hits)}"
    return hits[0]


def _inside_a_loop(node, fn, parents):
    """True when `node` runs once per iteration of a loop or comprehension in fn."""
    child = node
    while child is not fn and child in parents:
        parent = parents[child]
        if isinstance(parent, (ast.For, ast.AsyncFor)) and child is not parent.iter:
            return True
        if isinstance(parent, ast.While) and child is not parent.test:
            return True
        if isinstance(parent, ast.comprehension) and child is not parent.iter:
            return True
        if (isinstance(parent, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp))
                and child not in parent.generators):
            return True
        child = parent
    return False


def test_the_twin_rules_are_decided_in_one_function():
    """The four case-B conditions live in _twin_redirect_target. A second copy
    anywhere a facility link is built would drift from the page silently."""
    slug_rows_reads, same_site_calls = set(), set()
    for rel in ["routes/facility_profile_page.py", *EMITTERS]:
        tree = _tree(rel)
        parents = _parents(tree)
        for node in ast.walk(tree):
            if ((isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
                 and isinstance(node.slice, ast.Constant)
                 and node.slice.value == "slug_rows")
                    or (isinstance(node, ast.Call) and _callee(node) == "get"
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == "slug_rows")):
                slug_rows_reads.add((rel, _enclosing_def(node, parents)))
            if isinstance(node, ast.Call) and _callee(node) == "_same_physical_site":
                same_site_calls.add((rel, _enclosing_def(node, parents)))
    only = {("routes/facility_profile_page.py", "_twin_redirect_target")}
    assert slug_rows_reads == only, f"slug_rows is read in {sorted(slug_rows_reads)}"
    assert same_site_calls == only, f"_same_physical_site is asked in {sorted(same_site_calls)}"
    walk = _one_def(_tree("routes/facility_profile_page.py"), "served_slugs")
    decisions = [n for n in ast.walk(walk)
                 if isinstance(n, ast.Call) and _callee(n) == "_twin_redirect_target"]
    assert len(decisions) == 1 and [k.arg for k in decisions[0].keywords] == ["keeper_row"], (
        "served_slugs must decide each hop with _twin_redirect_target, handing it "
        "the batch's keepers")


def test_each_emitter_resolves_its_list_in_one_call_outside_any_loop():
    for rel, names in EMITTERS.items():
        tree = _tree(rel)
        parents = _parents(tree)
        for name in names:
            fn = _one_def(tree, name)
            calls = [n for n in ast.walk(fn)
                     if isinstance(n, ast.Call) and _callee(n) == "served_slugs"]
            assert len(calls) == 1, f"{rel} {name}() calls served_slugs {len(calls)}x"
            assert not _inside_a_loop(calls[0], fn, parents), (
                f"{rel} {name}() calls served_slugs once per row — resolve the list")
    # IndexNow submits through one shared helper; each builder hands it the
    # whole list it is about to submit, never a row at a time.
    tree = _tree("routes/indexnow.py")
    parents = _parents(tree)
    for name in ("_recent_facility_urls", "ping_new_facilities"):
        fn = _one_def(tree, name)
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and _callee(n) == "_served_facility_urls"]
        assert len(calls) == 1, f"{name}() calls _served_facility_urls {len(calls)}x"
        assert not _inside_a_loop(calls[0], fn, parents), (
            f"{name}() resolves per row — one call for the list it submits")
    tree = _tree("routes/mcp_tier1_tools.py")
    parents = _parents(tree)
    for name in ("find_alternatives", "score_facility"):
        fn = _one_def(tree, name)
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and _callee(n) == "_facility_page_urls"]
        assert len(calls) == 1, f"{name}() calls _facility_page_urls {len(calls)}x"
        assert not _inside_a_loop(calls[0], fn, parents), (
            f"{name}() builds facility URLs per row — one call for the list")


# ── 3. the statements are the page's ────────────────────────────────────────
class _RecordingConnection:
    """psycopg2-shaped, over an EMPTY database, recording every statement. The
    information_schema probes find the probed column on both tables, so every
    lookup the page can make is made."""

    def __init__(self):
        self.statements = []

    def cursor(self, *_a, **_k):
        return _RecordingCursor(self)

    def rollback(self):
        pass

    def commit(self):
        pass

    def close(self):
        pass


class _RecordingCursor:
    description = ()

    def __init__(self, conn):
        self._conn, self._rows = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self._conn.statements.append(text)
        if "information_schema.columns" in text:
            self._rows = ([("discovered_facilities",), ("facilities",)]
                          if "table_name IN" in text else [(1,)])
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def close(self):
        pass


def _top_level(text, sep):
    """Split at `sep` wherever it sits outside parentheses and quotes."""
    parts, depth, quoted, start, i = [], 0, False, 0, 0
    while i < len(text):
        ch = text[i]
        if ch == "'":
            quoted = not quoted
        elif not quoted and ch == "(":
            depth += 1
        elif not quoted and ch == ")":
            depth -= 1
        elif not quoted and depth == 0 and text.startswith(sep, i):
            parts.append(text[start:i].strip())
            i += len(sep)
            start = i
            continue
        i += 1
    parts.append(text[start:].strip())
    return parts


def _statement(sql):
    """A one-table SELECT as its parts."""
    assert sql.startswith("SELECT "), sql
    body, distinct_on = sql[len("SELECT "):], None
    if body.startswith("DISTINCT ON ("):
        i, depth = len("DISTINCT ON ("), 1
        while depth:
            depth += {"(": 1, ")": -1}.get(body[i], 0)
            i += 1
        distinct_on, body = body[len("DISTINCT ON ("):i - 1], body[i:].strip()
    select_list, rest = _top_level(body, " FROM ")
    rest, _, limit = rest.partition(" LIMIT ")
    rest, _, order = rest.partition(" ORDER BY ")
    source, _, where = rest.partition(" WHERE ")
    return {"distinct_on": distinct_on, "items": _top_level(select_list, ","),
            "table": source.split()[0], "where": _top_level(where, " AND "),
            "order": _top_level(order, ",") if order else [], "limit": limit or None}


def _row_lookups(statements):
    out = {}
    for sql in statements:
        if not sql.startswith("SELECT ") or "information_schema" in sql:
            continue
        st = _statement(sql)
        if st["table"] not in ("discovered_facilities", "facilities"):
            continue
        key = st["where"][0].split(" = ")[0]
        out[(st["table"], "slug" if key == "canonical_slug" else "hash8")] = st
    return out


@pytest.fixture
def recorder(monkeypatch):
    conn = _RecordingConnection()
    _main_with(monkeypatch, get_read_db=lambda: conn, get_db=lambda: conn)
    # the page warms the NER noindex cache on its connection first; a recorded
    # empty answer would mark that process-wide cache fresh for other tests
    import util.facility_ner_noindex as ner
    monkeypatch.setattr(ner, "refresh_suppressed_slugs", lambda *_a, **_k: frozenset())
    return conn


def test_the_batch_row_lookups_are_the_page_row_lookups(recorder):
    assert fpp._fetch_facility_by_slug(FR5_TWIN) is None
    page = _row_lookups(recorder.statements)
    recorder.statements.clear()
    cur = recorder.cursor()
    assert fpp._batch_page_rows(recorder, cur, {FR5_TWIN},
                                fpp._canonical_slug_tables(recorder, cur)) == {}
    batch = _row_lookups(recorder.statements)
    arms = {("discovered_facilities", "slug"), ("facilities", "slug"),
            ("discovered_facilities", "hash8"), ("facilities", "hash8")}
    assert set(page) == arms, f"the page's row lookups changed shape: {sorted(page)}"
    assert set(batch) == arms, f"the batch's row lookups: {sorted(batch)}"
    for arm in sorted(arms):
        p, b = page[arm], batch[arm]
        key = p["where"][0].split(" = ")[0]
        assert (p["distinct_on"], p["where"], p["limit"]) == (None, [key + " = %s"], "1"), arm
        assert b["distinct_on"] == key and b["where"] == [key + " = ANY(%s)"], (arm, b)
        assert b["limit"] is None, arm
        assert b["order"] == [key] + p["order"], (
            f"{arm}: the batch must pick the page's LIMIT 1 row — ORDER BY "
            f"{b['order']} against the page's {p['order']}")
        page_cols = {c for c in p["items"] if "substation_band" not in c}
        batch_cols = {c for c in b["items"] if c != key + " AS " + fpp._BATCH_KEY}
        assert batch_cols == page_cols, (
            f"{arm}: the batch selects different columns — a column the page names "
            f"is what decides whether its lookup raises: {sorted(batch_cols ^ page_cols)}")


def test_the_batch_keeper_lookup_is_the_page_keeper_lookup(recorder):
    assert fpp._canonical_twin_row(337) is None
    (page_sql,) = [s for s in recorder.statements if " FROM discovered_facilities k " in s]
    recorder.statements.clear()
    assert fpp._batch_keeper_rows(recorder.cursor(), [337]) == {}
    (batch_sql,) = [s for s in recorder.statements if " FROM discovered_facilities k " in s]
    p, b = _statement(page_sql), _statement(batch_sql)
    assert p["table"] == b["table"] == "discovered_facilities"
    assert b["items"] == ["k.id"] + p["items"], (b["items"], p["items"])
    assert (p["limit"], b["limit"]) == ("1", None)
    assert "k.id = %s" in p["where"] and "k.id = ANY(%s)" in b["where"]
    assert sorted(w.replace("k.id = ANY(%s)", "k.id = %s") for w in b["where"]) == sorted(p["where"])


def test_the_batch_alias_lookup_is_the_page_alias_lookup(recorder):
    assert fsf.resolve_alias("gone-facility-0badf00d") is None
    (page_sql,) = [s for s in recorder.statements if "facility_slug_aliases" in s]
    recorder.statements.clear()
    assert fpp._batch_alias_targets(recorder.cursor(), ["gone-facility-0badf00d"]) == {}
    (batch_sql,) = [s for s in recorder.statements if "facility_slug_aliases" in s]
    p, b = _statement(page_sql), _statement(batch_sql)
    assert p["table"] == b["table"] == "facility_slug_aliases"
    assert b["items"] == ["old_slug"] + p["items"]
    assert b["where"] == [p["where"][0].replace("= %s", "= ANY(%s)")]
