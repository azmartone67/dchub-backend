#!/usr/bin/env python3
"""tests/test_substation_hifld_link_pass.py — the link pass must skip ambiguity,
never guess it.

NO NETWORK, NO DB. `plan_links` is pure; every case here is a hand-built fixture.

CONTEXT. The March 2026 substation load stored the ArcGIS OBJECTID (an export
row number) instead of the upstream HIFLD `ID`, so `hifld_id` is NULL on 52,625
of 127,269 held rows and the nightly ingest refuses to write — it would insert
~75,000 duplicates. Step 2 of
migrations/2026-08-12_substation_hifld_id_identity.sql closes that by matching
held rows to upstream on coordinate rounded to 4dp, NAME EXCLUDED.

★ THE PROPERTY THAT MATTERS IS WHAT IT REFUSES TO DO. A link pass that
tiebreaks co-located assets writes a wrong identity into a unique index, and the
damage is silent and permanent. Every skip rule is fenced here, in both
directions — ambiguous upstream AND ambiguous held — because they are separate
branches and a fix to one does not imply the other.

The migration's own measured expectation, for scale: 110 ambiguous upstream keys
and 125 ambiguous held keys, with ~8,552 held keys upstream no longer lists.

Run standalone:   python3 tests/test_substation_hifld_link_pass.py
Run under pytest: pytest tests/test_substation_hifld_link_pass.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools.substation_hifld_link import coord_key, plan_links  # noqa: E402


def test_clean_one_to_one_match_links():
    plan = plan_links(
        upstream=[("107655", 45.7684, -91.8647), ("107656", 40.1000, -80.2000)],
        held=[(2599, 45.7684, -91.8647), (2600, 40.1000, -80.2000)],
    )
    assert sorted(plan["links"]) == [(2599, "107655"), (2600, "107656")]


def test_two_upstream_assets_at_one_point_are_skipped():
    """Co-located upstream assets need a human, not a tiebreak."""
    plan = plan_links(
        upstream=[("1", 45.0, -91.0), ("2", 45.0, -91.0)],
        held=[(500, 45.0, -91.0)],
    )
    assert plan["links"] == [], "guessed between two co-located upstream assets"
    assert plan["ambiguous_upstream_keys"] == 1


def test_two_held_rows_at_one_point_are_skipped():
    """The other direction — a separate branch, so fenced separately."""
    plan = plan_links(
        upstream=[("1", 45.0, -91.0)],
        held=[(500, 45.0, -91.0), (501, 45.0, -91.0)],
    )
    assert plan["links"] == [], "wrote one upstream id against two held rows"
    assert plan["ambiguous_held_keys"] == 1


def test_one_upstream_id_is_never_written_to_two_rows():
    """Distinct coordinates, same upstream ID — the partial unique index would
    reject the second write. Catch it in the plan, name the cause."""
    plan = plan_links(
        upstream=[("777", 45.0, -91.0), ("777", 46.0, -92.0)],
        held=[(1, 45.0, -91.0), (2, 46.0, -92.0)],
    )
    assert plan["links"] == []
    assert plan["collisions"] == 1


def test_held_row_absent_upstream_is_unmatched_not_linked():
    plan = plan_links(upstream=[("1", 45.0, -91.0)], held=[(9, 12.3456, -70.1234)])
    assert plan["links"] == []
    assert plan["unmatched_held"] == 1


def test_rounding_is_four_decimals_and_tolerates_sub_11m_drift():
    """Upstream ships double precision; the held load rounded. Within 4dp they
    must still meet, or the pass links almost nothing."""
    plan = plan_links(
        upstream=[("42", 45.76840001, -91.86469999)],
        held=[(7, 45.7684, -91.8647)],
    )
    assert plan["links"] == [(7, "42")], "4dp match failed on sub-millimetre drift"


def test_distinct_points_beyond_the_tolerance_do_not_match():
    plan = plan_links(upstream=[("42", 45.7684, -91.8647)],
                      held=[(7, 45.7700, -91.8647)])
    assert plan["links"] == []


def test_null_island_and_missing_coordinates_never_form_a_key():
    """0,0 is the classic placeholder. If it keyed, every unlocated row on both
    sides would collapse onto one point and link to each other."""
    assert coord_key(0, 0) is None
    assert coord_key(None, -91.0) is None
    assert coord_key(45.0, None) is None
    assert coord_key("not-a-number", 1.0) is None
    plan = plan_links(upstream=[("1", 0, 0), ("2", 0, 0)], held=[(5, 0, 0), (6, 0, 0)])
    assert plan["links"] == []
    assert plan["ambiguous_held_keys"] == 0, "0,0 formed a key"


def test_upstream_row_without_an_id_is_ignored():
    plan = plan_links(upstream=[(None, 45.0, -91.0), ("", 46.0, -92.0)],
                      held=[(1, 45.0, -91.0), (2, 46.0, -92.0)])
    assert plan["links"] == []


def test_mixed_batch_links_only_the_unambiguous_rows():
    """The realistic shape: some clean, some ambiguous each way, some absent."""
    plan = plan_links(
        upstream=[("100", 1.0, 1.0),                      # clean
                  ("200", 2.0, 2.0), ("201", 2.0, 2.0),   # ambiguous upstream
                  ("300", 3.0, 3.0)],                     # held is ambiguous here
        held=[(10, 1.0, 1.0),
              (20, 2.0, 2.0),
              (30, 3.0, 3.0), (31, 3.0, 3.0),
              (40, 9.0, 9.0)],                            # absent upstream
    )
    assert plan["links"] == [(10, "100")]
    assert plan["ambiguous_upstream_keys"] == 1
    assert plan["ambiguous_held_keys"] == 1
    assert plan["unmatched_held"] == 1


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("ok", _name)
