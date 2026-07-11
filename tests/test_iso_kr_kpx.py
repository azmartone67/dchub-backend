"""South Korea KPX real-time page parser — global grid expansion (2026-07-11).

Locks in the scrape contract for new.kpx.or.kr's embedded JS: the ictArr
5-min fuel-mix slots (future slots ship as regDate:"0" + zeros and must be
skipped), the coal+localCoal merge, and the demand chart arrays. Pure-
function tests — no network, no DB, never imports main.
"""
from routes.iso_kr_kpx import _parse_kpx_page

_PAGE = """<html><script>
var ictArr = [
 {"localCoal":"373.9","ppa":"0","windPower":"707.7","nuclearPower":"20439.1",
  "regDate":"2026-07-11 19:15","raisingWater":"199.5","newRenewable":"3284.4",
  "sunlight":"400.0","oil":"161.1","once":"20260711191500","gas":"16422.2",
  "coal":"26478.6","newRenewablePlusWindPower":"3992.1","btm":"0",
  "waterPower":"159.7","seq":0},
 {"localCoal":"375.7","ppa":"0","windPower":"635.4","nuclearPower":"20439.4",
  "regDate":"2026-07-11 19:20","raisingWater":"1514.3","newRenewable":"4072.0",
  "sunlight":"321.0","oil":"262.0","once":"20260711192000","gas":"24339.8",
  "coal":"25765.0","newRenewablePlusWindPower":"4707.4","btm":"12.0",
  "waterPower":"611.0","seq":1},
 {"localCoal":"0","ppa":"0","windPower":"0","nuclearPower":"0","regDate":"0",
  "raisingWater":"0","newRenewable":"0","sunlight":"0","oil":"0",
  "once":"20260711235500","gas":"0","coal":"0",
  "newRenewablePlusWindPower":"0","btm":"0","waterPower":"0","seq":"99999"}
];
function drawChart() {
  var t_time = ['20260711191000','20260711191500','20260711192000'];
  var x = [78167, 78321, 78336];
  var v = [76000, 76100, 76200];
}
</script></html>"""


def test_picks_last_populated_slot_not_future_zeros():
    out = _parse_kpx_page(_PAGE)
    assert out is not None
    assert out["as_of_kst"].startswith("2026-07-11T19:20")
    assert out["cats"]["gas"] == 24339.8


def test_coal_merges_imported_and_domestic():
    out = _parse_kpx_page(_PAGE)
    assert abs(out["cats"]["coal"] - (25765.0 + 375.7)) < 0.01


def test_field_mapping_matches_kpx_names():
    cats = _parse_kpx_page(_PAGE)["cats"]
    assert cats["nuclear"] == 20439.4      # nuclearPower
    assert cats["hydro"] == 611.0          # waterPower
    assert cats["solar"] == 321.0          # sunlight
    assert cats["wind"] == 635.4           # windPower
    assert cats["other_renewable"] == 4072.0  # newRenewable (NOT renewable_pct)
    assert cats["pumped"] == 1514.3        # raisingWater (storage, not in total)
    # ppa/btm are deliberately absent — fuel-unattributed / behind-the-meter
    assert "ppa" not in cats and "btm" not in cats


def test_demand_from_chart_arrays():
    out = _parse_kpx_page(_PAGE)
    assert out["demand_mw"] == 78336.0
    assert out["demand_as_of_kst"] == "20260711192000"


def test_chart_redesign_never_kills_the_fuel_mix():
    page = _PAGE.split("function drawChart")[0] + "</script></html>"
    out = _parse_kpx_page(page)
    assert out is not None
    assert out["demand_mw"] is None
    assert out["cats"]["gas"] == 24339.8


def test_all_future_slots_refused():
    page = """<script>var ictArr = [{"localCoal":"0","gas":"0","coal":"0",
    "nuclearPower":"0","waterPower":"0","windPower":"0","sunlight":"0",
    "newRenewable":"0","oil":"0","raisingWater":"0","regDate":"0",
    "seq":"99999"}];</script>"""
    assert _parse_kpx_page(page) is None


def test_garbage_refused():
    assert _parse_kpx_page(None) is None
    assert _parse_kpx_page("") is None
    assert _parse_kpx_page("<html>maintenance</html>") is None
    assert _parse_kpx_page("<script>var ictArr = [not json];</script>") is None
