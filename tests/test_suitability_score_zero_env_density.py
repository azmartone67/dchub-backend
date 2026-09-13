"""compute_suitability_score reads an env_score or density_score of 0 as a real reading, and a
reading it cannot use as missing (2026-09-13).

The environmental and congestion tiers each defaulted a falsy reading to 50 before converting
it. 0 is falsy, so:

- env_score 0, the worst score on its scale (risk 100), scored as risk 50: "moderate_risk", 6 of
  15 points, where "high_risk" gives 2.
- density_score 0, nothing counted within the radius, scored as density 50: "moderate", 5 of 8
  points, where "low" gives 8. The breakdown served tier "moderate" beside the value "Low", the
  level estimate_congestion had served for the same count.

A reading float() cannot use, such as 'N/A', raised ValueError out of the scorer instead of
scoring as missing.

Measured on origin/main 79de5480b, before the fix:
- screen_environmental never serves env_score 0. Across all 45 response shapes its three lookups
  can take, env_score ranged from 25 to 100, and 25 already scores "high_risk". The environmental
  half reaches a score only through a caller that passes its own env.
- estimate_congestion serves density_score 0 when both of its counts are 0, and a count that
  fails is counted as 0: execute_query returns None and the producer reads None as 0. This file
  pins how the scorer reads a 0, not what the producer means by it. (Since fixed in the producer:
  a count that did not run is served as density_score None, which scores as the default.)

  Z1  env_score 0 and 0.0 score the tier that holds risk 100, which the default 50 does not
  Z2  density_score 0 and 0.0 score the tier that holds density 0, which the default 50 does not
  P1  estimate_congestion with nothing counted serves density_score 0 and level Low, and the
      scorer puts that in the tier the level names
  M1  None, no key at all, '' and 'N/A' score exactly as the default 50 does, for both factors,
      and raise nothing
  C1  control: every tier of both factors is reachable by a real reading, so a scorer that
      ignored the reading could not pass Z1, Z2 and M1 together

The scorer is called directly, with each factor in the shape its producer serves. P1 runs the
real estimate_congestion with execute_query replaced by the rows two empty counts return.
Nothing here reads a database or opens a connection.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ABSENT = object()  # the served dict carries no such key at all

NOT_A_READING = {"none": None, "absent": ABSENT, "empty": "", "label": "N/A"}


@pytest.fixture(scope="module")
def sp():
    import site_planner

    assert os.path.samefile(site_planner.__file__, os.path.join(ROOT, "site_planner.py")), site_planner.__file__
    return site_planner


def _tiers(sp, factor, bound):
    """A factor's tiers as (name, spec) pairs, lowest upper bound first."""
    return sorted(sp.DEFAULT_SCORING_WEIGHTS[factor]["thresholds"].items(), key=lambda tier: tier[1][bound])


def _environmental(sp, env_score):
    """Score the environmental factor alone and return its breakdown entry."""
    env = {"flood_risk": "Low", "wetland_risk": "Low", "species_risk": "Low", "risks_identified": []}
    if env_score is not ABSENT:
        env["env_score"] = env_score
    return sp.compute_suitability_score([], None, None, env, None)["breakdown"].get("environmental")


def _congestion(sp, density_score, level):
    """Score the congestion factor alone and return its breakdown entry."""
    congestion = {"level": level, "substations_within_radius": 0, "power_plants_within_radius": 0,
                  "total_generation_mw": 0, "radius_miles": 15}
    if density_score is not ABSENT:
        congestion["density_score"] = density_score
    return sp.compute_suitability_score([], None, None, None, congestion)["breakdown"].get("congestion")


@pytest.mark.parametrize("zero", [0, 0.0], ids=["int", "float"])
def test_z1_an_env_score_of_zero_scores_the_tier_that_holds_risk_100(sp, zero):
    tier, spec = _tiers(sp, "environmental", "max_risk_score")[-1]
    assert spec["max_risk_score"] == 100, (tier, spec)
    # Without this, a table that put risk 50 and risk 100 in one tier would let a scorer that
    # still read 0 as 50 pass the assertion below.
    assert _environmental(sp, 50)["tier"] != tier

    assert _environmental(sp, zero) == {"points": spec["points"], "tier": tier, "value": f"Score {zero}"}


@pytest.mark.parametrize("zero", [0, 0.0], ids=["int", "float"])
def test_z2_a_density_score_of_zero_scores_the_tier_that_holds_density_0(sp, zero):
    tier, spec = _tiers(sp, "congestion", "max_density")[0]
    assert spec["max_density"] >= 0, (tier, spec)
    assert _congestion(sp, 50, "Moderate")["tier"] != tier

    assert _congestion(sp, zero, "Low") == {"points": spec["points"], "tier": tier, "value": "Low"}


def test_p1_nothing_counted_scores_the_tier_the_producer_named(sp, monkeypatch):
    counts = []

    def empty_count(query, params=None, fetchone=False):
        # The row COUNT(*) returns when nothing lies inside the box.
        counts.append(query)
        if "FROM substations" in query:
            return {"sub_count": 0}
        if "FROM discovered_power_plants" in query:
            return {"plant_count": 0, "total_mw": 0}
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(sp, "execute_query", empty_count)
    congestion = sp.estimate_congestion(39.0440, -77.4870)
    assert len(counts) == 2, counts
    assert (congestion["density_score"], congestion["level"]) == (0, "Low"), congestion

    scored = sp.compute_suitability_score([], None, None, None, congestion)["breakdown"]["congestion"]
    _, spec = _tiers(sp, "congestion", "max_density")[0]
    assert scored == {"points": spec["points"], "tier": congestion["level"].lower(), "value": congestion["level"]}


@pytest.mark.parametrize("reading", list(NOT_A_READING.values()), ids=list(NOT_A_READING))
def test_m1_an_env_score_it_cannot_read_scores_as_the_default_and_raises_nothing(sp, reading):
    served = "N/A" if reading is ABSENT or reading is None else reading
    assert _environmental(sp, reading) == dict(_environmental(sp, 50), value=f"Score {served}")


@pytest.mark.parametrize("reading", list(NOT_A_READING.values()), ids=list(NOT_A_READING))
def test_m1_a_density_score_it_cannot_read_scores_as_the_default_and_raises_nothing(sp, reading):
    assert _congestion(sp, reading, "Unknown") == _congestion(sp, 50, "Unknown")


def test_c1_control_every_environmental_tier_is_reachable_by_a_real_score(sp):
    below = 0
    for tier, spec in _tiers(sp, "environmental", "max_risk_score"):
        env_score = 100 - (below + spec["max_risk_score"]) / 2  # inside the tier, off its edges
        assert _environmental(sp, env_score) == {"points": spec["points"], "tier": tier, "value": f"Score {env_score}"}
        below = spec["max_risk_score"]


def test_c1_control_every_congestion_tier_is_reachable_by_a_real_density(sp):
    below = 0
    for tier, spec in _tiers(sp, "congestion", "max_density"):
        density = (below + spec["max_density"]) // 2  # inside the tier, off its edges
        assert _congestion(sp, density, "Unknown") == {"points": spec["points"], "tier": tier, "value": "Unknown"}
        below = spec["max_density"]
