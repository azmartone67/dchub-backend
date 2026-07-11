"""r-retirement-headroom (2026-07-11): Gemini co-design round 3.

Unit-level only (green-main rule: never import main; no DB in pre-merge
pytest). Covers the ISO→BA mapping that replaces state-footprint
approximation, the haversine, and the contract invariants in source.
"""

import pathlib

from routes.retirement_headroom import (retirement_headroom_bp, _ISO_TO_BA,
                                        _haversine_km)

SRC = pathlib.Path(__file__).resolve().parent.parent / "routes" / "retirement_headroom.py"


def test_iso_ba_mapping_covers_the_seven_isos():
    for iso, ba in (("ERCOT", "ERCO"), ("SPP", "SWPP"), ("CAISO", "CISO"),
                    ("NYISO", "NYIS"), ("ISONE", "ISNE"), ("MISO", "MISO"),
                    ("PJM", "PJM")):
        assert _ISO_TO_BA[iso] == ba


def test_haversine_sane():
    # Ashburn VA → Manassas VA ≈ 30-40 km
    d = _haversine_km(39.04, -77.49, 38.75, -77.48)
    assert 25 < d < 45
    assert _haversine_km(40.0, -100.0, 40.0, -100.0) == 0


def test_contract_invariants_in_source():
    src = SRC.read_text()
    # the envelope entity Gemini's state machine branches on
    assert '"_entity": "retirement_headroom_results"' in src
    # handoff parity: LIVE analyze_site arg names, not the sketch's
    assert '"lon"' in src and '"capacity_mw": target_mw' in src
    # honesty: the RMR caveat ships in meta, and pre-ingest degrades loudly
    assert "reliability reviews" in src
    assert "not yet ingested" in src
    # region filter is BA-code equality, never a state list
    assert "ba_code = ANY(%s)" in src


def test_blueprint_route_registered():
    assert retirement_headroom_bp.name == "retirement_headroom"
    src = SRC.read_text()
    assert '@retirement_headroom_bp.route("/api/v1/retirement-headroom")' in src
