"""Guards for r-crawl-citable (2026-09-03) — the crawled market page must carry
a citable measurement, not just an Article.

market_short_html() has a Place + variableMeasured + Dataset block with a CC-BY
license and NEVER REACHES IT for any market that has a deep-dive: the handler
returns _render_deep_dive_body() ~200 lines earlier. Verified live 2026-09-03 on
/markets/ashburn (the URL rank_markets points at, after a 301):

    JSON-LD @types  : Article, BreadcrumbList, ListItem, Organization, Place
    variableMeasured: False    Dataset: False    license: False

Article markup says "someone's write-up". Dataset + variableMeasured says "a
citable measurement" — the difference between a page being read and a number
being quoted.

Also guards capacity_basis, the MW vocabulary util/facility_count_basis.py was
missing. Ashburn reads 5,793 / 11,052 / 12,438 MW across three surfaces; all are
correct and they differ by POPULATION and AGGREGATION, which nothing published.
"""
import io
import json
import pathlib
import re

import pytest

from routes.market_deep_dive import _market_dataset_ld
from util.facility_count_basis import AGGREGATIONS, capacity_basis

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = io.open(REPO / "routes" / "market_deep_dive.py", encoding="utf-8").read()

STATS = {"total_mw": 11052.0, "facility_count": 328, "dcpi_score": 71}


class TestCapacityBasis:
    def test_declares_all_three_axes(self):
        b = capacity_basis("operational", "sum_rows", "city_state")
        for k in ("population", "aggregation", "grouping", "fleet_filter",
                  "aggregation_means", "compare_note"):
            assert k in b

    def test_an_unknown_term_RAISES_rather_than_shipping_a_plausible_basis(self):
        with pytest.raises(ValueError, match="aggregation"):
            capacity_basis("operational", "sum_of_vibes", "city_state")
        with pytest.raises(ValueError, match="population"):
            capacity_basis("made_up", "sum_rows", "city_state")

    def test_the_three_live_readings_are_all_expressible(self):
        # 5,793 rank_markets · 11,052 page · 12,438 /api/v1/markets
        assert capacity_basis("operational", "sum_rows", "city_state")
        assert capacity_basis("tracked", "sum_sites", "market_slug")
        assert "sum_rows" in AGGREGATIONS and "sum_sites" in AGGREGATIONS

    def test_compare_note_warns_that_bigger_is_usually_a_wider_population(self):
        note = capacity_basis("tracked", "sum_sites", "market_slug")["compare_note"]
        assert "population" in note.lower()


class TestDatasetJsonLd:
    def test_it_is_a_Dataset_not_an_Article(self):
        d = json.loads(_market_dataset_ld("ashburn", "Northern Virginia", STATS, "2026-09-03"))
        assert d["@type"] == "Dataset"
        assert d["@context"] == "https://schema.org"

    def test_the_numbers_are_TYPED_properties_not_prose(self):
        d = json.loads(_market_dataset_ld("ashburn", "Northern Virginia", STATS, "2026-09-03"))
        by = {v["name"]: v for v in d["variableMeasured"]}
        assert by["Total Capacity"]["value"] == 11052.0
        assert by["Total Capacity"]["unitText"] == "MW"
        assert by["Facilities"]["value"] == 328
        assert isinstance(by["Facilities"]["value"], int)

    def test_every_measure_carries_its_BASIS(self):
        d = json.loads(_market_dataset_ld("ashburn", "Northern Virginia", STATS, "2026-09-03"))
        # EVERY measure must say how it was measured — no bare numbers.
        for v in d["variableMeasured"]:
            assert v.get("measurementTechnique"), f"{v['name']} has no measurementTechnique"
        # r-one-builder (2026-09-03): `population=` is the vocabulary of
        # util/facility_count_basis, which describes COUNTS and CAPACITIES. A
        # DCPI score is neither, so demanding a population of it was wrong — it
        # states its method instead. Assert the population axis only where the
        # vocabulary actually applies, so this cannot pass vacuously.
        by = {v["name"]: v for v in d["variableMeasured"]}
        for nm in ("Total Capacity", "Facilities"):
            assert "population=" in by[nm].get("description", ""), \
                f"{nm} does not state its population"
        assert "0-100" in by["DCPI Score"]["measurementTechnique"]

    def test_it_is_attributable(self):
        d = json.loads(_market_dataset_ld("ashburn", "Northern Virginia", STATS, "2026-09-03"))
        assert "creativecommons.org/licenses/by/4.0" in d["license"]
        assert d["citation"] == "DC Hub, dchub.cloud"
        assert d["creator"]["name"] == "DC Hub"
        # r-one-builder: the URL now resolves to the page that SERVES. 'ashburn'
        # 301s to 'northern-virginia', so emitting the raw slug shipped a
        # redirect in our own structured data.
        assert d["url"] == "https://dchub.cloud/markets/northern-virginia"

    def test_a_missing_measure_is_OMITTED_never_zero_filled(self):
        d = json.loads(_market_dataset_ld("x", "X", {"facility_count": 5}, "2026-09-03"))
        names = [v["name"] for v in d["variableMeasured"]]
        assert names == ["Facilities"]        # no fabricated 0 MW

    def test_it_never_raises_and_never_silently_ships_empty(self):
        # fail-soft must be reachable ONLY on real failure — a normal call with
        # real stats must never return the {} fallback.
        assert _market_dataset_ld("x", "X", STATS, None) != "{}"
        assert json.loads(_market_dataset_ld(None, None, {}, None)) is not None


class TestItActuallyShips:
    def test_the_block_is_emitted_by_the_body_that_is_RETURNED(self):
        # the defect was a correct emitter behind an early return
        assert '<script type="application/ld+json">{_market_dataset_ld(' in SRC

    def test_json_is_imported_so_the_helper_cannot_fail_soft_forever(self):
        # it was NOT imported; json.dumps would have raised into the except and
        # shipped "{}" on every page, silently, forever.
        assert re.search(r"^import json$", SRC, re.M)
