"""Two halls of one campus must not be merged because the name spells the
separator with a hyphen.

Measured 2026-09-15 on the live sitemap. Six DATA4 Milan halls publish six URLs
whose <h1> and <title> are byte-identical, so they land in ONE rendered-identity
group:

    Data4 Italia - Campus MIL01 - DC01   45.47126,  9.036168
    Data4 Italia - Campus MIL01 - DC02   45.471542, 9.037061
    … DC03, DC05, DC06, DC10             all within ~300 m

`_SUFFIX_WORD_RE` needs a single leading space (" DC1"), so " - DC01" never
matched and every hall reduced to the CAMPUS code MIL01. The veto that exists
for exactly this class ('SecureIT DCB1.1' vs 'DCB1.2') therefore read the two
halls as AGREEING, and `co_located` — 200 m — was free to corroborate the merge.
Only MAX_GROUP = 4 refused this group, and that is an accident of this campus
having six halls rather than three.

A false merge hides a real building, so this file pins both directions: the
corroboration that would have carried the merge is really there, and the veto is
the thing refusing it.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import util.facility_site_code as fsc  # noqa: E402
from routes import facility_dedup_v4 as v4  # noqa: E402


def _row(id_, slug, name, lat, lon):
    """Shaped exactly as routes.facility_dedup_v4._collect builds a row.

    ★ Note what is ABSENT: `city`. _collect selects city for identity_key and
      then does not put it in the dict, so designators_disagree always runs with
      city="" in production. A fixture that supplied one would be more capable
      than the real object and could hide a live failure.
    """
    return {"table": "discovered_facilities", "id": id_,
            "canonical_slug": slug, "name": name, "provider": "Data4 Italia",
            "latitude": lat, "longitude": lon, "power_mw": 0.0,
            "duplicate_of_id": None, "merged_facility_id": None,
            "discovered_twin_id": None}


HALL_A = _row(19486, "data4-italia-data4-italia-campus-mil01-dc01-9e500901",
              "Data4 Italia - Campus MIL01 - DC01", 45.47126, 9.036168)
HALL_B = _row(19487, "data4-italia-data4-italia-campus-mil01-dc02-65271383",
              "Data4 Italia - Campus MIL01 - DC02", 45.471542, 9.037061)


def test_the_corroboration_that_would_carry_the_merge_is_really_there():
    """The control. Without it, "the veto refuses this pair" would be a claim
    about a pair nothing was going to merge anyway."""
    assert v4.co_located(HALL_A, HALL_B) is True
    assert v4.same_name(HALL_A["name"], HALL_B["name"]) is False
    assert v4.has_coords(HALL_A) and v4.has_coords(HALL_B)


def test_the_designator_veto_refuses_two_colocated_halls():
    assert v4.designators_disagree(HALL_A, HALL_B) is True
    plan = v4.plan_group([HALL_A, HALL_B])
    assert plan["writes"] == [], "no pointer may be written between two halls"
    assert plan["name_mismatch"] == [HALL_B["canonical_slug"]]
    assert plan["mismatch_reason"][HALL_B["canonical_slug"]] == \
        "designator_conflict"


def test_reading_the_headline_designator_instead_would_merge_them(monkeypatch):
    """The defect itself, pinned. designators_disagree imports at CALL time, so
    restoring the old reading restores the old behaviour — and it writes a
    pointer from one hall to the other."""
    monkeypatch.setattr(fsc, "detect_merge_designator",
                        fsc.detect_site_designator)
    plan = v4.plan_group([HALL_A, HALL_B])
    assert plan["writes"] == [19487]


def test_the_veto_change_moves_no_headline_and_no_slug():
    """site_code_headline reaches _designator_suffix directly, so nothing here
    can move a live <h1>, <title> or slug. Facility URLs are frozen."""
    for name in (HALL_A["name"], "Data4 Italia - Campus MIL01 - DC10"):
        assert fsc.detect_site_designator(name, "Milan") == "MIL01"
        head = fsc.site_code_headline(name, "Data4 Italia", "Milan")
        assert head.endswith("MIL01 — Milan Data Center"), head


def test_the_two_readings_agree_everywhere_except_a_dashed_hall_code():
    """The whole pinned corpus, so a widened rule cannot quietly restate every
    other name. Only the dashed-hall row may differ."""
    from test_facility_site_code_titles import DESIGNATORS, NON_DESIGNATORS

    dashed = {"Data4 Italia - Campus MIL01 - DC10": "MIL01 DC10"}
    names = {n for n, _c, _code in NON_DESIGNATORS}
    assert set(dashed) <= names, "the fixture stopped carrying the dashed hall"

    for name, city, code, ident in DESIGNATORS:
        assert fsc.detect_merge_designator(name, city) == ident, name
    for name, city, code in NON_DESIGNATORS:
        assert fsc.detect_merge_designator(name, city) == \
            dashed.get(name, code), name


def test_a_dashed_location_or_legal_token_is_not_a_hall():
    """The dashed branch inherits both refusals the space branch already makes,
    or "BAR1 - MILAN" becomes a building and two spellings of one site stop
    agreeing."""
    # ★ RENO, not MILAN: the token class is [A-Z]{2,4}\d{0,3}, so a five-letter
    #   city never reaches the deny-list at all and asserting on one would pass
    #   with the city rule deleted. Mutation-checked exactly there.
    assert fsc.detect_merge_designator("Foo BAR1 - RENO", "Reno") == "BAR1"
    assert fsc.detect_merge_designator("Foo BAR1 - RENO", "Las Vegas") == \
        "BAR1 RENO"
    assert fsc.detect_merge_designator("Foo BAR1 - LLC", "Reno") == "BAR1"
    assert fsc.detect_merge_designator("Foo BAR1 - DC2", "Reno") == "BAR1 DC2"


def test_a_designator_never_appears_where_there_is_no_code():
    for name in ("Some Colocation Facility", "", None):
        assert fsc.detect_merge_designator(name, "Milan") is None
