#!/usr/bin/env python3
"""A gated facility record must say so IN THE RECORD, not only beside it.

NO NETWORK, NO DB.

Reported by the partner generating a connector from our spec, 2026-09-20:

    "dchub_facility_detail does not fail. It returns 200 with the contacts
     stripped and coordinates rounded to two decimals, and the only signal is
     _gated in the body. An agent asks for a facility, gets a plausible
     record, and reports it as complete. Every other gate you have announces
     itself with a 403."

Measured before the fix — and worse than reported:

    latitude            39.02        (rounded from 39.0221, ~1.1 km)
    coordinates_status  "known"      <- the field whose JOB is to describe
                                        coordinate quality, asserting the
                                        opposite of what had been done to it

routes.provenance.normalize_coordinates computes coordinates_status from the
RAW values and must run BEFORE the gate (verified_flag reads is_duplicate,
which the mask drops). So it was computed before the transformation that
invalidated it.

Two fixes, both tested here: the record restates coordinate quality after
coarsening, and the envelope names the withheld FIELDS rather than only
counting them.
"""
import pytest

from util.facility_tier_gate import apply_record_gate, gate_record

RAW = {
    "name": "Level 3 Ashburn", "city": "Ashburn", "state": "VA",
    "country": "US", "status": "Operational",
    "latitude": 39.0221, "longitude": -77.4891,
    "coordinates_status": "known",
    "power_mw": 2300, "address": "1 Main St", "provider": "Lumen",
}


# identified rounds like anon since 2026-09-21 (2 dp by default); its exact
# location comes from the monthly allowance, not from a sharper default.
@pytest.mark.parametrize("tier,expected_dp", [("anon", 2), ("identified", 2)])
def test_a_coarsened_coordinate_is_not_still_called_known(tier, expected_dp, monkeypatch):
    monkeypatch.delenv("MAP_ANON_COORD_DP", raising=False)
    monkeypatch.delenv("MAP_FREE_COORD_DP", raising=False)
    out, _ = gate_record(dict(RAW), tier)
    assert out["latitude"] != RAW["latitude"], "precondition: it should coarsen"
    assert out["coordinates_status"] == f"approximate_{expected_dp}dp", (
        f"coordinates_status is {out['coordinates_status']!r} next to a "
        f"latitude rounded to {expected_dp}dp — an agent reads that as an "
        f"exact location"
    )


def test_the_marker_names_the_precision_actually_applied(monkeypatch):
    """A fixed string could drift from the dp the gate used. The two rungs share
    a default now, so the knob is moved to make them differ — the marker must
    follow the dp each one actually got."""
    monkeypatch.delenv("MAP_ANON_COORD_DP", raising=False)
    monkeypatch.setenv("MAP_FREE_COORD_DP", "3")
    anon, _ = gate_record(dict(RAW), "anon")
    ident, _ = gate_record(dict(RAW), "identified")
    assert anon["coordinates_status"] == "approximate_2dp"
    assert ident["coordinates_status"] == "approximate_3dp", (
        "the tiers were rounded differently but report the same coordinate "
        "quality — the marker is not derived from the dp applied"
    )


def test_an_allowance_exact_record_keeps_its_known_status():
    """Spending the monthly allowance makes the coordinate exact, so the record
    must NOT be relabelled approximate."""
    out, _ = gate_record(dict(RAW), "identified", exact_location=True)
    assert out["latitude"] == RAW["latitude"]
    assert out["coordinates_status"] == "known"


def test_a_paid_record_is_untouched():
    """The fix must not stamp a caveat on a record that has none."""
    out, n = gate_record(dict(RAW), "pro")
    assert out["latitude"] == RAW["latitude"]
    assert out["coordinates_status"] == "known"
    assert n == 0


def test_an_exact_record_gated_at_its_own_precision_is_not_relabelled():
    """Rounding that changes nothing is not a redaction, so it must not claim
    one. A 2dp record gated at 2dp keeps its status."""
    already = dict(RAW, latitude=39.02, longitude=-77.49)
    out, _ = gate_record(already, "anon")
    assert out["coordinates_status"] == "known", (
        "nothing was coarsened, so the record must not advertise a coarsening"
    )


def test_the_envelope_names_what_it_withheld_not_just_how_many():
    resp = apply_record_gate({"success": True, "data": dict(RAW)}, "anon")
    withheld = resp.get("_withheld_fields")
    assert withheld, "_withheld_fields absent — a caller learns only a count"
    assert "power_mw" in withheld and "address" in withheld
    assert resp["_redacted_values"] >= len(withheld)


def test_withheld_names_never_carry_the_values():
    """Naming the field is the point; leaking it would defeat the gate."""
    resp = apply_record_gate({"success": True, "data": dict(RAW)}, "anon")
    blob = repr(resp.get("_withheld_fields"))
    assert "2300" not in blob and "1 Main St" not in blob


def test_a_paid_envelope_gets_no_withheld_list():
    resp = apply_record_gate({"success": True, "data": dict(RAW)}, "pro")
    assert "_withheld_fields" not in resp
    assert "_gated" not in resp
