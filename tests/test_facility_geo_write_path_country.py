"""facility_country_mislabeled kept recurring (105 -> 136 -> 109 mismatches
seen by /api/v1/admin/facility-geo/analyze) because the post-hoc repair in
routes/facility_geo_quality.py only cleans up `facilities` after the fact.
The two live promotion paths that WRITE into `facilities` --
facility_auto_approve.py and discovery_auto_approve.py -- copied a scraped
`country` straight from `discovered_facilities` (or defaulted a blank one to
'US') with no check against the row's own latitude/longitude, so every
scheduler run recreated the backlog the repair endpoint was meant to drain.

resolve_country() applies the SAME conservative policy `_scan()` already uses
(only overrides a blank country or the bulk 'US' default; never touches a
deliberately-set non-US tag; never guesses without one confident single-box
match) before the row is ever written.
"""
import pathlib
import re

import routes.facility_geo_quality as gq

SRC = pathlib.Path(__file__).resolve().parent.parent


def _code(name):
    return (SRC / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# resolve_country() itself
# --------------------------------------------------------------------------
def test_blank_country_is_inferred_from_coords():
    assert gq.resolve_country(None, 51.50, -0.12) == "GB"
    assert gq.resolve_country("", 48.85, 2.35) == "FR"


def test_bulk_us_default_is_overridden_when_coords_disagree():
    """The exact class this finding reported: US->NL, US->GB, US->FR, ..."""
    assert gq.resolve_country("US", 52.37, 4.90) == "NL"   # Amsterdam
    assert gq.resolve_country("us", 51.50, -0.12) == "GB"  # case-insensitive


def test_us_label_kept_when_coords_agree():
    assert gq.resolve_country("US", 39.04, -77.49) == "US"  # Ashburn


def test_deliberate_non_us_tag_is_never_overridden():
    """RU is not in _AUTOFIX_FROM -- a Kaliningrad-style exclave must survive
    even though its coords sit outside the RU box."""
    assert gq.resolve_country("RU", 54.71, 20.51) == "RU"


def test_ambiguous_or_unmapped_coords_leave_declared_value_alone():
    # No single unambiguous box match here (mid-ocean) -> can't infer.
    assert gq.resolve_country("US", 0.0, -30.0) == "US"
    assert gq.resolve_country(None, 0.0, -30.0) is None


def test_bad_coords_leave_declared_value_alone():
    assert gq.resolve_country("US", None, None) == "US"
    assert gq.resolve_country("US", "not-a-number", 4.9) == "US"


# --------------------------------------------------------------------------
# The two write paths must actually call it before their INSERT, not just
# have the helper exist unused.
# --------------------------------------------------------------------------
def test_discovery_auto_approve_resolves_country_before_insert():
    code = _code("discovery_auto_approve.py")
    assert "from routes.facility_geo_quality import resolve_country" in code
    assert re.search(r"resolve_country\(\s*disc\.get\('country'\)", code), (
        "discovery_auto_approve must run the declared country + coords "
        "through resolve_country before promoting a discovered row")
    assert "disc.get('country') or 'US'" not in code, (
        "the un-geo-checked default must be gone, not just supplemented")


def test_facility_auto_approve_resolves_country_before_insert():
    code = _code("facility_auto_approve.py")
    assert "from routes.facility_geo_quality import resolve_country" in code
    assert re.search(r"resolve_country\(\s*row\.get\('country'\)", code), (
        "facility_auto_approve must run the declared country + coords "
        "through resolve_country before promoting a discovered row")


def test_both_write_paths_import_the_same_helper():
    import discovery_auto_approve
    import facility_auto_approve
    assert discovery_auto_approve.resolve_country is gq.resolve_country
    assert facility_auto_approve.resolve_country is gq.resolve_country
