"""The ENTSO-E request window must reach data that has actually been published.

MEASURED 2026-09-09 against the live API with the production token
(A75 / processType A16), DE-LU, FR and ES — identical in all three:

    end=now-0h  none     end=now-12h  none
    end=now-4h  none     end=now-16h  DATA
    end=now-8h  none     end=now-24h  DATA

The lane asked for a 5-hour window ending at NOW. ENTSO-E answered every call
with an Acknowledgement — "No matching data found for Data item
AGGREGATED_GENERATION_PER_TYPE_R3" — _parse_generation_xml correctly returned
None, every zone read unavailable, and the feed logged 199 CONSECUTIVE ZERO-ROW
RUNS. The token was valid the whole time (a deliberately bad one returns 401)
and the API was up. Nothing was broken except the window.

★ TWO HALVES, AND BOTH MUST HOLD. Widening only works because the parser ranks
  Periods by time: a 30h window returns many Periods, and if selection ever
  regressed to document order the lane would serve an OLD reading as current —
  silently, and looking healthy. Fixing the window without fencing the ranking
  would trade a visible outage for an invisible lie.
"""
import datetime

import pytest

from routes.iso_eu_entsoe import _LOOKBACK_H, _parse_generation_xml

# ENTSO-E publishes generation-per-type well behind real time; 16h was the
# first window that carried data on the day this was measured.
MEASURED_LAG_H = 16


def test_the_window_clears_the_measured_publishing_lag():
    assert _LOOKBACK_H >= MEASURED_LAG_H + 4, (
        f"lookback is {_LOOKBACK_H}h; generation-per-type was measured "
        f"{MEASURED_LAG_H}h behind on 2026-09-09, so this window asks for data "
        f"that does not exist yet and every zone reads unavailable")


def test_the_window_is_not_so_wide_it_is_just_payload():
    # 48h returned twice the points and the SAME latest instant — the extra
    # hours buy nothing and cost bandwidth on every zone, every run.
    assert _LOOKBACK_H <= 36, f"lookback {_LOOKBACK_H}h is past the point of new data"


def _doc(periods):
    """Minimal A75 GL_MarketDocument with one TimeSeries and N Periods."""
    body = "".join(
        f"""<Period>
              <timeInterval><start>{s}</start><end>{e}</end></timeInterval>
              <resolution>PT60M</resolution>
              <Point><position>1</position><quantity>{q}</quantity></Point>
            </Period>""" for s, e, q in periods)
    return f"""<GL_MarketDocument>
      <TimeSeries><MktPSRType><psrType>B16</psrType></MktPSRType>{body}</TimeSeries>
    </GL_MarketDocument>"""


OLD = ("2026-09-08T00:00Z", "2026-09-08T01:00Z", 111)
NEW = ("2026-09-09T00:00Z", "2026-09-09T01:00Z", 999)


def test_the_newest_period_wins_whatever_the_document_order():
    """The property widening depends on. Asserted BOTH ways round so a parser
    that simply takes the last Period in the file cannot pass."""
    for label, periods in (("old-then-new", [OLD, NEW]), ("new-then-old", [NEW, OLD])):
        out = _parse_generation_xml(_doc(periods))
        assert out is not None, f"{label}: document did not parse"
        assert out["fuels"].get("solar") == 999, (
            f"{label}: took the {out['fuels'].get('solar')} MW period — a wider "
            f"window would now publish a stale reading as current")


def test_an_acknowledgement_is_still_no_data_not_zero():
    """The no-data document must keep reading as absent. If it ever parsed as
    an empty-but-valid reading, the outage would become a published zero."""
    ack = ("<Acknowledgement_MarketDocument><Reason><code>999</code>"
           "<text>No matching data found</text></Reason></Acknowledgement_MarketDocument>")
    assert _parse_generation_xml(ack) is None


def test_control_the_fixture_can_fail():
    """Without this, the 999 assertions prove nothing about the ranking."""
    only_old = _parse_generation_xml(_doc([OLD]))
    assert only_old is not None and only_old["fuels"].get("solar") == 111
