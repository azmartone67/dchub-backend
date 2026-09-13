"""compute_suitability_score reads a served distance of 0.0 as a real distance, and a
distance it cannot read as missing (2026-09-13).

The substation, transmission and gas tiers each defaulted a falsy distance to 999 mi
before converting it. 0.0 is falsy, so a site 0.0 mi away scored in the 999 mi tier:
"poor" (2 of 25 points) for a substation, "poor" (2 of 15) for a transmission line and
"limited" (0 of 6) for a gas pipeline, while 0.04 mi scored "excellent" in all three.
find_nearest_transmission and find_nearby_gas_pipelines round the distance to 0.1
before serving it, so a line or pipeline within 0.05 mi of the site serves exactly 0.0.

A distance float() cannot read raised instead of counting as missing. The live
transmission fallback that #4524 removed served 'N/A (live query)', which is truthy,
so float() raised ValueError, and the composite score served its power_grid factor as
unavailable.

  Z1  0.0 and 0 score the nearest tier, exactly as 0.04 does, for each of the three
  M1  None, no distance at all, '' and 'N/A (live query)' score in the 999 mi tier and
      raise nothing, for each of the three
  C1  control: every tier is reachable by a real distance, so a scorer that ignored the
      distance could not pass both Z1 and M1

The scorer is called directly, with each factor in the shape its lookup serves. It
reads no database and opens no connection.
"""
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FACTORS = ["substation_proximity", "transmission_proximity", "gas_access"]

ABSENT = object()  # the served dict carries no distance_miles key at all


@pytest.fixture(scope="module")
def sp():
    import site_planner

    assert os.path.samefile(site_planner.__file__, os.path.join(ROOT, "site_planner.py")), site_planner.__file__
    return site_planner


def _score(sp, factor, distance):
    """Score one factor alone and return its breakdown entry."""
    served = {} if distance is ABSENT else {"distance_miles": distance}
    substations, transmission, gas = [], None, None
    if factor == "substation_proximity":
        substations = [{"name": "ASHBURN", "voltage_kv": 230.0, **served}]
    elif factor == "transmission_proximity":
        transmission = {"line_name": "PLEASANT VIEW", "voltage_kv": 230.0, **served}
    else:
        gas = {"count": 1, "nearest_pipeline": {"name": "TRANSCO", **served}}
    result = sp.compute_suitability_score(substations, transmission, None, None, None, gas=gas)
    return result["breakdown"].get(factor)


def _nearest_and_farthest(sp, factor):
    """The factor's nearest tier and its 999 mi tier, as (name, spec) pairs."""
    tiers = sorted(sp.DEFAULT_SCORING_WEIGHTS[factor]["thresholds"].items(),
                   key=lambda tier: tier[1]["max_miles"])
    nearest, farthest = tiers[0], tiers[-1]
    # Without these, a scorer that put 0.0 in the 999 mi tier could satisfy Z1.
    assert farthest[1]["max_miles"] == 999, farthest
    assert nearest[1]["points"] > farthest[1]["points"], (nearest, farthest)
    return nearest, farthest


@pytest.mark.parametrize("zero", [0.0, 0], ids=["float", "int"])
@pytest.mark.parametrize("factor", FACTORS)
def test_z1_a_distance_of_zero_scores_the_nearest_tier(sp, factor, zero):
    (tier, spec), _ = _nearest_and_farthest(sp, factor)

    expected = {"points": spec["points"], "tier": tier, "value": "0.0 mi"}
    assert _score(sp, factor, zero) == expected
    assert _score(sp, factor, 0.04) == expected


NOT_A_DISTANCE = {"none": None, "absent": ABSENT, "empty": "", "live-query-label": "N/A (live query)"}


@pytest.mark.parametrize("distance", list(NOT_A_DISTANCE.values()), ids=list(NOT_A_DISTANCE))
@pytest.mark.parametrize("factor", FACTORS)
def test_m1_a_distance_it_cannot_read_scores_as_missing_and_raises_nothing(sp, factor, distance):
    _, (tier, spec) = _nearest_and_farthest(sp, factor)

    assert _score(sp, factor, distance) == {"points": spec["points"], "tier": tier, "value": "999.0 mi"}


@pytest.mark.parametrize("factor", FACTORS)
def test_c1_control_every_tier_is_reachable_by_a_real_distance(sp, factor):
    for tier, spec in sp.DEFAULT_SCORING_WEIGHTS[factor]["thresholds"].items():
        miles = float(spec["max_miles"])
        assert _score(sp, factor, miles) == {"points": spec["points"], "tier": tier, "value": f"{miles:.1f} mi"}
