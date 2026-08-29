"""Guard for routes/site_selection_canvas.py — an empty shortlist ships the rows.

REPORTED LIVE by three external agents in one round (2026-08-29), against the
#2582 build that already returned an explanation:

  Grok       ran {region:"OH"} and {region:"OH", verdict:"AVOID"} back to back:
             "I can now tell a human 'Ohio has 9 markets, all AVOID'. I still
             cannot show scores without a second call. Bar not cleared."
  Gemini     "Returning matches: 0 for Ohio when 9 sites were calculated at
             composite 28-29 (AVOID) looks like a data gap rather than a
             location risk" — asked for verdict counts carrying the ZEROES.
  Perplexity asked for next_best_action on the empty path "especially after an
             empty result", and for applied_filters distinct from raw inputs.

A hint to re-run costs a round trip an agent may not spend; a stateless caller
that has already composed its answer never spends it. So the rows ride the first
response.

These tests use the REAL Ohio shape (9 markets, all AVOID, composite 28-29) so
the fixture cannot drift away from the case that produced the report.

Pure module data: no DB, no network, never imports main.
"""
import pytest

ssc = pytest.importorskip("routes.site_selection_canvas")


# The live Ohio set as measured 2026-08-29 — 9 markets, every one AVOID.
OHIO = [
    {"market_name": "Luckey",       "market_slug": "luckey-oh",       "state": "OH",
     "iso": "PJM", "verdict": "AVOID", "composite_score": 29.4,
     "excess_power_score": 41.0, "constraint_score": 18.0, "time_to_power_months": 40},
    {"market_name": "Akron",        "market_slug": "akron-oh",        "state": "OH",
     "iso": "PJM", "verdict": "AVOID", "composite_score": 29.3,
     "excess_power_score": 40.0, "constraint_score": 17.0, "time_to_power_months": 42},
    {"market_name": "Canton",       "market_slug": "canton-oh",       "state": "OH",
     "iso": "PJM", "verdict": "AVOID", "composite_score": 29.3,
     "excess_power_score": 39.0, "constraint_score": 17.0, "time_to_power_months": 42},
    {"market_name": "Lebanon",      "market_slug": "lebanon-oh",      "state": "OH",
     "iso": "PJM", "verdict": "AVOID", "composite_score": 29.1,
     "excess_power_score": 38.0, "constraint_score": 16.0, "time_to_power_months": 44},
    {"market_name": "West Chester", "market_slug": "west-chester-oh", "state": "OH",
     "iso": "PJM", "verdict": "AVOID", "composite_score": 28.8,
     "excess_power_score": 37.0, "constraint_score": 16.0, "time_to_power_months": 45},
    {"market_name": "Cincinnati",   "market_slug": "cincinnati-oh",   "state": "OH",
     "iso": "PJM", "verdict": "AVOID", "composite_score": 28.1,
     "excess_power_score": 36.0, "constraint_score": 15.0, "time_to_power_months": 46},
    {"market_name": "Dayton",       "market_slug": "dayton-oh",       "state": "OH",
     "iso": "PJM", "verdict": "AVOID", "composite_score": 27.9,
     "excess_power_score": 35.0, "constraint_score": 15.0, "time_to_power_months": 47},
    {"market_name": "Toledo",       "market_slug": "toledo-oh",       "state": "OH",
     "iso": "PJM", "verdict": "AVOID", "composite_score": 27.4,
     "excess_power_score": 34.0, "constraint_score": 14.0, "time_to_power_months": 48},
    {"market_name": "Youngstown",   "market_slug": "youngstown-oh",   "state": "OH",
     "iso": "PJM", "verdict": "AVOID", "composite_score": 27.0,
     "excess_power_score": 33.0, "constraint_score": 14.0, "time_to_power_months": 50},
]
DEFAULT_VERDICTS = {"BUILD", "CAUTION"}       # the view's default


def _ohio_empty(limit=12):
    return ssc._empty_result(OHIO, "OH", None, DEFAULT_VERDICTS, limit)


# ── Grok: the rows, on the FIRST call ───────────────────────────────────────
def test_the_rows_ride_the_first_response():
    """The whole point. Without this an agent needs a second call to say anything
    beyond 'nine, all AVOID' — which is what Grok reported after #2582."""
    e = _ohio_empty()
    assert len(e["excluded_top"]) == 9
    assert e["excluded_total"] == 9


def test_each_row_carries_the_scores_grok_asked_for():
    """"at least the top one with composite/excess/constraint" — all three, on
    every row, so the agent can rank and explain without another call."""
    for row in _ohio_empty()["excluded_top"]:
        for field in ("composite_score", "excess_power_score", "constraint_score"):
            assert row[field] is not None, f"{row['market']} missing {field}"


def test_rows_use_the_same_shape_as_shortlist():
    """A caller must be able to reuse its shortlist parser unchanged."""
    assert (set(_ohio_empty()["excluded_top"][0].keys())
            == set(ssc._row_public(OHIO[0]).keys()))


def test_rows_are_ranked_best_first():
    scores = [r["composite_score"] for r in _ohio_empty()["excluded_top"]]
    assert scores == sorted(scores, reverse=True)


def test_limit_bounds_the_rows_but_never_the_reported_total():
    """Trimming must not be able to understate how many markets exist."""
    e = _ohio_empty(limit=3)
    assert len(e["excluded_top"]) == 3
    assert e["excluded_total"] == 9        # the honest count survives the trim


# ── the rows must NOT be laundered into `shortlist` ─────────────────────────
def test_excluded_rows_are_not_promoted_into_the_shortlist_contract():
    """`shortlist` means 'markets that cleared your bar'. Widening it to make the
    response look non-empty would make the verdict filter a lie — the exact class
    of silent wrong answer this file exists to prevent."""
    e = _ohio_empty()
    assert "shortlist" not in e
    assert e["reason"] == "no_market_met_the_verdict_filter"


# ── Gemini: the zeroes are the finding ──────────────────────────────────────
def test_verdict_counts_carry_the_zeroes():
    """{"AVOID": 9} reads as '9 of something'. {"BUILD":0,"CAUTION":0,"AVOID":9}
    reads as the finding: nothing clears the bar, and it is not close."""
    assert _ohio_empty()["verdict_counts"] == {"BUILD": 0, "CAUTION": 0, "AVOID": 9}


def test_unscored_verdicts_stay_visible_rather_than_being_dropped():
    mixed = OHIO[:2] + [dict(OHIO[0], market_slug="x-oh", verdict=None)]
    counts = ssc._empty_result(mixed, "OH", None, DEFAULT_VERDICTS, 12)["verdict_counts"]
    assert counts["UNSCORED"] == 1
    assert counts["BUILD"] == 0


# ── Perplexity: next_best_action on the empty path ──────────────────────────
def test_next_best_action_points_at_the_data_already_returned():
    """Not 'go make another call' — the answer is in this response."""
    nba = _ohio_empty()["next_best_action"]
    assert nba["action"] == "answer_from_excluded_top"
    assert "9" in nba["reason"]


def test_a_genuine_coverage_gap_is_named_differently_from_a_scoring_result():
    """The distinction that makes the block worth reading: 'no records here' and
    'records here, all below your bar' must never look the same."""
    gap = ssc._empty_result([], "ZZ", None, DEFAULT_VERDICTS, 12)
    assert gap["reason"] == "no_tracked_market_in_region"
    assert gap["excluded_top"] == []
    assert gap["next_best_action"]["action"] == "widen_geography"
    assert gap["excluded_note"] is None


# ── the honest-answer contract ──────────────────────────────────────────────
def test_meaning_says_the_markets_exist():
    """An agent reading only `meaning` must not conclude DC Hub lacks Ohio data."""
    assert "not missing data" in _ohio_empty()["meaning"]


def test_a_non_empty_result_still_produces_a_block_only_when_asked():
    """_empty_result is only ever called when ranked is empty; called with a set
    that DOES contain a matching verdict it still describes that geography
    truthfully rather than inventing an emptiness."""
    e = ssc._empty_result(OHIO, "OH", None, {"AVOID"}, 12)
    assert e["verdicts_requested"] == ["AVOID"]
    assert e["excluded_total"] == 9
