"""Japan eria_jukyu fuel-mix parser — full-mix upgrade (2026-07-11).

Locks in the three per-TSO format variants verified live 2026-07-11:
TEPCO (ASCII parens, unquoted), Kyushu (quoted cells, full-width ＬＮＧ,
YYYYMMDD dates), Tohoku (extra 出力制御量 columns) — plus the
last-populated-row rule (future half-hours ship as empty rows) and the
storage/interconnector exclusion from generation_total. Pure-function
tests — no network, no DB, never imports main.
"""
from routes.iso_jp_denkiyoho import (_parse_eria_jukyu, _aggregate_mix,
                                      _ERIA_AREAS, _TSOS)

# Column layout: DATE,TIME,エリア需要,原子力,火力(LNG),火力(石炭),火力(石油),
# 火力(その他),水力,地熱,バイオマス,太陽光発電実績,太陽光出力制御量,
# 風力発電実績,風力出力制御量,揚水,蓄電池,連系線,その他,合計
_TEPCO_STYLE = """単位[MW平均],,,供給力
DATE,TIME,エリア需要,原子力,火力(LNG),火力(石炭),火力(石油),火力(その他),水力,地熱,バイオマス,太陽光発電実績,太陽光出力制御量,風力発電実績,風力出力制御量,揚水,蓄電池,連系線,その他,合計
2026/7/11,18:00,35000,1300,15000,6300,300,1700,1800,0,490,100,0,140,0,2600,40,4100,370,35000
2026/7/11,18:30,35257,1299,15905,6324,323,1719,1828,0,492,18,0,138,0,2664,40,4133,373,35256
2026/7/11,19:00,,,,,,,,,,,,,,,,,,
"""

_KYUSHU_STYLE = '''"単位[MW平均]","","","供給力"
"DATE","TIME","エリア需要","原子力","火力（ＬＮＧ）","火力（石炭）","火力（石油）","火力（その他）","水力","地熱","バイオマス","太陽光発電実績","太陽光出力制御量","風力発電実績","風力出力制御量","揚水","蓄電池","連系線","その他","合計"
"20260711","19:00","13020","2950","2330","3846","154","406","1204","112","830","470","0","246","0","1066","-10","856","274","13022"
'''

_TOHOKU_STYLE = """単位[MW平均],,供給力
DATE,TIME,エリア需要,原子力,火力(LNG),火力(石炭),火力(石油),火力(その他),火力出力制御量,水力,地熱,バイオマス,バイオマス出力制御量,太陽光発電実績,太陽光出力制御量,風力発電実績,風力出力制御量,揚水,蓄電池,連系線,その他,合計
2026/7/11,18:30,9162,791,3775,3513,0,280,0,1020,157,775,0,8,0,173,0,-50,-1,-1889,26,8025
2026/7/11,22:30,,,,,,,,,,,,,,,,,,,,
"""


def test_tepco_style_last_populated_row_wins():
    out = _parse_eria_jukyu(_TEPCO_STYLE)
    assert out is not None
    assert out["demand_mw"] == 35257.0          # 18:30 row, not 18:00, not empty 19:00
    assert out["fuel_gas_mw"] == 15905.0
    assert out["as_of_jst"].startswith("2026-07-11T18:30")


def test_generation_total_excludes_storage_and_interconnector():
    out = _parse_eria_jukyu(_TEPCO_STYLE)
    fuels = (1299 + 15905 + 6324 + 323 + 1719 + 1828 + 0 + 492 + 18 + 138 + 373)
    assert abs(out["generation_total_mw"] - fuels) < 0.11
    # pumped(2664) + battery(40) + interconnector(4133) carried, not counted
    assert out["pumped_storage_mw"] == 2664.0
    assert out["interconnector_mw"] == 4133.0


def test_kyushu_quoted_fullwidth_and_compact_dates():
    out = _parse_eria_jukyu(_KYUSHU_STYLE)
    assert out is not None
    assert out["fuel_gas_mw"] == 2330.0          # 火力（ＬＮＧ） → NFKC → 火力(LNG)
    assert out["fuel_coal_mw"] == 3846.0
    assert out["as_of_jst"].startswith("2026-07-11T19:00")
    assert out["battery_storage_mw"] == -10.0    # charging keeps its sign


def test_tohoku_extra_curtailment_columns_map_by_name():
    out = _parse_eria_jukyu(_TOHOKU_STYLE)
    assert out is not None
    # the extra 火力出力制御量/バイオマス出力制御量 columns must not shift anything
    assert out["fuel_hydro_mw"] == 1020.0
    assert out["fuel_solar_mw"] == 8.0
    assert out["fuel_wind_mw"] == 173.0
    assert out["interconnector_mw"] == -1889.0   # exporting keeps its sign
    assert out["pumped_storage_mw"] == -50.0     # pumping keeps its sign


def test_all_empty_rows_refused():
    empty = _TEPCO_STYLE.split("\n")
    text = "\n".join(empty[:2]) + "\n2026/7/11,0:00,,,,,,,,,,,,,,,,,,\n"
    assert _parse_eria_jukyu(text) is None


def test_garbage_refused():
    assert _parse_eria_jukyu("") is None
    assert _parse_eria_jukyu("hello,world\n1,2\n") is None
    # legacy juyo (demand-only) file must NOT parse as a mix file
    assert _parse_eria_jukyu(
        "2026/7/11 19:15 UPDATE\nDATE,TIME,当日実績(万kW),予測値(万kW)\n"
        "2026/7/11,18:00,3525,3600\n") is None


def test_aggregate_mix_renewable_definition():
    a = _parse_eria_jukyu(_TEPCO_STYLE)
    b = _parse_eria_jukyu(_KYUSHU_STYLE)
    agg = _aggregate_mix({"tepco": a, "kyushu": b})
    assert agg["mix_areas_reporting"] == 2.0
    total = agg["generation_total_mw"]
    renew = agg["fuel_wind_mw"] + agg["fuel_solar_mw"] + agg["fuel_hydro_mw"]
    assert abs(agg["renewable_pct"] - round(100.0 * renew / total, 1)) < 0.11
    assert abs(agg["gas_pct"] - round(100.0 * agg["fuel_gas_mw"] / total, 1)) < 0.11
    # geothermal + biomass count in the total but never as renewable
    assert agg["fuel_biomass_mw"] > 0
    assert _aggregate_mix({}) is None


def test_every_mix_area_has_iso_and_url():
    assert len(_ERIA_AREAS) == 10
    for code, (iso, name, url) in _ERIA_AREAS.items():
        assert iso and name
        assert url.startswith("https://")
        assert "{ym}" in url or "{ymd}" in url
    # juyo/mix ISO codes stay consistent where both exist
    for code in ("tepco", "chubu", "kyushu", "hokuriku", "shikoku", "okinawa"):
        assert _ERIA_AREAS[code][0] == _TSOS[code][0]
