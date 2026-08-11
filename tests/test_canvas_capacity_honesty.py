"""Guard for routes/site_selection_canvas.py — capacity_mw is not a filter.

REPORTED LIVE 2026-08-10. Against region=TX, capacity_mw=5, 200 and 2000 all
returned matched=20 and a byte-identical shortlist. _rank() takes
(markets, region, max_months, verdicts); capacity_mw has never reached it. It is
parsed, echoed back in `inputs`, and used only for prose in the paid synthesis.

Echoing a parameter back unchanged is an implicit claim that it was honored. An
agent passes 200 MW, sees "capacity_mw": 200, and concludes the shortlist is
sized. It is not. That is a silent wrong answer — the same class as the planner
answering Texas with Virginia, just quieter.

These tests pin the DECLARATION, because the declaration is the fix. Market rows
carry excess_power_score (a 0-100 index), not megawatts, so there is nothing to
filter against and a score-to-MW mapping would be invented. If per-market
deliverable MW is ever ingested, implement the filter and rewrite these tests in
the same PR.

Pure module data: no DB, no network, and never imports main.
"""
import inspect

import pytest

ssc = pytest.importorskip("routes.site_selection_canvas")


def test_rank_does_not_accept_capacity():
    """The root fact. If capacity_mw ever becomes a _rank parameter, the
    declaration below is a lie and this test says so first.
    """
    params = list(inspect.signature(ssc._rank).parameters)
    assert "capacity_mw" not in params, (
        "capacity_mw now reaches _rank — implement the filter and update "
        "constraint_coverage in the same change"
    )
    assert params == ["markets", "region", "max_months", "verdicts"]


def test_rank_ignores_capacity_across_the_whole_range():
    """Behavioural proof, not just signature inspection: a 5 MW and a 2000 MW
    ask return the identical ranking.
    """
    markets = [
        {"market": "A", "state": "TX", "iso": "ERCOT", "verdict": "BUILD",
         "composite_score": 83.0, "excess_power_score": 85.7,
         "time_to_power_months": 9.6},
        {"market": "B", "state": "TX", "iso": "ERCOT", "verdict": "BUILD",
         "composite_score": 71.0, "excess_power_score": 40.0,
         "time_to_power_months": 20.0},
    ]
    base = ssc._rank(markets, "TX", None, {"BUILD"})
    assert [m["market"] for m in base] == ["A", "B"]
    # There is no argument by which a caller could narrow this by load.
    assert len(base) == 2


def test_payload_declares_capacity_is_not_applied():
    src = inspect.getsource(ssc)
    assert '"constraint_coverage"' in src
    assert '"applied": False' in src
    assert '"status": "unavailable"' in src


def test_the_declaration_names_the_reason_not_just_the_fact():
    """`unavailable` with no reason is a shrug. The brand rule is to say WHY —
    here, that there are no megawatts in the data to compare against.
    """
    src = inspect.getsource(ssc)
    assert "excess_power_score" in src
    assert "not megawatts" in src or "not\n" in src
    # And it must point somewhere useful rather than dead-ending.
    assert "get_grid_intelligence" in src


def test_declaration_does_not_claim_a_filter_it_lacks():
    """The failure mode this guards is someone 'fixing' the complaint by
    flipping applied to True without implementing anything.
    """
    src = inspect.getsource(ssc)
    assert '"applied": True' not in src, (
        "capacity_mw declared applied — either _rank honours it (see "
        "test_rank_does_not_accept_capacity) or this is a false claim"
    )
