"""Guards for r-one-builder (2026-09-03) — ONE market Dataset builder, and
every measure states its basis.

#3757 claimed "same builder as the page's embedded block, so the two cannot
drift". That was not true. Main carried TWO market Dataset builders —
routes.market_deep_dive._market_dataset_ld and util.market_entity.market_entity
— and they had already drifted:

    measure          page builder    .json twin
    Total Capacity   basis           basis
    Facilities       basis           BARE INTEGER
    DCPI Score       absent          present, no method

Two structured-data answers for one market is the defect the whole effort
exists to remove, reproduced inside it. Verified live on the deployed twin:
Facilities = 469 with no basis, against 768 (/api/v1/markets 'Northern
Virginia') and 328 ('Ashburn').
"""
import json

from routes.market_deep_dive import _market_dataset_ld
from util.market_entity import market_entity

STATS = {"total_mw": 11052.0, "facility_count": 469, "dcpi_score": 11.7}


class TestOneBuilder:
    def test_the_page_block_and_the_twin_are_BYTE_IDENTICAL(self):
        page = json.loads(_market_dataset_ld("ashburn", "Northern Virginia",
                                             STATS, "2026-09-03"))
        twin = market_entity("ashburn", "Northern Virginia", STATS,
                             canonical_slug="ashburn",
                             as_of="2026-09-03")
        assert page == twin

    def test_the_page_builder_DELEGATES_rather_than_rebuilding(self):
        import io, pathlib
        src = io.open(pathlib.Path(__file__).resolve().parent.parent
                      / "routes" / "market_deep_dive.py", encoding="utf-8").read()
        body = src.split("def _market_dataset_ld")[1].split("\ndef ")[0]
        assert "market_entity(" in body, "page builder must delegate"
        # the tells of a second implementation
        assert "PropertyValue" not in body
        assert "variableMeasured" not in body
        assert "creativecommons" not in body

    def test_the_page_url_avoids_the_redirect(self):
        # r-market-canon-split (2026-09-05): 'ashburn' IS the page now — the
        # /markets surface used to 301 it to 'northern-virginia', which is the
        # direction /dcpi and every sitemap disagreed with.
        page = json.loads(_market_dataset_ld("ashburn", "Northern Virginia",
                                             STATS, "2026-09-03"))
        assert page["url"] == "https://dchub.cloud/markets/ashburn"


class TestEveryMeasureStatesItsBasis:
    def test_all_three_carry_a_measurementTechnique(self):
        e = market_entity("ashburn", "Northern Virginia", STATS)
        for v in e["variableMeasured"]:
            assert v.get("measurementTechnique"), \
                f"{v['name']} ships as a bare number"

    def test_the_COUNT_basis_is_the_one_that_was_missing(self):
        e = market_entity("ashburn", "Northern Virginia", STATS)
        f = [v for v in e["variableMeasured"] if v["name"] == "Facilities"][0]
        assert "population=tracked" in f["description"]
        assert "unit=distinct_site" in f["description"]
        assert "grouping=market_slug" in f["description"]

    def test_the_DCPI_method_says_what_it_is_NOT_comparable_to(self):
        e = market_entity("ashburn", "Northern Virginia", STATS)
        d = [v for v in e["variableMeasured"] if v["name"] == "DCPI Score"][0]
        t = d["measurementTechnique"]
        assert "not a capacity" in t.lower()
        assert "0-100" in t

    def test_a_missing_measure_is_still_OMITTED(self):
        e = market_entity("x", "X", {"facility_count": 5})
        assert [v["name"] for v in e["variableMeasured"]] == ["Facilities"]


class TestStillFailSoft:
    def test_the_entity_survives_when_the_basis_module_is_absent(self):
        import sys, types
        real = sys.modules.pop("util.facility_count_basis", None)
        sys.modules["util.facility_count_basis"] = types.ModuleType(
            "util.facility_count_basis")          # neither basis nor capacity_basis
        try:
            e = market_entity("ashburn", "Northern Virginia", STATS)
            by = {v["name"]: v for v in e["variableMeasured"]}
            assert by["Facilities"]["value"] == 469      # number intact
            assert by["Total Capacity"]["value"] == 11052.0
            assert e["citation"] == "DC Hub, dchub.cloud"
        finally:
            sys.modules.pop("util.facility_count_basis", None)
            if real is not None:
                sys.modules["util.facility_count_basis"] = real

    def test_the_page_builder_returns_empty_json_rather_than_raising(self):
        assert _market_dataset_ld(None, None, None, None) in ("{}", ) or \
            json.loads(_market_dataset_ld(None, None, None, None)) is not None
