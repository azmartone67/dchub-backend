"""GUARD — ENTSO-E A75 parsing: the right Point, and a real data timestamp.

Two defects, both live on all 33 bidding zones (33 of the 47 grids the MCP
scoreboard ranks) as of 2026-08-08:

1. PERIOD SELECTION. _parse_generation_xml picked the largest `position` across
   ALL Periods of a TimeSeries. ENTSO-E numbers positions RELATIVE TO EACH
   PERIOD, restarting at 1 in every one — so a document split into several
   Periods (a resolution change, a DST boundary, or just a multi-hour query
   window) handed the win to whichever Period had the MOST points, frequently
   an earlier one. The result was published as the "latest settled period".

2. NO DATA TIMESTAMP. Every zone shipped `observed_age_s` and nothing else, and
   observed_age_s measures how long ago DC Hub FETCHED. On a fresh fetch it is
   0 — read as "this instant" — for a feed ENTSO-E itself lags 1-2 hours.

Pure tests: XML strings in, parsed values out. No token, no network.
"""
import datetime

import pytest

import routes.iso_eu_entsoe as eu

NS = 'xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"'


def _doc(periods_xml, psr="B19"):
    """One A75 GL_MarketDocument with a single TimeSeries (psrType B19 = wind
    onshore) carrying the given Periods verbatim."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<GL_MarketDocument {NS}>
  <TimeSeries>
    <mRID>1</mRID>
    <MktPSRType><psrType>{psr}</psrType></MktPSRType>
    {periods_xml}
  </TimeSeries>
</GL_MarketDocument>"""


def _period(start, end, resolution, quantities):
    pts = "".join(
        f"<Point><position>{i}</position><quantity>{q}</quantity></Point>"
        for i, q in enumerate(quantities, start=1))
    return (f"<Period><timeInterval><start>{start}</start><end>{end}</end>"
            f"</timeInterval><resolution>{resolution}</resolution>{pts}</Period>")


# ── 1. Period selection ─────────────────────────────────────────────────────

def test_positions_restart_per_period_so_the_later_period_wins():
    """THE regression. Period A (earlier) has EIGHT points, Period B (later)
    has TWO. Max-position-across-Periods picks A's position 8; the correct
    answer is B's last point."""
    early = _period("2026-08-07T20:00Z", "2026-08-07T22:00Z", "PT15M",
                    [100, 101, 102, 103, 104, 105, 106, 8888])   # pos 8 = 8888
    late = _period("2026-08-07T22:00Z", "2026-08-07T23:00Z", "PT30M",
                   [200, 4242])                                   # pos 2 = 4242
    out = eu._parse_generation_xml(_doc(early + late))
    assert out is not None
    assert out["fuels"]["wind"] == 4242, (
        "picked the earlier Period's position 8 — positions are period-local "
        "and must never be compared across Periods")
    assert out["fuels"]["wind"] != 8888


def test_period_order_in_the_document_does_not_decide_it():
    """Same two Periods, later one written FIRST. The timestamps decide."""
    early = _period("2026-08-07T20:00Z", "2026-08-07T22:00Z", "PT15M",
                    [100, 101, 102, 103, 104, 105, 106, 8888])
    late = _period("2026-08-07T22:00Z", "2026-08-07T23:00Z", "PT30M",
                   [200, 4242])
    out = eu._parse_generation_xml(_doc(late + early))
    assert out["fuels"]["wind"] == 4242


def test_single_period_still_takes_its_last_point():
    one = _period("2026-08-07T22:00Z", "2026-08-07T23:00Z", "PT15M",
                  [10, 20, 30, 40])
    out = eu._parse_generation_xml(_doc(one))
    assert out["fuels"]["wind"] == 40


def test_point_end_is_period_start_plus_position_times_resolution():
    one = _period("2026-08-07T22:00Z", "2026-08-07T23:00Z", "PT15M",
                  [10, 20, 30, 40])          # position 4 -> 22:00 + 60min
    out = eu._parse_generation_xml(_doc(one))
    assert out["period_end"] == "2026-08-07T23:00:00+00:00"


def test_point_end_never_claims_past_the_period_end():
    """A document whose position count overruns its own timeInterval must not
    produce a timestamp in the future."""
    one = _period("2026-08-07T22:00Z", "2026-08-07T22:30Z", "PT15M",
                  [10, 20, 30, 40])          # 4 x 15min = 60min > the 30min window
    out = eu._parse_generation_xml(_doc(one))
    assert out["period_end"] == "2026-08-07T22:30:00+00:00"


def test_fuels_and_timestamps_are_separate_keys():
    """The timestamps must not sit inside the fuel map — a consumer summing
    the fuels would trip over a string."""
    one = _period("2026-08-07T22:00Z", "2026-08-07T23:00Z", "PT60M", [500])
    out = eu._parse_generation_xml(_doc(one))
    assert set(out) == {"fuels", "period_end", "period_end_newest"}
    assert all(isinstance(v, float) for v in out["fuels"].values())
    assert sum(v for v in out["fuels"].values() if v > 0) == 500


def test_mix_age_is_judged_on_the_oldest_component():
    """Two fuels settled at different instants: the mix is only as current as
    its stalest component."""
    wind = _period("2026-08-07T20:00Z", "2026-08-07T21:00Z", "PT60M", [100])
    solar = _period("2026-08-07T22:00Z", "2026-08-07T23:00Z", "PT60M", [50])
    doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<GL_MarketDocument {NS}>
  <TimeSeries><mRID>1</mRID><MktPSRType><psrType>B19</psrType></MktPSRType>{wind}</TimeSeries>
  <TimeSeries><mRID>2</mRID><MktPSRType><psrType>B16</psrType></MktPSRType>{solar}</TimeSeries>
</GL_MarketDocument>"""
    out = eu._parse_generation_xml(doc)
    assert out["period_end"] == "2026-08-07T21:00:00+00:00"          # oldest
    assert out["period_end_newest"] == "2026-08-07T23:00:00+00:00"   # newest


def test_consumption_leg_is_still_skipped():
    one = _period("2026-08-07T22:00Z", "2026-08-07T23:00Z", "PT60M", [999])
    doc = f"""<?xml version="1.0" encoding="UTF-8"?>
<GL_MarketDocument {NS}>
  <TimeSeries><mRID>1</mRID><MktPSRType><psrType>B10</psrType></MktPSRType>
    <outBiddingZone_Domain.mRID>10Y1001A1001A83F</outBiddingZone_Domain.mRID>{one}</TimeSeries>
</GL_MarketDocument>"""
    assert eu._parse_generation_xml(doc) is None


def test_acknowledgement_and_garbage_still_return_none():
    assert eu._parse_generation_xml(
        f'<Acknowledgement_MarketDocument {NS}><reason/></Acknowledgement_MarketDocument>') is None
    assert eu._parse_generation_xml("not xml at all") is None
    assert eu._parse_generation_xml("") is None


# ── 2. Data age vs fetch age ────────────────────────────────────────────────

NOW = datetime.datetime(2026, 8, 8, 0, 40, tzinfo=datetime.timezone.utc)


def test_data_age_is_measured_from_the_readings_own_instant():
    """THE second regression: observed_age_s 0 on a fresh fetch must not be the
    only age on the row."""
    snap = {"code": "DE_LU", "data_period_end": "2026-08-07T22:00:00+00:00"}
    out = eu._with_ages(snap, observed_age_s=0, now=NOW)
    assert out["observed_age_s"] == 0          # we fetched just now …
    assert out["data_age_s"] == 9600           # … and the data is 2h40m old
    assert out["data_age_s"] != out["observed_age_s"]


def test_observed_age_basis_says_it_is_not_the_data_age():
    out = eu._with_ages({"data_period_end": None}, observed_age_s=0, now=NOW)
    basis = out["observed_age_basis"].lower()
    assert "not the age of the data" in basis
    assert "data_age_s" in basis


def test_unknown_timestamp_is_null_never_zero():
    """A missing period must read as UNKNOWN age, not as 'now' — zero is
    exactly how the old field misled."""
    out = eu._with_ages({"data_period_end": None}, observed_age_s=0, now=NOW)
    assert out["data_age_s"] is None
    assert "do not read this as fresh" in out["data_age_unknown_reason"].lower()


def test_cached_reading_keeps_its_own_data_age_not_the_cache_age():
    """A row served from the 900s zone cache: fetch age moves, data age is
    still measured from the reading's instant."""
    snap = {"code": "FR", "data_period_end": "2026-08-07T22:00:00+00:00"}
    out = eu._with_ages(snap, observed_age_s=880, now=NOW)
    assert out["observed_age_s"] == 880
    assert out["data_age_s"] == 9600


def test_future_dated_period_clamps_to_zero_not_negative():
    snap = {"data_period_end": "2026-08-08T02:00:00+00:00"}
    out = eu._with_ages(snap, observed_age_s=0, now=NOW)
    assert out["data_age_s"] == 0


@pytest.mark.parametrize("res,seconds", [
    ("PT15M", 900), ("PT30M", 1800), ("PT60M", 3600), ("PT1H", 3600),
    ("P1D", 86400), ("pt15m", 900), ("nonsense", None), ("", None),
])
def test_resolution_parsing(res, seconds):
    assert eu._resolution_seconds(res) == seconds
