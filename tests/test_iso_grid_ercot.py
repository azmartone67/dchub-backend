"""fetch_ercot extraction — pure-function tests (no network, no Flask app,
never imports main.py).

Payload fixtures are shape-verbatim captures from
api.ercot.com/api/public-reports on 2026-07-16:
  np6-625-cd/se_ld_rpt_ercot_gen      → seExeTime + seMW (sorted DESC)
  np6-235-cd/system_wide_demand       → deliveryDate/timeEnding/demand
                                        (sorted date DESC, time ASC!)
  np4-733-cd/wpp_actual_5min_avg_values → intervalEnding + genSystemWide
Locks in: latest-row selection that does NOT trust server sort order, the
dead-feed staleness guard, and the end-to-end record shape fetch_ercot emits.
"""
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import iso_grid_adapters as iga


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


GEN_PAYLOAD = {
    "fields": [{"name": "seExeTime"}, {"name": "seExeTimeDST"},
               {"name": "seMW"}, {"name": "seMVAR"},
               {"name": "scadaMW"}, {"name": "scadaMVAR"}],
    # deliberately NOT newest-first — the helper must not trust sort order
    "data": [
        ["2026-07-16T09:07:11", "d", 64859.66, 4132.47, 64752.89, "3594.11"],
        ["2026-07-16T10:07:11", "d", 68410.02, 4942.59, 68640.02, "4401.14"],
        ["2026-07-16T08:07:11", "d", 62119.40, 3357.08, 62253.68, "2946.00"],
    ],
}

WIND_PAYLOAD = {
    "fields": [{"name": "postedDatetime"}, {"name": "intervalEnding"},
               {"name": "genSystemWide"}, {"name": "LZSouthHouston"},
               {"name": "LZWest"}, {"name": "LZNorth"},
               {"name": "HSLSystemWide"}, {"name": "DSTFlag"}],
    "data": [
        ["2026-07-16T10:20:31", "2026-07-16T10:10:00", 11048.38, 5698.66, 4164.52, 1185.2, 11301.38, False],
        ["2026-07-16T10:20:31", "2026-07-16T10:15:00", 11143.75, 5753.72, 4195.54, 1194.49, 11389.49, False],
    ],
}

SOLAR_PAYLOAD = {
    "fields": [{"name": "postedDatetime"}, {"name": "intervalEnding"},
               {"name": "genSystemWide"}, {"name": "HSLSystemWide"},
               {"name": "DSTFlag"}],
    "data": [
        ["2026-07-16T10:20:14", "2026-07-16T10:15:00", 14829.69, 15013.82, False],
        ["2026-07-16T10:20:14", "2026-07-16T10:10:00", 14265.60, 14529.84, False],
    ],
}


def _demand_payload(date: str) -> dict:
    return {
        "fields": [{"name": "deliveryDate"}, {"name": "timeEnding"},
                   {"name": "demand"}, {"name": "DSTFlag"}],
        # date DESC but time ASC within the day — the live server's actual sort
        "data": [
            [date, "00:00", 56888.25, False],
            [date, "10:00", 59558.00, False],
            [date, "09:45", 59102.50, False],
        ],
    }


# ── _ercot_table ─────────────────────────────────────────────────────

def test_table_zips_fields_and_data():
    rows = iga._ercot_table(GEN_PAYLOAD)
    assert len(rows) == 3
    assert rows[1]["seExeTime"] == "2026-07-16T10:07:11"
    assert rows[1]["seMW"] == 68410.02


def test_table_tolerates_garbage():
    assert iga._ercot_table(None) == []
    assert iga._ercot_table({"fields": [], "data": [[1]]}) == []
    assert iga._ercot_table({"fields": [{"name": "a"}], "data": ["junk", [1]]}) == [{"a": 1}]


# ── latest-row selection (never trusts server sort) ──────────────────

def test_latest_gen_picks_max_se_exe_time():
    assert iga._ercot_latest_gen_mw(iga._ercot_table(GEN_PAYLOAD)) == 68410.02


def test_latest_demand_picks_max_date_time_pair():
    rows = iga._ercot_table(_demand_payload(_today()))
    assert iga._ercot_latest_demand_mw(rows) == 59558.00


def test_latest_demand_rejects_stale_feed():
    """A feed frozen for days must degrade to None (modeled fallback),
    never masquerade as live."""
    rows = iga._ercot_table(_demand_payload("2026-01-01"))
    assert iga._ercot_latest_demand_mw(rows) is None


def test_latest_syswide_picks_max_interval():
    assert iga._ercot_latest_syswide_mw(iga._ercot_table(WIND_PAYLOAD)) == 11143.75
    assert iga._ercot_latest_syswide_mw(iga._ercot_table(SOLAR_PAYLOAD)) == 14829.69


# ── fetch_ercot end-to-end (HTTP + bearer stubbed) ───────────────────

def _detail(pid: str) -> dict:
    return {"artifacts": [{"_links": {"endpoint": {
        "href": f"https://api.ercot.com/api/public-reports/{pid}/data"}}}]}


def _fake_http_json(demand_date: str):
    base = iga.ISO_REGISTRY["ERCOT"]["base"]
    payloads = {
        f"{base}/np6-625-cd": _detail("np6-625-cd"),
        f"{base}/np6-235-cd": _detail("np6-235-cd"),
        f"{base}/np4-733-cd": _detail("np4-733-cd"),
        f"{base}/np4-738-cd": _detail("np4-738-cd"),
        "https://api.ercot.com/api/public-reports/np6-625-cd/data": GEN_PAYLOAD,
        "https://api.ercot.com/api/public-reports/np6-235-cd/data": _demand_payload(demand_date),
        "https://api.ercot.com/api/public-reports/np4-733-cd/data": WIND_PAYLOAD,
        "https://api.ercot.com/api/public-reports/np4-738-cd/data": SOLAR_PAYLOAD,
    }

    def fake(url, headers=None, timeout=20):
        assert headers and headers.get("Ocp-Apim-Subscription-Key") == "test-key"
        return payloads[url.split("?")[0]]

    return fake


def test_fetch_ercot_emits_normalized_record(monkeypatch):
    monkeypatch.setenv("ERCOT_API_KEY", "test-key")
    monkeypatch.delenv("ERCOT_GEN_PRODUCT_ID", raising=False)
    monkeypatch.delenv("ERCOT_LOAD_PRODUCT_ID", raising=False)
    monkeypatch.setattr(iga, "_ERCOT_FUEL_SPACING_S", 0)
    monkeypatch.setattr(iga, "_ercot_bearer", lambda: "tok")
    monkeypatch.setattr(iga, "_http_json", _fake_http_json(_today()))
    recs = iga.fetch_ercot()
    assert len(recs) == 1
    r = recs[0]
    assert r["iso"] == "ERCOT" and r["zone"] == "ERCOT"
    assert r["online_gen_mw"] == 68410.0
    assert r["load_mw"] == 59558.0
    assert r["headroom_mw"] == 68410.0 - 59558.0
    assert r["fuel_mix"] == {"Wind": 11143.8, "Solar": 14829.7}
    assert "np6-625-cd" in r["source"] and "np6-235-cd" in r["source"]
    assert not r.get("source_unavailable")


def test_fetch_ercot_stale_demand_returns_empty(monkeypatch):
    """gen fresh + demand frozen → no record at all (no partial fabrication)."""
    monkeypatch.setenv("ERCOT_API_KEY", "test-key")
    monkeypatch.setattr(iga, "_ERCOT_FUEL_SPACING_S", 0)
    monkeypatch.setattr(iga, "_ercot_bearer", lambda: "tok")
    monkeypatch.setattr(iga, "_http_json", _fake_http_json("2026-01-01"))
    assert iga.fetch_ercot() == []


def test_fetch_ercot_no_key_returns_empty(monkeypatch):
    monkeypatch.delenv("ERCOT_API_KEY", raising=False)
    assert iga.fetch_ercot() == []
