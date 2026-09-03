"""Guards for r-facility-entity (2026-09-03) — the facility twin + typed measures.

Markets got typed, based, citable measurements across 249 pages. Facilities are
20,300+ — the bulk of the corpus, where most inbound crawl traffic lands — and
measured live 2026-09-03 they had:

    JSON-LD @types  : Dataset, Place, GeoCoordinates, PostalAddress, ...
    license CC-BY   : yes
    variableMeasured: NO      <- a CC-BY envelope around prose numbers
    /facilities/<slug>.json   : 404

★ The reconciliation note is the point. A facility's power_mw is ONE record.
Summing facility MW across a market does not reproduce the market total, and
saying so is what stops an agent deriving a third number from our own data.
"""
import ast
import builtins
import io
import pathlib

from werkzeug.routing import Map, Rule

from util.facility_entity import (
    CITE_AS, LICENSE_URL, NOT_AN_AGGREGATE, facility_entity, facility_measures,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = io.open(REPO / "routes" / "facility_profile_page.py", encoding="utf-8").read()

FAC = {"power_mw": 36.0, "status": "Operational", "city": "Ashburn",
       "state": "VA", "country": "US", "latitude": 39.04, "longitude": -77.48,
       "provider": "Lumen", "canonical_slug": "lumen-ashburn"}


class TestRouting:
    def test_json_outranks_the_greedy_path_converter(self):
        """<path:slug> is greedier than markets' <slug>; precedence still has to
        favour the suffix or the twin silently serves the HTML page."""
        m = Map([Rule("/facilities/<path:slug>", endpoint="html"),
                 Rule("/facilities/<path:slug>.json", endpoint="json")])
        a = m.bind("dchub.cloud")
        assert a.match("/facilities/lumen-ashburn-4a3f.json") == (
            "json", {"slug": "lumen-ashburn-4a3f"})
        assert a.match("/facilities/lumen-ashburn-4a3f") == (
            "html", {"slug": "lumen-ashburn-4a3f"})

    def test_the_route_is_registered(self):
        assert '@facility_profile_bp.route("/facilities/<path:slug>.json"' in SRC

    def test_every_name_the_route_uses_RESOLVES(self):
        """Three separate NameError-at-request-time bugs were caught this way
        during this work (SITE, json, facility_entity). Pin it."""
        tree = ast.parse(SRC)
        top = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for x in n.names:
                    top.add((x.asname or x.name).split(".")[0])
            if isinstance(n, ast.FunctionDef) and getattr(n, "col_offset", 1) == 0:
                top.add(n.name)
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        top.add(t.id)
        fn = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "facility_entity_json"][0]
        local = {a.arg for a in fn.args.args} | {
            n.id for n in ast.walk(fn)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        used = {n.id for n in ast.walk(fn)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        assert not (used - local - top - set(dir(builtins)))


class TestMeasures:
    def test_power_is_typed_with_a_unit(self):
        m = {v["name"]: v for v in facility_measures(FAC)}
        assert m["Power Capacity"]["value"] == 36.0
        assert m["Power Capacity"]["unitText"] == "MW"

    def test_every_power_measure_carries_the_NOT_AN_AGGREGATE_note(self):
        m = {v["name"]: v for v in facility_measures(FAC)}
        tech = m["Power Capacity"]["measurementTechnique"]
        assert tech == NOT_AN_AGGREGATE
        assert "does NOT reproduce" in tech      # the actual warning
        assert "market" in tech.lower()

    def test_absent_power_is_OMITTED_never_zero_filled(self):
        # power_mw absent is a DISCLOSURE gap, not a shut-down site
        assert [v["name"] for v in facility_measures({"status": "Operational"})] \
            == ["Lifecycle Status"]

    def test_zero_and_junk_power_do_not_become_a_measurement(self):
        for bad in (0, 0.0, "0", None, "", "n/a", [], {}):
            names = [v["name"] for v in facility_measures({"power_mw": bad})]
            assert "Power Capacity" not in names, f"{bad!r} became a measurement"

    def test_a_totally_empty_record_yields_no_measures_and_does_not_raise(self):
        assert facility_measures({}) == []
        assert facility_measures(None) == []


class TestEntity:
    def test_it_is_a_citable_dataset(self):
        e = facility_entity(FAC, canonical_url="https://dchub.cloud/facilities/x",
                            display_name="Lumen Ashburn")
        assert e["@type"] == "Dataset"
        assert e["license"] == LICENSE_URL
        assert e["citation"] == CITE_AS
        assert e["url"] == "https://dchub.cloud/facilities/x"

    def test_geo_and_address_ride_along_when_present(self):
        e = facility_entity(FAC, canonical_url="u", display_name="d")
        sc = e["spatialCoverage"]
        assert sc["geo"]["latitude"] == 39.04
        assert sc["address"]["addressLocality"] == "Ashburn"

    def test_junk_coordinates_are_dropped_not_crashed(self):
        e = facility_entity({**FAC, "latitude": "x", "longitude": None},
                            canonical_url="u", display_name="d")
        assert "geo" not in e["spatialCoverage"]

    def test_no_as_of_means_no_fabricated_date(self):
        e = facility_entity(FAC, canonical_url="u", display_name="d")
        assert "dateModified" not in e


class TestPageAndTwinShareOneBuilder:
    def test_the_page_dataset_node_now_has_variableMeasured(self):
        assert '"variableMeasured": facility_measures(fac),' in SRC

    def test_the_page_node_is_citable(self):
        assert '"citation": "DC Hub, dchub.cloud",' in SRC

    def test_the_twin_uses_the_same_module(self):
        assert "from util.facility_entity import facility_entity, facility_measures" in SRC
