"""ENTSO-E bidding-zone registry + A75 parser — ws2-entsoe (2026-07-29).

Locks the two things that fail SILENTLY in this module: a malformed registry
row (the zone just never appears — no log, no error) and a parser regression on
the Acknowledgement / consumption-leg branches. Pure-function tests — no
network, no DB, and (per the green-main rule) never imports main.
"""
import re
import time

from routes.iso_eu_entsoe import (
    _build_zones, _EXCLUDED_EICS, _PSR, _RENEWABLE_CATS, _ZONE_REGISTRY,
    _ZONE_REGISTRY_WARNINGS, _ZONES,
)
import routes.iso_eu_entsoe as eu

_NS = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"


# ── registry ────────────────────────────────────────────────────────────────
def test_every_registry_row_was_accepted():
    """A dropped row is invisible in prod — this is the only place it is loud."""
    assert _ZONE_REGISTRY_WARNINGS == []
    assert len(_ZONES) == len(_ZONE_REGISTRY)


def test_registry_rows_are_well_formed_and_sourced():
    for row in _ZONE_REGISTRY:
        assert len(row) == 5, row          # (code, eic, name, hub, provenance)
        code, eic, name, hub, prov = row
        assert re.fullmatch(r"[A-Z0-9_]+", code), code   # becomes EU_<code>
        assert eic.strip() and name.strip() and hub.strip(), row
        assert prov.strip(), f"{code} has no EIC provenance"


def test_eics_are_unique():
    eics = [r[1] for r in _ZONE_REGISTRY]
    assert len(eics) == len(set(eics))


def test_double_counted_grids_stay_out():
    """GB is its own scoreboard row (iso_uk_elexon); IE_SEM already covers IE."""
    live = {v[0] for v in _ZONES.values()}
    for eic in _EXCLUDED_EICS:
        assert eic not in live


def test_builder_is_fail_soft_never_raising():
    """main.py registers this blueprint in a try/except that only prints, so a
    raise here would 404 every /api/v1/iso/eu/* route with no other signal."""
    zones, warns = _build_zones([
        ("OK", "10YOK", "Okayland", "Okaytown", "test"),
        ("SHORT",),                                          # malformed
        ("EMPTY", "", "No EIC", "Nowhere", "test"),           # incomplete
        ("OK", "10YDUP", "dup code", "x", "test"),            # duplicate code
        ("OTHER", "10YOK", "dup eic", "x", "test"),           # duplicate EIC
        ("GB", "10YGB----------A", "Great Britain", "London", "test"),
        ("NOPROV", "10YNP", "No provenance", "x"),            # 4-tuple
    ])
    assert set(zones) == {"OK", "NOPROV"}
    joined = " ".join(warns)
    for expect in ("malformed_row", "incomplete_row", "duplicate_code",
                   "duplicate_eic", "excluded_eic", "no_eic_provenance"):
        assert expect in joined, (expect, warns)


# ── per-zone cache ──────────────────────────────────────────────────────────
def test_cache_hit_reports_its_age_and_needs_no_token(monkeypatch):
    monkeypatch.setattr(eu, "_token", lambda: "")   # every network path -> None
    eu._ZONE_CACHE["_TEST_ZZ"] = {"snap": {"code": "_TEST_ZZ",
                                           "generation_total_mw": 1234.0},
                                  "ts": time.time() - 5}
    try:
        got = eu._zone_snapshot("_TEST_ZZ")
        assert got["generation_total_mw"] == 1234.0
        assert 0 <= got["observed_age_s"] <= 60          # basis, never implied
        assert "observed_age_s" not in eu._ZONE_CACHE["_TEST_ZZ"]["snap"]
    finally:
        eu._ZONE_CACHE.pop("_TEST_ZZ", None)


def test_expired_or_skewed_cache_is_never_resurrected(monkeypatch):
    monkeypatch.setattr(eu, "_token", lambda: "")
    for ts in (time.time() - (eu._ZONE_TTL + 60), time.time() + 99999):
        eu._ZONE_CACHE["_TEST_ZZ"] = {"snap": {"code": "_TEST_ZZ"}, "ts": ts}
        try:
            assert eu._zone_snapshot("_TEST_ZZ") is None
        finally:
            eu._ZONE_CACHE.pop("_TEST_ZZ", None)


# ── A75 parser ──────────────────────────────────────────────────────────────
def _ts(psr, points, consumption=False):
    out = [f'<TimeSeries><MktPSRType><psrType>{psr}</psrType></MktPSRType>']
    if consumption:
        out.append('<outBiddingZone_Domain.mRID>10YXX</outBiddingZone_Domain.mRID>')
    out.append('<Period>')
    for pos, qty in points:
        out.append(f'<Point><position>{pos}</position><quantity>{qty}</quantity></Point>')
    out.append('</Period></TimeSeries>')
    return "".join(out)


def _doc(*series):
    return f'<GL_MarketDocument xmlns="{_NS}">' + "".join(series) + '</GL_MarketDocument>'


def test_parser_takes_latest_point_sums_fuels_and_skips_consumption():
    xml = _doc(_ts("B19", [(1, 100), (2, 250)]),      # wind onshore -> latest 250
               _ts("B18", [(1, 50), (2, 60)]),        # wind offshore -> +60
               _ts("B16", [(1, 10), (2, 20)]),        # solar
               _ts("B14", [(1, 900), (2, 900)]),      # nuclear
               _ts("B10", [(1, 500), (2, 500)], consumption=True))  # pump leg
    cats = eu._parse_generation_xml(xml)
    assert cats == {"wind": 310.0, "solar": 20.0, "nuclear": 900.0}
    assert _PSR["B18"] == "wind" and _PSR["B16"] == "solar"
    assert _RENEWABLE_CATS == {"wind", "solar", "hydro"}   # biomass NOT counted


def test_parser_returns_none_on_acknowledgement_and_garbage():
    ack = ('<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:'
           'tc57wg16:451-1:acknowledgementdocument:7:0"><Reason>'
           '<code>999</code></Reason></Acknowledgement_MarketDocument>')
    assert eu._parse_generation_xml(ack) is None
    assert eu._parse_generation_xml("not xml at all") is None
    assert eu._parse_generation_xml(_doc()) is None        # no TimeSeries
