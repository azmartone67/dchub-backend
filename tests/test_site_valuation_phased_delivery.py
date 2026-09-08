"""Phased-power-delivery fence — 2026-09-08.

THE ONE SENTENCE
----------------
These tests FAIL if the valuation engine prices a MW that energizes years
from now as though it energized today.

WHAT WENT WRONG (measured live, 2026-09-08)
-------------------------------------------
A 1,800 MW PJM campus with a PPL-confirmed delivery schedule — 300 MW in
Q2 2028, the balance ramping monthly for another five years — was valued at
$939.2M. The same POST with `grid_ttp_months` set to 6, 18, 24, 60 and 99
returned $939.2M every time:

    ttp | site_value_mid | per_mw_mid | ceiling    | firm_powered
      6 | $939.2M        | $483,132   | $1,600,000 | True
     18 | $939.2M        | $483,132   | $1,600,000 | True
     24 | $939.2M        | $483,132   | $1,200,000 | False
     60 | $939.2M        | $483,132   | $1,200,000 | False
     99 | $939.2M        | $483,132   | $1,200,000 | False

Time-to-power moved only the CEILING, and the ceiling is inert whenever the
computed $/MW sits below it (here $483K against a $1.2M cap), so the input
had no path to the number at all. The engine had `_npv()` and a discount
rate, but used them only for the LCOE cost scenarios — the site valuation
itself was `per_mw × target_mw`, undiscounted, delivery-blind.

Two distinct errors, both fenced below:
1. NO TIME VALUE. Every MW priced at the valuation date regardless of when
   it energizes. At an 8% discount, 1,800 MW arriving over 2028-2034 is
   worth ~74% of 1,800 MW arriving today — a ~$230M overstatement on one
   deal.
2. NO WAY TO STATE A RAMP. `grid_ttp_months` is a single scalar; a real
   utility commitment is a schedule. There was no input that could carry
   "300 MW in Q2 2028, then +18.75 MW/month."

WHY THESE ASSERTIONS AND NOT A GOLDEN NUMBER
--------------------------------------------
Following test_site_valuation_energized_site.py: assert DIRECTION and
INVARIANTS (later delivery is worth strictly less; no schedule reproduces
v2.2 exactly; the ramp is never charged twice), never a specific dollar
figure. The discount rate and multipliers are tunable by design.

MUTATION-VERIFIED (2026-09-08) — see the PR body for the run output.
"""
import datetime as dt
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from routes.site_valuation_engine import (  # noqa: E402
    _build_power_schedule,
    _compute_scenarios,
    _compute_valuation,
    _delivery_pv,
    _parse_delivery_month,
)

AS_OF = dt.date(2026, 9, 8)

DCPI = {
    "available": True,
    "verdict": "AVOID",
    "verdict_subtype": "developing",
    "excess_power_score": 23.1,
    "constraint_score": 43.3,
    "time_to_power_months": 24.9,
    "iso": "PJM",
}
GAS = {"$/MWh_ccgt_avg": 21.42}
ENTITLED = {"zoning_approved": True, "permits_in_hand": True}


def _scen(ttp=None):
    return _compute_scenarios(
        1800, DCPI, GAS,
        overrides={"readiness": {}, "live_queue_ttp_months": ttp})


def _value(delivery=None, readiness=None, ttp=None):
    return _compute_valuation(
        1800, 962.0, DCPI, {"scenario": "grid_only"}, _scen(ttp),
        readiness=readiness or {}, delivery=delivery)


# ── Date parsing ──────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["2028-Q2", "2028Q2", "2028-04", "2028-04-01"])
def test_quarter_month_and_day_forms_agree(raw):
    """Q2 2028, 2028-04 and 2028-04-01 are the same instant."""
    assert _parse_delivery_month(raw, AS_OF) == pytest.approx(
        _parse_delivery_month("2028-04", AS_OF), abs=0.05)


@pytest.mark.parametrize("raw", ["garbage", "", None, True, "2028-Q9", "9999999-01"])
def test_unparseable_dates_return_none_not_zero(raw):
    """A bad date must not silently become 'delivered today' (month 0),
    which would price an unknown schedule at full undiscounted value."""
    assert _parse_delivery_month(raw, AS_OF) is None


def test_past_dates_floor_at_zero_not_negative():
    """A schedule date in the past is 'already energized', never a
    negative exponent that would inflate value above nameplate."""
    assert _parse_delivery_month("2020-Q1", AS_OF) == 0.0


# ── Defect 1: time value of delivered MW ──────────────────────────

def test_later_delivery_is_worth_strictly_less():
    """THE fence. Same 1,800 MW, later energization, lower value."""
    vals = []
    for month in (0, 24, 60, 99):
        d = _delivery_pv([(1800, month)], 1800, 72, 0.08)
        vals.append(_value(d)["site_value_usd_mid"])
    assert vals == sorted(vals, reverse=True), vals
    assert vals[0] > vals[-1], "delivery timing had no effect on value"


def test_ramped_delivery_discounts_below_instant_delivery():
    """The measured regression: 1,800 MW over 2028-2034 must price below
    1,800 MW today by a material margin, not a rounding difference."""
    today = _value(_delivery_pv([(1800, 0)], 1800, 72, 0.08))
    tranches, _ = _build_power_schedule(
        {"power_ramp": {"first_mw": 300, "first_date": "2028-Q2",
                        "ramp_mw_per_month": 18.75}}, 1800, AS_OF)
    ramped = _value(_delivery_pv(tranches, 1800, 72, 0.08))
    assert ramped["site_value_usd_mid"] < today["site_value_usd_mid"] * 0.85


def test_pv_factor_never_exceeds_one():
    """Delay may only reduce present value. An escalation assumption above
    the discount rate must not make later MW worth MORE than earlier MW."""
    d = _delivery_pv([(1800, 96)], 1800, 72, 0.08, escalation_rate=0.50)
    assert d["pv_factor"] <= 1.0
    assert any("clamp" in w for w in d["warnings"])


def test_pv_equivalent_mw_is_reported_and_below_nameplate():
    """The buyer-facing number: 1,800 MW nameplate is N MW in PV terms."""
    tranches, _ = _build_power_schedule(
        {"power_ramp": {"first_mw": 300, "first_date": "2028-Q2",
                        "ramp_mw_per_month": 18.75}}, 1800, AS_OF)
    d = _delivery_pv(tranches, 1800, 72, 0.08)
    assert 0 < d["pv_equivalent_mw"] < d["nameplate_mw"]


# ── Backward compatibility: no schedule = v2.2 exactly ────────────

def test_no_schedule_is_bit_identical_to_unphased_valuation():
    """A caller who states only target_mw must get the v2.2 number. This
    is what makes the feature additive rather than a silent repricing of
    every existing integration."""
    assert (_value(delivery=None)["site_value_usd_mid"]
            == _value(delivery={"applied": False, "pv_factor": 1.0})["site_value_usd_mid"])


def test_zero_month_delivery_equals_no_schedule():
    """All MW energized today is the identity case."""
    d = _delivery_pv([(1800, 0)], 1800, 72, 0.08)
    assert d["pv_factor"] == 1.0
    assert (_value(d)["site_value_usd_mid"]
            == _value(delivery=None)["site_value_usd_mid"])


def test_empty_or_unusable_schedule_yields_no_delivery_block():
    """No tranches must return None (caller keeps v2.2), never a
    pv_factor of 0 that would zero out the site."""
    assert _delivery_pv([], 1800, 72, 0.08) is None
    assert _delivery_pv([(0, 24)], 1800, 72, 0.08) is None


# ── Defect 2: stating a ramp ──────────────────────────────────────

def test_ramp_rate_and_final_date_disagreement_is_reported():
    """The Plains deal brief states BOTH '+18.75 MW/month' AND 'through
    Q2 2034'. At 18.75 MW/mo the balance lands ~8 months late, so the two
    statements are not the same schedule. The engine must say so rather
    than silently picking one."""
    _, warnings = _build_power_schedule(
        {"power_ramp": {"first_mw": 300, "first_date": "2028-Q2",
                        "ramp_mw_per_month": 18.75, "final_date": "2034-Q2"}},
        1800, AS_OF)
    assert any("disagree" in w for w in warnings), warnings


# 18.75 divides 1,500 evenly (80 steps); 17 and 23.4 do not. A ramp that
# emits `rate` per step instead of `remaining/n` is correct ONLY for the
# even case — mutation testing caught this test passing that mutation when
# it exercised 18.75 alone.
@pytest.mark.parametrize("rate", [18.75, 17.0, 23.4, 7.3, 1000.0])
def test_ramp_totals_target_mw(rate):
    """The ramp must deliver exactly target_mw — no tranche rounding that
    silently sells 1,780 or 1,820 MW."""
    tranches, _ = _build_power_schedule(
        {"power_ramp": {"first_mw": 300, "first_date": "2028-Q2",
                        "ramp_mw_per_month": rate}}, 1800, AS_OF)
    assert sum(mw for mw, _ in tranches) == pytest.approx(1800, abs=0.01)


def test_ramp_never_overshoots_target_in_a_single_step():
    """A rate larger than the remaining balance must deliver the balance,
    not the rate."""
    tranches, _ = _build_power_schedule(
        {"power_ramp": {"first_mw": 300, "first_date": "2028-Q2",
                        "ramp_mw_per_month": 5000.0}}, 1800, AS_OF)
    assert sum(mw for mw, _ in tranches) == pytest.approx(1800, abs=0.01)
    assert all(mw <= 1500.01 for mw, _ in tranches)


def test_explicit_tranches_are_sorted_and_scheduled():
    tranches, _ = _build_power_schedule({"power_schedule": [
        {"mw": 600, "date": "2034-Q2"}, {"mw": 300, "date": "2028-Q2"},
        {"mw": 450, "date": "2032-Q2"}, {"mw": 450, "date": "2030-Q2"},
    ]}, 1800, AS_OF)
    months = [t for _, t in tranches]
    assert months == sorted(months)
    assert sum(mw for mw, _ in tranches) == 1800


def test_schedule_sets_shape_target_mw_sets_scale():
    """A schedule that does not sum to target_mw must move the TIMING but
    not the MW count — and must say so. Otherwise a typo in one tranche
    silently resizes the campus."""
    d = _delivery_pv([(900, 12)], 1800, 72, 0.08)
    assert d["nameplate_mw"] == 1800
    assert any("target_mw" in w for w in d["warnings"])


def test_mw_by_deadline_splits_the_buyers_horizon():
    """A buyer with a 72-month deadline needs to know how many MW actually
    land inside it — the single scalar ttp could never express this."""
    tranches, _ = _build_power_schedule(
        {"power_ramp": {"first_mw": 300, "first_date": "2028-Q2",
                        "ramp_mw_per_month": 18.75}}, 1800, AS_OF)
    d = _delivery_pv(tranches, 1800, 72, 0.08)
    assert 0 < d["mw_by_deadline"] < 1800
    assert d["mw_by_deadline"] + d["mw_after_deadline"] == pytest.approx(1800, abs=0.5)


# ── The ramp must not be charged twice ────────────────────────────

def test_ceiling_test_uses_first_mw_not_full_delivery():
    """`_firm_powered` asks 'is this powered land?' — answered the day the
    first firm MW flows. Reading it off the FULL-delivery date would charge
    the ramp once in the ceiling and again in the pv_factor."""
    d = _delivery_pv([(300, 6), (1500, 96)], 1800, 72, 0.08)
    v = _value(d, readiness=ENTITLED)
    assert v["entitlement"]["firm_powered"] is True
    assert v["entitlement"]["grid_ttp_months"] == pytest.approx(6, abs=0.5)


def test_breakdown_reports_the_ramp_discount_separately():
    """The dollars the ramp removed must be a visible line, not an
    unexplained gap between $/MW × MW and the headline."""
    tranches, _ = _build_power_schedule(
        {"power_ramp": {"first_mw": 300, "first_date": "2028-Q2",
                        "ramp_mw_per_month": 18.75}}, 1800, AS_OF)
    b = _value(_delivery_pv(tranches, 1800, 72, 0.08))["site_value_breakdown"]
    assert b["ramp_pv_discount_usd"] > 0
    assert (b["mw_contribution_usd"] + b["ramp_pv_discount_usd"]
            == pytest.approx(b["mw_contribution_nameplate_usd"], rel=1e-6))


# ── A partial schedule must not be applied as if it were whole ────

def test_unschedulable_balance_is_not_priced_at_the_first_tranche_date():
    """A ramp stating only 'first 300 MW in Q2 2028' has no date for the
    other 1,500 MW. Keeping the first tranche alone would schedule the WHOLE
    campus at month 19 — the most optimistic possible reading of an
    incomplete statement — while the warning said 'ignored'. No usable
    schedule means no schedule."""
    tranches, warnings = _build_power_schedule(
        {"power_ramp": {"first_mw": 300, "first_date": "2028-Q2"}}, 1800, AS_OF)
    assert tranches == []
    assert any("ignored" in w for w in warnings), warnings
    assert _delivery_pv(tranches, 1800, 72, 0.08) is None


def test_single_tranche_covering_all_target_mw_is_still_valid():
    """The legitimate no-ramp case: all target MW on one date."""
    tranches, warnings = _build_power_schedule(
        {"power_ramp": {"first_mw": 1800, "first_date": "2028-Q2"}}, 1800, AS_OF)
    assert len(tranches) == 1
    d = _delivery_pv(tranches, 1800, 72, 0.08)
    assert d["applied"] is True and d["pv_factor"] < 1.0


def test_final_date_before_first_delivery_does_not_partially_apply():
    """A backwards final date must not fall through to a first-tranche-only
    schedule."""
    tranches, warnings = _build_power_schedule(
        {"power_ramp": {"first_mw": 300, "first_date": "2032-Q2",
                        "final_date": "2028-Q2"}}, 1800, AS_OF)
    assert tranches == []
    assert any("not after" in w for w in warnings), warnings
