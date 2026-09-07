#!/usr/bin/env python3
"""tests/test_facility_dedup_v4.py — the rendered-identity deduper's decisions.

NO NETWORK, NO DB. plan_group is pure; _collect is driven with a stub cursor.

Each assertion below is a measured lesson from an earlier lane, not a
precaution. The two that matter most:

  ★ POINTER ONLY. `is_duplicate` is a VISIBILITY flag — setting it drops the
    row from every filtered COUNT and from the sitemap. Setting it on 2026-07-28
    left 57 of 58 slugs with NO keeper and was reverted. Consolidation is
    `duplicate_of_id` alone: the row stays live, counted, serving 200, and
    Google merges the two URLs itself. Suppression deletes a page; a canonical
    merges it.

  ★ A MISSED DUPLICATE IS SAFE; A FALSE MERGE HIDES A REAL SITE. 581 of the
    1,205 (name, city) groups v3 examined were Amazon IAD85 / IAD75 / IAD96 at
    Manassas — three distinct buildings under one generic name.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes.facility_dedup_v4 import (      # noqa: E402
    DEDUP_METHOD, MAX_GROUP, plan_group, is_junk_slug, _collect)


def _df(i, slug, provider=None, lat=None, lon=None, pw=None,
        dup=None, merged=None):
    return {"table": "discovered_facilities", "id": i, "canonical_slug": slug,
            "provider": provider, "latitude": lat, "longitude": lon,
            "power_mw": pw, "duplicate_of_id": dup, "merged_facility_id": merged}


def _lg(i, slug, provider=None, lat=None, lon=None, pw=None):
    return {"table": "facilities", "id": i, "canonical_slug": slug,
            "provider": provider, "latitude": lat, "longitude": lon,
            "power_mw": pw, "duplicate_of_id": None, "merged_facility_id": None}


# ── what it writes ───────────────────────────────────────────────────────

def test_two_discovered_rows_get_a_pointer_from_the_alternate_to_the_keeper():
    p = plan_group([_df(1, "a-11111111", pw=50), _df(2, "a-22222222", pw=5)])
    assert p["skip"] is None
    assert p["keeper"]["id"] == 1            # richest wins
    assert p["writes"] == [2]


def test_the_keeper_is_deterministic_when_capacity_ties():
    """The same group must always plan the same way, or two runs disagree about
    which URL Google is being pointed at."""
    rows = [_df(9, "a-99999999"), _df(3, "a-33333333")]
    assert plan_group(rows)["keeper"]["id"] == 3
    assert plan_group(list(reversed(rows)))["keeper"]["id"] == 3


def test_a_drain_fork_is_reported_and_NOT_written():
    """The legacy twin already consolidates through the drain's own
    merged_facility_id stamp (facility_profile_page._drained_twin_url +
    main._drained_keeper). Writing a second pointer here would create a rival
    answer that can disagree with it."""
    p = plan_group([_df(1, "a-11111111", merged="legacy-id"),
                    _lg("legacy-id", "a-22222222")])
    assert p["writes"] == []
    assert p["drain_fork"] == ["a-22222222"]
    assert p["unlinkable"] == []


def test_an_unlinked_legacy_alternate_is_reported_not_guessed_at():
    """facilities.duplicate_of_id is TEXT and addresses facilities.id — it
    CANNOT point at a discovered keeper. An independently-ingested legacy row
    that merely renders identically has no pointer available, so it is counted
    as residual rather than silently claimed as fixed."""
    p = plan_group([_df(1, "a-11111111"), _lg("other-id", "a-22222222")])
    assert p["writes"] == []
    assert p["drain_fork"] == []
    assert p["unlinkable"] == ["a-22222222"]


def test_it_never_overwrites_another_lanes_pointer():
    """v2/v3 verdicts outrank ours. The WHERE re-asserts this at write time
    too, so a pointer landing between analyze and apply is still safe."""
    p = plan_group([_df(1, "a-11111111", pw=50),
                    _df(2, "a-22222222", pw=5, dup=7)])
    assert p["writes"] == []


def test_a_row_that_already_points_elsewhere_is_never_elected_keeper():
    """A keeper that points onward is not a canonical target — that is how a
    canonical CHAIN starts. Row 1 is the richest and would win on capacity, but
    it already points at 7, so the keeper is row 2 and row 3 is sent there."""
    p = plan_group([_df(1, "a-11111111", pw=99, dup=7),
                    _df(2, "a-22222222"), _df(3, "a-33333333")])
    assert p["skip"] is None
    assert p["keeper"]["id"] == 2
    assert p["writes"] == [3]


def test_a_group_already_consolidated_by_another_lane_is_a_no_op():
    """Nothing left to write is not a failure and must not be re-reported as
    outstanding work — v3 spent 2026-08-16 re-counting its own output as a 12x
    over-report because its scan could not see what it had already done."""
    p = plan_group([_df(1, "a-11111111", pw=99, dup=7), _df(2, "a-22222222")])
    assert p["skip"] == "nothing_to_do"
    assert p["writes"] == []


# ── what it refuses ──────────────────────────────────────────────────────

def test_coordinates_veto_a_merge():
    """Coordinates VETO, never justify. Two rows 5km apart are not one
    building however their pages read."""
    p = plan_group([_df(1, "a-11111111", lat=40.0, lon=-70.0),
                    _df(2, "a-22222222", lat=40.05, lon=-70.0)])
    assert p["skip"] == "coords_far_apart"
    assert p["writes"] == []


def test_a_missing_coordinate_does_not_block():
    """A missing coordinate is not evidence of distance — the drain forks
    routinely carry none at all, and vetoing on absence would refuse the entire
    population this lane exists for."""
    p = plan_group([_df(1, "a-11111111", lat=40.0, lon=-70.0),
                    _df(2, "a-22222222")])
    assert p["skip"] is None and p["writes"] == [2]


def test_a_group_larger_than_the_cap_is_refused():
    """Live histogram is {2: 3,976 · 3: 11 · 5: 1 · 6: 1}. Anything bigger is a
    generic-name collision, not a facility.

    ★ The size is a LITERAL, not MAX_GROUP + 1. Written the obvious way this
    test read the module's own tunable, so raising MAX_GROUP to 10,000 moved
    the fixture with it and the mutation survived — a guard that cannot fail.
    The cap is pinned separately below, so changing it stays a deliberate,
    visible edit rather than a silent widening."""
    rows = [_df(i, f"a-{i:08d}") for i in range(1, 7)]      # 6 distinct URLs
    assert plan_group(rows)["skip"] == "group_too_large"
    # ...and 4 is still accepted, so the cap is a boundary and not an off switch
    ok = plan_group([_df(i, f"a-{i:08d}") for i in range(1, 5)])
    assert ok["skip"] is None and ok["writes"] == [2, 3, 4]


def test_the_group_cap_is_four():
    """Pinned so a widening is a code review, not a side effect."""
    assert MAX_GROUP == 4


def test_a_group_with_no_discovered_row_is_refused():
    """duplicate_of_id addresses discovered_facilities.id and nothing else."""
    p = plan_group([_lg("x", "a-11111111"), _lg("y", "a-22222222")])
    assert p["skip"] == "no_discovered_keeper"


def test_one_url_is_not_a_group():
    """Two ROWS sharing one canonical_slug are one URL — the 6,846-of-7,157
    lesson: 'this slug belongs to a duplicate' is not 'this URL is
    redundant'."""
    assert plan_group([_df(1, "a-11111111"),
                       _df(2, "a-11111111")])["skip"] == "single_url"


def test_junk_slugs_are_excluded_at_source():
    assert is_junk_slug("unknown-osm-dc-123-ab12cd34")
    assert is_junk_slug("data-center-343593591-ab12cd34")
    assert not is_junk_slug("equinix-dc5-ab12cd34")


# ── it never sets the visibility flag ────────────────────────────────────

def test_apply_writes_only_the_pointer_and_the_method():
    """SOURCE-pinned because the UPDATE is the one thing here that touches
    production data. Anchored on the single UPDATE in the module, not on prose:
    if `is_duplicate` ever appears on the SET side, 2026-07-28 repeats."""
    src = open(os.path.join(ROOT, "routes", "facility_dedup_v4.py"),
               encoding="utf-8").read()
    stmts = [s for s in src.split('"') if s.strip().startswith("UPDATE discovered_facilities")]
    assert len(stmts) == 2, stmts        # the apply UPDATE and the undo UPDATE
    sets = src[src.index("SET duplicate_of_id = %s, dedup_method = %s"):]
    assert "is_duplicate" not in sets.split("WHERE")[0]
    assert "COALESCE(is_duplicate, 0) = 0" in src      # read as a guard only


def test_the_method_stamp_is_unique_to_this_lane():
    """undo clears rows stamped by THIS lane only — a v4 undo must never roll
    back a v2 or v3 decision."""
    assert DEDUP_METHOD == "rendered-identity/v4"
    for other in ("brand+site_token/v2", "anon-provider-variant/v3",
                  "geo_crosscountry"):
        assert DEDUP_METHOD != other


# ── end to end over a stub cursor ────────────────────────────────────────

class _Cur:
    """Answers the two scan queries in the order _collect issues them."""

    def __init__(self, discovered, legacy):
        self._q = [discovered, legacy]
        self._rows = []

    def execute(self, sql, params=None):
        self._rows = self._q.pop(0) if self._q else []
        return self

    def fetchall(self):
        return self._rows


def test_collect_groups_across_the_two_tables_on_the_rendered_identity():
    """The whole point: the pair disagrees about `provider` — that is WHY the
    slugs differ — and is still one group."""
    # (tbl, id, slug, name, provider, city, state, country, lat, lon, pw,
    #  duplicate_of_id, merged_facility_id)
    discovered = [("discovered_facilities", "12300071",
                   "007-hebergement-paris-a8b78433", "007 Hebergement Paris",
                   None, "Paris", None, "FR", None, None, None, None,
                   "007-hebergement-paris-paris-fr")]
    legacy = [("facilities", "007-hebergement-paris-paris-fr",
               "007-hebergement-paris-d128fc26", "007 Hebergement Paris",
               "007 Hebergement Paris", "Paris", None, "FR",
               None, None, None, None, None)]
    plans, stats = _collect(_Cur(discovered, legacy))
    assert len(plans) == 1, plans
    assert plans[0]["keeper_slug"] == "007-hebergement-paris-a8b78433"
    assert plans[0]["drain_fork"] == ["007-hebergement-paris-d128fc26"]
    assert stats.get("drain_fork_no_write") == 1


def test_collect_does_not_group_two_distinct_buildings():
    """Amazon IAD85 / IAD75 at one address: same provider, same city, DIFFERENT
    names, so different <h1>s and different facilities. A lane that merged
    these would hide a real site."""
    discovered = [
        ("discovered_facilities", "1", "amazon-iad85-11111111", "Amazon IAD85",
         "Amazon", "Manassas", "VA", "US", 38.779, -77.542, None, None, None),
        ("discovered_facilities", "2", "amazon-iad75-22222222", "Amazon IAD75",
         "Amazon", "Manassas", "VA", "US", 38.779, -77.543, None, None, None),
    ]
    plans, _ = _collect(_Cur(discovered, []))
    assert plans == []


def test_collect_skips_nameless_rows():
    """_render_profile defaults a NULL name to "Data Center", so nameless rows
    would collapse into one enormous false cluster and merge unrelated sites."""
    discovered = [
        ("discovered_facilities", "1", "unknown-osm-dc-1-11111111", None,
         None, "Paris", None, "FR", None, None, None, None, None),
        ("discovered_facilities", "2", "unknown-osm-dc-2-22222222", None,
         None, "Paris", None, "FR", None, None, None, None, None),
    ]
    plans, _ = _collect(_Cur(discovered, []))
    assert plans == []


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
