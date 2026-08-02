"""Guards for the shared facility-count vocabulary.

Four public surfaces publish four different Ashburn facility counts (130, 141,
179, 206 on 2026-08-01), each correct under a different combination of
population / unit / grouping. util/facility_count_basis.py is the one place
those terms are defined; these tests keep the surfaces speaking it.

House rules: static AST extraction, no import of main.py or routes.radar (both
import flask at module scope), nothing executes at module scope here. The
vocabulary module itself is stdlib-only, so it IS imported directly.
"""
import ast
import os

from util.facility_count_basis import (GROUPINGS, POPULATIONS, UNITS, basis)

_MAIN = os.path.join(os.path.dirname(__file__), "..", "main.py")
_RADAR = os.path.join(os.path.dirname(__file__), "..", "routes", "radar.py")
_AI_CAP = os.path.join(os.path.dirname(__file__), "..", "routes",
                       "ai_capacity_index.py")


def _tree(path: str) -> ast.Module:
    with open(os.path.abspath(path), "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    # Guard the guard: a degenerate parse would vacuously pass every search.
    assert isinstance(tree, ast.Module) and len(tree.body) > 5, (
        f"{path} parsed to a degenerate module — the harness is not looking "
        "at the real file")
    return tree


def _fn(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() is gone — this guard needs updating, "
                         "not deleting")


def _basis_calls(node) -> list:
    """Every basis()/_basis() call under `node`, as its positional literals."""
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        fname = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
        if fname not in ("basis", "_basis"):
            continue
        args = [a.value for a in n.args if isinstance(a, ast.Constant)]
        if args:
            out.append(args)
    return out


# ── 1. the vocabulary itself ─────────────────────────────────────────────────

def test_every_term_is_defined():
    for vocab, axis in ((POPULATIONS, "population"), (UNITS, "unit"),
                        (GROUPINGS, "grouping")):
        assert vocab, f"{axis} vocabulary is empty"
        for term, meaning in vocab.items():
            assert term and term.islower(), f"{axis} {term!r} is not a slug"
            assert meaning and len(meaning) > 20, (
                f"{axis} {term!r} has no usable definition — a term nobody can "
                "look up is how these surfaces drifted apart in the first place")


def test_basis_rejects_an_unknown_term():
    """A typo must fail at the call site. Silently emitting a plausible-looking
    basis is worse than no basis: it reads as authoritative and cannot be
    cross-referenced against anything."""
    good = basis("metered", "row", "city_state")
    assert good["population"] == "metered" and good["unit"] == "row"
    assert good["grouping_means"] == GROUPINGS["city_state"]
    assert good["fleet_filter"] == "COALESCE(is_duplicate,0)=0"

    for bad in (("meterd", "row", "city_state"),
                ("metered", "rows", "city_state"),
                ("metered", "row", "citystate")):
        try:
            basis(*bad)
        except ValueError as e:
            assert "util/facility_count_basis.py" in str(e), (
                "the error must name where to add the term")
        else:
            raise AssertionError(f"basis{bad} silently accepted an unknown term")


def test_populations_stay_distinct_and_ordered_by_width():
    """tracked ⊇ operational ⊇/⊒ metered. If these ever collapse into synonyms
    the surfaces have no vocabulary left to disagree in."""
    assert set(POPULATIONS) == {"tracked", "operational", "metered"}
    assert "any lifecycle status" in POPULATIONS["tracked"]
    assert "power_mw > 0" in POPULATIONS["metered"]
    # The metered definition must keep saying what a MISSING row means, or the
    # count gets read as "these facilities shut down".
    assert "DISCLOSURE" in POPULATIONS["metered"]


# ── 2. the surfaces speak it ─────────────────────────────────────────────────

def test_radar_markets_block_declares_metered_and_tracked():
    """Behavioural: run the real _markets_block against stub rows."""
    node = _fn(_tree(_RADAR), "_markets_block")
    g = {"_BASELINE_MARKETS": [{"city": "X"}], "__builtins__": __builtins__}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<r>", "exec"), g)
    fn = g["_markets_block"]

    live = fn([{"city": "Ashburn", "state": "VA", "facility_count": 141,
                "tracked_count": 199, "total_mw": 6942}])
    assert live["results"][0]["facility_count"] == 141
    assert live["basis"]["facility_count"]["population"] == "metered", (
        "/radar prints its count beside its MW, so the count it labels "
        "facility_count must be the metered population")
    assert live["basis"]["tracked_count"]["population"] == "tracked"
    assert live["basis"]["facility_count"]["grouping"] == "city_state"

    # The frozen fallback must NOT claim a provenance it does not have.
    fell_back = fn([])
    assert fell_back.get("baseline") is True
    assert "basis" not in fell_back, (
        "_BASELINE_MARKETS predates the metered count — labelling those "
        "constants would assert a basis they were never computed under")


def test_by_market_declares_the_widest_population():
    """by-market returns the largest of the four counts, so it is the one most
    likely to be quoted bare. It must say which question it answers."""
    calls = _basis_calls(_fn(_tree(_MAIN), "facilities_by_market"))
    assert calls, ("/api/v1/facilities/by-market no longer publishes a "
                   "count_basis — its bare `count` is the widest published "
                   "facility number and the easiest to misread")
    population, unit, grouping = calls[0][:3]
    assert (population, unit, grouping) == ("tracked", "distinct_site", "city")


def test_every_surface_basis_uses_vocabulary_terms():
    """No surface may invent a term at the call site. This is the check that
    keeps 'one vocabulary' true as surfaces are added."""
    for path, fname in ((_RADAR, "_markets_block"),
                        (_MAIN, "facilities_by_market")):
        for args in _basis_calls(_fn(_tree(path), fname)):
            assert len(args) >= 3, f"{fname}: basis() called without all 3 axes"
            pop, unit, grouping = args[:3]
            assert pop in POPULATIONS, f"{fname}: unknown population {pop!r}"
            assert unit in UNITS, f"{fname}: unknown unit {unit!r}"
            assert grouping in GROUPINGS, f"{fname}: unknown grouping {grouping!r}"


def test_ai_capacity_still_publishes_the_three_population_triplet():
    """The module docstring reconciles ai-capacity's three counts by name. If
    that surface stops publishing all three, the reconciliation is stale — fix
    the docstring in the same change, don't let it rot into folklore."""
    with open(os.path.abspath(_AI_CAP), "r", encoding="utf-8") as f:
        src = f.read()
    for field in ("facility_count", "metered_facility_count", "tracked_count"):
        assert field in src, (
            f"routes/ai_capacity_index.py no longer publishes {field} — "
            "util/facility_count_basis.py's reconciliation table names it")
