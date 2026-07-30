"""tests/test_hosting_capacity_source_contract.py — capacity_type guard (2026-07-29).

Guards the SOURCES table and check_source_contract() in
routes/hosting_capacity_ingest.py, which decide what a megawatt in
hosting_capacity_feeders MEANS.

Why this test exists. The table mixes three incompatible quantities:

    load          what a NEW DATA-CENTRE LOAD CAN DRAW — the number a site
                  selection team acts on, and the only one that converts.
    gen           DER hosting capacity: what a feeder can ACCEPT from
                  solar/storage for EXPORT. NEVER relayable as siteable load.
    bus_headroom  transmission bus MW.

Publishing a `gen` figure as `load` is the worst defect this shell can ship —
worse than shipping no source at all — because the answer stays confident and
plausible while pointing the wrong way. Before this guard, capacity_type was
OPTIONAL: map_feature() read `src.get("capacity_type", "gen")`, so a source
that said nothing silently became gen, and nothing at all checked the reverse.

Ways it can regress, each asserted below:
  (1) UNDECLARED — a new SOURCES entry ships with no capacity_type and
      inherits a meaning nobody chose.
  (2) GEN-AS-LOAD — a source mapping a generation field declares "load". SCE
      is the live trap: on the ICA circuit-segment layer the gen fields
      (uniform_generation, ica_overall_pv) sit in the SAME ROW as the load
      field, both Doubles, both would map cleanly. Layer 14 of the same
      service is "Uniform GENERATION Static Grid" and layer 15 is "Uniform
      LOAD Static Grid" — one digit apart.
  (3) ROWS-AS-FEEDERS — a source whose rows are GIS vertices / line sections /
      attribute-dissolved multipart lands without the knob that undoes it, and
      a row count gets published as a feeder count (measured 15x-29x inflation
      on already-shipped sources; 172x on SCE).
  (4) STRING ORDERING — the capacity-DESC crawl orders by SCE's String
      capacity column instead of its Double twin. Verified live: ordering by
      the String returns 'NA' rows first (lexicographic), so a capped crawl
      would keep nothing but UNMEASURED rows.
  (5) CONFLATION — the Dominion EV LOAD layer overwrites, or is overwritten
      by, the separately-shipped Dominion GEN layer.

Expected counts (measured, not estimated — the unpatched run was executed
against `git show HEAD:routes/hosting_capacity_ingest.py` in a scratch tree):
  * patched:    18 passed
  * unpatched:  16 failed, 2 passed. Everything fails except
    test_source_keys_are_unique and test_firstenergy_is_not_configured_...,
    because on HEAD: 14 of 18 SOURCES entries carry no capacity_type,
    map_feature() defaults it to "gen", and _ALLOWED_CAPACITY_TYPES,
    _GEN_ONLY_FIELDS, _ROW_NOT_FEEDER_SOURCES, check_source_contract(),
    _dedupe_rows(), explode_multipart, order_field, per-source max_rows,
    the attribution maps and both LOAD entries do not exist at all.

House rules (reference_dchub_green_main_0709): pre-merge pytest has NO DB and
must NEVER import main — routes/hosting_capacity_ingest.py registers a Flask
blueprint and reads env at import, so SOURCES and the constants are pulled out
with `ast` instead, and check_source_contract() is exec'd against stubs.
★ An empty/silent extraction passes every downstream assertion, so each
extractor asserts it really found what it was looking for.
Nothing runs at module scope.

Run:  python3 -m pytest tests/test_hosting_capacity_source_contract.py -v
"""
from __future__ import annotations

import ast
import builtins
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MOD = _ROOT / "routes" / "hosting_capacity_ingest.py"


def _tree():
    src = _MOD.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert tree.body, "hosting_capacity_ingest.py parsed to an EMPTY module"
    return src, tree


def _literal(name):
    """ast.literal_eval a module-level literal assignment, or fail loudly."""
    src, tree = _tree()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                val = ast.literal_eval(node.value)
            except ValueError as exc:            # not a pure literal any more
                raise AssertionError(f"{name} is no longer a literal: {exc}")
            assert val, f"{name} extracted EMPTY — the guard would pass vacuously"
            return val
    raise AssertionError(f"{name} not found in {_MOD.name}")


def _sources():
    srcs = _literal("SOURCES")
    assert len(srcs) >= 18, f"SOURCES shrank to {len(srcs)} — extraction wrong?"
    return srcs


def _func_src(name: str) -> str:
    src, tree = _tree()
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = "\n".join(lines[node.lineno - 1:node.end_lineno])
            assert f"def {name}(" in body, f"{name} sliced but body is wrong"
            assert len(body.splitlines()) > 5, f"{name} extraction too short"
            return body
    raise AssertionError(f"{name}() not found in {_MOD.name}")


def _func_node(name: str) -> ast.FunctionDef:
    """The FunctionDef node, for free-variable analysis."""
    _src, tree = _tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            assert node.body, f"{name}() parsed with an EMPTY body"
            return node
    raise AssertionError(f"{name}() not found in {_MOD.name}")


def _free_vars(fn_node):
    """Module-level names the function reads without binding them itself."""
    assigned, loaded = set(), set()
    for n in ast.walk(fn_node):
        if isinstance(n, ast.Name):
            (assigned if isinstance(n.ctx, ast.Store) else loaded).add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                assigned.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.Lambda)):
            args = n.args
            assigned.update(a.arg for a in (list(args.posonlyargs)
                                            + list(args.args)
                                            + list(args.kwonlyargs)))
            for v in (args.vararg, args.kwarg):
                if v:
                    assigned.add(v.arg)
            if isinstance(n, ast.FunctionDef) and n is not fn_node:
                assigned.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            assigned.add(n.name)
    return loaded - assigned - set(dir(builtins))


def _contract_fn():
    """exec the real check_source_contract against its free variables.

    A NameError here means a free var stopped resolving, which would otherwise
    leave the function SILENTLY untested.

    ★ The free-var list is DERIVED from the function, not hardcoded. It used to
    be a literal triple, and adding a fourth constant to the contract
    (_CENSORING_CEILINGS, 2026-07-30) broke this harness with a NameError — the
    good outcome, but only because the exec happens to run the function. A
    hardcoded list of dependencies rots exactly as fast as the code it
    describes; deriving it means the next constant is supplied automatically and
    a genuinely MISSING one still fails loudly below.
    """
    fn_node = _func_node("check_source_contract")
    ns = {}
    for name in sorted(_free_vars(fn_node)):
        value = _literal(name)          # raises if it is not a module constant
        ns[name] = set(value) if isinstance(value, (set, frozenset)) else value
    exec(compile(_func_src("check_source_contract"), "<hci>", "exec"), ns)
    unresolved = _free_vars(fn_node) - set(ns)
    assert not unresolved, f"free vars unresolved: {sorted(unresolved)}"
    assert ns, "namespace is empty — the extraction read nothing"
    return ns["check_source_contract"]


def _by_key():
    return {s["key"]: s for s in _sources()}


def _fields(**over):
    base = {"feeder": None, "substation": None, "state": None, "region": None,
            "voltage_kv": None, "mw_max": ("X", 1.0), "mw_min": None,
            "queued_kw": None, "updated": None}
    base.update(over)
    return base


# ── (1) every source DECLARES what its megawatts mean ★ ──────────────────

def test_allowed_capacity_types_are_exactly_the_three_meanings():
    assert set(_literal("_ALLOWED_CAPACITY_TYPES")) == {
        "load", "gen", "bus_headroom"}


def test_every_source_declares_an_allowed_capacity_type():
    """★ FAILS UNPATCHED for 14 of 18 entries — they omit capacity_type and
    inherit 'gen' from a .get() default nobody chose.

    ★ Deliberately a loop, NOT @parametrize over _sources(): a decorator
    argument is evaluated at COLLECTION, and an AssertionError there aborts
    collection for the whole file (exit 3, zero tests run, rendered as an
    ordinary red job). Nothing in this file runs at module scope.
    """
    allowed = _literal("_ALLOWED_CAPACITY_TYPES")
    undeclared, wrong = [], {}
    for src in _sources():
        if "capacity_type" not in src:
            undeclared.append(src["key"])
        elif src["capacity_type"] not in allowed:
            wrong[src["key"]] = src["capacity_type"]
    assert not undeclared, (
        f"{len(undeclared)} sources do not declare capacity_type and would "
        f"inherit a meaning by default: {undeclared}")
    assert not wrong, f"capacity_type outside {list(allowed)}: {wrong}"


def test_map_feature_has_no_default_capacity_type():
    """The default is what made (1) possible; it must not come back."""
    body = _func_src("map_feature")
    assert 'src.get("capacity_type"' not in body, (
        "map_feature defaults capacity_type again — an undeclared source "
        "would silently become gen")
    assert 'src["capacity_type"]' in body


def test_source_keys_are_unique():
    keys = [s["key"] for s in _sources()]
    assert len(keys) == len(set(keys))


# ── (2) no gen source is ever labelled load ★ ────────────────────────────

def test_known_generation_sources_are_not_typed_load():
    """Every DER/solar hosting-capacity publication stays 'gen'."""
    gen_keys = ("phi", "ngrid_ny", "dominion_va", "coned", "oru", "nyseg_rge",
                "ri_energy", "bge", "xcel_nsp", "xcel_psco", "dte",
                "cenhud_gen", "ngrid_ma", "eversource_ct")
    by_key = _by_key()
    for k in gen_keys:
        assert k in by_key, f"{k} vanished from SOURCES"
        assert by_key[k]["capacity_type"] == "gen", (
            f"{k} publishes DER export headroom but is typed "
            f"{by_key[k]['capacity_type']!r} — that would be served as "
            f"siteable data-centre load")


def test_contract_refuses_a_load_source_mapping_a_generation_field():
    """★ SCE's own layer carries uniform_generation and ica_overall_pv in the
    SAME ROW as the load field — both Doubles, both map cleanly."""
    fn = _contract_fn()
    bad = {"key": "sce_ica_load", "capacity_type": "load",
           "fields": _fields(feeder="circuit_name",
                             mw_max=("uniform_generation", 1.0))}
    reason = fn(bad)
    assert reason and "generation field" in reason, (
        "a load-typed source mapping a generation field was accepted")
    # ★ `country` added 2026-07-30: check_source_contract now requires every
    # source to DECLARE its geography (see _ALLOWED_COUNTRIES). The REFUSAL
    # fixture above deliberately still omits it — the country gate is ordered
    # LAST precisely so a units bug is reported instead of being masked by a
    # missing-country message, and `bad` proves that ordering holds.
    ok = {"key": "sce_ica_load", "capacity_type": "load", "country": "US",
          "fields": _fields(feeder="circuit_name",
                            mw_max=("uniform_load_static_grid_legend", 1.0))}
    assert fn(ok) is None


def test_contract_refuses_an_undeclared_or_invented_capacity_type():
    fn = _contract_fn()
    assert fn({"key": "x", "fields": _fields()}), "undeclared type accepted"
    assert fn({"key": "x", "capacity_type": "solar", "fields": _fields()})
    # country is required as of 2026-07-30 — a source with a valid capacity_type
    # but no declared geography is still refused, by design.
    assert fn({"key": "x", "capacity_type": "gen", "fields": _fields()}), \
        "a source with no declared country was accepted"
    assert fn({"key": "x", "capacity_type": "gen", "country": "US",
               "fields": _fields()}) is None


def test_every_shipped_source_passes_its_own_contract():
    fn = _contract_fn()
    bad = {s["key"]: fn(s) for s in _sources() if fn(s)}
    assert not bad, f"shipped sources violate the contract: {bad}"


def test_sce_points_at_the_load_layer_not_the_generation_layer():
    """★ L14 = 'ICA - Uniform GENERATION Static Grid',
       L15 = 'ICA - Uniform LOAD Static Grid'. One digit apart."""
    sce = _by_key()["sce_ica_load"]
    assert "/FeatureServer/15/" in sce["url"], sce["url"]
    assert "/FeatureServer/14/" not in sce["url"]
    assert sce["capacity_type"] == "load"


# ── (3) the vertex/section dedupe knob is present where it is needed ★ ───

def test_row_not_feeder_registry_matches_shipped_sources():
    """★ Registry lists the sources whose ROWS ARE NOT FEEDERS; each must be a
    real source and must declare the knob that undoes the inflation."""
    reg = _literal("_ROW_NOT_FEEDER_SOURCES")
    by_key = _by_key()
    for key, (granularity, knob) in reg.items():
        assert key in by_key, f"{key} is registered row-inflated but not shipped"
        src = by_key[key]
        assert granularity, f"{key} declares no row granularity"
        if knob == "feeder_field":
            assert src["fields"]["feeder"], (
                f"{key} rows are {granularity} — without a feeder field the "
                f"row count is the only count available, and rows are not feeders")
        else:
            assert src.get(knob), (
                f"{key} rows are {granularity} but {knob!r} is not declared")


def test_dominion_ev_declares_explode_multipart():
    """★ 5,546 rows == 5,546 distinct attribute triples: each row aggregates
    every segment sharing a (capacity, voltage, voltage) combination, and
    _rep_point() would keep ONE path's midpoint and discard the rest. With no
    feeder id the spatial join is the only join, so this is fatal."""
    dom = _by_key()["dominion_va_ev_load"]
    assert dom["explode_multipart"] is True
    assert dom["fields"]["feeder"] is None, (
        "Dominion's EV layer publishes no feeder identifier — inventing one "
        "would fabricate a join")
    assert dom.get("key_extra"), (
        "with no feeder id the key is the rounded point alone; two capacities "
        "could collide and capacity-DESC would keep the flattering one")


def test_contract_refuses_a_row_inflated_source_missing_its_knob():
    fn = _contract_fn()
    reg = _literal("_ROW_NOT_FEEDER_SOURCES")
    key, (_gran, knob) = next(iter(reg.items()))
    stripped = {k: v for k, v in _by_key()[key].items() if k != knob}
    if knob == "feeder_field":
        stripped["fields"] = _fields(feeder=None)
    reason = fn(stripped)
    assert reason, f"{key} was accepted without {knob!r}"


def test_dedupe_keeps_the_first_row_not_the_largest():
    """★ PSE&G-class vertex duplication: NEGATIVE capacity is REAL (the
    circuit is over-subscribed) and must never be clamped or hidden behind a
    positive sibling, so the fold keeps the first row, not the max."""
    body = _func_src("_dedupe_rows")
    assert "max(" not in body, (
        "_dedupe_rows picks a maximum — that would hide a negative capacity")
    ns = {}
    exec(compile(body, "<hci>", "exec"), ns)
    rows = [{"_dedupe": "C1", "capacity_mw_max": -3.2},
            {"_dedupe": "C1", "capacity_mw_max": 8.0},
            {"_dedupe": "C2", "capacity_mw_max": 1.0},
            {"_dedupe": None, "capacity_mw_max": 2.0}]
    kept, folded = ns["_dedupe_rows"](rows, {"dedupe_key": "base_circuitid"})
    assert folded == 1
    assert kept[0]["capacity_mw_max"] == -3.2, "a real negative was dropped"
    assert len(kept) == 3, "an unkeyed row must not be folded into a phantom"
    assert ns["_dedupe_rows"](rows, {})[0] is rows, "dedupe ran without a key"


# ── (4) capacity-DESC ordering must use a NUMERIC column ★ ───────────────

def test_sce_orders_by_the_double_not_the_string():
    """★ Verified live 2026-07-29: orderByFields=uniform_load_static_grid DESC
    returns six straight 'NA' rows; the _legend Double returns 32.5788 first.
    With a capped crawl the string ordering fills the cap with UNMEASURED rows."""
    sce = _by_key()["sce_ica_load"]
    assert sce["order_field"] == "uniform_load_static_grid_legend"
    assert sce["fields"]["mw_max"][0] == "uniform_load_static_grid_legend"
    assert sce["fields"]["mw_max"][0] != "uniform_load_static_grid"
    # 'NA' rows carry -1 in the Double and are UNMEASURED — they must be
    # excluded at the source, never ingested as a capacity of -1 or 0.
    assert "uniform_load_static_grid_legend >= 0" in sce["where"]
    assert _func_src("_fetch_pages").count('src.get("order_field")') == 1


def test_large_load_sources_raise_the_row_cap_off_the_20k_default():
    """637,977 usable SCE rows against a 20k default is a 3% sample."""
    by_key = _by_key()
    assert by_key["sce_ica_load"]["max_rows"] > 20000
    assert by_key["dominion_va_ev_load"]["max_rows"] > 20000


# ── (5) the two Dominion layers stay separate ★ ──────────────────────────

def test_dominion_gen_and_load_layers_are_never_conflated():
    by_key = _by_key()
    gen, load = by_key["dominion_va"], by_key["dominion_va_ev_load"]
    assert gen["capacity_type"] == "gen" and load["capacity_type"] == "load"
    assert gen["url"] != load["url"]
    assert "EV_Hosting_Capacity_Available_EB" in load["url"]
    assert "Primary_Hosting_Capacity_Available_EB" in gen["url"]
    # utility strings key the unique index AND the attribution map — sharing
    # one would let the two layers upsert over each other.
    assert gen["utility"] != load["utility"]


# ── attribution rides with the data, not in a comment ────────────────────

def test_new_sources_carry_attribution_and_a_stated_basis():
    for key in ("dominion_va_ev_load", "sce_ica_load"):
        src = _by_key()[key]
        assert src.get("attribution"), f"{key} carries no attribution"
        assert src.get("capacity_basis"), f"{key} publishes MW with no basis"
    mod = _MOD.read_text(encoding="utf-8")
    # the maps are built FROM SOURCES (so they cannot drift) and are attached
    # to rows in all three serving paths
    assert "_SOURCE_ATTRIBUTION = {s[\"utility\"]: s[\"attribution\"]" in mod
    assert mod.count('_SOURCE_ATTRIBUTION.get(') >= 3


def test_firstenergy_is_not_configured_from_a_remembered_sweep():
    """NXDOMAIN on public DNS (re-checked 2026-07-29). A config pointing at an
    unresolvable host fails silently every run and reads as an empty ingest."""
    for s in _sources():
        blob = repr(s)
        assert "fenetwork.com" not in blob
        assert "firstenergy" not in blob.lower()
