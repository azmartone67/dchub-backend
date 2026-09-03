"""tests/test_ercot_direct_repoint.py — five datasets off three keyless dashboards.

The second repoint. ERCOT's authenticated Azure-APIM feed
(iso_grid_adapters.fetch_ercot, OAuth + Ocp-Apim-Subscription-Key) is untouched
— it still serves the real-time gen/load record. These five parked datasets
need none of it, because ERCOT publishes them on public dashboard JSON:

    supply-demand.json   forecast[].forecastedDemand  -> ercot_load_forecast
                         forecast[].availCapGen       -> ercot_capacity_forecast
                         data[] where forecast==0     -> ercot_capacity_committed
    daily-prc.json       data[].prc                   -> ercot_real_time_adders…
    fuel-mix.json        data[date][ts][fuel].gen     -> ercot_fuel_mix_detailed

Probed live 2026-09-03 — 200 / 84KB, 191KB, 107KB. Verified end to end the same
day: committed 82,454 MW · capacity forecast 104,866 MW · fuel mix 60,528 MW
across 8 fuels · load forecast 64,244 MW · PRC 13,885 MW.

    before   direct 2 | parked 16 | reachable 5
    after    direct 7 | parked 11 | reachable 10

★ NO TIMEZONE INFERENCE, unlike CAISO. Every ERCOT timestamp carries an
  explicit offset, so the instant is READ, never reconstructed from the server
  clock — `_ercot_ts` refuses a naive string rather than guessing a zone.

★ as_of FOLLOWS WHAT THE NUMBER IS. An observation carries its own interval; a
  forecast carries its PUBLICATION time, never the future hour it describes.
  Stamping a forecast at its target hour writes rows ahead of the clock.

★ THE `forecast` FIELD IS A 0/1 FLAG, NOT A VALUE. supply-demand.data[] mixes
  actual and projected rows; reading committed capacity without filtering it
  would report tomorrow's projection as today's committed capacity.

House rules: no DB, no network, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_ercot_direct_repoint.py -v
"""
from __future__ import annotations

import datetime as dt
import inspect

import pytest

from routes import grid_data_master_shell as g

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 3, 10, 0, tzinfo=UTC)     # 05:00 CDT

SUPPLY = {
    "lastUpdated": "2026-09-03 05:00:00-0500",       # == 10:00 UTC
    "data": [
        {"capacity": 86551, "demand": 68602, "forecast": 0,
         "hourEnding": 3, "timestamp": "2026-09-03 03:00:00-0500"},
        {"capacity": 82454, "demand": 66000, "forecast": 0,
         "hourEnding": 5, "timestamp": "2026-09-03 05:00:00-0500"},
        # a PROJECTED row — newer, and must never be read as committed
        {"capacity": 95188, "demand": 67296, "forecast": 1,
         "hourEnding": 24, "timestamp": "2026-09-04 00:00:00-0500"},
    ],
    "forecast": [
        {"hourEnding": 1, "availCapGen": 104866, "forecastedDemand": 64244,
         "timestamp": "2026-09-04 00:00:00-0500"},
        {"hourEnding": 2, "availCapGen": 103000, "forecastedDemand": 63000,
         "timestamp": "2026-09-04 01:00:00-0500"},
    ],
}
PRC = {
    "lastUpdated": "2026-09-03 05:03:34-0500",
    "current_condition": {"eea_level": 0, "state": "normal", "prc_value": 13885},
    "data": [
        {"timestamp": "2026-09-03 05:00:02-0500", "interval": "05:00:02", "prc": 7270},
        {"timestamp": "2026-09-03 05:03:34-0500", "interval": "05:03:34", "prc": 13885},
    ],
}
FUEL = {
    "lastUpdated": "2026-09-03 05:00:00-0500",
    "data": {
        "2026-09-02": {"2026-09-02 23:55:00-0500": {"Wind": {"gen": 1.0}}},
        "2026-09-03": {
            "2026-09-03 04:55:00-0500": {"Wind": {"gen": 100.0}},
            "2026-09-03 05:00:00-0500": {
                "Wind": {"gen": 20000.5}, "Natural Gas": {"gen": 30000.0},
                "Nuclear": {"gen": 4959.5}, "Power Storage": {"gen": -1334.0},
            },
        },
    },
}


@pytest.fixture
def ercot(monkeypatch):
    def _json(url, timeout=10):
        if url.endswith("supply-demand.json"): return SUPPLY
        if url.endswith("daily-prc.json"):     return PRC
        if url.endswith("fuel-mix.json"):      return FUEL
        return None
    monkeypatch.setattr(g, "_http_json", _json)
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)


# ── timestamps are read, never inferred ──────────────────────────────────

@pytest.mark.parametrize("s,expect", [
    ("2026-09-03 05:00:00-0500", dt.datetime(2026, 9, 3, 10, 0, tzinfo=UTC)),
    ("2026-09-03 04:47:14-0500", dt.datetime(2026, 9, 3, 9, 47, 14, tzinfo=UTC)),
    ("2026-09-04 00:00:00-0500", dt.datetime(2026, 9, 4, 5, 0, tzinfo=UTC)),
])
def test_an_offset_bearing_timestamp_is_read_exactly(s, expect):
    assert g._ercot_ts(s) == expect


@pytest.mark.parametrize("bad", [
    "2026-09-03 05:00:00",      # naive — NOT an instant, must not be guessed
    "", None, "nope", "2026-09-03",
])
def test_a_timestamp_without_an_offset_is_refused(bad):
    """★ Refusing beats guessing. A naive string read in the server's zone is
    the CAISO class of bug, and ERCOT gives us the offset so we never need to."""
    assert g._ercot_ts(bad) is None


# ── forecasts vs observations ────────────────────────────────────────────

def test_load_forecast_value_and_target_hour(ercot):
    out = g._ercot_load_forecast({"id": "ercot_load_forecast"})
    assert out["ok"] and out["primary_value"] == 64244.0
    assert out["raw"]["for_hour_utc"].startswith("2026-09-04 05:00")


def test_capacity_forecast_reads_avail_cap_gen(ercot):
    out = g._ercot_capacity_forecast({"id": "ercot_capacity_forecast"})
    assert out["ok"] and out["primary_value"] == 104866.0


@pytest.mark.parametrize("fn,did", [
    ("_ercot_load_forecast", "ercot_load_forecast"),
    ("_ercot_capacity_forecast", "ercot_capacity_forecast"),
])
def test_a_forecast_is_stamped_when_PUBLISHED_not_when_it_applies(ercot, fn, did):
    """★ Stamping a forecast at its target hour writes rows ahead of the clock
    and makes every freshness reader argue with itself."""
    out = getattr(g, fn)({"id": did})
    assert out["as_of"] == NOW, out["as_of"]
    assert out["as_of"] <= NOW
    assert out["raw"]["for_hour_utc"] > str(NOW)


def test_committed_capacity_ignores_the_forecast_FLAG_rows(ercot):
    """★ `forecast` is 0/1, not a value. The newest row in data[] is a
    PROJECTION — reading it would report tomorrow as today's committed."""
    out = g._ercot_capacity_committed({"id": "ercot_capacity_committed"})
    assert out["ok"]
    assert out["primary_value"] == 82454.0, "took a forecast row"
    assert out["primary_value"] != 95188.0
    assert out["as_of"] == NOW and out["raw"]["is_forecast_row"] is False


def test_an_observation_carries_its_own_interval(ercot):
    out = g._ercot_reserves({"id": "ercot_real_time_adders_and_reserves"})
    assert out["as_of"] == dt.datetime(2026, 9, 3, 10, 3, 34, tzinfo=UTC)


# ── reserves + fuel mix ──────────────────────────────────────────────────

def test_reserves_reads_prc_the_column_the_registry_declares(ercot):
    out = g._ercot_reserves({"id": "ercot_real_time_adders_and_reserves"})
    assert out["ok"] and out["primary_value"] == 13885.0
    entry = next(t for t in g.TARGET_DATASETS
                 if t["id"] == "ercot_real_time_adders_and_reserves")
    assert entry["value_col"] == "prc" and "prc_mw" in out["raw"]


def test_fuel_mix_takes_the_newest_interval_across_dates(ercot):
    out = g._ercot_fuel_mix_detailed({"id": "ercot_fuel_mix_detailed"})
    assert out["ok"] and len(out["raw"]["fuel_mw"]) == 4
    assert out["as_of"] == NOW


def test_negative_power_storage_is_kept_not_clamped(ercot):
    """Storage goes negative while charging. Clamping inflates the total —
    the same trap as CAISO's night-time solar."""
    out = g._ercot_fuel_mix_detailed({"id": "ercot_fuel_mix_detailed"})
    assert out["raw"]["fuel_mw"]["Power Storage"] == -1334.0
    assert out["primary_value"] == round(20000.5 + 30000.0 + 4959.5 - 1334.0, 1)


# ── fail-soft ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fn", [
    "_ercot_load_forecast", "_ercot_capacity_forecast", "_ercot_capacity_committed",
    "_ercot_reserves", "_ercot_fuel_mix_detailed",
])
def test_every_fetcher_fails_soft_when_the_dashboard_is_down(monkeypatch, fn):
    monkeypatch.setattr(g, "_http_json", lambda *a, **k: None)
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    out = getattr(g, fn)({"id": "x"})
    assert out["ok"] is False and out["error"]


def test_a_dashboard_with_no_usable_rows_is_a_refusal(monkeypatch):
    monkeypatch.setattr(g, "_http_json",
                        lambda *a, **k: {"lastUpdated": "2026-09-03 05:00:00-0500",
                                         "data": [], "forecast": []})
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    assert g._ercot_capacity_committed({"id": "x"})["ok"] is False
    assert g._ercot_load_forecast({"id": "x"})["ok"] is False


# ── wiring + the count ───────────────────────────────────────────────────

@pytest.mark.parametrize("did", [
    "ercot_load_forecast", "ercot_capacity_forecast", "ercot_capacity_committed",
    "ercot_real_time_adders_and_reserves", "ercot_fuel_mix_detailed",
])
def test_each_ercot_dataset_is_registered_direct_and_no_longer_parked(did):
    assert did in g._DIRECT_SOURCES
    label, _fn = g._DIRECT_SOURCES[did]
    assert label == "ercot_dashboard" and label != "gridstatus"
    assert did not in {t["id"] for t in g.parked_datasets()}


def test_all_five_ercot_datasets_are_covered():
    ercot_ids = {t["id"] for t in g.TARGET_DATASETS if t["iso"] == "ERCOT"}
    assert ercot_ids <= set(g._DIRECT_SOURCES), sorted(ercot_ids - set(g._DIRECT_SOURCES))
    assert not [t for t in g.parked_datasets() if t["iso"] == "ERCOT"]


def test_the_authenticated_ercot_adapter_is_untouched():
    """This repoint must not disturb the OAuth feed that serves the real-time
    gen/load record — it is a different dataset from a different endpoint."""
    import iso_grid_adapters as a
    src = inspect.getsource(a.fetch_ercot)
    assert "_ercot_headers()" in src
    assert "ercot.com/api/1/services/read/dashboards" not in src


def test_the_dashboards_used_are_the_three_probed_keyless_ones():
    """★ Keyed on CODE, not raw text. The first draft asserted
    "todays-outlook.json" not in the source and failed on its OWN comment
    explaining why that endpoint is unused — the guard-writing trap this repo
    has now hit four times. String literals come from the AST."""
    import ast

    tree = ast.parse(inspect.getsource(g))
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    used = {v for v in literals if v.endswith(".json")}
    assert {"supply-demand.json", "daily-prc.json", "fuel-mix.json"} <= used
    assert "todays-outlook.json" not in used, "that endpoint 403s to non-browsers"


# Newest interval deliberately NOT last in iteration order, and on the EARLIER
# date key — so "take the last one iterated" and "take the newest" disagree.
FUEL_OUT_OF_ORDER = {
    "lastUpdated": "2026-09-03 05:00:00-0500",
    "data": {
        "2026-09-03": {
            "2026-09-03 05:00:00-0500": {"Wind": {"gen": 999.0}},   # NEWEST, first
            "2026-09-03 04:55:00-0500": {"Wind": {"gen": 111.0}},
        },
        "2026-09-02": {"2026-09-02 23:55:00-0500": {"Wind": {"gen": 222.0}}},  # last
    },
}


def test_fuel_mix_takes_the_NEWEST_interval_not_the_last_iterated(monkeypatch):
    """★ REGRESSION GUARD. The original fixture listed the newest interval last,
    so `if True` (take whatever came last) scored identically to `t > best_ts`
    and the mutation survived. Dict order is not time order — a day key can be
    iterated after a newer one, and JSON gives no ordering promise at all."""
    monkeypatch.setattr(g, "_http_json", lambda *a, **k: FUEL_OUT_OF_ORDER)
    monkeypatch.setattr(g, "_utcnow", lambda: NOW)
    out = g._ercot_fuel_mix_detailed({"id": "ercot_fuel_mix_detailed"})
    assert out["ok"]
    assert out["primary_value"] == 999.0, (
        "took %s — that is the last interval iterated, not the newest"
        % out["primary_value"])
    assert out["as_of"] == dt.datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
