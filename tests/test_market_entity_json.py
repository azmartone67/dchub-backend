"""Guards for r-entity-json (2026-09-03) — the machine-readable market twin.

An agent that crawls /markets/<slug> had no way to get the same facts AS DATA:
no .json twin, no content negotiation, and the API path is a shape it cannot
guess from the page URL it holds.

/markets/<slug>.json did not 404 — it 301'd, which is worse because it looks
like it works: market_short_html normalises with slug_norm.replace(".", ""), so
the request landed on /markets/<slug>json and redirected away. Verified live
2026-09-03: `/markets/ashburn.json -> HTTP 301`.
"""
import io
import pathlib
import re
import sys
import types

from werkzeug.routing import Map, Rule

from util.market_entity import CITE_AS, LICENSE_URL, SITE, market_entity

REPO = pathlib.Path(__file__).resolve().parent.parent
DD = io.open(REPO / "routes" / "market_deep_dive.py", encoding="utf-8").read()

STATS = {"total_mw": 11052.0, "facility_count": 328, "dcpi_score": 71}


class TestRouting:
    def test_the_json_suffix_outranks_the_greedy_slug_rule(self):
        """The whole fix rests on this. If precedence ever flips, the twin
        silently becomes a 301 again — the exact defect it replaced."""
        m = Map([Rule("/markets/<slug>", endpoint="html"),
                 Rule("/markets/<slug>.json", endpoint="json")])
        a = m.bind("dchub.cloud")
        assert a.match("/markets/ashburn.json") == ("json", {"slug": "ashburn"})
        assert a.match("/markets/ashburn") == ("html", {"slug": "ashburn"})

    def test_the_route_is_registered_on_the_blueprint(self):
        assert '@market_deep_dive_bp.route("/markets/<slug>.json"' in DD

    def test_it_does_NOT_reach_the_dot_stripping_normaliser(self):
        # slug_norm.replace(".", "") is what turned the twin into a redirect;
        # the new rule must not be routed through market_short_html.
        route = DD.split('@market_deep_dive_bp.route("/markets/<slug>.json"')[1]
        route = route.split("@market_deep_dive_bp.route")[0]
        assert "market_short_html" not in route
        assert 'replace(".", "")' not in route


class TestEntityShape:
    def test_it_is_valid_schema_org_Dataset_json_ld(self):
        e = market_entity("ashburn", "Northern Virginia", STATS)
        assert e["@context"] == "https://schema.org"
        assert e["@type"] == "Dataset"
        assert e["identifier"] == "ashburn"

    def test_the_url_points_at_the_page_that_SERVES_not_a_redirect(self):
        e = market_entity("ashburn", "Northern Virginia", STATS,
                          canonical_slug="northern-virginia")
        assert e["url"] == f"{SITE}/markets/northern-virginia"

    def test_without_a_canonical_the_url_falls_back_to_the_slug(self):
        e = market_entity("dallas", "Dallas", STATS)
        assert e["url"] == f"{SITE}/markets/dallas"

    def test_it_is_attributable(self):
        e = market_entity("ashburn", "Northern Virginia", STATS, as_of="2026-09-03")
        assert e["license"] == LICENSE_URL
        assert e["citation"] == CITE_AS
        assert e["creator"]["name"] == "DC Hub"
        assert e["dateModified"] == "2026-09-03"

    def test_numbers_are_TYPED(self):
        e = market_entity("ashburn", "Northern Virginia", STATS)
        by = {v["name"]: v for v in e["variableMeasured"]}
        assert by["Total Capacity"]["value"] == 11052.0
        assert by["Total Capacity"]["unitText"] == "MW"
        assert isinstance(by["Facilities"]["value"], int)

    def test_a_measure_we_do_not_HOLD_is_omitted_never_zero_filled(self):
        e = market_entity("x", "X", {"facility_count": 5})
        assert [v["name"] for v in e["variableMeasured"]] == ["Facilities"]
        e2 = market_entity("x", "X", None)
        assert e2["variableMeasured"] == []
        assert "dateModified" not in e2          # no as_of -> no fabricated date


class TestBasisEnrichmentIsIndependentlyFailSoft:
    def _swap(self, mod):
        real = sys.modules.pop("util.facility_count_basis", None)
        if mod is not None:
            sys.modules["util.facility_count_basis"] = mod
        return real

    def _restore(self, real):
        sys.modules.pop("util.facility_count_basis", None)
        if real is not None:
            sys.modules["util.facility_count_basis"] = real

    def test_the_entity_survives_when_capacity_basis_is_ABSENT(self):
        real = self._swap(types.ModuleType("util.facility_count_basis"))
        try:
            e = market_entity("ashburn", "Northern Virginia", STATS)
            by = {v["name"]: v for v in e["variableMeasured"]}
            assert by["Total Capacity"]["value"] == 11052.0     # number intact
            assert "measurementTechnique" not in by["Total Capacity"]
            assert e["citation"] == CITE_AS                      # still citable
        finally:
            self._restore(real)

    def test_it_LIGHTS_UP_when_capacity_basis_is_available(self):
        stub = types.ModuleType("util.facility_count_basis")
        stub.capacity_basis = lambda p, a, g, note=None: {
            "population": p, "aggregation": a, "grouping": g,
            "aggregation_means": "AGG", "compare_note": "NOTE"}
        real = self._swap(stub)
        try:
            e = market_entity("ashburn", "Northern Virginia", STATS)
            mw = e["variableMeasured"][0]
            assert mw["measurementTechnique"] == "AGG"
            assert "population=tracked" in mw["description"]
        finally:
            self._restore(real)


class TestOneBuilderNotTwo:
    def test_the_route_uses_the_shared_builder(self):
        assert "from util.market_entity import" in DD
        assert "market_entity(" in DD.split('def market_entity_json')[1]

    def test_the_response_declares_itself_as_linked_data(self):
        route = DD.split("def market_entity_json")[1].split("@market_deep_dive_bp.route")[0]
        assert 'application/ld+json' in route
        assert 'Access-Control-Allow-Origin' in route      # crawlable cross-origin
        assert 'rel="canonical"' in route                  # points at the HTML twin

    def test_an_unknown_slug_is_a_404_with_a_way_forward(self):
        route = DD.split("def market_entity_json")[1].split("@market_deep_dive_bp.route")[0]
        assert "404" in route and "unknown_market" in route
        assert "rank_markets" in route      # tells the agent how to get a real slug


class TestDiscoverable:
    """r-entity-json #6: a twin nothing points at is a twin nobody finds.

    llms.txt is the file agents fetch to learn how to use us. Before this it
    listed 49 URLs and never mentioned that a market page has a data twin.
    """

    DISCOVERY = io.open(REPO / "ai_discovery_routes.py", encoding="utf-8").read()

    def test_llms_txt_tells_agents_the_json_twin_exists(self):
        assert "/markets/northern-virginia.json" in self.DISCOVERY

    def test_it_says_the_twin_needs_NO_key(self):
        blk = self.DISCOVERY.split("## No key, no connector")[1].split("## Integration")[0]
        assert "no auth" in blk.lower() and "no signup" in blk.lower()

    def test_it_tells_them_to_READ_THE_BASIS_before_comparing(self):
        blk = self.DISCOVERY.split("## No key, no connector")[1].split("## Integration")[0]
        assert "BASIS" in blk
        # the specific misreading that costs us the citation
        assert "wider population" in blk

    def test_it_names_the_attribution_fields(self):
        blk = self.DISCOVERY.split("## No key, no connector")[1].split("## Integration")[0]
        assert "citation" in blk and "license" in blk

    def test_the_documented_url_shape_matches_the_registered_route(self):
        # a doc that promises a URL the router does not serve is worse than none
        assert '@market_deep_dive_bp.route("/markets/<slug>.json"' in DD
        m = re.search(r"https://dchub\.cloud/markets/([a-z0-9-]+)\.json", self.DISCOVERY)
        assert m, "no example .json URL in llms.txt"
