"""GUARD — ENTSO-E curve type A03: a flat fuel must not date a whole bidding
zone to the start of the query window.

THE DEFECT, AS MEASURED (live /api/v1/iso/eu/debug?zone=DE_LU, 2026-09-11):

    30h window from 2026-09-09T22:00Z
      data_period_end          2026-09-09T22:15:00Z   <- window start + 15 min
      data_period_end_newest   2026-09-11T03:45:00Z   <- 42 min before the probe
    5h window from 2026-09-10T23:00Z
      period_end               2026-09-10T23:15:00Z   <- window start + 15 min

Whatever window was asked for, one fuel was dated to its first quarter-hour.
ENTSO-E's A03 curve type (variable-sized block) omits every position whose
value repeats the one before, so a fuel that holds one value all window —
Germany's nuclear at 0 MW — arrives as a SINGLE point at position 1 (entsoe-py
forward-fills A03 for this reason). `_period_latest_point` computed
start + position * resolution = window start + 15 min, and because a mix is
judged on its stalest fuel, that instant became the zone's data_period_end, its
grid_data timestamp and its age: ~30h on 16 zones once #4309 widened the
lookback to 30h — past the radar's 24h, so each filed iso_metric_count_zero_24h
while its data was fresh.

What is proved here:
  · an A03 series' last value holds until its Period ends;
  · the measured DE_LU shape no longer pins the zone to the window start;
  · a mid-window change on A03 (the FR shape, 20:15Z) holds to the Period end;
  · A01 and a missing curve type are UNCHANGED — nothing is extended on a guess;
  · an A03 Period with no parseable end keeps the formula instead of losing
    its timestamp;
  · the fix reaches data_period_end through the real _zone_snapshot call;
  · /debug names the curve type, which its 600-char xml_head never reached.

Pure tests: XML strings in, parsed values out. No token, no network, no DB.

Run:  python3 -m pytest tests/test_entsoe_compressed_curve_age.py -v
"""
import types

import pytest

import routes.iso_eu_entsoe as eu

NS = 'xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"'

START = "2026-09-09T22:00Z"                 # the measured 30h window's start
END = "2026-09-11T03:45Z"                   # the measured newest instant
END_ISO = "2026-09-11T03:45:00+00:00"
WINDOW_START_PLUS_15 = "2026-09-09T22:15:00+00:00"
FULL_A01 = [(i, 1000 + i) for i in range(1, 120)]   # 119 x 15min: 22:00Z -> 03:45Z


def _series(psr, points, curve="A03", start=START, end=END,
            resolution="PT15M", mrid=1):
    """One TimeSeries with one Period. `points` is [(position, quantity)],
    written exactly as given — an A03 gap is an OMITTED position, never a 0."""
    curve_xml = f"<curveType>{curve}</curveType>" if curve is not None else ""
    end_xml = f"<end>{end}</end>" if end is not None else ""
    pts = "".join(
        f"<Point><position>{p}</position><quantity>{q}</quantity></Point>"
        for p, q in points)
    return (f"<TimeSeries><mRID>{mrid}</mRID>{curve_xml}"
            f"<MktPSRType><psrType>{psr}</psrType></MktPSRType>"
            f"<Period><timeInterval><start>{start}</start>{end_xml}"
            f"</timeInterval><resolution>{resolution}</resolution>{pts}"
            f"</Period></TimeSeries>")


def _doc(*series):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f"<GL_MarketDocument {NS}>{''.join(series)}</GL_MarketDocument>")


def _de_lu_doc():
    """The measured shape: wind publishes every quarter-hour (A01), nuclear
    sits at 0 MW all window (A03, one point)."""
    return _doc(_series("B19", FULL_A01, curve="A01", mrid=1),
                _series("B14", [(1, 0)], curve="A03", mrid=2))


# ── 1. A03: the last block runs to the Period end ───────────────────────────

@pytest.mark.parametrize("curve", ["A03", " a03 "])
def test_a03_single_point_holds_until_the_period_end(curve):
    """THE regression in its smallest form: one flat fuel, one point."""
    out = eu._parse_generation_xml(_doc(_series("B14", [(1, 0)], curve=curve)))
    assert out is not None
    assert out["period_end"] == END_ISO, (
        "an A03 point's value holds until the next point or the Period end — "
        "dating it start + 1 x resolution is how DE_LU read 30h old")


def test_the_measured_de_lu_shape_is_no_longer_pinned_to_the_window_start():
    out = eu._parse_generation_xml(_de_lu_doc())
    assert out["period_end"] != WINDOW_START_PLUS_15, (
        "the zone is again dated to the window start by its flat nuclear series")
    assert out["period_end"] == END_ISO
    assert out["period_end_newest"] == END_ISO
    assert out["fuels"]["nuclear"] == 0
    assert out["fuels"]["wind"] == 1119


def test_a03_value_that_changed_mid_window_also_holds_to_the_period_end():
    """The FR shape: solar fell to 0 part-way through and stayed there, so the
    last point sits mid-window — position 89 is 20:15Z, the instant FR's zone
    read as its data_period_end. That value is the one in force, and it holds."""
    solar = _series("B16", [(1, 900), (40, 350), (89, 0)], curve="A03")
    out = eu._parse_generation_xml(_doc(solar))
    assert out["fuels"]["solar"] == 0, "the LAST point is the value in force"
    assert out["period_end"] == END_ISO


# ── 2. nothing else is extended ─────────────────────────────────────────────

def test_a01_lone_point_keeps_the_formula():
    """CONTROL. A01 lists every position, so a lone point really does cover only
    its own quarter-hour — extending it would claim data that was never sent."""
    out = eu._parse_generation_xml(_doc(_series("B14", [(1, 0)], curve="A01")))
    assert out["period_end"] == WINDOW_START_PLUS_15


def test_a_missing_curve_type_keeps_the_formula():
    """CONTROL. Unknown is not A03: the conservative reading stays."""
    out = eu._parse_generation_xml(_doc(_series("B14", [(1, 0)], curve=None)))
    assert out["period_end"] == WINDOW_START_PLUS_15


def test_a03_without_a_parseable_period_end_keeps_the_formula():
    """Nothing to extend to. The point keeps its own block rather than losing its
    timestamp — a None here would publish the zone's age as UNKNOWN."""
    out = eu._parse_generation_xml(
        _doc(_series("B14", [(1, 0)], curve="A03", end=None)))
    assert out["period_end"] == WINDOW_START_PLUS_15


# ── 3. the call sites ───────────────────────────────────────────────────────

def _stub_upstream(monkeypatch, xml):
    resp = types.SimpleNamespace(ok=True, status_code=200, text=xml)

    def _get(url, params=None, timeout=None):
        return resp

    monkeypatch.setattr(eu, "_rq", types.SimpleNamespace(get=_get))
    monkeypatch.setattr(eu, "_token", lambda: "test-token")
    monkeypatch.setattr(eu, "_ZONE_CACHE", {})


def test_the_fix_reaches_data_period_end_through_the_real_snapshot(monkeypatch):
    """Testing the parser is not testing the call. data_period_end off
    _zone_snapshot is what _persist_metrics stamps grid_data with, and the
    grid_data timestamp is what the freshness page and the radar age."""
    _stub_upstream(monkeypatch, _de_lu_doc())
    snap = eu._zone_snapshot("DE_LU")
    assert snap is not None
    assert snap["data_period_end"] == END_ISO


def test_debug_names_the_curve_type(monkeypatch):
    """/debug's xml_head is 600 characters and ends inside the document header,
    before any TimeSeries — it could never show the one field that decides how
    old a reading is."""
    from flask import Flask
    _stub_upstream(monkeypatch, _de_lu_doc())
    app = Flask(__name__)
    app.register_blueprint(eu.iso_eu_entsoe_bp)
    body = app.test_client().get("/api/v1/iso/eu/debug?zone=DE_LU").get_json()
    assert body["curve_types"] == ["A01", "A03"]
    assert body["zone_snapshot"]["data_period_end"] == END_ISO
