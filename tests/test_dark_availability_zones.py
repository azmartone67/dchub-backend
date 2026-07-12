"""routes/dark_availability_zones.py test suite (Gemini dark-fiber §4.3,
2026-07-11).

All mocked (no DB, no network, never imports main). Contract under test:
  1. carrier name canonicalization — exact + ASN/region-prefix matches, and
     the deliberate NON-matches (PDB "Bandwidth" is not Bandwidth IG, PDB
     "Windstream Communication Limited" is not Windstream Wholesale)
  2. bucketing (strong >=3 / moderate 1-2 / none 0)
  3. the point screen (dark_screen_from_carriers) — shape + honesty rails
  4. blank-state/country group merging is coordinate-checked (Paris, TX
     never merges into a far-away Paris)
  5. zone-row derivation — every row's notes stamped v:"inferred" with the
     method named
  6. metro matching + summary shapes (full + compact), fail-soft on
     empty/missing zone data
  7. rebuild job — kill switch short-circuits before any DB touch;
     <20h-fresh self-throttle skips; a full fake-cursor build writes the
     merged zones idempotently (ON CONFLICT upsert SQL)
  8. wiring — connectivity_score carries dark_screen; cron_heartbeat has
     the dark_zones_rebuild_daily entry (quiet hour, kill switch, HEAVY)
"""
import datetime as _dt
import json

import pytest

import routes.dark_availability_zones as daz


# ── 1. name canonicalization ─────────────────────────────────────────
@pytest.mark.parametrize("raw,canon", [
    ("Zayo", "Zayo"),
    ("Zayo Europe", "Zayo"),
    ("Cogent Communications, Inc.", "Cogent Communications"),
    ("Level3 Carrier", "Lumen (Level 3)"),
    ("GTT Communications (AS3257)", "GTT Communications"),
    ("GTT Communications (AS260)", "GTT Communications"),
    ("Consolidated Communications - NNE", "Consolidated Communications"),
    ("Arelion (Twelve99)", "Arelion (ex-Telia Carrier)"),
    ("Telia International Network", "Arelion (ex-Telia Carrier)"),
    ("Crown Castle", "Crown Castle Fiber"),
    ("NTT Global IP Network", "NTT Communications"),
    ("Everstream Solutions LLC", "Everstream"),
    ("PacketFabric", "PacketFabric"),
])
def test_canonical_matches(raw, canon):
    assert daz.canonical_dark_carrier(raw) == canon


@pytest.mark.parametrize("raw", [
    # deliberate non-matches — wrong-company traps documented in the module
    "Bandwidth",                          # Bandwidth.com, not Bandwidth IG
    "Windstream Communication Limited",   # Bangladeshi ISP, not Windstream
    "Summit",                             # ambiguous across >=4 companies
    "NTT DATA Services - HCLS Cloud",     # not the carrier arm
    "Teliax, Inc",                        # not Telia
    "Stelia",                             # substring trap
    "Equinix Fabric",                     # dark_fiber=FALSE in fiber_providers
    "", None, 42,
])
def test_canonical_non_matches(raw):
    assert daz.canonical_dark_carrier(raw) is None


# ── 2. bucketing ──────────────────────────────────────────────────────
@pytest.mark.parametrize("n,level", [
    (0, "none"), (1, "moderate"), (2, "moderate"), (3, "strong"),
    (10, "strong"), (None, "none"), ("junk", "none"),
])
def test_bucket_level(n, level):
    assert daz.bucket_level(n) == level


# ── 3. the point screen ───────────────────────────────────────────────
def test_dark_screen_shape_and_honesty():
    ds = daz.dark_screen_from_carriers(
        ["Zayo", "Zayo Europe", "GTT Communications (AS3257)",
         "Cogent Communications, Inc.", "Some Random ISP", "Bandwidth"])
    assert ds["dark_capable_carriers"] == 3          # Zayo deduped
    assert ds["carriers"] == ["Cogent Communications", "GTT Communications",
                              "Zayo"]
    assert ds["level"] == "strong"
    assert ds["v"] == "inferred"
    assert ds["basis"] == "capability x presence inference"
    assert "NOT confirmed strand availability" in ds["method"]
    assert "screening signal only" in ds["method"]


def test_dark_screen_none_level_is_the_answer():
    ds = daz.dark_screen_from_carriers(["Some Random ISP"])
    assert ds["level"] == "none"
    assert ds["dark_capable_carriers"] == 0
    assert ds["carriers"] == []
    assert ds["v"] == "inferred"
    # empty / junk input never raises
    assert daz.dark_screen_from_carriers([])["level"] == "none"
    assert daz.dark_screen_from_carriers(None)["level"] == "none"


# ── 4. coordinate-checked group merging ───────────────────────────────
def _g(city, state, country, fac, lat, lng, carriers):
    return {"city_key": city, "state": state, "country": country,
            "display_city": city.title(), "fac_count": fac,
            "lat": lat, "lng": lng, "carriers": set(carriers)}


def test_merge_blank_fields_into_nearby_richer_group():
    groups = [
        _g("ashburn", "VA", "US", 153, 39.04, -77.49, {"Zayo", "Segra"}),
        _g("ashburn", "", "US", 42, 39.02, -77.47, {"Zayo", "Lightpath"}),
    ]
    merged = daz.merge_city_groups(groups)
    assert len(merged) == 1
    z = merged[0]
    assert z["state"] == "VA"
    assert z["fac_count"] == 195
    assert z["carriers"] == {"Zayo", "Segra", "Lightpath"}


def test_merge_never_crosses_distance_paris_tx_vs_fr():
    groups = [
        _g("paris", "", "FR", 60, 48.85, 2.35, {"Zayo"}),      # Paris, FR
        _g("paris", "TX", "US", 3, 33.66, -95.55, {"Segra"}),  # Paris, TX
    ]
    merged = daz.merge_city_groups(groups)
    # the blank-state FR group is ~7,900 km from Paris TX — must NOT merge
    assert len(merged) == 2
    tx = [m for m in merged if m["state"] == "TX"][0]
    assert tx["carriers"] == {"Segra"}


def test_merge_orphan_with_no_target_survives_as_own_zone():
    groups = [_g("lonetown", "", "", 5, 40.0, -100.0, {"Zayo"})]
    merged = daz.merge_city_groups(groups)
    assert len(merged) == 1
    assert merged[0]["carriers"] == {"Zayo"}


# ── 5. zone-row derivation ────────────────────────────────────────────
def test_zone_row_stamped_inferred_with_method():
    g = _g("ashburn", "VA", "US", 195, 39.04, -77.49,
           {"Zayo", "Segra", "GTT Communications"})
    row = daz.zone_row_from_group(g, "2026-07-11T00:00:00+00:00")
    assert row["zone_id"] == "dark:us:va:ashburn"
    assert row["zone_name"] == "Ashburn, VA"
    assert row["zone_type"] == "dark_availability"
    assert row["provider_count"] == 3
    assert row["lit_building_count"] == 195
    assert row["dark_fiber_available"] is True
    assert row["carrier_list"] == "GTT Communications, Segra, Zayo"
    assert row["data_sources"].startswith("inferred:")
    notes = json.loads(row["notes"])
    assert notes["v"] == "inferred"
    assert notes["level"] == "strong"
    assert notes["presence_facility_count"] == 195
    assert "NOT confirmed strand availability" in notes["method"]
    assert "screening signal only" in notes["method"]


# ── 6. metro matching + summary shapes ────────────────────────────────
_FAKE_ZONES = [
    {"zone_name": "Ashburn, VA", "city_key": "ashburn", "state": "VA",
     "country": "US", "center_lat": 39.0, "center_lng": -77.5,
     "dark_capable_carrier_count": 4, "presence_facility_count": 195,
     "carriers": ["Zayo", "Segra", "GTT Communications", "Lightpath"]},
    {"zone_name": "Sterling, VA", "city_key": "sterling", "state": "VA",
     "country": "US", "center_lat": 39.0, "center_lng": -77.4,
     "dark_capable_carrier_count": 2, "presence_facility_count": 12,
     "carriers": ["Zayo", "Cogent Communications"]},
    {"zone_name": "Portland, OR", "city_key": "portland", "state": "OR",
     "country": "US", "center_lat": 45.5, "center_lng": -122.6,
     "dark_capable_carrier_count": 3, "presence_facility_count": 40,
     "carriers": ["Zayo", "Cogent Communications", "PacketFabric"]},
    {"zone_name": "Portland, ME", "city_key": "portland", "state": "ME",
     "country": "US", "center_lat": 43.7, "center_lng": -70.2,
     "dark_capable_carrier_count": 1, "presence_facility_count": 3,
     "carriers": ["FirstLight Fiber"]},
]


def test_zone_cities_for_market_defaults_and_multi_city():
    assert daz.zone_cities_for_market("Atlanta") == ["atlanta"]
    assert "ashburn" in daz.zone_cities_for_market("Northern Virginia")
    # the endpoint converts URL dashes to spaces before calling us
    assert "fort worth" in daz.zone_cities_for_market("dallas fort worth")
    assert daz.zone_cities_for_market("Dallas-Fort Worth") == \
        daz.zone_cities_for_market("dallas fort worth")
    assert daz.zone_cities_for_market("") == []


def test_match_zones_state_disambiguates_portland():
    got = daz.match_zones_to_market(_FAKE_ZONES, "Portland", state="OR")
    assert [z["zone_name"] for z in got] == ["Portland, OR"]
    got_me = daz.match_zones_to_market(_FAKE_ZONES, "Portland", state="ME")
    assert [z["zone_name"] for z in got_me] == ["Portland, ME"]


def test_match_zones_metro_map_and_sort():
    got = daz.match_zones_to_market(_FAKE_ZONES, "Northern Virginia",
                                    state="VA")
    # sorted by carrier count desc
    assert [z["zone_name"] for z in got] == ["Ashburn, VA", "Sterling, VA"]


def test_summarize_full_shape_and_honesty():
    matched = daz.match_zones_to_market(_FAKE_ZONES, "Northern Virginia",
                                        state="VA")
    s = daz.summarize_zones(matched)
    assert s["v"] == "inferred"
    assert "NOT confirmed strand availability" in s["method"]
    assert s["basis"] == "capability x presence inference"
    # union across zones: Zayo, Segra, GTT, Lightpath, Cogent = 5 -> strong
    assert s["dark_capable_carrier_count"] == 5
    assert s["level"] == "strong"
    assert s["presence_facility_count"] == 207
    assert len(s["zones"]) == 2
    for z in s["zones"]:
        assert z["v"] == "inferred"
        assert z["level"] in ("strong", "moderate")


def test_summarize_compact_shape():
    matched = daz.match_zones_to_market(_FAKE_ZONES, "Portland", state="ME")
    s = daz.summarize_zones(matched, compact=True)
    assert s == {"v": "inferred", "level": "moderate",
                 "dark_capable_carrier_count": 1}


def test_summarize_zone_list_capped_at_8():
    many = [dict(_FAKE_ZONES[0], zone_name=f"Z{i}", city_key="ashburn")
            for i in range(12)]
    s = daz.summarize_zones(many)
    assert len(s["zones"]) == 8


def test_summary_fail_soft(monkeypatch):
    assert daz.summarize_zones([]) is None
    # empty zone table -> None -> caller omits the field
    monkeypatch.setattr(daz, "_load_zones", lambda: [])
    assert daz.metro_dark_zone_summary("Atlanta", state="GA") is None
    # a raising loader NEVER propagates
    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(daz, "_load_zones", _boom)
    assert daz.metro_dark_zone_summary("Atlanta", state="GA") is None


# ── 7. the rebuild job ────────────────────────────────────────────────
class _Cur:
    """SQL-fragment-routed fake cursor (metric_truth test convention)."""
    def __init__(self, presence_rows, max_updated=None):
        self.presence_rows = presence_rows
        self.max_updated = max_updated
        self.log = []
        self._last = ""
        self.rowcount = 0
        self.connection = self

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        self._last = sql
        if "DELETE FROM fiber_coverage_zones" in sql:
            self.rowcount = 1

    def fetchone(self):
        if "MAX(updated_at)" in self._last:
            return (self.max_updated,)
        return None

    def fetchall(self):
        if "FROM carrier_facility_presence" in self._last:
            return self.presence_rows
        return []

    def rollback(self):
        pass


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


_NOW = _dt.datetime(2026, 7, 11, 1, 30, tzinfo=_dt.timezone.utc)


def test_rebuild_kill_switch_touches_no_db(monkeypatch):
    monkeypatch.setenv("DARK_ZONES_DISABLE", "1")

    def _no_db():
        raise AssertionError("kill switch must short-circuit before DB")
    monkeypatch.setattr(daz, "_conn", _no_db)
    out = daz.rebuild_zones()
    assert out == {"ok": True, "skipped": "DARK_ZONES_DISABLE"}


def test_rebuild_self_throttles_when_fresh(monkeypatch):
    cur = _Cur([], max_updated=_NOW - _dt.timedelta(hours=3))
    monkeypatch.setattr(daz, "_conn", lambda: _Conn(cur))
    out = daz.rebuild_zones(force=False, now=_NOW)
    assert out["ok"] is True and out["skipped"] == "fresh"
    assert not any("INSERT INTO fiber_coverage_zones" in s
                   for s, _ in cur.log)


def test_rebuild_full_build_writes_merged_zones(monkeypatch):
    presence = [
        # (city_key, state, country, display, fac, lat, lng, raw_names)
        ("ashburn", "VA", "US", "Ashburn", 153, 39.04, -77.49,
         ["Zayo", "GTT Communications (AS3257)",
          "Cogent Communications, Inc."]),
        ("ashburn", "", "US", "Ashburn", 42, 39.02, -77.47, ["Zayo"]),
        ("paris", "", "", "Paris", 5, 33.66, -95.55, ["Zayo"]),
        # canonicalizes to nothing -> dropped before grouping
        ("nowhere", "TX", "US", "Nowhere", 3, 31.0, -100.0, ["Bandwidth"]),
    ]
    cur = _Cur(presence, max_updated=None)
    monkeypatch.setattr(daz, "_conn", lambda: _Conn(cur))
    out = daz.rebuild_zones(force=False, now=_NOW)
    assert out["ok"] is True
    # ashburn(+blank-state merge) + paris orphan = 2 zones
    assert out["zones_written"] == 2
    assert out["levels"] == {"strong": 1, "moderate": 1}
    assert out["pruned"] == 1
    assert "NOT confirmed strand availability" in out["method"]
    inserts = [(s, p) for s, p in cur.log
               if "INSERT INTO fiber_coverage_zones" in s]
    assert len(inserts) == 2
    # idempotent upsert + provenance-stamped notes on every row
    for s, p in inserts:
        assert "ON CONFLICT (zone_id) DO UPDATE" in s
        assert json.loads(p["notes"])["v"] == "inferred"
    ash = [p for _, p in inserts if p["zone_id"] == "dark:us:va:ashburn"][0]
    assert ash["provider_count"] == 3
    assert ash["lit_building_count"] == 195


# ── 8. wiring ─────────────────────────────────────────────────────────
def test_connectivity_score_carries_dark_screen():
    import routes.connectivity_score as cs
    assert cs._dark_screen is daz.dark_screen_from_carriers


def test_cron_heartbeat_dispatch_entry(monkeypatch):
    monkeypatch.delenv("DARK_ZONES_DISABLE", raising=False)
    import routes.cron_heartbeat as ch
    entries = {label: pred for (label, _u, _m, pred) in ch._DISPATCH}
    assert "dark_zones_rebuild_daily" in entries
    pred = entries["dark_zones_rebuild_daily"]
    assert pred(_dt.datetime(2026, 7, 11, 1, 30)) is True     # quiet hour
    assert pred(_dt.datetime(2026, 7, 11, 2, 30)) is False
    assert bool(pred(_dt.datetime(2026, 7, 11, 1, 57))) is False
    monkeypatch.setenv("DARK_ZONES_DISABLE", "1")
    assert bool(pred(_dt.datetime(2026, 7, 11, 1, 30))) is False
    # bounded 3-wide with the other DB-heavy ticks
    assert "dark_zones_rebuild_daily" in ch._HEAVY_LABELS
    # the rebuild URL targets the admin endpoint the blueprint registers
    url = [u for (l, u, _m, _p) in ch._DISPATCH
           if l == "dark_zones_rebuild_daily"][0]
    assert url.endswith("/api/v1/admin/dark-zones/rebuild")
