"""Phase QQ+10 helpers: scrub_url + parse_eia_v2_fuel_mix.

scrub_url MUST prevent secret leaks in /extract responses — we
discovered EIA_API_KEY was being echoed back in `fetched_url` after
the QQ+9 deploy. Locking this in via CI tests so a future refactor
can't reintroduce the leak.

parse_eia_v2_fuel_mix is the canonical reader for api.eia.gov v2
fuel-mix + region-data responses. Locks in the shape we extract from.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IC = os.path.join(ROOT, "routes", "_iso_common.py")


def _extract(name):
    """Pull a top-level function out of routes/_iso_common.py and exec
    it in an isolated namespace. Preloads stdlib symbols the helper
    uses so we don't have to import the Flask blueprint chain."""
    src = open(IC, encoding="utf-8").read()
    m = re.search(rf"^def {name}\(.*?(?=^def |^class |\Z)",
                  src, re.DOTALL | re.MULTILINE)
    assert m, f"function {name} not found"
    import json as _json
    ns = {"json": _json}
    exec(m.group(0), ns)
    return ns[name]


# ── scrub_url ──────────────────────────────────────────────────────

def test_scrub_url_redacts_api_key_param():
    """The exact leak that PR #58 introduced."""
    fn = _extract("scrub_url")
    url = ("https://api.eia.gov/v2/electricity/rto/region-data/data/"
           "?api_key=SECRET-KEY-VALUE-XXXXXX&frequency=hourly"
           "&facets[respondent][]=TVA")
    out = fn(url)
    assert "SECRET-KEY-VALUE-XXXXXX" not in out
    assert "api_key=%2A%2A%2A" in out or "api_key=***" in out


def test_scrub_url_redacts_basic_auth_userinfo():
    """ISONE_USERNAME:ISONE_PASSWORD embedded in netloc."""
    fn = _extract("scrub_url")
    url = "https://myuser:mypassword@webservices.iso-ne.com/api/v1.1/genfuelmix/current.json"
    out = fn(url)
    assert "mypassword" not in out
    assert "myuser" not in out
    assert "webservices.iso-ne.com" in out


def test_scrub_url_redacts_multiple_secret_params():
    """Each known secret-param's VALUE must be redacted; non-secret
    params pass through. Use distinctive sentinel values that won't
    collide with normal URL chars."""
    fn = _extract("scrub_url")
    url = ("https://example.com/?api_key=SENTINEL_AKEY"
           "&token=SENTINEL_BTOK&password=SENTINEL_CPW&safe=keep")
    out = fn(url)
    assert "SENTINEL_AKEY" not in out
    assert "SENTINEL_BTOK" not in out
    assert "SENTINEL_CPW" not in out
    assert "safe=keep" in out


def test_scrub_url_preserves_safe_url():
    """URLs with no secrets should round-trip unchanged."""
    fn = _extract("scrub_url")
    url = "https://reports.ieso.ca/public/GenOutputbyFuelHourly/PUB_GenOutputbyFuelHourly.csv"
    out = fn(url)
    assert out == url


def test_scrub_url_handles_none():
    fn = _extract("scrub_url")
    assert fn(None) is None
    assert fn("") == ""


def test_scrub_url_fails_safely_on_malformed():
    """Failsafe: even on a totally broken URL we never leak."""
    fn = _extract("scrub_url")
    # Should at minimum drop the query string if anything goes wrong
    out = fn("not://a real url ?api_key=LEAK")
    assert "LEAK" not in out


# ── parse_eia_v2_fuel_mix ──────────────────────────────────────────

def test_eia_v2_fuel_mix_basic():
    fn = _extract("parse_eia_v2_fuel_mix")
    body = '''{"response": {"data": [
      {"period": "2026-05-13T08", "respondent": "TVA", "fueltype": "NG", "value": 12345.6, "value-units": "megawatthours"},
      {"period": "2026-05-13T08", "respondent": "TVA", "fueltype": "NUC", "value": 6789.1, "value-units": "megawatthours"},
      {"period": "2026-05-13T08", "respondent": "TVA", "fueltype": "COL", "value": 3000.0, "value-units": "megawatthours"}
    ]}}'''
    out = fn(body, prefix="fuel_")
    assert out["fuel_ng"]["value"] == 12345.6
    assert out["fuel_nuc"]["value"] == 6789.1
    assert out["fuel_col"]["value"] == 3000.0


def test_eia_v2_fuel_mix_takes_latest_per_fueltype():
    """Multiple periods present → keep only the latest (first-seen,
    since rows arrive sorted desc by period)."""
    fn = _extract("parse_eia_v2_fuel_mix")
    body = '''{"response": {"data": [
      {"period": "2026-05-13T08", "respondent": "TVA", "fueltype": "NG", "value": 100},
      {"period": "2026-05-13T07", "respondent": "TVA", "fueltype": "NG", "value": 200},
      {"period": "2026-05-13T06", "respondent": "TVA", "fueltype": "NG", "value": 300}
    ]}}'''
    out = fn(body, prefix="fuel_")
    assert out["fuel_ng"]["value"] == 100  # latest, not 300


def test_eia_v2_region_data_shape():
    """region-data variant uses `type` instead of `fueltype`."""
    fn = _extract("parse_eia_v2_fuel_mix")
    body = '''{"response": {"data": [
      {"period": "2026-05-13T08", "respondent": "TVA", "type": "D", "value": 23456},
      {"period": "2026-05-13T08", "respondent": "TVA", "type": "NG", "value": 12345}
    ]}}'''
    out = fn(body, prefix="fuel_")
    assert "fuel_d" in out
    assert out["fuel_d"]["value"] == 23456
    assert out["fuel_ng"]["value"] == 12345


def test_eia_v2_handles_missing_value():
    """Rows with null/missing value should be skipped, not crash."""
    fn = _extract("parse_eia_v2_fuel_mix")
    body = '''{"response": {"data": [
      {"period": "2026-05-13T08", "fueltype": "NG"},
      {"period": "2026-05-13T08", "fueltype": "NUC", "value": null},
      {"period": "2026-05-13T08", "fueltype": "SOL", "value": 99.9}
    ]}}'''
    out = fn(body, prefix="fuel_")
    assert "fuel_ng" not in out
    assert "fuel_nuc" not in out
    assert out["fuel_sol"]["value"] == 99.9


def test_eia_v2_handles_empty_response():
    fn = _extract("parse_eia_v2_fuel_mix")
    assert fn('{"response": {"data": []}}', prefix="fuel_") == {}
    assert fn('{}', prefix="fuel_") == {}
    assert fn('not json', prefix="fuel_") == {}


def test_eia_v2_skips_zero_values():
    """Zero MW values typically indicate offline plants, not real data."""
    fn = _extract("parse_eia_v2_fuel_mix")
    body = '''{"response": {"data": [
      {"period": "2026-05-13T08", "fueltype": "NG", "value": 0},
      {"period": "2026-05-13T08", "fueltype": "SOL", "value": 50.5}
    ]}}'''
    out = fn(body, prefix="fuel_")
    assert "fuel_ng" not in out
    assert out["fuel_sol"]["value"] == 50.5
