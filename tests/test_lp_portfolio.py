"""r-portfolio (2026-07-11) — pure-function tests for the per-saved-site
delta helpers behind /api/v1/lp/saved enrichment and the get_changes
portfolio overlay. No Flask app, no DB (conftest harness rules)."""
import datetime

from routes.lp_sites import (
    _haversine_km,
    _market_match_keys,
    _match_new_facilities,
    _site_movement,
    _clamp_since,
    _parse_since_param,
)


def test_market_match_keys_name_and_slug():
    assert _market_match_keys("Northern Virginia") == (
        "northern-virginia", "northern virginia")
    assert _market_match_keys("phoenix") == ("phoenix", "phoenix")
    assert _market_match_keys(None) == ("", "")
    assert _market_match_keys("  Dallas-Fort Worth ") == (
        "dallas-fort-worth", "dallas-fort worth")


def test_haversine_known_distance():
    # 1 degree of latitude ~ 111 km
    assert abs(_haversine_km(39.0, -77.0, 40.0, -77.0) - 111.2) < 1.0
    assert _haversine_km(39.0, -77.0, 39.0, -77.0) == 0.0


def test_match_new_facilities_radius_and_cap():
    sites = [{"id": 1, "latitude": 33.45, "longitude": -112.07}]
    facilities = [
        {"name": "Near A", "state": "AZ", "capacity_mw": 50,
         "latitude": 33.50, "longitude": -112.00},          # ~8 km
        {"name": "Near B", "state": "AZ", "capacity_mw": 20,
         "latitude": 33.30, "longitude": -112.10},          # ~17 km
        {"name": "Far", "state": "NV", "capacity_mw": 90,
         "latitude": 36.17, "longitude": -115.14},          # ~400 km
        {"name": "No coords", "state": "AZ", "capacity_mw": 10,
         "latitude": None, "longitude": None},              # skipped
    ]
    out = _match_new_facilities(sites, facilities, radius_km=50.0)
    assert set(out.keys()) == {1}
    names = [f["name"] for f in out[1]]
    assert names == ["Near A", "Near B"]           # sorted nearest-first
    assert all(f["km"] <= 50.0 for f in out[1])

    capped = _match_new_facilities(sites, facilities, radius_km=50.0,
                                   cap_per_site=1)
    assert len(capped[1]) == 1 and capped[1][0]["name"] == "Near A"


def test_match_new_facilities_skips_bad_site_rows():
    sites = [{"id": 7}, {"id": 8, "latitude": "x", "longitude": 1.0}]
    facilities = [{"name": "F", "latitude": 33.0, "longitude": -112.0}]
    assert _match_new_facilities(sites, facilities) == {}


def test_site_movement_signals():
    assert _site_movement({"verdict_changed": True}) is True
    assert _site_movement({"excess_power_delta_window": -1.3}) is True
    assert _site_movement({"excess_power_delta_window": 0.4}) is False
    assert _site_movement({"alerts_fired_window": 2}) is True
    assert _site_movement({"new_facilities_nearby": [{"name": "x"}]}) is True
    assert _site_movement({}) is False
    assert _site_movement({"excess_power_delta_window": "junk"}) is False


def test_clamp_since_defaults_and_bounds():
    now = datetime.datetime.now(datetime.timezone.utc)
    d = _clamp_since(None)
    assert abs((now - d).days - 7) <= 1
    # too far back → clamped to 30d
    old = now - datetime.timedelta(days=365)
    assert (now - _clamp_since(old)).days <= 31
    # too recent → floored at 1h back
    recent = now - datetime.timedelta(minutes=5)
    assert (now - _clamp_since(recent)) >= datetime.timedelta(minutes=59)
    # naive datetime accepted
    naive = datetime.datetime.now() - datetime.timedelta(days=3)
    assert _clamp_since(naive).tzinfo is not None


def test_parse_since_param():
    now = datetime.datetime.now(datetime.timezone.utc)
    d = _parse_since_param("24h")
    assert d is not None and abs((now - d).total_seconds() - 86400) < 120
    d = _parse_since_param("7d")
    assert d is not None and abs((now - d).days - 7) <= 1
    d = _parse_since_param("2026-07-01T00:00:00Z")
    assert d is not None and d.tzinfo is not None
    assert _parse_since_param("") is None
    assert _parse_since_param("garbage") is None
