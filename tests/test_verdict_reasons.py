"""Guards for routes.dcpi.verdict_reasons — typed reasons for a DCPI verdict.

Gemini asked for this shape in the 2026-08-30 partner round: "structure it as
strongly typed arrays rather than just plain text strings. This enables LLMs to
synthesize human-readable summaries while allowing deterministic code
downstream to branch on reason codes."

The three tests that matter are the three ways an explainer lies:

  1. it drifts from the verdict it explains (a second copy of the thresholds)
  2. it explains a component nobody measured (null read as zero)
  3. it implies an input affects the verdict when it does not (time-to-power)

Everything else here would still pass with all three broken.
"""
import pytest

dcpi = pytest.importorskip("routes.dcpi")
method = pytest.importorskip("util.dcpi_method")

# Measured live 2026-08-30 on site_selection_canvas region=OH verdict=ALL.
CANTON = (27.9, 47.7, None, "AVOID")


def _codes(reasons):
    return [r["code"] for r in reasons]


def test_reasons_agree_with_the_verdict_they_explain():
    """THE guard. The reasons walk the same band table derive_verdict walks, so
    an explanation can never contradict its own verdict. A second hardcoded copy
    of the thresholds is the failure this prevents, and it is invisible until
    the bands move.
    """
    for excess in (0, 27.9, 49.9, 50.0, 64.9, 65.0, 88.0, 100):
        for constraint in (0, 30.0, 47.7, 50.0, 50.1, 70.0, 70.1, 100):
            verdict = dcpi.derive_verdict(constraint, excess)
            reasons = dcpi.verdict_reasons(excess, constraint, None, verdict)
            codes = _codes(reasons)
            if verdict == "AVOID":
                # every band was missed, so at least one blocking reason exists
                assert any(c.startswith(("EXCESS_POWER_BELOW", "CONSTRAINT_ABOVE"))
                           for c in codes), (excess, constraint, codes)
                assert not any("MEETS" in c or "WITHIN" in c for c in codes), \
                    (excess, constraint, codes)
            else:
                assert f"EXCESS_POWER_MEETS_{verdict}_FLOOR" in codes, (excess, constraint, codes)
                assert f"CONSTRAINT_WITHIN_{verdict}_CEILING" in codes, (excess, constraint, codes)
            # and no reason may ever contradict the verdict by claiming the
            # achieved band was also missed
            assert not (f"EXCESS_POWER_MEETS_{verdict}_FLOOR" in codes
                        and f"EXCESS_POWER_BELOW_{verdict}_FLOOR" in codes)


def test_the_emitted_code_set_is_exactly_what_the_published_bands_imply():
    """★ THE GUARD THAT ACTUALLY BITES, and the one this file shipped without.

    An earlier version checked only the `threshold` FIELD on each reason. That
    reads what is PRINTED, not what is DECIDED — so replacing the comparison
    `e < band["excess_min"]` with a hardcoded `e < 55.0` left every printed
    threshold correct and passed the whole file. Caught by a mutation that
    survived, which is the only way this kind of hole is ever found.

    So: recompute the expected code set from util.dcpi_method here, and require
    an EXACT match. The condition is now under test, not just its label.
    """
    for excess in (0, 27.9, 49.9, 50.0, 55.0, 60.0, 64.9, 65.0, 88.0, 100):
        for constraint in (0, 30.0, 47.7, 50.0, 50.1, 60.0, 70.0, 70.1, 100):
            expected = set()
            achieved = None
            for label, band in method.VERDICT_BANDS:
                if excess >= band["excess_min"] and constraint <= band["constraint_max"]:
                    achieved = label
                    expected.add(f"EXCESS_POWER_MEETS_{label}_FLOOR")
                    expected.add(f"CONSTRAINT_WITHIN_{label}_CEILING")
                    break
            for label, band in method.VERDICT_BANDS:
                if achieved and label == achieved:
                    break
                if excess < band["excess_min"]:
                    expected.add(f"EXCESS_POWER_BELOW_{label}_FLOOR")
                if constraint > band["constraint_max"]:
                    expected.add(f"CONSTRAINT_ABOVE_{label}_CEILING")
            got = set(_codes(dcpi.verdict_reasons(excess, constraint, None, None)))
            assert got == expected, (
                f"excess={excess} constraint={constraint}\n"
                f"  missing: {sorted(expected - got)}\n"
                f"  extra:   {sorted(got - expected)}")


def test_thresholds_are_the_published_ones_not_a_second_copy():
    """Every `threshold` must be a number the methodology object actually
    publishes. A hand-typed 65.0 that happens to match today is the drift this
    catches when the bands move tomorrow.
    """
    published = set()
    for _label, band in method.VERDICT_BANDS:
        published.add(float(band["excess_min"]))
        published.add(float(band["constraint_max"]))
    published.add(float(method.COMPOSITE_TTP_CAP_MONTHS))

    seen = 0
    for excess, constraint in ((27.9, 47.7), (72.0, 30.0), (55.0, 60.0), (10.0, 90.0)):
        for r in dcpi.verdict_reasons(excess, constraint, 18, None):
            if r["threshold"] is not None:
                assert float(r["threshold"]) in published, (
                    f"{r['code']} publishes threshold {r['threshold']}, which the "
                    f"methodology does not")
                seen += 1
    assert seen >= 4, "the sweep did not actually exercise any thresholds"


def test_a_missing_component_is_never_explained_as_zero():
    """NULL IS NOT ZERO. derive_composite_score does `float(excess or 0)`
    because a rank needs a number. A reason must not: "score 0.0, below the 50.0
    floor" is a fabricated finding about a market nobody measured.
    """
    for excess, constraint in ((None, 47.7), (27.9, None), (None, None)):
        reasons = dcpi.verdict_reasons(excess, constraint, None, "AVOID")
        codes = _codes(reasons)
        assert "COMPONENT_UNAVAILABLE" in codes, (excess, constraint, codes)
        # and it must not also emit a band finding about the missing component
        if excess is None:
            assert not any("EXCESS_POWER" in c and c != "COMPONENT_UNAVAILABLE"
                           for c in codes), codes
        if constraint is None:
            assert not any("CONSTRAINT" in c and c != "COMPONENT_UNAVAILABLE"
                           for c in codes), codes
        for r in reasons:
            if r["code"] == "COMPONENT_UNAVAILABLE":
                assert r["value"] is None
                assert "0" not in (r["message"].split(".")[0])


def test_time_to_power_is_never_presented_as_a_verdict_input():
    """derive_verdict(constraint, excess) takes TWO arguments. Presenting
    time-to-power as a verdict reason teaches an agent that a faster
    interconnect could move a market out of AVOID. It cannot.
    """
    reasons = dcpi.verdict_reasons(27.9, 47.7, 30, "AVOID")
    ttp = [r for r in reasons if r["code"] == "TIME_TO_POWER"]
    assert len(ttp) == 1
    assert ttp[0]["affects"] == "composite_rank"
    assert ttp[0]["unit"] == "months"
    assert "NOT a verdict input" in ttp[0]["message"]
    for r in reasons:
        assert r["affects"] in dcpi.VERDICT_REASON_AFFECTS
        if r["affects"] == "verdict":
            assert r["component"] != "time_to_power_months"


def test_a_stored_verdict_the_bands_do_not_produce_is_reported_not_explained():
    """derive_verdict_v2 moves BUILD -> CAUTION on extreme water risk and
    AVOID -> CAUTION on strong renewable arbitrage. Those inputs are not on the
    row. Explaining the adjusted verdict from the base bands would be a
    confident wrong answer, so it is declared instead.
    """
    # bands say AVOID; the row carries CAUTION (the v2 arbitrage lift)
    reasons = dcpi.verdict_reasons(27.9, 47.7, None, "CAUTION")
    codes = _codes(reasons)
    assert "VERDICT_ADJUSTED_BEYOND_BASE_BANDS" in codes
    msg = [r for r in reasons if r["code"] == "VERDICT_ADJUSTED_BEYOND_BASE_BANDS"][0]["message"]
    assert "AVOID" in msg and "CAUTION" in msg
    # when they agree, no adjustment claim is made
    assert "VERDICT_ADJUSTED_BEYOND_BASE_BANDS" not in _codes(
        dcpi.verdict_reasons(27.9, 47.7, None, "AVOID"))


def test_the_ohio_row_explains_itself_the_way_the_data_actually_reads():
    """The measured case, and the reason this shipped: Ohio is AVOID because it
    has no excess-power headroom, NOT because it is constrained — 47.7 sits
    comfortably inside BUILD's 50.0 ceiling. Those are different decisions and a
    prose blob would have blurred them.
    """
    reasons = dcpi.verdict_reasons(*CANTON)
    codes = _codes(reasons)
    assert "EXCESS_POWER_BELOW_CAUTION_FLOOR" in codes
    assert "EXCESS_POWER_BELOW_BUILD_FLOOR" in codes
    assert not any(c.startswith("CONSTRAINT_ABOVE") for c in codes), (
        "constraint 47.7 is inside every ceiling — flagging it would misdirect "
        "the reader to the wrong lever")


def test_every_reason_is_machine_branchable_and_human_readable():
    """Gemini's stated requirement, both halves."""
    for r in dcpi.verdict_reasons(27.9, 47.7, 24, "AVOID"):
        assert r["code"] == r["code"].upper() and " " not in r["code"]
        assert set(r) >= {"code", "value", "threshold", "unit", "affects", "message"}
        assert len(r["message"]) > 30 and r["message"].endswith((".", ")"))


def test_the_canvas_row_carries_them_and_never_fails_over_them():
    ssc = pytest.importorskip("routes.site_selection_canvas")
    row = ssc._row_public({
        "market_name": "Canton", "market_slug": "canton", "state": "OH", "iso": "PJM",
        "verdict": "AVOID", "excess_power_score": 27.9, "constraint_score": 47.7,
        "time_to_power_months": None, "composite_score": 22.9,
    })
    assert row["verdict_reasons"], "the row does not carry the block"
    assert "EXCESS_POWER_BELOW_CAUTION_FLOOR" in _codes(row["verdict_reasons"])
    # fail-soft: garbage in must not take the row down with it
    assert ssc._verdict_reasons(object(), object(), None, None) == []
