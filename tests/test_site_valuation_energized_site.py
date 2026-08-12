"""Energized-site valuation fence — 2026-08-12.

THE ONE SENTENCE
----------------
These tests FAIL if the valuation engine prices an already-energized parcel
as though it were raw land in a multi-year interconnection queue.

WHAT WENT WRONG (measured on a real client report, Hopedale OH, 2026-08-04)
--------------------------------------------------------------------------
An 89.9-acre parcel with an on-site 56 MVA substation, a live 69 kV
interconnection and 22.7 MW of DEMONSTRATED billed load (120.1 GWh over two
years) was valued at $199,500/MW — the engine reported `readiness applied:
raw land` and a 20.4-month grid time-to-power, then recommended $59.1M of
new CCGT to "bypass the ISO queue" at a site that is already energized.

Three defects, all on the same path:

1. FALSY ZERO (five sites). `grid_ttp_months = 0` means "power flows today."
   It was discarded at every hop between the form field and the cost model:
     - the page JS returned undefined for `g > 0`
     - the endpoint had literal 0 inside `not in (None, "", 0)`
     - the overrides dict used `user_grid_ttp or <market queue>`
     - `_firm_grid` used `... and user_grid_ttp`
     - `_compute_scenarios` used `overrides.get(...) or <dcpi ttp>`
   The single value that describes an energized site was the single value
   the path could not carry. A field whose own HTML `min` is 0.

2. READINESS NEVER REACHED THE COST MODEL. The six readiness flags scaled
   the valuation multiplier but were not passed to `_compute_scenarios`, so
   a site reporting a substation INSIDE the parcel was still charged the
   $8M new-substation build plus a full greenfield interconnect. That
   inflated grid-only capex and handed best-fit to gas-BTM.

3. MOAT ATTENUATION COULD NOT FIRE FOR `developing`. The classifier defines
   `developing` as the AVOID subtype "whose verdict mainly tracks readiness"
   — yet attenuation was gated to `constrained` only, so the one subtype
   defined by readiness was the one readiness could not move.

WHY THESE ASSERTIONS AND NOT A GOLDEN NUMBER
--------------------------------------------
The tests assert DIRECTION and INVARIANTS (a stated 0 survives; an on-site
substation is not billed; an energized parcel outprices bare dirt), never a
specific dollar figure. The multipliers are tunable by design — pinning
$440,800/MW would turn every future recalibration into a false failure.

MUTATION-VERIFIED (2026-08-12)
------------------------------
Each fix was reverted one at a time; the mutation was confirmed APPLIED by
grep before the run, and the baseline confirmed green after each restore:
  - `_coerce_ttp_months` discards 0 again ....... 1 failed / 18 passed
  - on-site substation billed again ............ 1 failed / 18 passed
  - `_compute_scenarios` uses `or` again ....... 2 failed / 17 passed
  - moat re-gated to `constrained` only ........ 2 failed / 17 passed
  - moat widened to `weak_demand` .............. 1 failed / 18 passed
No mutation left the file green.

COVERAGE GAP — STATED, NOT PAPERED OVER
---------------------------------------
Three of the five falsy-zero sites are fenced above. The other two live
inside the `site_value` Flask handler (the `overrides` dict assembly and
`_firm_grid`), which needs the app and a live DB to reach, so they are
fixed but NOT covered by a test — verified by inspection only. A handler
that regressed either one would still ship green through this file.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes.site_valuation_engine import (  # noqa: E402
    _CAPEX_SUBSTATION_BUILD_USD,
    _coerce_ttp_months,
    _compute_scenarios,
    _compute_valuation,
)

# A slow-queue AVOID market, subtype `developing` — the Canton read the
# Hopedale site snapped to (DCPI composite 28.1, excess 37.4, TTP 20 mo).
DCPI_DEVELOPING_AVOID = {
    "available": True,
    "verdict": "AVOID",
    "verdict_subtype": "developing",
    "excess_power_score": 37.4,
    "constraint_score": 40.0,
    "time_to_power_months": 20.0,
    "iso": "PJM",
}
GAS = {"$/MWh_ccgt_avg": 21.42}

ENERGIZED = {"grid_interconnect_ready": True, "substation_on_site": True}
RAW_LAND = {}


def _scenarios(readiness=None, ttp=None):
    return _compute_scenarios(
        53, DCPI_DEVELOPING_AVOID, GAS,
        overrides={"readiness": readiness or {}, "live_queue_ttp_months": ttp},
    )


def _value(readiness, scenarios, dcpi=None):
    return _compute_valuation(
        53, 89.9, dcpi or DCPI_DEVELOPING_AVOID,
        {"scenario": "grid_only"}, scenarios, readiness=readiness,
    )


# ── Defect 1: the falsy zero ──────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (0, 0), ("0", 0), (12, 12), ("12", 12), (-5, 0),
    (None, None), ("", None), ("abc", None), (True, None), (False, None),
])
def test_stated_zero_survives_coercion(raw, expected):
    """0 is an answer ('power flows today'), not an absent value."""
    assert _coerce_ttp_months(raw) == expected


def test_stated_zero_reaches_the_cost_model():
    """A stated 0 must beat the market queue, not fall back to it."""
    assert _scenarios(ttp=0)["grid_only"]["time_to_power_months"] == 0
    # and absence still falls back to the DCPI queue read
    assert _scenarios(ttp=None)["grid_only"]["time_to_power_months"] == 20.0


def test_stated_zero_makes_grid_the_fastest_path():
    """An energized site must not be told to build gas to 'skip the queue'."""
    s = _scenarios(readiness=ENERGIZED, ttp=0)
    assert s["grid_only"]["time_to_power_months"] < s["gas_btm"]["time_to_power_months"]


# ── Defect 2: readiness never reached the cost model ──────────────

def test_on_site_substation_is_not_billed_as_a_new_build():
    """You cannot charge $8M to build a substation that is already there."""
    raw = _scenarios(readiness=RAW_LAND)["grid_only"]["capex_usd"]
    energized = _scenarios(readiness=ENERGIZED)["grid_only"]["capex_usd"]
    assert energized < raw - _CAPEX_SUBSTATION_BUILD_USD * 0.9


def test_signed_isa_reduces_but_does_not_erase_interconnect_capex():
    """An ISA settles the allocation; the point-of-interconnection is still built."""
    s = _scenarios(readiness={"grid_interconnect_ready": True})["grid_only"]
    assert 0 < s["capex_usd"] < _scenarios(readiness=RAW_LAND)["grid_only"]["capex_usd"]


# ── Defect 3: moat attenuation for `developing` ───────────────────

def test_developing_avoid_lifts_with_moat_flags_in_hand():
    """The subtype defined by readiness must be movable by readiness."""
    m = _value(ENERGIZED, _scenarios(ENERGIZED, ttp=0))["multipliers"]
    assert m["moat_attenuation_applied"] is True
    assert m["verdict_mult"] > m["verdict_mult_base"]


def test_developing_lift_stays_below_the_no_verdict_default():
    """No demand-side moat here — readiness may approach 0.85, never pass it."""
    every_moat_flag = {"grid_interconnect_ready": True, "substation_on_site": True,
                       "permits_in_hand": True}
    m = _value(every_moat_flag, _scenarios(every_moat_flag, ttp=0))["multipliers"]
    assert m["verdict_mult"] <= 0.85


def test_weak_demand_avoid_gets_no_moat_lift():
    """There is no moat in a market nobody wants to build in."""
    weak = dict(DCPI_DEVELOPING_AVOID, verdict_subtype="weak_demand")
    m = _value(ENERGIZED, _scenarios(ENERGIZED, ttp=0), dcpi=weak)["multipliers"]
    assert m["moat_attenuation_applied"] is False
    assert m["verdict_mult"] == m["verdict_mult_base"]


def test_raw_land_in_the_same_market_gets_no_lift():
    """Attenuation is earned by flags, not granted by subtype."""
    m = _value(RAW_LAND, _scenarios(RAW_LAND))["multipliers"]
    assert m["moat_attenuation_applied"] is False


# ── The composite regression: Hopedale ────────────────────────────

def test_energized_parcel_outprices_bare_dirt_in_the_same_market():
    """The headline defect: `readiness applied: raw land` on an energized site."""
    raw = _value(RAW_LAND, _scenarios(RAW_LAND))
    energized = _value(ENERGIZED, _scenarios(ENERGIZED, ttp=0))
    assert energized["$/mw_mid"] > raw["$/mw_mid"] * 1.5
    assert energized["$/mw_mid"] <= energized["$/mw_band_ceiling"]
