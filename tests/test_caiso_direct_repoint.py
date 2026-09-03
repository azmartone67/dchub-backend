"""tests/test_caiso_direct_repoint.py — the first repoint off the budget allowlist.

The 2026-07-26 cut was right (gridstatus free tier = 250 req/MONTH, July burned
375) and its note said each parked dataset "[has] a free direct source we
already hold creds for". This is the first of those repoints, and it costs ZERO
new upstream calls: CAISO's Today's Outlook CSVs are public, unauthenticated,
and iso_grid_adapters.fetch_caiso ALREADY downloads both of them.

    demand.csv      Time, Day ahead forecast, Hour ahead forecast,
                    Current demand, Demand response      -> caiso_load_forecast
    fuelsource.csv  Time + 13 fuel columns               -> caiso_fuel_mix

Verified live 2026-09-03: day-ahead 26,792 MW / hour-ahead 26,355 MW, and a
13-column fuel row. The data was already on the wire and being thrown away.

Effect on the standing finding, which is the point:

    before   registry 21 | allowlist 4 | direct 0 | parked 18 | reachable 3
    after    registry 21 | allowlist 4 | direct 2 | parked 16 | reachable 5

★ THE COUNT MOVES BY ARITHMETIC, NOT BY INTENT. parked_datasets() subtracts
  _DIRECT_SOURCES, so every future repoint shrinks the finding without anyone
  editing it — and the finding clears itself entirely when the last one lands.

★ THE TIMEZONE IS THE RISK, NOT THE PARSING. The CSVs carry a time with NO
  DATE. This repo runs UTC in prod and UTC-7 locally, so a naive read places
  every row up to seven hours off and dedups against the wrong slot — silently,
  because the row still writes. _caiso_asof anchors on America/Los_Angeles and
  is tested at both a normal slot and the midnight rollover.

House rules: no DB, no network, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_caiso_direct_repoint.py -v
"""
from __future__ import annotations

import datetime as dt
import inspect

import pytest

from routes import grid_data_master_shell as g

UTC = dt.timezone.utc

DEMAND = [
    {"Time": "01:55", "Day ahead forecast": "26000", "Hour ahead forecast": "25900",
     "Current demand": "25880", "Demand response": ""},
    {"Time": "02:00", "Day ahead forecast": "26100", "Hour ahead forecast": "26050",
     "Current demand": "26010", "Demand response": ""},
    # future slots already carry a forecast — taking the LAST row would stamp
    # a timestamp hours ahead of the clock
    {"Time": "14:00", "Day ahead forecast": "31000", "Hour ahead forecast": "",
     "Current demand": "", "Demand response": ""},
    {"Time": "23:55", "Day ahead forecast": "27000", "Hour ahead forecast": "",
     "Current demand": "", "Demand response": ""},
]
FUEL = [
    {"Time": "01:55", "Solar": "-30", "Wind": "5300", "Natural Gas": "4900",
     "Nuclear": "2234", "Imports": "7710"},
    {"Time": "02:00", "Solar": "-29", "Wind": "5335", "Natural Gas": "4941",
     "Nuclear": "2234", "Imports": "7700"},
]
# 02:05 Pacific Daylight = 09:05 UTC
NOW = dt.datetime(2026, 9, 3, 9, 5, tzinfo=UTC)


@pytest.fixture
def caiso(monkeypatch):
    def _csv(url, timeout=8):
        return DEMAND if url.endswith("demand.csv") else FUEL
    monkeypatch.setattr(g, "_http_csv", _csv)
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    monkeypatch.setattr(g, "_caiso_asof",
                        lambda hhmm, now=None: _real_asof(hhmm, now or NOW))


_real_asof = g._caiso_asof


# ── the timezone, which is the actual risk ───────────────────────────────

@pytest.mark.parametrize("hhmm,expect_utc", [
    ("02:00", dt.datetime(2026, 9, 3, 9, 0, tzinfo=UTC)),   # PDT = UTC-7
    ("14:00", dt.datetime(2026, 9, 3, 21, 0, tzinfo=UTC)),
    ("00:00", dt.datetime(2026, 9, 3, 7, 0, tzinfo=UTC)),
])
def test_a_pacific_slot_becomes_the_right_utc_instant(hhmm, expect_utc):
    assert _real_asof(hhmm, NOW) == expect_utc


def test_the_date_is_always_todays_pacific_day():
    """These are "Today's Outlook" CSVs — one Pacific day per file, reset at
    local midnight — so a row is never yesterday's and the date needs no
    adjustment.

    ★ An earlier draft subtracted a day from any slot >6h ahead, reading a
      future slot as a midnight rollover. That moved every afternoon forecast
      back a day. Future slots are excluded by the PICKER, not by the clock."""
    late = dt.datetime(2026, 9, 4, 6, 58, tzinfo=UTC)      # 23:58 PT on 09-03
    assert _real_asof("00:05", late) == dt.datetime(2026, 9, 3, 7, 5, tzinfo=UTC)
    assert _real_asof("23:55", late) == dt.datetime(2026, 9, 4, 6, 55, tzinfo=UTC)


@pytest.mark.parametrize("bad", ["", "nope", "25:99", None, "1", ":"])
def test_an_unparseable_slot_is_None_not_a_guess(bad):
    assert _real_asof(bad, NOW) is None


def test_it_anchors_on_pacific_not_the_server_clock():
    src = inspect.getsource(g._caiso_asof)
    assert "America/Los_Angeles" in src or "_CAISO_TZ" in src
    assert "ZoneInfo" in src


# ── load forecast ────────────────────────────────────────────────────────

def test_load_forecast_takes_the_newest_slot_AT_OR_BEFORE_now(caiso):
    """Not the last row. The file spans a whole Pacific day and its later rows
    are future slots that already carry a forecast."""
    out = g._caiso_load_forecast({"id": "caiso_load_forecast"})
    assert out["ok"] and out["primary_value"] == 26100.0
    assert out["as_of"] == dt.datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
    assert out["as_of"] <= NOW


def test_both_forecasts_are_kept_as_two_facts(caiso):
    raw = g._caiso_load_forecast({"id": "caiso_load_forecast"})["raw"]
    assert raw["day_ahead_forecast_mw"] == 26100.0
    assert raw["hour_ahead_forecast_mw"] == 26050.0
    assert "caiso.com" in raw["source_url"]


def test_load_forecast_fails_soft_on_an_empty_feed(monkeypatch):
    monkeypatch.setattr(g, "_http_csv", lambda *a, **k: [])
    out = g._caiso_load_forecast({"id": "caiso_load_forecast"})
    assert out["ok"] is False and "unavailable" in out["error"]


def test_no_forecast_at_or_before_now_is_a_refusal_not_a_future_row(monkeypatch):
    monkeypatch.setattr(g, "_http_csv", lambda *a, **k: [
        {"Time": "23:55", "Day ahead forecast": "27000"}])
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    monkeypatch.setattr(g, "_caiso_asof", lambda h, now=None: _real_asof(h, NOW))
    out = g._caiso_load_forecast({"id": "caiso_load_forecast"})
    assert out["ok"] is False, "a future-only file must not be written as now"
    assert "at_or_before_now" in out["error"]


# ── fuel mix ─────────────────────────────────────────────────────────────

def test_fuel_mix_totals_every_fuel_and_keeps_the_breakdown(caiso):
    out = g._caiso_fuel_mix({"id": "caiso_fuel_mix"})
    assert out["ok"]
    assert out["primary_value"] == round(-29 + 5335 + 4941 + 2234 + 7700, 1)
    assert out["raw"]["fuel_mw"]["Wind"] == 5335.0
    assert len(out["raw"]["fuel_mw"]) == 5


def test_negative_solar_is_kept_not_clamped(caiso):
    """Solar goes negative on this feed at night. Clamping would quietly
    inflate the total."""
    out = g._caiso_fuel_mix({"id": "caiso_fuel_mix"})
    assert out["raw"]["fuel_mw"]["Solar"] == -29.0


def test_fuel_mix_fails_soft(monkeypatch):
    monkeypatch.setattr(g, "_http_csv", lambda *a, **k: [{"Time": "02:00"}])
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    out = g._caiso_fuel_mix({"id": "caiso_fuel_mix"})
    assert out["ok"] is False


# ── routing + provenance ─────────────────────────────────────────────────

def test_a_repointed_dataset_routes_direct_not_to_gridstatus(monkeypatch):
    seen = {}
    monkeypatch.setattr(g, "_ingest_direct", lambda e: seen.setdefault("direct", e["id"]))
    monkeypatch.setattr(g, "_ingest_gridstatus_dataset",
                        lambda e: seen.setdefault("gridstatus", e["id"]))
    g._ingest_dataset({"id": "caiso_fuel_mix"})
    assert seen == {"direct": "caiso_fuel_mix"}, seen


def test_everything_else_still_routes_to_gridstatus(monkeypatch):
    seen = {}
    monkeypatch.setattr(g, "_ingest_direct", lambda e: seen.setdefault("direct", e["id"]))
    monkeypatch.setattr(g, "_ingest_gridstatus_dataset",
                        lambda e: seen.setdefault("gridstatus", e["id"]))
    g._ingest_dataset({"id": "pjm_fuel_mix"})
    assert seen == {"gridstatus": "pjm_fuel_mix"}, seen


def test_direct_rows_stamp_their_own_source_never_gridstatus():
    """★ A row that names the wrong upstream is how a feed gets 'repointed' on
    paper and audited as still-gridstatus."""
    src = inspect.getsource(g._ingest_direct)
    assert "'gridstatus'" not in src and '"gridstatus"' not in src
    assert "VALUES (%s, %s" in src, "source must be a bound param, not a literal"
    for _, (label, _fn) in [(k, v) for k, v in g._DIRECT_SOURCES.items()]:
        assert label and label != "gridstatus"


def test_the_tick_routes_through_the_dispatcher():
    src = inspect.getsource(g)
    assert "res = _ingest_dataset(target)" in src
    assert "res = _ingest_gridstatus_dataset(target)" not in src


# ── the finding shrinks by arithmetic ────────────────────────────────────

def test_repointed_datasets_are_no_longer_parked():
    parked = {t["id"] for t in g.parked_datasets()}
    for did in g._DIRECT_SOURCES:
        assert did not in parked, "%s is repointed but still counted parked" % did


def test_the_parked_count_is_registry_minus_allowlist_minus_direct():
    ids = {t["id"] for t in g.TARGET_DATASETS}
    expected = ids - set(g._GS_ALLOWLIST) - set(g._DIRECT_SOURCES)
    assert {t["id"] for t in g.parked_datasets()} == expected


def test_the_finding_reports_the_repointed_ones(monkeypatch):
    _, detail = g._parked_finding()
    assert "repointed to a free direct source" in detail
    for did in g._DIRECT_SOURCES:
        assert did in detail


def test_the_finding_clears_when_every_dataset_is_repointed(monkeypatch):
    """★ It must be able to go away — that is what makes progress arithmetic."""
    monkeypatch.setattr(g, "_DIRECT_SOURCES",
                        {t["id"]: ("x", None) for t in g.TARGET_DATASETS})
    assert g.parked_datasets() == []
    assert g._parked_finding() is None


def test_caiso_costs_no_new_upstream_calls():
    """Both datasets come from the two CSVs iso_grid_adapters.fetch_caiso
    already downloads — that is why CAISO was the right first repoint."""
    src = inspect.getsource(g)
    assert "outlook/current" in src
    import iso_grid_adapters as a
    existing = inspect.getsource(a.fetch_caiso)
    assert "fuelsource.csv" in existing and "demand.csv" in existing
