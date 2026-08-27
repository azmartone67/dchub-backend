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


def _top_level_func_src(rel_path, name):
    """Source of a top-level ``def <name>`` sliced from a file's TEXT.

    Deliberately avoids importing the module: this suite never imports the
    Flask app / DB (see tests/conftest.py), and CI runs pytest with no
    DATABASE_URL / JWT_SECRET, so ``import main`` would raise there. Reading
    the source text keeps this a pure source-level regression guard."""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lines = open(os.path.join(root, rel_path), encoding="utf-8").read().splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.startswith(f"def {name}(") or l.startswith(f"def {name} "))
    body = [lines[start]]
    for l in lines[start + 1:]:
        if l and not l[0].isspace():   # next col-0 construct ends the function
            break
        body.append(l)
    return "\n".join(body)


def test_market_dcpi_signature_takes_coords():
    """_market_dcpi must accept lat/lng — the nearest-metro fallback needs them.
    Regression: original was _market_dcpi(city, state) and callers dropped coords."""
    from routes.facility_profile_page import _market_dcpi
    params = list(inspect.signature(_market_dcpi).parameters)
    assert params[:4] == ["city", "state", "lat", "lng"], params


def test_market_dcpi_prefers_city_then_nearest_then_state():
    """The resolution order must be intact, and the bare state code must NOT be
    a candidate for the exact-match query (that OR-state clause was the
    Dallas→Midland collapse).

    r-market-resolve-geo (2026-08-26) made this FOUR tiers, inserting a
    coordinate step between the exact match and the state-scoped fallbacks —
    every earlier tier was gated on `state`, a US-shaped field, so an
    international facility could not resolve at all. That step is fenced here
    too; behavioural coverage of it (and of the distance guard that stops a
    Brazilian `SC` matching Charleston SC) is in
    tests/test_facility_market_resolution_geo.py, which needs no database.
    """
    from routes.facility_profile_page import _market_dcpi
    src = _src(_market_dcpi)
    # Tier order documented + implemented.
    assert "exact city/metro slug match" in src
    assert "NEAREST metro" in src
    # The coordinate tier: bounded by a lat/lon box, and distance-checked.
    assert "latitude BETWEEN" in src, (
        "the coordinate step is gone — every fallback is gated on `state` "
        "again and international facility pages lose their market context")
    assert "_too_far" in src, (
        "the distance guard is gone — a bare state code can match a market on "
        "another continent, as BR/SC matched Charleston SC (7,395 km)")
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
    src = _top_level_func_src("main.py", "_grid_ext_metrics_for")
    assert "grid_ext_metrics" in src
    assert "iso_lmp_snapshots" in src
    assert "return {}" in src  # fail-soft on error


# ── r-declone-2 (2026-07-17): per-market DCPI differentiation guards ────────
# The master-shell iso_clone_ratio measured 0.653 because (a) the local-
# footprint query was US-only, cloning every international market to its ISO
# baseline, and (b) the linear saturation index compressed real footprint
# differences below the 0.1 output rounding. These guards pin both fixes.

def test_declone_covers_international_markets():
    """gather_metrics_for_market must have an intl footprint branch (city-level,
    non-US rows) and _US_DCPI_ISOS must classify the ISOs correctly — POSOCO/
    AEMO/NORDPOOL markets were 100% cloned because the old query could only
    ever match country='US' rows."""
    from routes.dcpi import (gather_metrics_for_market, _US_DCPI_ISOS,
                             _market_country_scope)
    src = _src(gather_metrics_for_market)
    # r-namesake (2026-08-07): the non-US predicate moved out of this function
    # into _market_country_scope, which the US branch, the intl branch and the
    # page facility list now share. Assert the BEHAVIOUR at its new home rather
    # than the old literal — a string match here would have gone green again
    # the moment someone re-typed the text without wiring it up.
    assert "_market_country_scope" in src, \
        "intl footprint branch missing — international markets re-cloned"
    _intl_sql, _intl_params = _market_country_scope("NGESO", "UK")
    assert "NOT IN ('US', 'USA')" in _intl_sql, \
        "the intl scope no longer excludes US rows"
    # r-namesake-territory: the accepted-country list is PARAMETERIZED now
    # (san-juan also accepts 'PR'), so read the params, not an inline literal.
    _us_sql, _us_params = _market_country_scope("PJM", "VA", 38.9, -77.2)
    assert "NOT IN" not in _us_sql and " IN (" in _us_sql, \
        "the US scope no longer restricts to US rows"
    assert _us_params[:2] == ["US", "USA"], \
        f"US scope accepts {_us_params[:2]!r}, not US/USA"
    assert _market_country_scope("PREPA", "PR", 18.47, -66.1)[1][:3] == \
        ["US", "USA", "PR"], "san-juan must accept its own territory code"
    assert "COUNT(DISTINCT provider)" in src, "operator-diversity signal dropped"
    for intl in ("POSOCO", "AEMO", "NORDPOOL", "NGESO", "ENTSOE-DE"):
        assert intl not in _US_DCPI_ISOS, f"{intl} misfiled as US ISO"
    for us in ("WECC", "PJM", "ERCOT", "CAISO", "MISO"):
        assert us in _US_DCPI_ISOS, f"{us} missing from US ISO set"


def test_declone_saturation_survives_output_rounding():
    """The log-scaled index must turn a real small-metro footprint difference
    (5 vs 13 facilities — tempe vs henderson class) into >=0.1 months of
    queue-wait spread on a typical 28-month ISO anchor, i.e. visible after
    round(x, 1). The old linear fac/80 term produced 0.014 months here and
    every published value collapsed back to the ISO clone."""
    from routes.dcpi import _log_sat
    d_sat = 0.40 * (_log_sat(13, 400.0) - _log_sat(5, 400.0))
    assert 28.0 * 0.45 * d_sat >= 0.1, \
        "saturation resolution below output rounding — markets re-clone"
    # Determinism + bounds: pure function of the row inputs, clipped 0..1.
    assert _log_sat(0, 400.0) == 0.0
    assert _log_sat(400, 400.0) == 1.0
    assert _log_sat(10_000, 400.0) == 1.0   # clipped, never >1
    assert _log_sat(5, 400.0) == _log_sat(5, 400.0)


def test_declone_is_bounded_and_documented():
    """The modifier must stay a bounded delta anchored on the ISO baseline
    (×0.90..×1.35 on queue wait), skip hand-calibrated override markets, and
    stamp an auditable `differentiation` note into data_basis."""
    from routes.dcpi import gather_metrics_for_market
    src = _src(gather_metrics_for_market)
    assert "0.90 + 0.45 * _sat" in src, "bounded queue-wait modifier changed"
    assert "if not _override_applied:" in src, \
        "curated flagship overrides no longer protected"
    assert '"differentiation"' in src, "auditable differentiation note dropped"
    assert "no_local_facility_footprint" in src, \
        "footprint-less markets no longer documented as honest ISO baseline"
