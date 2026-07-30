"""Guard: the AUTHORISED hosting-capacity sources (NV Energy, ComEd).

capacity_type is the whole point of hosting_capacity_feeders — `load` is what
a new data-centre load can DRAW, `gen` is what a feeder can ACCEPT from
solar/storage for export, `bus_headroom` is transmission bus MW. Relaying gen
as load is the worst defect this table can carry, because a site-selection
team acts on that number.

These sources each carry a trap that only config can defuse, so the config is
what is asserted here:

  * NV Energy LHC — the GENERATION twin (GHC) sits in the SAME ROW as the load
    field, both Doubles, both mapping cleanly. Only the field name stops the
    swap. Its rows are LINE SECTIONS (781.5 per feeder), so it must declare a
    dedupe key or a row count becomes a feeder count. And LHC=20.0 is a
    CENSORING CEILING ('Over 20MW'), not a maximum, so it must be excluded
    rather than published as a number.
  * NV Energy GNA — a DEFICIENCY table; the publisher requires attribution.
  * ComEd — reachable only with a Referer, which must never be sent without
    our own identifying User-Agent alongside it. Its layer 28 is the circuit
    grain; layers 29-32 are PLSS tiles carrying the HIGHEST circuit value in
    each tile, so a tile value overstates any specific site inside it.

Never imports main (or the route module): the config is read out of the source
with ast and the SOURCES assignment alone is executed. Nothing runs at module
scope.
"""

import ast
import os

_INGEST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "routes", "hosting_capacity_ingest.py")

_ALLOWED_TYPES = ("load", "gen", "bus_headroom")
_AUTHORISED = {"nvenergy_lhc": "load",
               "nvenergy_gna": "bus_headroom",
               "comed_ev_load": "load"}


def _tree():
    """Parse the ingester. ★ Assert it PARSED: an empty or truncated tree
    satisfies every downstream assertion vacuously, which is how an AST guard
    quietly stops guarding."""
    with open(_INGEST, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=_INGEST)
    assert isinstance(tree, ast.Module), "ast.parse did not yield a Module"
    assert len(tree.body) > 20, (
        "parsed tree has only %d top-level nodes — the file did not parse as "
        "expected and these assertions would pass vacuously" % len(tree.body))
    return tree


def _assign(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name
                for t in node.targets):
            return node
    raise AssertionError("module-level assignment %r not found in %s"
                         % (name, _INGEST))


def _literal(tree, name):
    return ast.literal_eval(_assign(tree, name).value)


def _name_set(tree, name):
    """frozenset({...}) / set({...}) → a plain set."""
    value = _assign(tree, name).value
    if isinstance(value, ast.Call):
        value = value.args[0]
    return set(ast.literal_eval(value))


def _sources(tree):
    """Execute ONLY the SOURCES assignment. Entries call
    int(os.environ.get(...)) for their row caps, so literal_eval cannot be
    used; `os` is supplied and any other free name raises NameError here
    rather than being silently untested."""
    node = _assign(tree, "SOURCES")
    module = ast.Module(body=[node], type_ignores=[])
    namespace = {"os": os}
    exec(compile(ast.fix_missing_locations(module), _INGEST, "exec"),
         namespace)
    sources = namespace["SOURCES"]
    assert isinstance(sources, list), "SOURCES is not a list"
    assert len(sources) >= 18, (
        "only %d SOURCES parsed — the extraction is broken, not the config"
        % len(sources))
    return sources


def _by_key(sources):
    return {s["key"]: s for s in sources}


def _field_names(src):
    """Every source field name actually mapped into a column."""
    out = []
    for spec in (src.get("fields") or {}).values():
        if isinstance(spec, tuple):
            out.append(spec[0])
        elif spec:
            out.append(spec)
    return out


def _urls(src):
    return [u for u in ([src.get("url")] + list(src.get("url_candidates")
                                                or [])) if u]


# ── invariants that already hold (regression guards) ────────────────────

def test_every_source_declares_an_allowed_capacity_type():
    tree = _tree()
    assert tuple(_literal(tree, "_ALLOWED_CAPACITY_TYPES")) == _ALLOWED_TYPES
    for src in _sources(tree):
        assert src.get("capacity_type") in _ALLOWED_TYPES, (
            "source %r declares capacity_type %r — every source must declare "
            "what its megawatts MEAN" % (src.get("key"),
                                         src.get("capacity_type")))


def test_no_generation_field_is_ever_labelled_load():
    tree = _tree()
    gen_only = _name_set(tree, "_GEN_ONLY_FIELDS")
    assert gen_only, "_GEN_ONLY_FIELDS is empty — the check would pass always"
    for src in _sources(tree):
        if src.get("capacity_type") != "load":
            continue
        for slot in ("mw_max", "mw_min"):
            spec = (src.get("fields") or {}).get(slot)
            name = spec[0] if isinstance(spec, tuple) else spec
            assert not name or name not in gen_only, (
                "%s maps GENERATION field %r into %s while declaring "
                "capacity_type 'load'" % (src["key"], name, slot))
        # Belt and braces: a *.GHC-style qualified generation field would not
        # be caught by the bare-name list.
        for name in _field_names(src):
            assert not name.upper().endswith(".GHC"), (
                "%s maps %r (generation hosting capacity) as load"
                % (src["key"], name))


def test_declared_row_not_feeder_sources_honour_their_knob():
    tree = _tree()
    granular = _literal(tree, "_ROW_NOT_FEEDER_SOURCES")
    by_key = _by_key(_sources(tree))
    for key, (granularity, knob) in granular.items():
        src = by_key.get(key)
        if src is None:
            continue          # declared ahead of its config; nothing to check
        if knob == "feeder_field":
            assert (src.get("fields") or {}).get("feeder"), (
                "%s rows are %s but no feeder field is mapped, so distinct "
                "feeders cannot be counted apart from rows"
                % (key, granularity))
        else:
            assert src.get(knob), (
                "%s rows are %s and %r is not declared — its row count would "
                "be published as a feeder count" % (key, granularity, knob))


# ── the authorised sources ──────────────────────────────────────────────

def test_authorised_sources_are_present_with_exact_capacity_types():
    by_key = _by_key(_sources(_tree()))
    for key, want in _AUTHORISED.items():
        assert key in by_key, "authorised source %r is not configured" % key
        assert by_key[key]["capacity_type"] == want, (
            "%s declares capacity_type %r, must be %r"
            % (key, by_key[key]["capacity_type"], want))


def test_nv_energy_attribution_rides_on_the_data():
    """NV publishes copyrightText='NV Energy' at the SERVICE ROOT (the layer
    endpoints carry an empty string). Attribution is a condition of use, and
    _SOURCE_ATTRIBUTION is built from this key, so it must be exact."""
    by_key = _by_key(_sources(_tree()))
    for key in ("nvenergy_lhc", "nvenergy_gna"):
        src = by_key.get(key)
        assert src is not None, "%s is not configured" % key
        assert src.get("attribution") == "NV Energy", (
            "%s attribution is %r — must be exactly the string the service "
            "itself publishes" % (key, src.get("attribution")))
        assert src.get("capacity_basis"), (
            "%s publishes a megawatt figure with no basis" % key)


def test_nv_load_maps_lhc_and_never_the_generation_twin():
    """GHC (generation) and LHC (load) are both Doubles in the SAME ROW."""
    src = _by_key(_sources(_tree())).get("nvenergy_lhc")
    assert src is not None, "nvenergy_lhc is not configured"
    spec = src["fields"]["mw_max"]
    name = spec[0] if isinstance(spec, tuple) else spec
    assert name.endswith(".LHC"), (
        "nvenergy_lhc maps %r as its capacity — must be the LHC (load) field"
        % name)
    for mapped in _field_names(src):
        assert "GHC" not in mapped.upper(), (
            "nvenergy_lhc maps %r; GHC is DER EXPORT headroom, not load"
            % mapped)


def test_nv_censoring_ceiling_is_excluded_not_published():
    """LHC=20.0 means 'Over 20MW' — a floor. Publishing it as a maximum would
    state an unmeasured quantity as a number."""
    src = _by_key(_sources(_tree())).get("nvenergy_lhc")
    assert src is not None, "nvenergy_lhc is not configured"
    where = (src.get("where") or "").replace(" ", "")
    assert "LHC<20" in where, (
        "nvenergy_lhc where-clause is %r — it must exclude the 20 MW study "
        "cap, whose rows are censored ('Over 20MW'), not measured at 20"
        % src.get("where"))


def test_nv_section_rows_are_folded_to_feeders():
    """977,693 rows / 1,251 feeders = 781.5 line sections each."""
    tree = _tree()
    granular = _literal(tree, "_ROW_NOT_FEEDER_SOURCES")
    assert "nvenergy_lhc" in granular, (
        "nvenergy_lhc is not registered as a row-is-not-a-feeder source, so "
        "nothing stops its row count being published as a feeder count")
    granularity, knob = granular["nvenergy_lhc"]
    assert knob == "dedupe_key", (
        "nvenergy_lhc needs the dedupe_key knob, got %r" % knob)
    src = _by_key(_sources(tree)).get("nvenergy_lhc")
    assert src is not None, "nvenergy_lhc is not configured"
    assert src.get("dedupe_key", "").endswith("SYN_FEEDER_ID"), (
        "nvenergy_lhc dedupe_key is %r — sections must fold to the FEEDER id"
        % src.get("dedupe_key"))
    assert (src.get("fields") or {}).get("feeder"), (
        "nvenergy_lhc maps no feeder field, so distinct feeders cannot be "
        "counted apart from rows")


def test_comed_access_method_is_recorded_and_identifies_us():
    """A Referer is genuinely required (UA-only and bare-origin both return a
    403 envelope), so it is sent — but never instead of saying who we are."""
    tree = _tree()
    src = _by_key(_sources(tree)).get("comed_ev_load")
    assert src is not None, "comed_ev_load is not configured"
    headers = src.get("headers") or {}
    referer = headers.get("Referer", "")
    assert "webappviewer" in referer and "id=" in referer, (
        "comed_ev_load Referer is %r — the bare origin returns 403 GWM_0003; "
        "it must be the exact full app URL" % referer)
    assert not any(k.lower() == "user-agent" for k in headers), (
        "comed_ev_load overrides User-Agent — our honest identifying UA must "
        "always travel with the Referer so the operator can contact us")
    ua = _literal(tree, "_UA")
    assert "dchub.cloud" in ua and "contact" in ua.lower(), (
        "_UA is %r — it must identify us AND carry a contact URL" % ua)
    assert src.get("capacity_basis"), (
        "comed_ev_load publishes a megawatt figure with no basis")


def test_comed_ingests_the_circuit_layer_not_the_tile_layers():
    """L28 = one row per circuit (ratio 1.000). L29-L32 are PLSS tiles, each
    showing the HIGHEST circuit value inside it — which overstates any
    specific site in the tile."""
    src = _by_key(_sources(_tree())).get("comed_ev_load")
    assert src is not None, "comed_ev_load is not configured"
    urls = _urls(src)
    assert urls, "comed_ev_load has neither url nor url_candidates"
    for url in urls:
        assert url.endswith("/28/query"), (
            "comed_ev_load points at %r — layer 28 is the circuit grain; "
            "29-32 are tile aggregations that overstate a site" % url)
    assert src.get("url_resolver"), (
        "comed_ev_load pins a URL that ComEd rotates MONTHLY with no runtime "
        "resolver — it would fail as a silent empty ingest within weeks")
    assert (src.get("fields") or {}).get("feeder") == "Feeders", (
        "comed_ev_load must map the circuit identifier so distinct feeders "
        "are countable")
