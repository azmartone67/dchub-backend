"""Pure-unit tests for routes/find_sites.py (/api/v1/sites/find).

No DB, no Flask app, never imports main: geodesic + chord math on known
distances, the NODATA voltage sentinel, spatial clustering, the coverage
builder, and — the reason most of this file exists — the invariant that a
constraint whose layer did not answer NEVER filters the candidate set.
"""
import math

from routes.find_sites import (
    DEFAULT_CLUSTER_KM,
    FIND_SITES_PARAMS,
    assemble_candidates,
    bbox_for,
    bbox_of_points,
    build_coverage,
    build_layer_status,
    clean_kv,
    clean_operator,
    cluster_anchors,
    haversine_km,
    point_to_segment_km,
    site_ref,
)


def _anchor(name="SUB", lat=39.0, lon=-77.0, kv=230.0, state="VA"):
    return {"name": name, "city": "Ashburn", "state": state, "status": "active",
            "voltage_kv": kv, "capacity_mva": 500.0, "lat": lat, "lon": lon,
            "operator": "Dominion"}


# ── geodesic ────────────────────────────────────────────────────────────────
def test_haversine_zero_for_same_point():
    assert haversine_km(39.04, -77.48, 39.04, -77.48) == 0.0


def test_haversine_one_degree_longitude_at_equator():
    assert abs(haversine_km(0.0, 0.0, 0.0, 1.0) - 111.19) < 0.5


def test_haversine_nyc_la_known_distance():
    assert abs(haversine_km(40.7128, -74.0060, 34.0522, -118.2437) - 3936) < 40


def test_haversine_returns_none_on_garbage():
    assert haversine_km(None, -77.0, 39.0, -77.0) is None
    assert haversine_km("x", -77.0, 39.0, -77.0) is None


# ── chord approximation ─────────────────────────────────────────────────────
def test_point_to_segment_perpendicular_foot_inside_segment():
    # Segment runs east-west along lat 39; point sits 1 degree of latitude north
    # of its midpoint. Perpendicular distance ~110.6 km, NOT the distance to
    # either endpoint (~136 km).
    d = point_to_segment_km(40.0, -77.0, 39.0, -78.0, 39.0, -76.0)
    assert abs(d - 110.6) < 2.0


def test_point_to_segment_clamps_to_endpoint_when_foot_is_outside():
    # Point is far east of an east-west segment that ends at lon -76, so the
    # nearest point on the chord is that endpoint, not the infinite line.
    d = point_to_segment_km(39.0, -70.0, 39.0, -78.0, 39.0, -76.0)
    straight = haversine_km(39.0, -70.0, 39.0, -76.0)
    assert abs(d - straight) < 2.0


def test_point_to_segment_degenerate_segment_is_point_distance():
    d = point_to_segment_km(39.0, -77.0, 40.0, -77.0, 40.0, -77.0)
    assert abs(d - haversine_km(39.0, -77.0, 40.0, -77.0)) < 1.0


def test_point_to_segment_zero_on_the_line():
    assert point_to_segment_km(39.0, -77.0, 39.0, -78.0, 39.0, -76.0) < 0.5


def test_point_to_segment_returns_none_on_garbage():
    assert point_to_segment_km(None, -77.0, 39.0, -78.0, 39.0, -76.0) is None


# ── NODATA sentinels ────────────────────────────────────────────────────────
def test_clean_kv_rejects_hifld_nodata_sentinel():
    assert clean_kv(-999999) is None
    assert clean_kv(0) is None
    assert clean_kv(-1) is None


def test_clean_kv_accepts_real_voltages():
    assert clean_kv(230) == 230.0
    assert clean_kv("500") == 500.0


def test_clean_kv_rejects_absurd_high_and_garbage():
    assert clean_kv(99999) is None
    assert clean_kv("abc") is None
    assert clean_kv(None) is None


def test_clean_operator_strips_placeholders():
    assert clean_operator("Unknown") is None
    assert clean_operator("  ") is None
    assert clean_operator("Dominion") == "Dominion"


# ── clustering ──────────────────────────────────────────────────────────────
def test_cluster_collapses_near_duplicates_into_one_area():
    # Four substations inside ~1 km of each other must yield ONE candidate area.
    anchors = [_anchor("A", 39.000, -77.000), _anchor("B", 39.002, -77.002),
               _anchor("C", 39.004, -77.001), _anchor("D", 39.001, -77.003)]
    assert len(cluster_anchors(anchors, DEFAULT_CLUSTER_KM, 10)) == 1


def test_cluster_keeps_distinct_areas():
    anchors = [_anchor("A", 39.0, -77.0), _anchor("B", 40.0, -78.0),
               _anchor("C", 41.0, -79.0)]
    assert len(cluster_anchors(anchors, DEFAULT_CLUSTER_KM, 10)) == 3


def test_cluster_respects_limit():
    anchors = [_anchor(str(i), 39.0 + i, -77.0) for i in range(10)]
    assert len(cluster_anchors(anchors, DEFAULT_CLUSTER_KM, 3)) == 3


def test_cluster_keeps_the_first_of_a_group_so_voltage_order_wins():
    # Input is voltage-desc, so the survivor of a cluster is its highest kV.
    anchors = [_anchor("HI", 39.000, -77.000, kv=500.0),
               _anchor("LO", 39.002, -77.001, kv=115.0)]
    kept = cluster_anchors(anchors, DEFAULT_CLUSTER_KM, 10)
    assert [a["name"] for a in kept] == ["HI"]


def test_cluster_skips_anchors_without_coordinates():
    anchors = [dict(_anchor("A"), lat=None), _anchor("B", 41.0, -79.0)]
    assert [a["name"] for a in cluster_anchors(anchors, DEFAULT_CLUSTER_KM, 10)] == ["B"]


# ── bbox ────────────────────────────────────────────────────────────────────
def test_bbox_for_widens_longitude_away_from_the_equator():
    _, _, w_lo, e_lo = bbox_for(60.0, 0.0, 111.0)
    _, _, w_eq, e_eq = bbox_for(0.0, 0.0, 111.0)
    assert (e_lo - w_lo) > (e_eq - w_eq)


def test_bbox_of_points_contains_every_point():
    pts = [(39.0, -77.0), (40.0, -76.0), (38.5, -78.5)]
    s, n, w, e = bbox_of_points(pts)
    assert all(s <= la <= n and w <= lo <= e for la, lo in pts)


def test_bbox_of_points_none_when_empty():
    assert bbox_of_points([]) is None


# ── coverage builder ────────────────────────────────────────────────────────
def test_coverage_marks_applied_and_unapplied():
    coverage, unapplied = build_coverage(
        {"max_gas_km": 8, "max_fiber_km": 5},
        {"max_gas_km": True, "max_fiber_km": False},
        {"max_fiber_km": ("fiber layer did not answer", "call get_fiber_readiness")})
    assert coverage["max_gas_km"]["applied"] is True
    assert "reason" not in coverage["max_gas_km"]
    assert coverage["max_fiber_km"]["applied"] is False
    assert coverage["max_fiber_km"]["reason"]
    assert coverage["max_fiber_km"]["instead"]
    assert unapplied == ["max_fiber_km"]


def test_coverage_reports_only_arguments_the_caller_sent():
    coverage, unapplied = build_coverage({}, {"max_gas_km": True}, {})
    assert coverage == {} and unapplied == []


def test_declared_params_cover_every_documented_argument():
    for arg in ("state", "lat", "lon", "radius_km", "min_voltage_kv",
                "max_gas_km", "max_fiber_km", "exclude_moratorium", "limit"):
        assert arg in FIND_SITES_PARAMS


# ── ★ the invariant: an unevaluable constraint never filters ────────────────
def test_gas_constraint_filters_when_the_layer_answered():
    anchors = [_anchor("NEAR", 39.0, -77.0), _anchor("FAR", 41.0, -79.0)]
    gas = [(39.01, -77.01)]  # ~1.4 km from NEAR, ~250 km from FAR
    out = assemble_candidates(anchors, gas, [], {}, max_gas_km=8,
                              evaluated={"max_gas_km": True})
    assert [c["anchor"]["name"] for c in out] == ["NEAR"]


def test_gas_constraint_does_not_filter_when_the_layer_did_not_answer():
    # ★ The whole point. The caller asked for gas within 8 km and the layer
    # errored, so gas_pts is empty and evaluated is False. Dropping every
    # candidate would read as "nothing matches"; keeping them as though they
    # passed would be a false screen. Neither: return them UNFILTERED, and the
    # route reports applied:false in constraint_coverage.
    anchors = [_anchor("A", 39.0, -77.0), _anchor("B", 41.0, -79.0)]
    out = assemble_candidates(anchors, [], [], {}, max_gas_km=8,
                              evaluated={"max_gas_km": False})
    assert len(out) == 2
    assert all(c["gas_distance_km"] is None for c in out)


def test_fiber_constraint_does_not_filter_when_the_layer_did_not_answer():
    anchors = [_anchor("A", 39.0, -77.0)]
    out = assemble_candidates(anchors, [], [], {}, max_fiber_km=2,
                              evaluated={"max_fiber_km": False})
    assert len(out) == 1


def test_moratorium_excluded_only_when_the_layer_answered():
    anchors = [_anchor("VA_SITE", 39.0, -77.0, state="VA")]
    mor = {"VA": [{"jurisdiction": "Loudoun", "title": "pause", "source_url": "u"}]}
    dropped = assemble_candidates(anchors, [], [], mor, exclude_moratorium=True,
                                  evaluated={"exclude_moratorium": True})
    assert dropped == []
    kept = assemble_candidates(anchors, [], [], mor, exclude_moratorium=True,
                               evaluated={"exclude_moratorium": False})
    assert len(kept) == 1


def test_a_threshold_without_evaluation_is_not_a_filter_even_with_data_present():
    # Data present but the layer was marked unevaluated: still must not filter.
    anchors = [_anchor("FAR", 41.0, -79.0)]
    gas = [(39.0, -77.0)]
    out = assemble_candidates(anchors, gas, [], {}, max_gas_km=1,
                              evaluated={"max_gas_km": False})
    assert len(out) == 1
    # the distance is still MEASURED and reported, it is simply not a gate
    assert out[0]["gas_distance_km"] > 1


def test_no_threshold_means_no_filter_but_distance_is_still_reported():
    anchors = [_anchor("A", 39.0, -77.0)]
    gas = [(39.5, -77.5)]
    out = assemble_candidates(anchors, gas, [], {}, evaluated={"max_gas_km": True})
    assert len(out) == 1
    assert out[0]["gas_distance_km"] > 0


# ── tier gating ─────────────────────────────────────────────────────────────
def test_free_tier_coarsens_coordinates_and_withholds_anchor_detail():
    out = assemble_candidates([_anchor("A", 39.0437, -77.4874)], [], [], {},
                              full=False)
    c = out[0]
    assert c["lat"] == 39.0 and c["lon"] == -77.5
    assert c["coordinate_precision_km"] == 11.0
    assert c["anchor"]["operator"] is None
    assert c["anchor"]["capacity_mva"] is None
    # voltage and place stay visible — the preview must remain steerable
    assert c["anchor"]["voltage_kv"] == 230.0
    assert c["anchor"]["state"] == "VA"


def test_paid_tier_returns_exact_coordinates_and_detail():
    out = assemble_candidates([_anchor("A", 39.0437, -77.4874)], [], [], {},
                              full=True)
    c = out[0]
    assert c["lat"] == 39.0437 and c["lon"] == -77.4874
    assert c["coordinate_precision_km"] == 0.1
    assert c["anchor"]["operator"] == "Dominion"


def test_site_ref_is_stable_across_tiers_and_distinct_per_site():
    a = _anchor("A", 39.0437, -77.4874)
    free = assemble_candidates([a], [], [], {}, full=False)[0]["site_ref"]
    paid = assemble_candidates([a], [], [], {}, full=True)[0]["site_ref"]
    # computed from the EXACT anchor, so coarsening does not fork the identity
    assert free == paid
    other = assemble_candidates([_anchor("B", 41.0, -79.0)], [], [], {},
                                full=True)[0]["site_ref"]
    assert other != paid
    assert paid.startswith("site_")


# ── shape contract ──────────────────────────────────────────────────────────
def test_every_candidate_publishes_the_fiber_chord_basis():
    out = assemble_candidates([_anchor("A")], [], [(39.0, -78.0, 39.0, -76.0)], {},
                              evaluated={"max_fiber_km": True})
    assert "chord" in out[0]["fiber_distance_basis"]
    assert "NOT the true polyline path" in out[0]["fiber_distance_basis"]


def test_candidate_hands_off_to_the_scoring_tools():
    out = assemble_candidates([_anchor("A")], [], [], {})
    joined = " ".join(out[0]["next_calls"])
    assert "analyze_site" in joined and "get_fiber_readiness" in joined


# ── layer status: the hole constraint_coverage left ─────────────────────────
def test_layer_status_reports_a_healthy_layer_with_its_row_count():
    st = build_layer_status({"max_gas_km": True}, {}, {"gas": 42},
                            {"gas": True, "fiber": False, "moratorium": False})
    assert st["gas"]["answered"] is True
    assert st["gas"]["rows_in_region"] == 42
    assert st["gas"]["table"] == "gas_pipelines"
    assert "reason" not in st["gas"]


def test_layer_status_distinguishes_read_but_empty_from_failed():
    read_empty = build_layer_status({"max_gas_km": True}, {}, {"gas": 0},
                                    {"gas": True})["gas"]
    failed = build_layer_status(
        {"max_gas_km": False},
        {"max_gas_km": ('column "latitude" does not exist', "call get_infrastructure")},
        {"gas": 0}, {"gas": True})["gas"]
    # Both would show gas_distance_km: null on every candidate. `answered` is
    # the only thing that tells them apart.
    assert read_empty["answered"] is True and read_empty["rows_in_region"] == 0
    assert failed["answered"] is False
    assert "latitude" in failed["reason"]
    assert failed["instead"]
    assert failed["rows_in_region"] is None


def test_layer_status_marks_an_unqueried_layer_as_neither_ok_nor_broken():
    st = build_layer_status({}, {}, {}, {"gas": True, "moratorium": False})
    assert st["moratorium"]["answered"] is None
    assert "not queried" in st["moratorium"]["note"]


def test_a_broken_layer_is_visible_even_when_no_constraint_asked_for_it():
    # ★ THE REGRESSION. find_sites shipped querying discovered_pipelines for a
    # `latitude` column that does not exist. With no max_gas_km the failure was
    # invisible: constraint_coverage reports only REQUESTED arguments, so every
    # candidate carried gas_distance_km: null and nothing said why. It surfaced
    # only when a caller happened to pass max_gas_km and read the coverage
    # block. layer_status must show it with an empty request.
    coverage, unapplied = build_coverage({}, {"max_gas_km": False}, {})
    assert coverage == {} and unapplied == []          # coverage stays silent
    st = build_layer_status(
        {"max_gas_km": False},
        {"max_gas_km": ("gas layer (gas_pipelines) did not answer: boom", None)},
        {"gas": 0}, {"gas": True})
    assert st["gas"]["answered"] is False              # layer_status does not
    assert "did not answer" in st["gas"]["reason"]
