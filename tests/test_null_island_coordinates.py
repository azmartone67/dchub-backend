"""
r-nullisland (2026-08-31) — absent coordinates must say they are absent.

The live row that prompted this returned latitude 0.0 / longitude 0.0 while
power_mw on the same row correctly returned null. A null sends a consumer to
find the value elsewhere; a 0.0 tells them they already have it.

The equatorial test is the one that matters most: over-eager zero-stripping
would silently delete real locations in Uganda, Ecuador, Kenya and Indonesia,
which would be a WORSE bug than the one being fixed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from routes.provenance import normalize_coordinates, COORDS_KNOWN, COORDS_UNKNOWN


def test_null_island_becomes_null_and_says_why():
    row = {"name": "Ashburn II", "latitude": 0.0, "longitude": 0.0}
    normalize_coordinates(row)
    assert row["latitude"] is None and row["longitude"] is None
    assert row["coordinates_status"] == COORDS_UNKNOWN
    assert "Null Island" in row["coordinates_note"]


def test_real_coordinates_are_untouched_and_marked_known():
    row = {"latitude": 39.0438, "longitude": -77.4874}
    normalize_coordinates(row)
    assert row["latitude"] == 39.0438 and row["longitude"] == -77.4874
    assert row["coordinates_status"] == COORDS_KNOWN


# ---- the anti-overreach pin -------------------------------------------------

def test_equatorial_latitude_zero_with_real_longitude_survives():
    """Kampala, Uganda sits at latitude ~0. Stripping it would invent a bug."""
    row = {"latitude": 0.0, "longitude": 32.5825}
    normalize_coordinates(row)
    assert row["latitude"] == 0.0, "lat=0 with a real longitude is a REAL place"
    assert row["longitude"] == 32.5825
    assert row["coordinates_status"] == COORDS_KNOWN


def test_prime_meridian_longitude_zero_with_real_latitude_survives():
    """Greenwich. lon=0 alone is equally real."""
    row = {"latitude": 51.4779, "longitude": 0.0}
    normalize_coordinates(row)
    assert row["latitude"] == 51.4779 and row["longitude"] == 0.0
    assert row["coordinates_status"] == COORDS_KNOWN


# ---- degradation ------------------------------------------------------------

def test_already_null_is_reported_unknown():
    row = {"latitude": None, "longitude": None}
    normalize_coordinates(row)
    assert row["coordinates_status"] == COORDS_UNKNOWN
    assert "coordinates_note" not in row, "only Null Island gets the placeholder note"


def test_one_sided_null_is_unknown():
    row = {"latitude": 39.0, "longitude": None}
    normalize_coordinates(row)
    assert row["coordinates_status"] == COORDS_UNKNOWN


def test_unparseable_values_do_not_raise():
    row = {"latitude": "n/a", "longitude": "n/a"}
    normalize_coordinates(row)
    assert row["coordinates_status"] == COORDS_UNKNOWN


def test_string_zero_is_still_null_island():
    row = {"latitude": "0", "longitude": "0.0"}
    normalize_coordinates(row)
    assert row["latitude"] is None and row["longitude"] is None


def test_row_without_coordinate_keys_is_left_alone():
    row = {"name": "x"}
    normalize_coordinates(row)
    assert row == {"name": "x"}, "must not stamp status onto rows that carry no coords"
