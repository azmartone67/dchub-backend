"""Pure-unit tests for routes/cluster_latency.py (cluster_sites_by_latency
backend, Gemini partnership spec 2026-07-11).

No DB, no Flask app, never imports main (reference_dchub_green_main_0709):
haversine known distances, floor math, clustering on toy sets,
physics_impossible flagging, the confidence ladder, and the fail-soft
math-without-enrichment contract.
"""
import math

from routes.cluster_latency import (
    DEFAULT_BUDGET_US,
    FIBER_US_PER_KM,
    ROUTE_FACTOR,
    build_cluster_response,
    confidence_for,
    est_rtt_us,
    find_clusters,
    floor_rtt_us,
    haversine_km,
    parse_budget,
    parse_min_confidence,
    parse_sites,
    published_check_from_points,
)


# ── haversine: known distances ───────────────────────────────────────────
def test_haversine_zero_for_same_point():
    assert haversine_km(39.04, -77.48, 39.04, -77.48) == 0.0


def test_haversine_nyc_la():
    # NYC City Hall → LA City Hall great-circle ≈ 3,936 km
    d = haversine_km(40.7128, -74.0060, 34.0522, -118.2437)
    assert abs(d - 3936) < 40


def test_haversine_one_degree_longitude_at_equator():
    # 1° of longitude at the equator ≈ 111.19 km (R=6371 convention)
    d = haversine_km(0.0, 0.0, 0.0, 1.0)
    assert abs(d - 111.19) < 0.5


def test_haversine_symmetry():
    a = haversine_km(39.04, -77.48, 33.45, -112.07)
    b = haversine_km(33.45, -112.07, 39.04, -77.48)
    assert abs(a - b) < 1e-9


# ── floor / estimate math (the cited constants) ──────────────────────────
def test_floor_math_100km():
    # 100 km × 4.9 µs/km × 2 = 980 µs RTT floor
    assert abs(floor_rtt_us(100.0) - 980.0) < 1e-9


def test_estimate_is_floor_times_route_factor():
    assert abs(est_rtt_us(100.0) - 980.0 * ROUTE_FACTOR) < 1e-9
    assert abs(est_rtt_us(100.0) - 1372.0) < 1e-9


def test_constants_are_the_spec_values():
    assert FIBER_US_PER_KM == 4.9
    assert ROUTE_FACTOR == 1.4
    assert DEFAULT_BUDGET_US == 1000.0


# ── parsing ──────────────────────────────────────────────────────────────
def test_parse_sites_basic_and_labels():
    sites, err = parse_sites("39.04,-77.48:ashburn;40.79,-77.86")
    assert err is None
    assert [s["label"] for s in sites] == ["ashburn", "site_2"]
    assert sites[0]["lat"] == 39.04 and sites[0]["lon"] == -77.48


def test_parse_sites_rejects_too_few_and_too_many():
    _, err = parse_sites("39.04,-77.48")
    assert err and "at least" in err
    nine = ";".join(f"{i}.0,{i}.0" for i in range(9))
    _, err = parse_sites(nine)
    assert err and "at most" in err


def test_parse_sites_rejects_garbage_and_out_of_range():
    _, err = parse_sites("foo;bar")
    assert err
    _, err = parse_sites("91.0,0.0;0.0,0.0")
    assert err and "out of range" in err
    _, err = parse_sites("")
    assert err and "required" in err


def test_parse_sites_dedupes_labels():
    sites, err = parse_sites("1.0,1.0:x;2.0,2.0:x;3.0,3.0:x")
    assert err is None
    assert [s["label"] for s in sites] == ["x", "x_2", "x_3"]


def test_parse_budget_and_min_confidence_defaults():
    assert parse_budget(None) == DEFAULT_BUDGET_US
    assert parse_budget("nope") == DEFAULT_BUDGET_US
    assert parse_budget(0) == 1.0            # clamped to the floor
    assert parse_min_confidence(None) == "inferred"
    assert parse_min_confidence("PUBLISHED") == "published"
    assert parse_min_confidence("bogus") == "inferred"


# ── confidence ladder ────────────────────────────────────────────────────
def _e(cc, dark):
    return {"carrier_count": cc, "dark_level": dark, "dark_screen": None}


def test_confidence_published_needs_dark_both_ends_and_route():
    assert confidence_for(_e(5, "strong"), _e(3, "moderate"), True) == "published"


def test_confidence_tracked_when_route_missing_or_dark_weak():
    assert confidence_for(_e(5, "strong"), _e(3, "moderate"), False) == "tracked"
    assert confidence_for(_e(5, "strong"), _e(3, "moderate"), None) == "tracked"
    assert confidence_for(_e(5, "strong"), _e(3, "none"), True) == "tracked"


def test_confidence_inferred_when_presence_missing_or_no_enrichment():
    assert confidence_for(_e(5, "strong"), _e(0, None), True) == "inferred"
    assert confidence_for(None, _e(5, "strong"), True) == "inferred"
    assert confidence_for(None, None, None) == "inferred"


# ── clustering (toy sets) ────────────────────────────────────────────────
def test_find_clusters_triangle_plus_outlier():
    # 0-1-2 fully connected; 3 isolated → one maximal clique of 3
    cliques = find_clusters(4, [(0, 1), (0, 2), (1, 2)])
    assert cliques == [[0, 1, 2]]


def test_find_clusters_two_overlapping():
    # 0-1-2 triangle plus edge 2-3 → cliques [0,1,2] and [2,3], largest first
    cliques = find_clusters(4, [(0, 1), (0, 2), (1, 2), (2, 3)])
    assert cliques == [[0, 1, 2], [2, 3]]


def test_find_clusters_empty_graph():
    assert find_clusters(4, []) == []


# ── the full pure build (math without enrichment = fail-soft path) ──────
# Metro-scale toy geometry: three sites within ~60 km + one ~3,900 km away.
_ASH = {"label": "ashburn", "lat": 39.0437, "lon": -77.4875}
_STER = {"label": "sterling", "lat": 39.0062, "lon": -77.4286}
_BALT = {"label": "baltimore", "lat": 39.2904, "lon": -76.6122}
_LA = {"label": "la", "lat": 34.0522, "lon": -118.2437}


def test_build_response_toy_cluster_and_pruning():
    # 1,200 µs budget: the DC-metro triangle fits (max est ≈ 1,102 µs);
    # every LA pair's floor (≈ 35,800 µs) is orders of magnitude over.
    res = build_cluster_response([_ASH, _STER, _BALT, _LA], budget_us=1200.0)
    assert len(res["pairs"]) == 6
    la_pairs = [p for p in res["pairs"] if "la" in (p["from"], p["to"])]
    assert la_pairs and all(p["physics_impossible"] for p in la_pairs)
    assert all(not p["viable"] for p in la_pairs)
    metro = [p for p in res["pairs"] if "la" not in (p["from"], p["to"])]
    assert len(metro) == 3 and all(p["viable"] for p in metro)
    assert all(not p["physics_impossible"] for p in metro)
    assert res["viable_count"] == 3 and res["pruned_count"] == 3
    assert res["clusters"] == [{
        "sites": ["ashburn", "sterling", "baltimore"], "size": 3,
        "max_est_rtt_us": res["clusters"][0]["max_est_rtt_us"]}]
    assert res["clusters"][0]["max_est_rtt_us"] <= 1200.0


def test_build_response_math_is_deterministic_and_cited():
    res = build_cluster_response([_ASH, _BALT], budget_us=1000.0)
    p = res["pairs"][0]
    d = haversine_km(_ASH["lat"], _ASH["lon"], _BALT["lat"], _BALT["lon"])
    assert abs(p["distance_km"] - round(d, 1)) < 0.05
    assert abs(p["floor_rtt_us"] - round(d * 4.9 * 2, 1)) < 0.1
    assert abs(p["est_rtt_us"] - round(d * 4.9 * 2 * 1.4, 1)) < 0.1
    assert res["assumptions"]["fiber_us_per_km_one_way"] == 4.9
    assert res["assumptions"]["route_factor"] == 1.4


def test_build_response_physics_impossible_flagging():
    # tight 100 µs budget: ~76 km Ashburn-Baltimore floor (~745 µs) exceeds it
    res = build_cluster_response([_ASH, _BALT], budget_us=100.0)
    p = res["pairs"][0]
    assert p["physics_impossible"] is True and p["viable"] is False
    assert res["viable_count"] == 0 and res["pruned_count"] == 1
    assert res["clusters"] == []


def test_build_response_fail_soft_without_enrichment():
    # enrichment=None (fiber tables unavailable): math returns, confidence
    # degrades to "inferred", no dark_screen / endpoint_dark_screen fields.
    res = build_cluster_response([_ASH, _STER], budget_us=1000.0,
                                 enrichment=None, published_pair_check=None)
    p = res["pairs"][0]
    assert p["viable"] is True
    assert p["confidence_v"] == "inferred"
    assert "endpoint_dark_screen" not in p
    assert all("dark_screen" not in s for s in res["sites"])


def test_build_response_min_confidence_prunes_unenriched_pairs():
    # min_confidence=tracked with no enrichment → nothing can qualify, but
    # the math still returns (pairs listed, all pruned).
    res = build_cluster_response([_ASH, _STER], budget_us=1000.0,
                                 min_confidence="tracked", enrichment=None)
    assert res["viable_count"] == 0 and res["pruned_count"] == 1
    assert res["pairs"][0]["physics_impossible"] is False
    assert res["clusters"] == []


def test_build_response_confidence_ladder_wired_through():
    enrich = [_e(5, "strong"), _e(4, "moderate")]
    res = build_cluster_response(
        [_ASH, _STER], budget_us=1000.0, enrichment=enrich,
        published_pair_check=lambda i, j: True)
    p = res["pairs"][0]
    assert p["confidence_v"] == "published"
    assert p["endpoint_dark_screen"] == {"a": "strong", "b": "moderate"}
    # and min_confidence=published keeps it viable
    res2 = build_cluster_response(
        [_ASH, _STER], budget_us=1000.0, min_confidence="published",
        enrichment=enrich, published_pair_check=lambda i, j: True)
    assert res2["viable_count"] == 1


def test_published_check_from_points():
    sites = [_ASH, _STER, _LA]
    # a published-route endpoint sitting between Ashburn and Sterling
    check = published_check_from_points(sites, [(39.02, -77.45)])
    assert check(0, 1) is True
    assert check(0, 2) is True      # inside the wide Ashburn-LA bbox too
    check_none = published_check_from_points(sites, None)
    assert check_none(0, 1) is None
    check_far = published_check_from_points(sites, [(10.0, 10.0)])
    assert check_far(0, 1) is False
