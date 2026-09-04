"""OSM transmission proxy — the layer the map could not draw outside the US.

Measured 2026-09-04 against the HIFLD ArcGIS service the map itself calls:
177 features over N. Virginia, 0 over Hesse, 0 over all of Germany. OSM has
65,460 voltage-tagged power lines in Germany, so this endpoint is what puts a
transmission layer under a German site.

The tests that matter here are the PARSER and the BBOX GUARD, because both fail
silently in production: a voltage that does not parse drops a line from the map
with no error, and a bbox that is too large becomes an upstream 429 rather than
a message anyone can act on.
"""
import json

import pytest

# The module imports psycopg2/flask at import time; these are present in CI.
from routes import global_infra as gi


# ── voltage parsing ───────────────────────────────────────────────────
#
# OSM tags voltage in VOLTS, as free text. A tower carrying two circuits at
# different voltages uses a semicolon list. The first implementation filtered
# Overpass with ["voltage"~"^[0-9]+$"] and took the FIRST integer, which
# silently dropped every multi-voltage way: measured 63,793 vs 65,460 over
# Germany — 1,667 lines, and they are the multi-circuit towers, not a random
# sample. Locally it turned 3 x 380 kV lines into 1.

@pytest.mark.parametrize("raw,expected", [
    ("380000", 380000),
    ("110000", 110000),
    ("380000;110000", 380000),     # highest wins regardless of order
    ("110000;380000", 380000),
    ("220000;110000", 220000),
    ("380 000", 380000),           # space is a thousands separator, not a delimiter
    ("380000;220000;110000", 380000),
    (None, None),
    ("", None),
    ("abc", None),
])
def test_max_volts(raw, expected):
    assert gi._osm_max_volts(raw) == expected


def test_semicolon_voltage_is_not_truncated_to_the_first_value():
    """The specific regression: taking the first integer read a 380 kV line
    tagged '110000;380000' as 110 kV, mis-styling it and mis-sorting it."""
    assert gi._osm_max_volts("110000;380000") == 380000, \
        "multi-voltage way collapsed to its first value"


def test_query_does_not_exclude_multi_voltage_ways():
    """A digits-only Overpass filter drops '380000;110000' upstream, where no
    amount of parsing can recover it."""
    q = gi._osm_query(50.0, 9.0, 50.5, 9.5, 110)
    assert "^[0-9]+$" not in q, "digits-only filter would drop multi-voltage ways"
    assert '["power"="line"]' in q and '["voltage"]' in q
    assert "out geom;" in q, "geometry is the whole point"


# ── GeoJSON conversion ────────────────────────────────────────────────

_OVERPASS = {
    "elements": [
        {"type": "way", "id": 1, "tags": {"voltage": "380000", "operator": "TenneT TSO",
                                          "cables": "6", "circuits": "2"},
         "geometry": [{"lat": 50.1, "lon": 9.1}, {"lat": 50.2, "lon": 9.2}]},
        {"type": "way", "id": 2, "tags": {"voltage": "110000;380000", "operator": "Avacon"},
         "geometry": [{"lat": 50.3, "lon": 9.3}, {"lat": 50.4, "lon": 9.4}]},
        {"type": "way", "id": 3, "tags": {"voltage": "20000"},          # below min_kv
         "geometry": [{"lat": 50.5, "lon": 9.5}, {"lat": 50.6, "lon": 9.6}]},
        {"type": "way", "id": 4, "tags": {"operator": "no voltage tag"},
         "geometry": [{"lat": 50.7, "lon": 9.7}, {"lat": 50.8, "lon": 9.8}]},
        {"type": "way", "id": 5, "tags": {"voltage": "380000"},          # single point
         "geometry": [{"lat": 50.9, "lon": 9.9}]},
        {"type": "node", "id": 6, "tags": {"voltage": "380000"}},        # not a way
    ]
}


class _Resp:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


def _build(monkeypatch, doc=_OVERPASS, status=200, min_kv=110.0):
    monkeypatch.setattr(gi.requests, "post",
                        lambda url, **kw: _Resp(status, json.dumps(doc)))
    return json.loads(gi._build_osm_transmission(50.0, 9.0, 51.0, 10.0, min_kv))


def test_builds_a_featurecollection_of_linestrings(monkeypatch):
    d = _build(monkeypatch)
    assert d["type"] == "FeatureCollection"
    assert all(f["geometry"]["type"] == "LineString" for f in d["features"])
    assert d["count"] == len(d["features"])


def test_coordinates_are_geojson_lng_lat_not_lat_lng(monkeypatch):
    """Leaflet and GeoJSON disagree on axis order; getting this backwards puts
    German lines in the Indian Ocean."""
    d = _build(monkeypatch)
    lng, lat = d["features"][0]["geometry"]["coordinates"][0]
    assert 9.0 <= lng <= 10.0, f"first ordinate {lng} is not a longitude"
    assert 50.0 <= lat <= 51.0, f"second ordinate {lat} is not a latitude"


def test_drops_ways_that_cannot_be_drawn_or_priced(monkeypatch):
    d = _build(monkeypatch)
    ids = {f["properties"]["osm_id"] for f in d["features"]}
    assert ids == {1, 2}, f"unexpected survivors: {ids}"
    # 3 below min_kv, 4 untagged, 5 single-point (not a line), 6 not a way


def test_min_kv_filter_is_applied(monkeypatch):
    """At min_kv=110 the 20 kV way is excluded; at min_kv=0 it survives. Way 5
    stays out either way — a single-point way is not a line."""
    assert {f["properties"]["osm_id"]
            for f in _build(monkeypatch, min_kv=110.0)["features"]} == {1, 2}
    assert {f["properties"]["osm_id"]
            for f in _build(monkeypatch, min_kv=0)["features"]} == {1, 2, 3}


def test_volts_are_converted_to_kv(monkeypatch):
    d = _build(monkeypatch)
    kv = {f["properties"]["osm_id"]: f["properties"]["voltage_kv"] for f in d["features"]}
    assert kv[1] == 380.0, "380000 V must render as 380 kV, not 380000"
    assert kv[2] == 380.0, "multi-voltage way must take its highest"


def test_sorted_highest_voltage_first(monkeypatch):
    doc = {"elements": [
        {"type": "way", "id": 1, "tags": {"voltage": "110000"},
         "geometry": [{"lat": 50.1, "lon": 9.1}, {"lat": 50.2, "lon": 9.2}]},
        {"type": "way", "id": 2, "tags": {"voltage": "380000"},
         "geometry": [{"lat": 50.3, "lon": 9.3}, {"lat": 50.4, "lon": 9.4}]},
    ]}
    d = _build(monkeypatch, doc)
    assert [f["properties"]["voltage_kv"] for f in d["features"]] == [380.0, 110.0]


def test_odbl_attribution_travels_with_the_data(monkeypatch):
    """ODbL requires attribution. If it is not in the payload the map cannot
    render it, and we are redistributing OSM data unattributed."""
    d = _build(monkeypatch)
    assert "OpenStreetMap" in d["attribution"]
    assert "ODbL" in d["_licence"]


# ── upstream failure ──────────────────────────────────────────────────

def test_upstream_failure_raises_so_cache_serves_stale(monkeypatch):
    """Raising (not returning an empty FeatureCollection) is what lets _cached
    keep serving the last good payload instead of caching 'no lines here' — the
    same reasoning as _require_feature_collection for the GDACS proxy."""
    monkeypatch.setattr(gi.requests, "post", lambda url, **kw: _Resp(429, "rate limited"))
    with pytest.raises(ValueError) as e:
        gi._build_osm_transmission(50.0, 9.0, 51.0, 10.0, 110.0)
    assert "overpass" in str(e.value).lower()


def test_falls_over_to_the_second_mirror(monkeypatch):
    """Overpass 429s are the documented reason the browser-side OSM loaders were
    stubbed out in 2026-06. One mirror failing must not blank the layer."""
    assert len(gi._OSM_MIRRORS) >= 2, "a single mirror is a single point of failure"
    seen = []

    def post(url, **kw):
        seen.append(url)
        if len(seen) == 1:
            return _Resp(429, "slow down")
        return _Resp(200, json.dumps(_OVERPASS))

    monkeypatch.setattr(gi.requests, "post", post)
    d = json.loads(gi._build_osm_transmission(50.0, 9.0, 51.0, 10.0, 110.0))
    assert d["count"] == 2 and len(seen) == 2


# ── bbox guard ────────────────────────────────────────────────────────

@pytest.fixture
def client():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(gi.global_infra_bp)
    return app.test_client()


@pytest.mark.parametrize("bbox,why", [
    ("", "empty"),
    ("1,2,3", "three values"),
    ("a,b,c,d", "non-numeric"),
    ("10,50,9,51", "lng transposed"),
    ("9,51,10,50", "lat transposed"),
    ("-200,50,-190,51", "out of range"),
])
def test_bad_bbox_is_a_400_not_a_crash(client, bbox, why):
    r = client.get(f"/api/v1/infrastructure/osm-transmission?bbox={bbox}")
    assert r.status_code == 400, f"{why} should be rejected"
    assert r.get_json().get("error")


def test_oversized_bbox_is_refused_with_an_actionable_message(client):
    """Overpass bills by area: a continent-sized bbox comes back 429/504 after a
    long wait. Refusing here turns a slow mystery failure into 'zoom in'."""
    r = client.get("/api/v1/infrastructure/osm-transmission?bbox=-10,35,40,70")
    assert r.status_code == 400
    body = r.get_json()
    assert "deg" in body["error"] and "zoom in" in body["error"]
    assert body["max_area_deg2"] == gi._OSM_MAX_AREA_DEG2


def test_area_cap_admits_a_realistic_viewport(client, monkeypatch):
    """The cap must not be so tight that a normal map viewport is rejected.
    ~0.35 x 0.5 deg is the Hesse view this was built for."""
    called = {}
    monkeypatch.setattr(gi, "_cached",
                        lambda k, b: called.setdefault("key", k) or {"ok": True})
    client.get("/api/v1/infrastructure/osm-transmission?bbox=9.10,50.20,9.60,50.55")
    assert "key" in called, "a realistic viewport was rejected by the area cap"


def test_cache_key_rounds_so_panning_reuses_one_fetch(client, monkeypatch):
    """Without rounding, every pixel of pan mints a new key and every pan is an
    upstream Overpass call — which is how the browser-side loaders earned 429s."""
    keys = []
    monkeypatch.setattr(gi, "_cached",
                        lambda k, b: keys.append(k) or {"ok": True})
    client.get("/api/v1/infrastructure/osm-transmission?bbox=9.101,50.201,9.601,50.551")
    client.get("/api/v1/infrastructure/osm-transmission?bbox=9.104,50.204,9.604,50.554")
    assert len(keys) == 2 and keys[0] == keys[1], f"near-identical bboxes minted {keys}"
