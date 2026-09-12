"""Two junk values an INDEXABLE facility page was publishing, and the
suppression that stops each.

r-mw-one-owner / r-twin-nullisland (2026-09-12). Re-measured from scratch on
live data — 900 pages drawn in three independent samples from the published
sitemap shards (sitemap-facilities-1, sitemap-ai-facilities-1/2; 18,950 unique
URLs), 781 of which answered 200 and every one of those `index, follow`.

★★★ CLASS 1 — A NUMBER TOO BIG TO BE A BUILDING, CAPPED ON ONE SURFACE OF SIX.

util/facility_headline.MW_PLAUSIBLE_MAX = 5000 has existed since the title work
and the <title> was the only thing that asked it. Live, same second, same row,
/facilities/aep-none-0dc136e7 — `index, follow`, and listed TWICE in the
sitemap. The rendered HTML contained the string "63000" FOUR times:

    <title>                    "AEP None · United States · Planned | DC Hub"
                               ^ display_mw refused it, so no MW at all
    inline Dataset JSON-LD     variableMeasured[0].value = 63000.0
    narrative paragraph        "It carries a reported power capacity of
                                63000.0 MW and is currently planned."
    body stat tile             "Power   63000.0 MW"
    LANE-2 context block       "Reported capacity   63000.0 MW"
    /facilities/<slug>.json    variableMeasured[0].value = 63000.0

★ THE LAST TWO WERE NOT ON THE FIRST LIST. A fix scoped from an enumerated set
  of surfaces gated three of them and left the prose and the LANE-2 row live;
  what caught it was counting occurrences in the whole rendered page rather
  than checking the surfaces someone had thought of. TestBodyTile::
  test_the_number_appears_ZERO_times_in_the_whole_page is that counter, and it
  is the guard to keep if any other is ever dropped. A seventh surface is a
  normal thing for this page to grow.

  A peer's capacity is the SAME number on somebody else's page: the
  comparables list is ORDER BY power DESC, so one fleet-sized row printed
  first on every co-located page in its market. Gated on the same predicate.

63,000 MW is American Electric Power's whole generating fleet. The largest
OPERATIONAL data centre campus on earth is the Citadel, Reno, at ~650 MW; the
largest single campus ever ANNOUNCED is Fermi America's 11 GW Amarillo
HyperGrid. 5,000 MW is therefore ~8x any building that exists and above every
announced campus but one — a cap that cannot plausibly hide a real single site,
which is exactly why this file REUSES the repo's existing constant instead of
choosing a second number. A second number is how this happened.

★★★ CLASS 2 — NULL ISLAND, ALIVE IN THE MACHINE-READABLE TWIN ONLY.

routes.provenance.normalize_coordinates owns the (0,0) sentinel and was already
wired into this page's market block (#4455) and its fiber block (#4474).
Measured over the same 781 indexable pages:

    HTML "Coordinates" stat tile     0 leaks   (`if lat and lng`)
    inline Place JSON-LD geo         0 leaks   (same guard)
    /api/v1/facilities/<slug>        0 leaks   (calls the normaliser)
    /facilities/<slug>.json          10 leaks  <- 1.28%, per draw 0.7/1.3/2.0%

util.facility_entity emits geo "if lat is not None and lon is not None", and
0.0 is not None. The ten were named buildings under a CC-BY "you may cite this"
licence, placed in the Gulf of Guinea: Equinix Washington, Digital Realty
London / Paris / Singapore, CyrusOne Frankfurt FRA1 and FRA3, STT Mumbai 3,
DataBank Indianapolis.

★ WHAT THESE TESTS DELIBERATELY DO NOT ASSERT. Nothing here pins a stored row,
  a corpus count or a slug. The suppression is at the READ boundary precisely
  so that a bad value keeps its provenance and a fix stays reversible; a test
  that named aep-none-0dc136e7 would go green the day that row is re-ingested
  under a different slug while the class stayed live — which is how the
  2026-09-12 audit's two example slugs came to be reported as "gone" when both
  were still serving 200.

Run:  python3 -m pytest tests/test_facility_junk_value_suppression.py -v
"""

import ast
import importlib
import io
import pathlib
import sys
import types

import pytest

from util.facility_entity import facility_entity, facility_measures
from util.facility_headline import MW_PLAUSIBLE_MAX, display_mw, plausible_mw

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "routes" / "facility_profile_page.py"
PAGE_SRC = io.open(PAGE, encoding="utf-8").read()
PAGE_TREE = ast.parse(PAGE_SRC)

# A row shaped like the real one, with the real number. NOT the real slug —
# see the note at the top of this file.
FLEET = {"power_mw": 63000.0, "status": "Planned", "country": "US",
         "name": "Utility None", "provider": "Utility",
         "canonical_slug": "utility-none-deadbeef"}
SITE = {"power_mw": 350.0, "status": "Operational", "city": "Ashburn",
        "state": "VA", "country": "US", "name": "Real DC", "provider": "Lumen",
        "canonical_slug": "lumen-real-dc-cafe1234"}


def _fn(name, tree=PAGE_TREE):
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} is not a top-level def in {PAGE}")


# ───────────────────────── class 1: the plausibility predicate ──────────────

class TestPlausibleMw:
    """The one comparison against the cap, and what it admits."""

    def test_a_utility_fleet_is_not_a_facility(self):
        """The measured value, from the measured page. 63,000 MW is ~97x the
        largest data centre campus that has ever been built."""
        assert plausible_mw(63000.0) is None
        assert plausible_mw(17000) is None       # the second-largest measured
        assert plausible_mw(8000) is None

    def test_the_cap_itself_is_admitted_and_one_step_past_it_is_not(self):
        """An INCLUSIVE boundary, matching display_mw's own `<=`. Meta's
        announced Hyperion campus is 5,000 MW; refusing exactly 5,000 would
        suppress the largest campus anyone has actually announced at that
        number."""
        assert plausible_mw(MW_PLAUSIBLE_MAX) == MW_PLAUSIBLE_MAX
        assert plausible_mw(MW_PLAUSIBLE_MAX + 0.1) is None

    def test_real_capacities_survive(self):
        """The 50th/75th/90th percentile of live indexable pages carrying a
        capacity was 50 / 200 / 800 MW. None of that may move."""
        for good in (0.5, 2.5, 36, 50.0, "200", 650, 800, 1200, 2000):
            assert plausible_mw(good) is not None, good

    def test_absent_zero_and_unparseable_are_refused_not_raised(self):
        """power_mw is absent on ~89% of rows; a fabricated 0 is
        indistinguishable from a real reading of zero."""
        for bad in (None, 0, 0.0, "0", "", "n/a", "Unknown", [], {},
                    float("nan"), -5):
            assert plausible_mw(bad) is None, repr(bad)

    def test_a_bool_is_not_a_capacity(self):
        """True is 1.0 to float(). A column that degraded to boolean must not
        publish "1 MW"."""
        assert plausible_mw(True) is None
        assert plausible_mw(False) is None

    def test_infinity_is_refused(self):
        assert plausible_mw(float("inf")) is None
        assert plausible_mw(float("-inf")) is None


class TestOneOwner:
    """The cap is compared in ONE place. Four surfaces disagreeing about the
    same number is the defect, not the symptom."""

    def test_display_mw_delegates_rather_than_re_spelling_the_cap(self):
        src = io.open(ROOT / "util" / "facility_headline.py",
                      encoding="utf-8").read()
        tree = ast.parse(src)
        fn = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "display_mw"][0]
        assert "MW_PLAUSIBLE_MAX" not in ast.unparse(fn), (
            "display_mw compares against the cap itself again — a second "
            "spelling is how the cap came to be applied on one surface of four")
        assert "plausible_mw" in ast.unparse(fn)

    def test_the_title_and_the_predicate_cannot_disagree(self):
        """display_mw prints exactly when plausible_mw admits. Checked ACROSS
        the boundary, not just near it."""
        for v in (0, 0.4, 1, 36, 350, 4999.9, MW_PLAUSIBLE_MAX,
                  MW_PLAUSIBLE_MAX + 0.1, 8000, 17000, 63000, None, "x", True):
            assert bool(display_mw(v)) is (plausible_mw(v) is not None), v


# ───────────────────────── class 1: the surfaces ────────────────────────────

class TestMeasuredSurface:
    """facility_measures feeds BOTH the inline Dataset JSON-LD and the .json
    twin — the two that wrap a CC-BY 'you may cite this' envelope."""

    def test_a_fleet_sized_number_is_not_published_as_a_measurement(self):
        names = [v["name"] for v in facility_measures(FLEET)]
        assert "Power Capacity" not in names
        assert names == ["Lifecycle Status"], (
            "the rest of the record must survive — suppression costs the "
            "value, not the page")

    def test_the_number_is_absent_from_the_whole_serialised_twin(self):
        """Not just from variableMeasured. A number that reappears in a
        description string is still a published claim."""
        ent = facility_entity(FLEET, canonical_url="https://dchub.cloud/x",
                              display_name="Utility None")
        assert "63000" not in repr(ent)
        assert "63,000" not in repr(ent)

    def test_a_real_capacity_is_still_typed_and_cited(self):
        m = {v["name"]: v for v in facility_measures(SITE)}
        assert m["Power Capacity"]["value"] == 350.0
        assert m["Power Capacity"]["unitText"] == "MW"
        assert "does NOT reproduce" in m["Power Capacity"]["measurementTechnique"]


class TestBodyTile:
    """The stat card a human reads. It asked `_has`, which answers 'is there a
    value' — the wrong question for a number presented as a fact."""

    def test_the_tile_asks_the_predicate_not_mere_presence(self):
        fn = _fn("_render_profile")
        src = ast.unparse(fn)
        assert '_plausible_mw(power) is not None' in src, (
            "the Power tile must gate on plausibility; `_has(power)` passed "
            "63000.0 straight through to a reader")
        assert '_has(power)' not in src

    def test_the_narrative_asks_it_too(self):
        """Prose is the worst surface for an uncitable number: a sentence reads
        as an assertion, not a field dump."""
        src = ast.unparse(_fn("_narrative"))
        assert "_plausible_mw(power) is not None" in src
        assert "str(power) not in ('0', '0.0')" not in src

    def test_the_lane_2_context_block_asks_it_too(self):
        """util/thin_content renders LANE 2 — the facts added to make a thin
        page rankable. It had its own `_has` gate."""
        src = io.open(ROOT / "util" / "thin_content.py", encoding="utf-8").read()
        fn = [n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "context_block"][0]
        body = ast.unparse(fn)
        assert "_plausible_mw(fac.get('power_mw')) is not None" in body
        assert "_has(fac.get('power_mw'))" not in body

    def test_the_number_appears_ZERO_times_in_the_whole_page(self):
        """★★★ THE FLOOR, AND THE REASON IT COUNTS RATHER THAN SPOT-CHECKS.

        The first draft of this fix gated the stat tile, the inline Dataset
        JSON-LD and the twin — and this assertion still failed, because the
        live page printed 63000.0 FOUR times, not once:

            inline Dataset JSON-LD  "value": 63000.0
            narrative paragraph     "It carries a reported power capacity of…"
            body stat tile          "Power   63000.0 MW"
            LANE-2 context block    "Reported capacity   63000.0 MW"

        Counting the whole rendered page is what found the two that a
        surface-by-surface list had missed. Keep it counting."""
        import routes.facility_profile_page as fpp
        html = fpp._render_profile(dict(FLEET), FLEET["canonical_slug"])
        assert "63000" not in html and "63,000" not in html
        assert 'stat-label">Power<' not in html
        assert "reported power capacity" not in html
        assert "Reported capacity" not in html

    def test_the_page_still_prints_a_real_capacity_on_every_surface(self):
        """The other half of the floor. A suppression that suppressed
        everything would pass every assertion above."""
        import routes.facility_profile_page as fpp
        html = fpp._render_profile(dict(SITE), SITE["canonical_slug"])
        assert 'stat-label">Power<' in html
        assert "reported power capacity of 350.0 MW" in html
        # ★ NOT DECORATION — the anchor that stops the FLEET assertions above
        #   from being vacuous. "Reported capacity" not in html is satisfied
        #   just as well by a LANE-2 block that never rendered at all, which is
        #   the shape of a substring assertion that goes green on a deletion.
        assert "Reported capacity" in html, (
            "the LANE-2 context block must actually render for a plausible "
            "capacity, or its suppression test proves nothing")
        assert html.count("350") >= 3, (
            "tile + prose + a JSON-LD measurement, at least")

    def test_a_suppressed_capacity_costs_only_the_capacity(self):
        """Fail soft, at the granularity of the value — the page, its status,
        its country and its prose paragraph all survive."""
        import routes.facility_profile_page as fpp
        html = fpp._render_profile(dict(FLEET), FLEET["canonical_slug"])
        assert 'stat-label">Status<' in html
        assert 'stat-label">Country<' in html
        assert "is currently planned" in html, (
            "the narrative must fall through to its status-only clause, not "
            "lose the sentence")


class TestComparablesSurface:
    """A peer's capacity is the same number on somebody else's page — and the
    comparables query is ORDER BY power DESC, so one fleet-sized row is printed
    FIRST on every co-located page in its market."""

    def test_the_peer_annotation_asks_the_same_predicate(self):
        src = ast.unparse(_fn("_comparables_html"))
        assert "_plausible_mw(rpow) is not None" in src
        assert 'str(rpow) not in (\'0\', \'0.0\')' not in src

    def test_a_real_peer_capacity_is_still_annotated(self):
        src = ast.unparse(_fn("_comparables_html"))
        assert "MW" in src, "the annotation itself must survive the gate"


# ───────────────────────── class 2: Null Island in the twin ─────────────────

class _Resp:
    """jsonify's stand-in: keeps the dict reachable for assertions."""

    def __init__(self, payload):
        self.payload = payload
        self.headers = {}


def _twin_route(fetch_returns, provenance=None):
    """Compile facility_entity_json out of the AST and drive it.

    The route is a Flask view; importing the module to call it drags a request
    context in. Compiling the single FunctionDef is the pattern
    tests/test_facility_fiber_connectivity.py established for exactly this.
    `provenance` replaces sys.modules["routes.provenance"] for the call, so the
    normaliser-unavailable branch is reachable without breaking the real one.
    """
    node = _fn("facility_entity_json")
    node = ast.FunctionDef(
        name=node.name, args=node.args, body=node.body, decorator_list=[],
        returns=node.returns, type_comment=None, type_params=[],
        lineno=node.lineno, col_offset=0)
    ns = {
        "_fetch_facility_by_slug": lambda s: fetch_returns,
        "jsonify": _Resp,
        "facility_entity": facility_entity,
        "logger": types.SimpleNamespace(warning=lambda *a, **k: None),
    }
    saved = sys.modules.get("routes.provenance")
    had = "routes.provenance" in sys.modules
    if provenance is not None:
        sys.modules["routes.provenance"] = provenance
    try:
        exec(compile(ast.Module(body=[node], type_ignores=[]),  # noqa: S102
                     str(PAGE), "exec"), ns)
        return ns["facility_entity_json"]("some-slug")
    finally:
        if had:
            sys.modules["routes.provenance"] = saved
        else:
            sys.modules.pop("routes.provenance", None)


NULL_ISLAND = {"name": "Washington", "provider": "Equinix", "city": "Washington",
               "latitude": 0.0, "longitude": 0.0, "status": "Operational",
               "canonical_slug": "equinix-washington-00000000"}
REAL_GEO = dict(NULL_ISLAND, latitude=39.04, longitude=-77.48,
                canonical_slug="equinix-ashburn-11111111")


class TestTwinCoordinates:
    def test_zero_zero_does_not_reach_the_twin(self):
        """The measured leak: 10 of 781 indexable pages published a named
        building at 0,0 under a CC-BY licence."""
        resp, status = _twin_route(dict(NULL_ISLAND))
        assert status == 200
        assert "geo" not in resp.payload["spatialCoverage"]
        assert "0.0" not in repr(resp.payload["spatialCoverage"])

    def test_the_rest_of_the_record_survives_the_suppression(self):
        """Suppression costs the coordinates, never the record. The address and
        the name are the page's remaining citable facts."""
        resp, _ = _twin_route(dict(NULL_ISLAND))
        place = resp.payload["spatialCoverage"]
        assert place["name"] == "Washington"
        assert place["address"]["addressLocality"] == "Washington"
        assert resp.payload["provider"]["name"] == "Equinix"

    def test_real_coordinates_still_reach_the_twin(self):
        """The floor. Nulling every pair would satisfy the test above."""
        resp, _ = _twin_route(dict(REAL_GEO))
        geo = resp.payload["spatialCoverage"]["geo"]
        assert geo["latitude"] == 39.04 and geo["longitude"] == -77.48

    def test_a_missing_normaliser_drops_the_geo_not_the_response(self):
        """Fails by dropping the ELEMENT — the choice _fiber_carrier_names
        already made. Publishing an UNCHECKED pair would be the regression."""
        empty = types.ModuleType("routes.provenance")   # no normalize_coordinates
        resp, status = _twin_route(dict(NULL_ISLAND), provenance=empty)
        assert status == 200
        assert "geo" not in resp.payload["spatialCoverage"]
        resp, status = _twin_route(dict(REAL_GEO), provenance=empty)
        assert status == 200
        assert "geo" not in resp.payload["spatialCoverage"], (
            "with no normaliser available the route must not publish a "
            "coordinate pair it could not check")

    def test_the_route_owns_no_second_copy_of_the_zero_rule(self):
        """Two spellings of 'is this Null Island' is the defect this fix
        removes, not one it may add."""
        src = ast.unparse(_fn("facility_entity_json"))
        assert "normalize_coordinates" in src
        for tell in ("0.0", "abs(", "1e-9", "COORDS_UNKNOWN"):
            assert tell not in src, f"{tell!r} re-implements the sentinel rule"

    def test_the_pure_entity_builder_stays_pure(self):
        """normalize_coordinates lives in the ROUTE layer. util.facility_entity
        declares 'no Flask, no DB, no network' and callers rely on importing it
        without pulling routes/ in."""
        src = io.open(ROOT / "util" / "facility_entity.py",
                      encoding="utf-8").read()
        assert "routes" not in src.split('"""')[2], src.split('"""')[2][:200]


class TestNormaliserContract:
    """What this fix leans on the normaliser to do. If these move, the twin
    fix moves with them and should fail here rather than in production."""

    def test_it_nulls_the_pair_and_says_why(self):
        prov = importlib.import_module("routes.provenance")
        row = {"latitude": 0.0, "longitude": 0.0}
        prov.normalize_coordinates(row)
        assert row["latitude"] is None and row["longitude"] is None
        assert row["coordinates_status"] == prov.COORDS_UNKNOWN

    def test_it_leaves_a_real_pair_alone(self):
        prov = importlib.import_module("routes.provenance")
        row = {"latitude": 39.04, "longitude": -77.48}
        prov.normalize_coordinates(row)
        assert row["latitude"] == 39.04 and row["longitude"] == -77.48


# ───────────────────────── the shape of the fix ─────────────────────────────

class TestFailSoft:
    def test_no_hostile_capacity_value_raises(self):
        """A page is never worth losing over a bad column. Cheap surfaces only
        — _render_profile is exercised twice below, not ten times."""
        for bad in (float("nan"), float("inf"), float("-inf"), "", "n/a",
                    None, True, [], {"a": 1}, -1, 10 ** 12, "63,000"):
            p = plausible_mw(bad)
            assert p is None or isinstance(p, float), repr(bad)
            assert isinstance(facility_measures(dict(SITE, power_mw=bad)), list)

    def test_the_page_renders_for_the_two_rows_that_matter(self):
        import routes.facility_profile_page as fpp
        for row in (dict(SITE, power_mw=float("nan")),
                    dict(SITE, power_mw=10 ** 12)):
            assert fpp._render_profile(row, SITE["canonical_slug"])

    def test_a_nan_pair_costs_the_response_nothing(self):
        """★ WHAT THIS DOES *NOT* CLAIM. normalize_coordinates compares
        `abs(lat) < eps`, which NaN fails, so a NaN pair is passed through as a
        KNOWN coordinate and Flask serialises it as the literal `NaN` — invalid
        JSON. That is NOT fixed here, and deliberately: measured over 4,440
        indexable pages whose twin carried geo, NaN appeared 0 times and
        out-of-range pairs 0 times. Shipping a renderer for a class with no
        members is the thing this work was asked not to do. The test that is
        owed is that the route survives one."""
        resp, status = _twin_route(dict(NULL_ISLAND, latitude=float("nan"),
                                        longitude=float("nan")))
        assert status == 200
        assert resp.payload["spatialCoverage"]["name"] == "Washington"


@pytest.mark.parametrize("mod,name", [
    ("util.facility_headline", "plausible_mw"),
    ("util.facility_entity", "facility_measures"),
])
def test_the_predicate_is_importable_where_the_callers_look(mod, name):
    """Three NameError-at-request-time bugs were caught on this route before."""
    assert callable(getattr(importlib.import_module(mod), name))
