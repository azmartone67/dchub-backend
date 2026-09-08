"""Per-row citation block on /dcpi (r-citable-rows, 2026-09-08).

r-citable-top10 freed the top-10 numbers so a model would have something to
quote. It worked, and Perplexity quoted them WRONG: "Midland-Odessa at 81.0"
against a live page rendering 85.7, citing method_version 2.0.1 against a live
2.3.0 — while stating it had just re-fetched. Nothing on the page let it know:
/dcpi carried one Dataset ld+json block and ZERO per-row structure, so every
score was HTML text with no as_of and no method version.

These tests pin the two properties that make the block safe rather than merely
present: the timestamp comes from the ROW, and signal_tier never travels
without its caveat.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.dcpi import (  # noqa: E402
    _citation_itemlist, _row_as_of, _ALWAYS_MODELED_INPUTS,
    _NEVER_POPULATED_INPUTS,
)

_TS = datetime.datetime(2026, 9, 8, 4, 36, 34, tzinfo=datetime.timezone.utc)


def _row(slug="midland-tx", name="Midland-Odessa", score=85.7, **kw):
    base = {"market_slug": slug, "market_name": name, "iso": "ERCOT",
            "excess_power_score": score, "constraint_score": 22.8,
            "composite_score": 83.0, "time_to_power_months": 10,
            "verdict": "BUILD", "computed_at": _TS,
            "method_version": "2.3.0", "signal_tier": "full"}
    base.update(kw)
    return base


def _props(item):
    return {p["name"]: p for p in item["item"]["additionalProperty"]}


def test_as_of_is_the_rows_own_timestamp_never_a_render_clock():
    """★ THE LOAD-BEARING ONE. A render-time timestamp beside a cached score
    certifies a stale number as fresh — strictly worse than no timestamp."""
    out = _citation_itemlist([_row()], 10)
    assert out["itemListElement"][0]["item"]["observationDate"] == _TS.isoformat()
    # and it must MOVE when the row's timestamp moves
    older = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    out2 = _citation_itemlist([_row(computed_at=older)], 10)
    assert out2["itemListElement"][0]["item"]["observationDate"] == older.isoformat()


def test_signal_tier_never_travels_without_its_caveat():
    """'full' means every LIVE-CAPABLE adapter returned — not that every input is
    measured. Bare, it reads as a measurement guarantee."""
    item = _citation_itemlist([_row()], 10)["itemListElement"][0]
    tier = _props(item)["signal_tier"]
    assert tier["value"] == "full"
    desc = tier["description"]
    assert "does NOT mean every score input is measured" in desc
    for inp in _ALWAYS_MODELED_INPUTS:
        assert inp in desc, f"{inp} missing from the caveat"
    for inp in _NEVER_POPULATED_INPUTS:
        assert inp in desc


def test_only_rows_whose_numbers_were_published_are_cited():
    """The block is built AFTER masking. A masked row has score None and must
    not appear — citing a number the page did not render is the worst case."""
    rows = [_row(slug="a", name="A"), _row(slug="b", name="B"),
            _row(slug="c", name="C", score=None)]
    out = _citation_itemlist(rows, 10)
    assert out["numberOfItems"] == 2
    assert [i["item"]["observationAbout"]["identifier"]
            for i in out["itemListElement"]] == ["a", "b"]


def test_the_free_window_is_respected():
    rows = [_row(slug=f"m{i}", name=f"M{i}") for i in range(25)]
    assert _citation_itemlist(rows, 10)["numberOfItems"] == 10
    assert _citation_itemlist(rows, 3)["numberOfItems"] == 3


def test_nothing_quotable_returns_None_not_an_empty_list():
    """An empty ItemList published on the page reads as 'no markets scored'."""
    assert _citation_itemlist([], 10) is None
    assert _citation_itemlist([_row(score=None)], 10) is None
    assert _citation_itemlist(None, 10) is None


def test_positions_are_contiguous_after_skips():
    rows = [_row(slug="a", name="A"), _row(slug="b", name="B", score=None),
            _row(slug="c", name="C")]
    out = _citation_itemlist(rows, 10)
    assert [i["position"] for i in out["itemListElement"]] == [1, 2]


def test_row_as_of_handles_a_missing_or_string_timestamp():
    assert _row_as_of({}) is None
    assert _row_as_of({"computed_at": None}) is None
    assert _row_as_of({"computed_at": "2026-09-08T04:36:34+00:00"}) == \
        "2026-09-08T04:36:34+00:00"


def test_each_item_carries_a_ready_made_citation_and_method_link():
    item = _citation_itemlist([_row()], 10)["itemListElement"][0]["item"]
    assert item["citation"] == (
        "DC Hub DCPI ranks Midland-Odessa at 85.7 on its excess-power score. "
        "https://dchub.cloud/dcpi/midland-tx")
    assert item["isBasedOn"].endswith("/api/v1/dcpi/methodology")
    assert _props({"item": item})["method_version"]["value"] == "2.3.0"
