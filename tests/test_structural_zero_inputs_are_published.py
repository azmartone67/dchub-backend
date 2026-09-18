"""A weight the scorer applies to a field nobody fills must be disclosed.

r-queue-saturation-honesty (2026-09-17). `emergency_count_30d` carried an
in-code note and a prose line in signal_detail since it was found: it is never
assigned, so 20% of every constraint_score is a constant zero.

Its excess-side twin had NO disclosure anywhere. `stranded_capacity_mw` is
filled only by the hand-curated slug_overrides set (8 markets), so for the
other ~325 published markets 15% of excess_power_score is a constant zero —
and those 8 are the only markets that can earn the points. Measured live
2026-09-17 over 70 published markets: median excess 36.95 against a CAUTION
floor of 50.0; 46 of 70 fell below that floor on EXCESS while only 5 breached
the constraint ceiling, i.e. the excess side is what decides the verdict.
"""
import routes.dcpi as D
from util.dcpi_method import (STRUCTURAL_ZERO_INPUTS, SIGNAL_TIER,
                              EXCESS_WEIGHTS, CONSTRAINT_WEIGHTS)


def test_both_structural_zeros_are_named():
    assert set(STRUCTURAL_ZERO_INPUTS) == {
        "emergency_count_30d", "stranded_capacity_mw"}


def test_the_declared_weight_matches_the_weight_the_scorer_actually_applies():
    """The disclosure is only worth anything if its number is the real one.

    Reads the published weight tables the scorers import, so a weight change
    that forgets this table fails here instead of publishing a stale figure.
    """
    assert (STRUCTURAL_ZERO_INPUTS["emergency_count_30d"]["weight"]
            == CONSTRAINT_WEIGHTS["emergencies"])
    assert (STRUCTURAL_ZERO_INPUTS["stranded_capacity_mw"]["weight"]
            == EXCESS_WEIGHTS["stranded"])


def test_stranded_is_not_claimed_to_be_never_populated():
    """It IS populated — for 8 curated markets. Filing it under
    never_populated_inputs would trade one inaccuracy for another."""
    assert "stranded_capacity_mw" not in SIGNAL_TIER["never_populated_inputs"]
    assert "stranded_capacity_mw" in SIGNAL_TIER["structural_zero_inputs"]


def test_the_scorer_really_does_read_a_zero_for_an_absent_stranded_value():
    """The claim under test, exercised rather than asserted about.

    A market with stranded capacity at the ceiling scores exactly the declared
    weight higher than the same market with none — which is both the proof the
    weight is live and the size of what 325 markets are giving up.
    """
    base = {"reserve_margin_pct": 20.0, "gen_additions_12mo_mw": 1000.0,
            "curtailment_pct": 5.0, "queue_approval_rate_pct": 50.0}
    without = D.compute_excess_power_score(dict(base))
    with_max = D.compute_excess_power_score(
        dict(base, stranded_capacity_mw=1000.0))   # EXCESS_CEILINGS value
    gained = round(with_max - without, 1)
    want = round(EXCESS_WEIGHTS["stranded"] * 100, 1)
    assert gained == want, f"stranded is worth {gained} pts, declared {want}"


def test_the_published_signal_detail_names_the_excess_side_too():
    """Before this, the prose named only the constraint-side zero, so a reader
    correcting for it under-corrected by 15 points of excess."""
    detail = str(SIGNAL_TIER.get("scope_note", "")) + " " + str(SIGNAL_TIER)
    assert "stranded_capacity_mw" in detail
