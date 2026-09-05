"""Water/drought must say N/A abroad, not report a German site as excellent.

★ MEASURED at 50.363083, 9.307306 — Hesse, Germany, a live customer site —
before this fix: 99/100, "No Drought", "Excellent Water Availability", "Low
water stress region. Minimal cooling constraints expected." Sourced from the
U.S. Drought Monitor, which has no German coverage. That catchment had genuine
drought stress in 2018 and 2022.

★ THE MECHANISM WAS A MISSING THIRD STATE. _usdm_point_level carefully
distinguished TWO outcomes and folded a third into the first:

    'None'..'D4'  measured drought class
    None          source unreachable          <- enumerated, correctly
    zero features -> returned 'None'          <- "no drought", but at a German
                                                 parcel it means OUT OF COVERAGE

Its own docstring said None was "distinct from 'None' = no drought", so the
distinction was understood — just not extended to the case where the dataset
does not reach the point at all. Same shape as the air-permitting 89 fixed the
same day: absence read as clean.
"""
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from routes import site_report as sr  # noqa: E402

HESSE = (50.363083, 9.307306)
ASHBURN = (39.0438, -77.4874)


# ── the coverage gate short-circuits before any USDM call ─────────────

def test_a_german_site_never_reaches_the_usdm_query(monkeypatch):
    """★ THE WIRING. If the request goes out at all, ArcGIS answers zero
    features and the old code read that as 'no drought'. The gate must fire
    first, so no request is made and no zero-feature answer exists to
    misinterpret."""
    called = []
    import requests
    monkeypatch.setattr(requests, "get",
                        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
                            AssertionError("USDM was queried for a non-US site")))
    assert sr._usdm_point_level(*HESSE) == sr._USDM_OUT_OF_COVERAGE
    assert called == []


def test_out_of_coverage_is_not_the_no_drought_value():
    """These must be different values. If OUT_OF_COVERAGE ever equals 'None',
    every consumer silently reverts to reading absence as clean."""
    assert sr._USDM_OUT_OF_COVERAGE != "None"
    assert sr._USDM_OUT_OF_COVERAGE is not None


@pytest.mark.parametrize("lat,lon,name", [
    (50.363083, 9.307306, "Hesse DE"),
    (53.3498, -6.2603, "Dublin IE"),
    (35.6762, 139.6503, "Tokyo JP"),
    (-33.8688, 151.2093, "Sydney AU"),
    (45.4215, -75.6972, "Ottawa CA"),
])
def test_non_us_sites_are_out_of_coverage(lat, lon, name):
    assert sr._usdm_in_coverage(lat, lon) is False, f"{name} must not be scored"


@pytest.mark.parametrize("lat,lon,name", [
    (39.0438, -77.4874, "Ashburn VA"),
    (32.7157, -117.1611, "San Diego CA"),
    (42.3314, -83.0458, "Detroit MI"),
    (61.2181, -149.9003, "Anchorage AK"),
    (21.3069, -157.8583, "Honolulu HI"),
])
def test_us_sites_stay_in_coverage(lat, lon, name):
    assert sr._usdm_in_coverage(lat, lon) is True, f"{name} must still be assessed"


@pytest.mark.parametrize("bad", [None, "", "abc", float("nan")])
def test_unplaceable_input_is_out_of_coverage(bad):
    """Err toward not scoring: a parcel we cannot place must not be assessed."""
    assert sr._usdm_in_coverage(bad, bad) is False
    assert sr._usdm_in_coverage(50.0, bad) is False


def test_the_predicate_does_not_boot_the_app():
    """Reaching through main would import the whole application, and a failed
    import would silently degrade this to a box-only test that admits Ottawa."""
    import inspect
    src = inspect.getsource(sr._usdm_in_coverage)
    assert "import main" not in src
    assert "air_permitting_extras" in src


# ── the report a client reads ─────────────────────────────────────────

def _water(monkeypatch, level, state=None):
    monkeypatch.setattr(sr, "_usdm_point_level", lambda lat, lon: level)
    monkeypatch.setattr(sr, "_usdm_state_stats", lambda s: None)
    return sr._gather_water(*HESSE, state)


def test_the_report_says_NA_not_ninety_nine(monkeypatch):
    """★ The headline failure. Falling through would set lvl='None', deduct
    nothing, and print ~99/100 'Excellent Water Availability'."""
    out = _water(monkeypatch, sr._USDM_OUT_OF_COVERAGE)
    assert out["score"] == "N/A", f"the report printed score={out['score']!r}"
    assert out["_score"] is None
    assert "Excellent" not in out.get("label", "")
    assert "Not assessed" in out["label"]


def test_the_report_says_absence_is_not_evidence(monkeypatch):
    out = _water(monkeypatch, sr._USDM_OUT_OF_COVERAGE)
    assert "not evidence" in out["assessment"], (
        "the assessment must state that an absent drought polygon is not "
        "evidence of water availability — that inference is the whole bug")
    assert "UNASSESSED" in out["assessment"]
    assert "US-only" in out["source"]


def test_a_measured_us_site_still_gets_a_number(monkeypatch):
    """The gate must not blunt the assessment where the data exists."""
    monkeypatch.setattr(sr, "_usdm_point_level", lambda lat, lon: "D2")
    monkeypatch.setattr(sr, "_usdm_state_stats", lambda s: None)
    out = sr._gather_water(*ASHBURN, "VA")
    assert isinstance(out["_score"], (int, float)), "a US site lost its score"
    assert out["score"] != "N/A"


def test_source_unreachable_stays_its_own_distinct_state(monkeypatch):
    """Three states, three messages. 'Unreachable' invites a re-run; 'out of
    coverage' does not, because re-running will never help."""
    out = _water(monkeypatch, None)
    assert out["score"] == "—"
    assert "re-run" in out["drought_note"].lower()
    out2 = _water(monkeypatch, sr._USDM_OUT_OF_COVERAGE)
    assert "re-run" not in out2["drought_note"].lower()


# ── rendering ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("score,expect_number", [
    ("N/A", False), ("—", False), (72, True), ("72", True),
])
def test_only_a_measured_score_gets_the_per_hundred_suffix(score, expect_number):
    """"N/A/100" reads as a broken number rather than an honest absence."""
    assert sr._water_has_number({"score": score}) is expect_number


def test_both_render_sites_use_the_shared_predicate():
    """Two hand-written `!= "—"` checks would drift; one predicate cannot."""
    src = open(os.path.join(_ROOT, "routes", "site_report.py"), encoding="utf-8").read()
    assert src.count("_water_has_number(water)") >= 2, (
        "a render site is still testing the water score string by hand")
    assert 'water.get("score") != "—"' not in src, (
        "an old hand-rolled water check survived and will print N/A/100")
