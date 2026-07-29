"""tests/test_hosting_capacity_source_invariants.py — capacity_type integrity guard.

Guards the SOURCES table in routes/hosting_capacity_ingest.py, which decides what
every ingested row MEANS. capacity_type is the whole point of this table:

  load         = what a NEW DATA-CENTRE LOAD CAN DRAW. The question that converts.
  gen          = DER hosting capacity — what a feeder can ACCEPT from solar/storage
                 for EXPORT. NEVER relayable as siteable load.
  bus_headroom = transmission bus MW.

Mislabelling a `gen` layer as `load` is the worst defect this file can ship: a
site-selection team would read solar-export headroom as available data-centre
power. Worse than not shipping the source at all.

Ways it can regress, each one asserted below:
  (1) SILENT DEFAULT — map_feature() used to read src.get("capacity_type", "gen"),
      so an entry that forgot the key was typed `gen` with nobody having decided
      that. Declaration must stay EXPLICIT on every entry.
  (2) GEN SERVICE TYPED LOAD — a service whose own name says Generation/DER is
      typed `load`, or a known GENERATION field is mapped into a load source's
      capacity slot. check_source_contract() is executed here against every
      shipped entry, so this is tested through the REAL function, not a copy.
  (3) VERTEX ROWS AS FEEDERS — PSE&G publishes 287,307 GIS vertex rows for 2,317
      circuits (measured 123.5x). Without a dedupe key the table stores vertices
      and any count taken off it is a fabricated feeder count.
  (4) BINNED VALUE PUBLISHED AS A FIGURE — LADWP publishes only text ranges and
      badges its top 4.8 kV bin (at most 0.6 MW) "High Capacity". A bin midpoint
      published as MW would relay 0.6 MW as a data-centre answer.
  (5) SILENT NO-OP — a source that maps no capacity field and does NOT declare
      negative_signal is dropped row-by-row by map_feature() and reports a clean
      empty ingest. That is the failure mode this shell family keeps rediscovering
      ("fires nothing", "scaffold = no-op"), so it is asserted structurally.
  (6) UNIT UNSTATED — PECO's number is MVA, not MW, and no power factor is
      published. The unit must ride in the data, not in a comment.

House rules (reference_dchub_green_main_0709): pre-merge pytest has NO DB and must
NEVER import main — routes/hosting_capacity_ingest.py imports flask at module
scope, so SOURCES and check_source_contract() are pulled out with `ast` and
executed against a built namespace, never imported. Nothing runs at module scope.

★ The AST-extract trap (repeat offender in this repo): an extractor that silently
yields an EMPTY list makes every assertion below vacuously true — a green suite
proving nothing. _sources() asserts the file parsed AND that SOURCES was found AND
that it is non-empty; _contract_ns() asserts every free variable the real function
needs actually resolved. test_extractor_is_not_vacuous is the MUST-FAIL control.

Run:  python3 -m pytest tests/test_hosting_capacity_source_invariants.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INGEST = _ROOT / "routes" / "hosting_capacity_ingest.py"

# The only meanings a row may carry. The map legend, feeders_near()'s
# capacity_type_meaning block and the /feeders endpoint all switch on exactly
# these three.
_ALLOWED_TYPES = {"load", "gen", "bus_headroom"}

# Substrings marking a service as a GENERATION/DER publication in the utility's
# OWN naming. Matched case-insensitively against the source URL(s).
# ★ "hosting_capacity" is deliberately NOT a marker: utilities use the phrase
# for BOTH directions (RI_Hosting_Capacity_2025 is gen, Dominion's
# EV_Hosting_Capacity_Available_EB is load), so it flags nothing useful and
# produces a false positive on a correctly-typed load source. The load/gen
# separation is enforced for real by _GEN_ONLY_FIELDS inside
# check_source_contract(), which matches the CAPACITY FIELD rather than a name;
# this URL list is only a cheap second net for unambiguous namings.
_GEN_MARKERS = ("generation", "_gen_", "gen_popup", "dg_hosting", "nodalhcv")

# Sources whose raw rows are GIS vertices, with the dedupe field that collapses
# them. Measured 2026-07-29: 287,307 rows / 2,317 circuits = 123.5x.
_VERTEX_INFLATED = {"pseg_nj_load": "base_circuitid"}

# Load sources that shipped BEFORE capacity_basis existed. This is a RATCHET:
# the set may shrink, never grow. A new load source without a stated basis
# fails test_load_sources_state_their_basis.
_LEGACY_LOAD_WITHOUT_BASIS = {"aep_load", "ameren_il_load", "cenhud_load"}


def _tolerant_eval(node):
    """literal_eval, but non-literal values (env lookups etc.) become a marker.

    SOURCES has carried `int(os.environ.get(...))` values; a strict
    literal_eval raises on those and would take the whole guard offline.
    """
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        pass
    if isinstance(node, ast.Dict):
        return {_tolerant_eval(k): _tolerant_eval(v)
                for k, v in zip(node.keys, node.values)}
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [_tolerant_eval(e) for e in node.elts]
    return "<dynamic>"


def _tree():
    src_text = _INGEST.read_text(encoding="utf-8")
    assert src_text.strip(), "ingest module is empty — nothing to guard"
    tree = ast.parse(src_text)
    assert isinstance(tree, ast.Module), "ast.parse did not yield a Module"
    assert tree.body, "ast.parse yielded an EMPTY module — extractor is broken"
    return tree


def _sources() -> list:
    """The shipped SOURCES list, without importing the module."""
    node = None
    for stmt in _tree().body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "SOURCES":
                    node = stmt.value
    assert node is not None, "SOURCES assignment not found in the ingest module"
    sources = _tolerant_eval(node)
    assert isinstance(sources, list), "SOURCES is not a list"
    assert sources, "SOURCES extracted EMPTY — every assertion would be vacuous"
    return sources


def _contract_ns() -> dict:
    """Build a namespace holding the REAL check_source_contract() + its globals.

    ★ The function reads three module-level constants. Executing it without them
    would raise NameError — or worse, silently test nothing if the call were
    wrapped. Every name is asserted present after the exec.
    """
    wanted = {"_ALLOWED_CAPACITY_TYPES", "_GEN_ONLY_FIELDS",
              "_ROW_NOT_FEEDER_SOURCES"}
    keep, found_fn = [], False
    for stmt in _tree().body:
        if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in wanted
                for t in stmt.targets):
            keep.append(stmt)
        elif (isinstance(stmt, ast.FunctionDef)
                and stmt.name == "check_source_contract"):
            keep.append(stmt)
            found_fn = True
    assert found_fn, "check_source_contract() not found in the ingest module"
    ns: dict = {}
    exec(compile(ast.Module(body=keep, type_ignores=[]), "<contract>", "exec"), ns)
    for name in wanted:
        assert name in ns, "free variable %r did not resolve — extract is broken" % name
    assert callable(ns.get("check_source_contract"))
    return ns


def _urls(src: dict) -> str:
    parts = [src.get("url") or ""]
    parts.extend(src.get("url_candidates") or [])
    return " ".join(p for p in parts if isinstance(p, str)).lower()


def test_extractor_is_not_vacuous():
    """MUST-FAIL control: proves _sources() really loaded the shipped table."""
    sources = _sources()
    assert len(sources) >= 18, (
        "expected the full shipped SOURCES table; got %d — the extractor is not "
        "reading the real list and every other assertion here is vacuous"
        % len(sources))
    keys = [s.get("key") for s in sources]
    for anchor in ("phi", "aep_load", "avista_bus"):
        assert anchor in keys, "extractor lost known-shipped source %r" % anchor


def test_every_source_declares_capacity_type():
    """(1) EXPLICIT declaration — never a silent default."""
    missing = [s.get("key") for s in _sources() if "capacity_type" not in s]
    assert not missing, (
        "these SOURCES entries do not DECLARE capacity_type: %s. It decides "
        "whether a row means siteable load or solar-export headroom, so it must "
        "be an explicit, reviewable choice on every entry." % missing)


def test_capacity_type_values_are_in_the_allowed_set():
    bad = [(s.get("key"), s.get("capacity_type")) for s in _sources()
           if s.get("capacity_type") not in _ALLOWED_TYPES]
    assert not bad, ("capacity_type must be one of %s; offending: %s"
                     % (sorted(_ALLOWED_TYPES), bad))


def test_no_generation_service_is_typed_load():
    """(2) The worst possible defect: DER export headroom relayed as load."""
    offenders = []
    for s in _sources():
        if s.get("capacity_type") != "load":
            continue
        hit = [m for m in _GEN_MARKERS if m in _urls(s)]
        if hit:
            offenders.append((s.get("key"), hit))
    assert not offenders, (
        "typed capacity_type='load' but the service URL carries a "
        "GENERATION/DER marker: %s. A gen layer served as load tells a "
        "site-selection team it can draw power that is really solar-export "
        "headroom." % offenders)


def test_every_source_passes_the_real_runtime_contract():
    """(2) Executed through the shipped check_source_contract(), not a copy.

    A source this refuses is never crawled at runtime, so an entry that fails
    here would ship as a permanently silent no-op.
    """
    check = _contract_ns()["check_source_contract"]
    refused = [(s.get("key"), check(s)) for s in _sources() if check(s)]
    assert not refused, (
        "check_source_contract() refuses these entries, so they would be "
        "skipped on every run: %s" % refused)


def test_known_gen_layers_keep_their_gen_type():
    """Dominion's public BINNED layer is the GEN layer; a separate Dominion
    LOAD layer now also ships. The two must never be conflated."""
    by_key = {s.get("key"): s for s in _sources()}
    dom = by_key.get("dominion_va")
    assert dom is not None, "dominion_va entry disappeared"
    assert dom.get("capacity_type") == "gen", (
        "dominion_va is the BINNED DER/generation layer and must stay "
        "capacity_type='gen'; got %r" % dom.get("capacity_type"))
    if "dominion_va_ev_load" in by_key:
        assert by_key["dominion_va_ev_load"].get("url") != dom.get("url"), (
            "the Dominion LOAD and GEN entries point at the same service — one "
            "of them is mislabelled")


def test_vertex_inflated_sources_declare_a_dedupe_key():
    """(3) Vertex rows must collapse to feeders, or counts are fabricated."""
    by_key = {s.get("key"): s for s in _sources()}
    for key, expected in sorted(_VERTEX_INFLATED.items()):
        s = by_key.get(key)
        assert s is not None, (
            "source %r is missing; it publishes GIS vertex rows and needs a "
            "dedupe key before it can be ingested" % key)
        assert s.get("dedupe_key") == expected, (
            "source %r stores one row per GIS VERTEX (measured 123.5x). It must "
            "declare dedupe_key=%r so vertices collapse onto one row per "
            "circuit; got %r" % (key, expected, s.get("dedupe_key")))
        assert s.get("fields", {}).get("feeder") == expected, (
            "source %r dedupes on %r but reads a different feeder field (%r) — "
            "the dedupe key must be a field the query actually returns"
            % (key, expected, s.get("fields", {}).get("feeder")))


def test_binned_source_publishes_no_numeric_capacity():
    """(4) LADWP publishes text ranges only — never a number."""
    by_key = {s.get("key"): s for s in _sources()}
    s = by_key.get("ladwp_no_capacity")
    assert s is not None, "ladwp_no_capacity source is missing"
    for slot in ("mw_max", "mw_min"):
        assert s.get("fields", {}).get(slot) is None, (
            "LADWP bins capacity into text ranges and badges its top 4.8 kV bin "
            "(at most 0.6 MW) 'High Capacity'. %s must stay None — a bin "
            "midpoint published as MW would relay 0.6 MW as a data-centre "
            "answer." % slot)
    neg = s.get("negative_signal")
    assert isinstance(neg, dict) and neg.get("field") and neg.get("value"), (
        "ladwp_no_capacity must declare negative_signal={'field':..,'value':..} "
        "so map_feature() keeps the disqualifying flag instead of dropping "
        "every row for having no MW; got %r" % (neg,))


def test_capacityless_sources_declare_a_negative_signal():
    """(5) SILENT NO-OP guard, stated generally rather than per source.

    map_feature() drops any row whose mw_max is None unless the source declares
    negative_signal. A source mapping no capacity field and no negative_signal
    therefore ingests exactly zero rows while reporting a clean empty run.
    """
    offenders = [s.get("key") for s in _sources()
                 if not s.get("fields", {}).get("mw_max")
                 and not s.get("negative_signal")]
    assert not offenders, (
        "these sources map no mw_max and declare no negative_signal, so every "
        "row is dropped by map_feature() and the run reports a successful EMPTY "
        "ingest: %s" % offenders)


def test_unit_converted_source_states_its_unit_in_the_data():
    """(6) PECO's number is MVA. The unit must ride WITH the data."""
    by_key = {s.get("key"): s for s in _sources()}
    s = by_key.get("peco_load")
    assert s is not None, "peco_load source is missing"
    basis = s.get("capacity_basis") or ""
    assert "MVA" in basis, (
        "PECO's NET_AVAILABLE_CAPACITY is MVA, not MW — the unit is spelled out "
        "in the service's own class-break legend. capacity_basis must say so; "
        "got %r" % basis)
    assert "MVA" in (s.get("utility") or ""), (
        "the MVA caveat must also ride in the utility label, which is the field "
        "the /coverage, /feeders and feeders_near responses surface per row")


def test_load_sources_state_their_basis():
    """No figure ships without its basis. Ratchet: the legacy set may shrink."""
    missing = {s.get("key") for s in _sources()
               if s.get("capacity_type") == "load"
               and not (s.get("capacity_basis") or "").strip()}
    new = missing - _LEGACY_LOAD_WITHOUT_BASIS
    assert not new, (
        "these LOAD sources publish a magnitude with no capacity_basis, so the "
        "serving layer cannot say what the number MEANS: %s" % sorted(new))


def test_legacy_basis_ratchet_does_not_rot():
    """If a legacy entry gains a basis, drop it from the allowlist."""
    missing = {s.get("key") for s in _sources()
               if s.get("capacity_type") == "load"
               and not (s.get("capacity_basis") or "").strip()}
    stale = _LEGACY_LOAD_WITHOUT_BASIS - missing
    assert not stale, (
        "these keys now have a capacity_basis (or no longer exist) and must be "
        "removed from _LEGACY_LOAD_WITHOUT_BASIS so the ratchet keeps "
        "tightening: %s" % sorted(stale))


def test_multi_id_sources_are_flagged():
    """Duke Ohio packs several circuit ids into one cell on 33% of rows.

    Without id_is_multi the comma string becomes feeder_id, inventing a feeder
    named "H4920580052, H492102000A, ..." and corrupting feeder_key.
    """
    by_key = {s.get("key"): s for s in _sources()}
    s = by_key.get("duke_oh_load")
    assert s is not None, "duke_oh_load source is missing"
    assert s.get("id_is_multi") is True, (
        "duke_oh_load reads circuit_ids_v, which is a COMMA-SEPARATED LIST on "
        "2,471 of 7,451 rows; it must declare id_is_multi=True")
    assert s.get("fields", {}).get("voltage_kv") is None, (
        "Duke's voltage columns are multi-valued ('15, 35') and _num() takes "
        "the MAX of a comma list, so mapping one would silently publish the "
        "HIGHEST kV class present. It must stay unmapped.")


def test_attribution_present_where_terms_require_it():
    """Attribution must be a data path, not a comment — several operators
    require credit as a condition of use."""
    need = ("peco_load", "ladwp_no_capacity", "pseg_nj_load", "duke_oh_load")
    by_key = {s.get("key"): s for s in _sources()}
    missing = [k for k in need
               if k in by_key and not (by_key[k].get("attribution") or "").strip()]
    assert not missing, "sources missing an attribution string: %s" % missing


def test_source_keys_are_unique():
    keys = [s.get("key") for s in _sources()]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, "duplicate SOURCES keys: %s" % dupes


@pytest.mark.parametrize("required", ["utility", "key", "fields"])
def test_every_source_has_the_structural_keys(required):
    missing = [s.get("key") or "<no key>" for s in _sources() if required not in s]
    assert not missing, "entries missing %r: %s" % (required, missing)
