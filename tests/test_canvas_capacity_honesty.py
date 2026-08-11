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


# ── An empty shortlist is an ANSWER (2026-08-11) ────────────────────────────
# Found by an external agent running the live planner. region=OH and region=GA
# both returned matched=0, shortlist=[]. Both are CORRECT — Ohio has 9 tracked
# markets and Georgia 8, every one AVOID, so none survive the default
# BUILD,CAUTION filter.
#
# But a bare [] is indistinguishable from "DC Hub has no data for Ohio" and
# from "the tool broke". Downstream, the caller's next step needs a
# market_slug, gets nothing, and reports skipped_unresolved with a
# constraint_check FAIL — so a truthful "no market clears your bar" read as a
# broken execution.


def _ohio_like():
    """9 in-region markets, all AVOID — the shape that produced the report."""
    return [{"market": f"M{i}", "state": "OH", "iso": "PJM", "verdict": "AVOID",
             "composite_score": 29 - i, "time_to_power_months": 15.0}
            for i in range(9)]


def test_the_empty_case_is_real_not_a_filter_bug():
    """Pin the fact first: the rows EXIST, they just score below the bar."""
    mk = _ohio_like()
    assert ssc._rank(mk, "OH", None, {"BUILD", "CAUTION"}) == []
    assert len(ssc._rank(mk, "OH", None, None)) == 9


def test_scored_out_and_no_coverage_are_distinguishable():
    """The whole point. These two produce identical shortlists ([]) and must
    NOT produce identical explanations — one is a scoring result, the other a
    coverage gap, and an agent acts differently on each.
    """
    mk = _ohio_like()
    scored_out = ssc._rank(mk, "OH", None, None)      # 9 rows exist
    no_coverage = ssc._rank(mk, "ZZ", None, None)     # region not tracked
    assert len(scored_out) == 9
    assert len(no_coverage) == 0


def test_payload_explains_an_empty_shortlist():
    src = inspect.getsource(ssc)
    assert '"empty_result"' in src
    assert "no_market_met_the_verdict_filter" in src
    assert "no_tracked_market_in_region" in src
    # It must report how many rows DID match the geography — that count is the
    # difference between "scored out" and "not covered".
    assert '"markets_in_region"' in src
    assert '"verdicts_present"' in src


def test_the_explanation_points_somewhere_actionable():
    """An explanation that dead-ends is only half an answer."""
    src = inspect.getsource(ssc)
    assert "verdict=ALL" in src


def test_empty_result_is_absent_when_there_are_rows():
    """Additive only — a normal response must not grow an empty_result key."""
    src = inspect.getsource(ssc)
    assert '**({"empty_result": out_empty} if out_empty else {})' in src
