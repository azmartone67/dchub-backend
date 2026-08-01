"""r-anonbulk (2026-08-01) — regressions for anonymous bulk exposure.

Two defects, found by probing production with no key and no cookie:

1. /api/v1/map served the whole proprietary discovered_facilities registry —
   19,947 rows, name + slug + EXACT lat/lon (5,938 rows at 6 decimal places,
   ~0.1m) — to any unauthenticated caller in ONE request. The masking block
   had claimed "city-level (~11km) coords" in a comment since 2026-05-28 but
   never rounded anything.

2. api_data_protection bucketed every keyless caller under the single literal
   string "anonymous", so the rolling-window anomaly detection and the daily
   record cap could not tell one scraper from the entire organic long tail.

These tests pin the behaviour, not the implementation: they exec the real
gating block out of main.py rather than restating its logic, so a future edit
that reverts the split fails here instead of silently re-opening the hole.
"""
import os
import textwrap

import pytest


# ---------------------------------------------------------------------------
# 1. Map tier/bbox gating — exec'd straight from main.py source
# ---------------------------------------------------------------------------

def _gating_block():
    """Extract the live decision block from main.py so this test cannot drift
    into testing a copy of the logic that no longer ships."""
    main_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    with open(main_py, encoding="utf-8") as fh:
        src = fh.read()
    start_marker = "        _MAP_TIER_CAP = {'anonymous': 50000"
    end_marker = "_map_exact_coords = False"
    assert start_marker in src, "map tier-cap block not found — did the gate move or get deleted?"
    start = src.index(start_marker)
    end = src.index(end_marker, start) + len(end_marker)
    return textwrap.dedent(src[start:end])


def _run(tier, bbox_deg2, requested_limit=30000, env=None):
    block = _gating_block()
    saved = dict(os.environ)
    if env:
        os.environ.update(env)
    try:
        scope = {
            "os": os,
            "_map_tier": tier,
            "_bbox_deg2": bbox_deg2,
            "limit": min(requested_limit, 30000),
        }
        exec(block, scope)  # noqa: S102 — executing our own source under test
        return scope["limit"], scope["_map_exact_coords"], scope["_MAP_ANON_COORD_DP"]
    finally:
        os.environ.clear()
        os.environ.update(saved)


GLOBAL_SWEEP = None
SMALL_VIEWPORT = 4.0      # 2deg x 2deg — a real zoomed-in view
HUGE_BBOX = 400.0         # 20deg x 20deg — a global sweep wearing a bbox


@pytest.mark.parametrize("tier", ["anonymous", "free", "identified"])
def test_global_sweep_never_returns_exact_coords_to_unpaid(tier):
    """The bulk-export shape. Dots stay uncapped (the SEO map must render
    complete) but precision is withheld."""
    rows, exact, _ = _run(tier, GLOBAL_SWEEP)
    assert exact is False, f"{tier} global sweep still leaks exact coordinates"
    assert rows >= 19947, "map went incomplete — the SEO surface regressed"


@pytest.mark.parametrize("tier", ["anonymous", "free", "identified"])
def test_small_viewport_is_rounded_by_default_and_row_capped(tier):
    """r-anonbbox (2026-08-01): the exact-coords bbox exemption served only
    harvesters (no legitimate caller sends bbox — map.html fetches
    ?all=true&limit=25000, land-power-app.js pages by offset), and ~335 tiles
    of ≤25 deg² re-extracted the registry's exact locations at 500 rows/tile.
    Default is now rounded; MAP_ANON_BBOX_EXACT=1 restores the exemption."""
    rows, exact, _ = _run(tier, SMALL_VIEWPORT)
    assert exact is False, f"{tier} viewport leaks exact coords — tile-sweeping re-extracts the registry"
    assert rows <= 500, f"{tier} viewport not row-capped — bbox is walkable as a pager"


@pytest.mark.parametrize("tier", ["anonymous", "free", "identified"])
def test_bbox_exact_env_restores_viewport_exemption_but_keeps_row_cap(tier):
    """MAP_ANON_BBOX_EXACT=1 is the no-deploy switch for when the frontend
    starts sending bbox on zoom. It must NOT lift the row cap."""
    rows, exact, _ = _run(tier, SMALL_VIEWPORT, env={"MAP_ANON_BBOX_EXACT": "1"})
    assert exact is True, f"{tier} viewport exemption not restored by MAP_ANON_BBOX_EXACT=1"
    assert rows <= 500, f"{tier} bbox row cap lost when the exemption is on"


def test_bbox_exact_env_does_not_unround_global_or_oversized_sweeps():
    """The switch re-opens ONLY the genuine-viewport path."""
    _, exact_global, _ = _run("anonymous", GLOBAL_SWEEP, env={"MAP_ANON_BBOX_EXACT": "1"})
    assert exact_global is False, "MAP_ANON_BBOX_EXACT leaked into the global sweep"
    _, exact_huge, _ = _run("anonymous", HUGE_BBOX, env={"MAP_ANON_BBOX_EXACT": "1"})
    assert exact_huge is False, "MAP_ANON_BBOX_EXACT leaked into the oversized-bbox path"


@pytest.mark.parametrize("tier", ["anonymous", "free", "identified"])
def test_oversized_bbox_is_treated_as_a_global_sweep(tier):
    """Closes the obvious bypass: ask for exact coords with a bbox so large it
    is really the whole world."""
    _, exact, _ = _run(tier, HUGE_BBOX)
    assert exact is False, f"{tier} escaped coarsening via an oversized bbox"


@pytest.mark.parametrize("tier", ["developer", "pro", "enterprise", "founding"])
def test_paying_tiers_are_untouched(tier):
    """Precise bulk access is the paid product — do not regress it."""
    for bbox in (GLOBAL_SWEEP, SMALL_VIEWPORT, HUGE_BBOX):
        _, exact, _ = _run(tier, bbox)
        assert exact is True, f"paying tier {tier} was wrongly coarsened"


def test_coord_precision_is_env_tunable_and_reversible():
    """MAP_ANON_COORD_DP=6 is the no-deploy kill switch."""
    _, _, dp_default = _run("anonymous", GLOBAL_SWEEP)
    assert dp_default == 3
    _, _, dp_off = _run("anonymous", GLOBAL_SWEEP, env={"MAP_ANON_COORD_DP": "6"})
    assert dp_off == 6
    _, _, dp_tight = _run("anonymous", GLOBAL_SWEEP, env={"MAP_ANON_COORD_DP": "1"})
    assert dp_tight == 1


def test_rounding_actually_destroys_the_survey_grade_tail():
    """3dp must collapse a 6dp coordinate; the point of the change."""
    _, _, dp = _run("anonymous", GLOBAL_SWEEP)
    assert round(42.123456, dp) == 42.123
    assert round(-87.654321, dp) != -87.654321


# ---------------------------------------------------------------------------
# 2. Anonymous request bucketing
# ---------------------------------------------------------------------------

@pytest.fixture
def protection():
    import api_data_protection as mod
    return mod


@pytest.fixture
def ctx():
    import flask
    return flask.Flask(__name__)


def _bucket(protection, ctx, headers):
    with ctx.test_request_context("/", headers=headers):
        return protection._get_key_hash(None)


def test_distinct_networks_get_distinct_buckets(protection, ctx):
    a = _bucket(protection, ctx, {"CF-Connecting-IP": "203.0.113.44"})
    b = _bucket(protection, ctx, {"CF-Connecting-IP": "198.51.100.7"})
    assert a != b, "keyless callers still collapse into one bucket"
    assert a != "anonymous" and b != "anonymous"


def test_same_slash24_shares_a_bucket(protection, ctx):
    a = _bucket(protection, ctx, {"CF-Connecting-IP": "203.0.113.44"})
    b = _bucket(protection, ctx, {"CF-Connecting-IP": "203.0.113.201"})
    assert a == b, "/24 aggregation lost — trivial IP rotation would evade detection"


def test_bucket_never_contains_a_raw_address(protection, ctx):
    b = _bucket(protection, ctx, {"CF-Connecting-IP": "203.0.113.44"})
    assert "203.0.113" not in b
    assert b.startswith("anon:")


def test_falls_back_outside_request_context(protection):
    assert protection._get_key_hash(None) == "anonymous"


def test_keyed_callers_are_unaffected(protection, ctx):
    keyed = protection._get_key_hash("dchub_live_example")
    anon = _bucket(protection, ctx, {"CF-Connecting-IP": "203.0.113.44"})
    assert keyed != anon
    assert not keyed.startswith("anon:")


def test_idle_buckets_are_evicted(protection, ctx):
    """Per-IP bucketing makes the key space the public internet; without
    eviction these dicts leak."""
    saved_ttl, saved_sweep = protection._BUCKET_IDLE_SECONDS, protection._BUCKET_SWEEP_EVERY
    protection._BUCKET_IDLE_SECONDS, protection._BUCKET_SWEEP_EVERY = 0, 3
    try:
        protection._bucket_seen.clear()
        for i in range(12):
            with ctx.test_request_context("/", headers={"CF-Connecting-IP": f"10.1.{i}.5"}):
                protection._log_request(None, "/api/v1/map", {"limit": "10000"})
        assert len(protection._bucket_seen) < 12, "idle buckets are not being reclaimed"
    finally:
        protection._BUCKET_IDLE_SECONDS, protection._BUCKET_SWEEP_EVERY = saved_ttl, saved_sweep


def test_eviction_skips_a_held_lock(protection):
    """Dropping a locked bucket would let the next caller build a second lock
    for the same bucket and race the holder."""
    saved_ttl, saved_sweep = protection._BUCKET_IDLE_SECONDS, protection._BUCKET_SWEEP_EVERY
    protection._BUCKET_IDLE_SECONDS, protection._BUCKET_SWEEP_EVERY = 0, 2
    try:
        protection._bucket_seen.clear()
        protection._request_logs.clear()
        protection._locks.clear()
        held = "anon:heldbucket"
        protection._bucket_seen[held] = 0.0
        protection._request_logs[held] = [(0.0, "x", "y", {})]
        protection._locks[held].acquire()
        try:
            for _ in range(6):
                protection._touch_bucket("anon:otherbucket")
            assert held in protection._request_logs, "evicted a bucket whose lock was held"
        finally:
            protection._locks[held].release()
    finally:
        protection._BUCKET_IDLE_SECONDS, protection._BUCKET_SWEEP_EVERY = saved_ttl, saved_sweep
