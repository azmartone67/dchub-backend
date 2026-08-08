"""Guard: the metering board must convict a meter that cannot move, must
ABSOLVE a meter that does move, and must never convict on absence of evidence.

FENCES routes/metering_honesty_master_shell.py. Every test drives the real
shipped lane functions against synthesised observations — no network — because
the live surface can only ever show this IP one state per tool per day, and a
lane that has only ever rendered '?' on live data is an unproven lane.

★★ EVERY LANE IS PROVEN IN BOTH DIRECTIONS. That is the point of this file.
The first live tick returned consistent=PASS spends=? unspendable=FAIL
coherent=PASS — three of the four lanes never rendered a False, and lane 2
never rendered anything BUT '?'. A lane that cannot fail is decoration; a lane
that cannot pass is an alarm nobody can clear. So each one is driven to True,
to False and to None here.

──────────────────────────────────────────────────────────────────────────
★★★ THE DISCRIMINATOR BUG THIS FILE EXISTS TO KEEP DEAD
(test_depth_sliced_answer_that_decrements_is_clean).

The first draft treated `_upgrade`, `upgrade`, `inline_full` and any
`*_total_in_pro` key as evidence the caller got a preview, and abstained
whenever it saw one. Measured live 2026-08-08, get_market_intel carried THREE
`*_total_in_pro` keys and an `_upgrade` while its meter went 1 -> 0 — i.e. the
rule absolved the one tool that was working, and by the same logic would have
absolved get_gas_intelligence too. Depth-slicing ("3 more providers in Pro")
rides on answers that DO cost a full answer. Only `omitted_no_fabrication` —
the payload withdrawn at every tier — makes a budget unspendable.

Live figures, measured 2026-08-08T19:5x UTC from an anonymous seat:
  get_gas_intelligence  cap=2 remaining=[2, 2]  every call WITHDRAWN
  get_market_intel      cap=2 remaining=1 -> 0  (depth-sliced, still billed)
  get_iso_context       no full_answers meter published at all
──────────────────────────────────────────────────────────────────────────

Every "indeterminate" assertion is `is None`, never `not passed` — `assert not
x` passes on False and would let a wrongful conviction through.
"""
from __future__ import annotations

import pytest

from routes.metering_honesty_master_shell import (
    _lane_coherent, _lane_consistent, _lane_spends, _lane_unspendable,
    _population, _read_envelope, _todays_sample, _SAMPLE_POOL)


def obs(remaining=None, cap=None, answer_keys=None, tier="free",
        withdrawn=False, sliced=False, error=False, args=None):
    """Build one observation by running the REAL envelope reader.

    Deliberately not a hand-written dict: the classifier is the thing under
    test, so the tests must go through it or they fence a fiction.
    """
    sc: dict = {}
    if remaining is not None:
        sc["quota"] = {"tier": tier, "full_answers_remaining_today": remaining,
                       "full_answers_cap_today": cap}
    else:
        sc["quota"] = {"tier": tier}
    for k in (answer_keys or ["market", "stats"]):
        sc[k] = 1
    if withdrawn:
        sc["omitted_no_fabrication"] = ["price"]
        sc["_omitted_no_fabrication_total_in_pro"] = 3
    if sliced:
        sc["_top_providers_total_in_pro"] = 9
        sc["_upgrade"] = "buy"
        sc["inline_full"] = True
    if error:
        sc["error"] = "bad input"
    return _read_envelope(sc, args or {"state": "TX"})


def only(checks, tool):
    return next(c for c in checks if c["id"].endswith(f"::{tool}"))


# ── the classifier itself ─────────────────────────────────────────────

def test_withdrawn_payload_is_not_an_answer():
    o = obs(remaining=2, cap=2, withdrawn=True)
    assert o["answer"] == "WITHDRAWN"
    assert "omitted_no_fabrication" in o["withdrawn"]


def test_depth_slice_is_an_answer_not_a_withdrawal():
    """★ THE REGRESSION. Pro-depth slicing is a normal billed answer."""
    o = obs(remaining=1, cap=2, sliced=True)
    assert o["answer"] == "ANSWERED", (
        "a Pro-depth slice is what a paying-tier answer looks like from the "
        "free seat; calling it a preview absolved the one tool that worked")
    assert o["withdrawn"] == []


def test_cta_furniture_is_never_data():
    """_upgrade/inline_full ride on everything and prove nothing."""
    sc = {"quota": {"tier": "free"}, "_upgrade": "buy", "inline_full": True}
    assert _read_envelope(sc, {})["answer"] == "EMPTY"


# ── lane 1: consistent ────────────────────────────────────────────────

def test_vanishing_meter_is_convicted():
    """#2210: a meter that disappears between calls."""
    runs = {"t": [obs(remaining=2, cap=2), obs(remaining=None)]}
    c = only(_lane_consistent(runs), "t")
    assert c["pass"] is False
    assert "ABSENT" in c["detail"]


def test_meter_on_every_call_passes():
    runs = {"t": [obs(remaining=2, cap=2), obs(remaining=1, cap=2)]}
    assert only(_lane_consistent(runs), "t")["pass"] is True


def test_tool_that_never_publishes_a_meter_is_not_convicted():
    """Silence is a defensible contract — absence of evidence is not a defect."""
    runs = {"t": [obs(), obs()]}
    assert only(_lane_consistent(runs), "t")["pass"] is None


# ── lane 2: spends ────────────────────────────────────────────────────

def test_static_meter_with_room_is_convicted():
    """The lane's whole reason to exist — and it must be REACHABLE."""
    runs = {"t": [obs(remaining=2, cap=2, args={"state": "TX"}),
                  obs(remaining=2, cap=2, args={"state": "PA"})]}
    c = only(_lane_spends(runs), "t")
    assert c["pass"] is False
    assert c["critical"] is True


def test_depth_sliced_answer_that_decrements_is_clean():
    """★★★ get_market_intel live: sliced in Pro, meter 1 -> 0. NOT a defect."""
    runs = {"t": [obs(remaining=1, cap=2, sliced=True, args={"market": "a"}),
                  obs(remaining=0, cap=2, sliced=True, args={"market": "b"})]}
    assert only(_lane_spends(runs), "t")["pass"] is True


def test_spent_meter_proves_nothing():
    runs = {"t": [obs(remaining=0, cap=2), obs(remaining=0, cap=2)]}
    c = only(_lane_spends(runs), "t")
    assert c["pass"] is None
    assert "cannot count down" in c["detail"]


def test_withdrawn_payload_defers_to_lane_three():
    """One defect must be one red, not two."""
    runs = {"t": [obs(remaining=2, cap=2, withdrawn=True),
                  obs(remaining=2, cap=2, withdrawn=True)]}
    assert only(_lane_spends(runs), "t")["pass"] is None
    assert only(_lane_unspendable(runs), "t")["pass"] is False


def test_unanswered_call_is_not_billed():
    runs = {"t": [obs(remaining=2, cap=2, error=True),
                  obs(remaining=2, cap=2, error=True)]}
    assert only(_lane_spends(runs), "t")["pass"] is None


# ── lane 3: unspendable ───────────────────────────────────────────────

def test_withdrawn_budget_is_convicted():
    """★ THE GAS CASE, live 2026-08-08: cap=2 remaining=2, payload withdrawn."""
    runs = {"t": [obs(remaining=2, cap=2, withdrawn=True),
                  obs(remaining=2, cap=2, withdrawn=True)]}
    c = only(_lane_unspendable(runs), "t")
    assert c["pass"] is False
    assert "never move" in c["detail"]


def test_real_budget_passes():
    """Lane 3 must be able to say YES, or a green there means nothing."""
    runs = {"t": [obs(remaining=2, cap=2, sliced=True),
                  obs(remaining=1, cap=2, sliced=True)]}
    c = only(_lane_unspendable(runs), "t")
    assert c["pass"] is True
    assert "still exists" in c["detail"]


def test_zero_budget_cannot_be_shown_unspendable():
    runs = {"t": [obs(remaining=0, cap=2, withdrawn=True)]}
    assert only(_lane_unspendable(runs), "t")["pass"] is None


# ── lane 4: coherent ──────────────────────────────────────────────────

def test_remaining_above_cap_is_convicted():
    runs = {"t": [obs(remaining=5, cap=2)]}
    assert only(_lane_coherent(runs), "t")["pass"] is False


def test_meter_that_rises_inside_one_tick_is_convicted():
    runs = {"t": [obs(remaining=1, cap=2), obs(remaining=2, cap=2)]}
    c = only(_lane_coherent(runs), "t")
    assert c["pass"] is False
    assert "ROSE" in c["detail"]


def test_sane_numbers_pass():
    runs = {"t": [obs(remaining=2, cap=2), obs(remaining=1, cap=2)]}
    assert only(_lane_coherent(runs), "t")["pass"] is True


# ── seat and blindness ────────────────────────────────────────────────

@pytest.mark.parametrize("lane", [_lane_consistent, _lane_spends,
                                  _lane_unspendable, _lane_coherent])
def test_a_paid_seat_never_produces_a_verdict(lane):
    """★ If our egress is allowlisted we are not the anonymous caller, and
    nothing we saw describes a real agent. Every lane must abstain."""
    runs = {"t": [obs(remaining=2, cap=2, tier="pro", withdrawn=True),
                  obs(remaining=2, cap=2, tier="pro", withdrawn=True)]}
    c = only(lane(runs), "t")
    assert c["pass"] is None
    assert "pro" in c["detail"]


@pytest.mark.parametrize("lane", [_lane_consistent, _lane_spends,
                                  _lane_unspendable, _lane_coherent])
def test_unreachable_tool_is_never_a_platform_defect(lane):
    """UNREADABLE IS NOT DEAD."""
    runs = {"t": [{"ok": False, "why": "transport ReadTimeout"}]}
    c = only(lane(runs), "t")
    assert c["pass"] is None
    assert "ReadTimeout" in c["detail"]


# ── sampling: the instrument spends what it measures ──────────────────

def test_rotation_never_spends_the_whole_pool_in_one_tick():
    sample = _todays_sample()
    assert 0 < len(sample) < len(_SAMPLE_POOL), (
        "sampling every tool per tick burns this IP's whole anonymous budget "
        "and blinds the board by the next tick (#2439)")
    assert len({t for t, _, _ in sample}) == len(sample)


def test_every_pooled_tool_varies_its_arguments():
    """An unchanged meter must never be explainable as a cached response."""
    for tool, _param, arglist in _SAMPLE_POOL:
        assert len(arglist) >= 2, tool
        assert arglist[0] != arglist[1], tool


def test_pool_is_read_only():
    banned = ("save_", "set_", "subscribe_", "claim_", "bind_", "unlock_",
              "recover_", "execute_plan")
    for tool, _p, _a in _SAMPLE_POOL:
        assert not tool.startswith(banned), tool
        assert tool != "execute_plan"


def test_population_is_built_from_the_executed_sample():
    """#2253: the published population must never be hand-typed."""
    sample = _todays_sample("get_gas_intelligence")

    class _Seat:
        server = "x"
        error = None
    pop = _population(sample, _Seat())
    assert pop["sampled_tools"] == [t for t, _, _ in sample]
    assert pop["seat"].startswith("anonymous")
