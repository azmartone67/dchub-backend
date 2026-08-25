"""The funnel lane must be graded on the AGENT chain, not on claim redemptions.

★ 2026-08-25 defect this pins: `_sense_funnel` walked `steps[]` and broke on the
FIRST step whose name contained "redeem" — `claim_redeemed` (2787) rather than
`agent_redeemed` (126). `kpi_main` therefore tracked a number that cannot fall,
the lane reported "stable within the healthy band" for three days, and the
prescore (redeem_rate/0.5, pinned to 1.0) sent the neediest lane to the BACK of
the reasoning queue — 5 of 30 decisions, while agent_paid sat at 0.

These tests assert the INVARIANT (the grade follows the agent chain), not the
values, so they stay meaningful as the real counts move.

★ Nothing at module scope (see CLAUDE.md — a module-scope failure aborts
collection and silently kills the whole suite).
"""
import pytest


# The real 30d payload shape, 2026-08-25. claim_redeemed is 22x agent_first_call
# and precedes every agent step — exactly the ordering that produced the bug.
def _payload():
    return {
        "ok": True,
        "http": 200,
        "data": {
            "killer_step": "agent_first_call",
            "paywall_sessions": 3385,
            "steps": [
                {"step": "paywall_sessions", "count": 3385},
                {"step": "claims_minted", "count": 2995},
                {"step": "claim_redeemed", "count": 2787},
                {"step": "agent_redeemed", "count": 126},
                {"step": "agent_key_issued", "count": 126},
                {"step": "agent_first_call", "count": 83},
                {"step": "agent_upsell", "count": 11},
                {"step": "agent_click", "count": 0},
                {"step": "agent_paid", "count": 0},
                {"step": "human_redeemed", "count": 8},
                {"step": "human_key_issued", "count": 8},
                {"step": "human_paid", "count": 0},
            ],
        },
    }


@pytest.fixture()
def sensed(monkeypatch):
    import routes.brain_lane_driver as d
    monkeypatch.setattr(d, "_req", lambda *a, **k: _payload())
    return d._sense_funnel()


def test_kpi_main_is_the_agent_first_call_not_the_claim_step(sensed):
    """★ The core property. 83 is the killer step; 2787 is the claim step."""
    assert sensed["kpi_main"] == 83.0
    assert sensed["kpi_main"] != 2787.0


def test_the_claim_step_is_carried_as_context_under_an_honest_name(sensed):
    assert sensed["claim_redeemed_30d"] == 2787
    # The old key asserted a claim count under an agent-sounding name.
    assert "claims_redeemed_30d" not in sensed


def test_the_whole_agent_chain_is_visible_to_the_reasoner(sensed):
    """A diagnosis cannot cite a step the sense dict never carried."""
    for k, want in (("agent_redeemed_30d", 126), ("agent_key_issued_30d", 126),
                    ("agent_first_call_30d", 83), ("agent_upsell_30d", 11),
                    ("agent_click_30d", 0), ("agent_paid_30d", 0),
                    ("human_paid_30d", 0)):
        assert sensed[k] == want, k


def test_activation_rate_is_agent_side_on_both_terms(sensed):
    """83/126 — NOT 126/2787. The claim→agent gap is ~81% attribution loss
    (mint-cliff `unattributable_no_session`); publishing it as a rate would
    read as 'the agent left'."""
    assert sensed["activation_rate"] == pytest.approx(83 / 126, abs=1e-3)
    assert sensed["redeem_unattributed_30d"] == 2787 - 126 - 8


def test_prescore_ranks_the_funnel_by_activation_not_by_claim_redeems(sensed):
    """★ The bug's second half: the old prescore pinned to 1.0 = healthiest,
    so the funnel lane was reasoned about least."""
    import routes.brain_lane_driver as d
    score = d._prescore("funnel", sensed)
    assert score < 1.0, "a funnel with 0 paid must not pre-score as maximally healthy"
    assert score == pytest.approx(min(1.0, (83 / 126) / 0.8), abs=1e-3)


def test_a_renamed_step_reads_zero_rather_than_binding_to_a_neighbour(monkeypatch):
    """Exact-name matching: if `agent_first_call` is renamed upstream the lane
    must go to 0 (a visible regression), never silently grab an adjacent step."""
    import routes.brain_lane_driver as d
    p = _payload()
    for st in p["data"]["steps"]:
        if st["step"] == "agent_first_call":
            st["step"] = "agent_first_call_v2"
    monkeypatch.setattr(d, "_req", lambda *a, **k: p)
    out = d._sense_funnel()
    assert out["kpi_main"] == 0.0
    assert out["agent_upsell_30d"] == 11, "the other steps must still read"


def test_an_empty_payload_does_not_crash_the_tick(monkeypatch):
    import routes.brain_lane_driver as d
    monkeypatch.setattr(d, "_req", lambda *a, **k: {"ok": False, "data": {}})
    out = d._sense_funnel()
    assert out["kpi_main"] == 0.0
    assert out["activation_rate"] == 0.0
