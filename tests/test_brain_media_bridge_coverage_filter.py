"""Guard: the brain->media bridge must drop cumulative coverage/inventory stats
(e.g. "311 markets across 7 ISOs") so they can't win a media slot over real
data-desk movers. Delta/mover findings must still pass through.

See routes/brain_media_bridge.py::_COVERAGE_STAT.
"""
import pytest

from routes.brain_media_bridge import _COVERAGE_STAT


# Cumulative inventory snapshots — NOT news, must be filtered out.
COVERAGE_STATS = [
    "311 DCPI markets across 7 live ISOs",
    "facilities span 178 countries",
    "21,000 facilities across 178 countries",
    "Tracking 232 markets across 10 ISOs",
    "5,300 operators across 40 regions",
]

# Genuine movers / change findings — must survive (this is the content we WANT).
MOVERS = [
    "Dallas added 3 facilities in Q2",
    "ERCOT queue rose 127 GW this week",
    "Cheyenne climbed 8 points to 72/100",
    "Phoenix flipped to AVOID on the DCPI",
    "5 new facilities energized in Loudoun County",
]


@pytest.mark.parametrize("headline", COVERAGE_STATS)
def test_coverage_stats_are_filtered(headline):
    assert _COVERAGE_STAT.search(headline), f"expected coverage stat to be filtered: {headline!r}"


@pytest.mark.parametrize("headline", MOVERS)
def test_movers_are_not_filtered(headline):
    assert not _COVERAGE_STAT.search(headline), f"mover wrongly filtered: {headline!r}"
