"""Regression guards for the facility→market resolution + market-coords fixes
(commits 0c297e05 + ee400bc0, 2026-07-06).

Pure in-process source/contract asserts (no DB, no network) — mirrors
tests/test_peace_regression.py. These catch a code-level revert; the live
runtime breach is caught by GET /api/v1/dcpi/resolution-guard (which files a
brain_finding). Both layers are intentional.
"""
import inspect


def _src(fn):
    return inspect.getsource(fn)


def test_market_dcpi_signature_takes_coords():
    """_market_dcpi must accept lat/lng — the nearest-metro fallback needs them.
    Regression: original was _market_dcpi(city, state) and callers dropped coords."""
    from routes.facility_profile_page import _market_dcpi
    params = list(inspect.signature(_market_dcpi).parameters)
    assert params[:4] == ["city", "state", "lat", "lng"], params


def test_market_dcpi_prefers_city_then_nearest_then_state():
    """The three-tier resolution order must be intact, and the bare state code
    must NOT be a candidate for the exact-match query (that OR-state clause was
    the Dallas→Midland collapse)."""
    from routes.facility_profile_page import _market_dcpi
    src = _src(_market_dcpi)
    # Tier order documented + implemented.
    assert "exact city/metro slug match" in src
    assert "NEAREST metro" in src
    # The exact-match query uses the city candidate list, not a bare state OR.
    assert "city_cands" in src
    # The old single-query collapse must be gone.
    assert "OR LOWER(state) = ANY" not in src
    # Nearest-metro fallback ranks by lat/lng distance.
    assert "RADIANS" in src and "POWER(latitude" in src


def test_load_markets_dynamic_uses_median_centroid():
    """_load_markets_dynamic must emit a MEDIAN facility centroid (percentile_cont),
    not None and not a mean — a mean gets dragged into the Gulf by bad coord rows,
    and None re-NULLs market_power_scores on every recompute."""
    from routes.dcpi import _load_markets_dynamic
    src = _src(_load_markets_dynamic)
    assert "percentile_cont(0.5)" in src, "median centroid missing"
    # It must select + emit lat/lon (the tuple can no longer be (..., None, None)).
    assert "None, None))" not in src, "loader still emits hardcoded None coords"
    assert "lat, lon" in src


def test_grid_ext_metrics_helper_is_failsoft():
    """The grid extended-metrics wiring must be fail-soft (never break the base
    grid payload) and pull from grid_ext_metrics / iso_lmp_snapshots."""
    import main
    src = _src(main._grid_ext_metrics_for)
    assert "grid_ext_metrics" in src
    assert "iso_lmp_snapshots" in src
    assert "return {}" in src  # fail-soft on error
