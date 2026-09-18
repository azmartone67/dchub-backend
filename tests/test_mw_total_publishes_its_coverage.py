""""Total MW" must not publish a SUM over an unknown subset as a market total.

r-mw-coverage (2026-09-17). Measured live off the published /markets/<slug>
stat tiles:

    austin     91 facilities     107 MW    1.18 MW/facility
    columbus  120 facilities   2,179 MW   18.2  MW/facility
    dallas    386 facilities   7,067 MW   18.3  MW/facility
    reno       64 facilities   1,166 MW   18.2  MW/facility
    phoenix   280 facilities   4,735 MW   16.9  MW/facility
    ashburn   317 facilities   8,662 MW   27.3  MW/facility

Austin is ~15x below its peers because almost none of its rows carry power_mw.
_FAC_UNION_SQL COALESCEs a NULL power_mw to 0, so the sum is real and the
denominator is invisible — COUNT(mw) cannot recover it, only a FILTER can.
None of this feeds the DCPI score (no scorer input reads total_mw); it is a
published number that states more than it knows.
"""
import re
import routes.market_deep_dive as M


def test_the_union_counts_reporting_rows_with_a_filter_not_count_mw():
    """COUNT(mw) is 91 for Austin, not 9 — the COALESCE already happened.

    Pins the one SQL construct that can actually recover the denominator.
    """
    sel = M._FAC_COUNTS_SELECT
    assert "FILTER (WHERE mw > 0)" in sel, sel
    assert "COUNT(mw)" not in sel, "COUNT(mw) cannot see a COALESCEd NULL"


def test_austin_shaped_coverage_is_stated():
    assert M.mw_coverage_note(9, 91) == "9 of 91 report MW"


def test_full_coverage_is_still_stated():
    """Not a warning that fires only on bad data — a denominator that is always
    published. A note that appears only when thin teaches readers to treat its
    absence as 'complete', which is the same unstated assumption again."""
    assert M.mw_coverage_note(317, 317) == "317 of 317 report MW"


def test_unknown_coverage_renders_nothing():
    """The page must degrade to today's output, never to a fabricated 0 of 0."""
    assert M.mw_coverage_note(None, 91) == ""
    assert M.mw_coverage_note(9, None) == ""
    assert M.mw_coverage_note(9, 0) == ""
    assert M.mw_coverage_note(9, "—") == ""      # the shell's missing marker
    assert M.mw_coverage_note("—", "—") == ""


def test_the_denominator_never_travels_without_its_numerator():
    """overlay_mw_coverage attaches both or neither. A denominator beside no
    numerator is the same defect in miniature."""
    assert M.overlay_mw_coverage({}, None) == {}
    assert M.overlay_mw_coverage({}, {"facility_count": 91}) == {}
    got = M.overlay_mw_coverage({}, {"facility_count": 91,
                                     "mw_reporting_count": 9})
    assert got == {"mw_reporting_count": 9, "mw_coverage_facility_count": 91}


def test_overlay_does_not_overwrite_the_narratives_stored_totals():
    """The stored total_mw/facility_count are what the PROSE was written
    against. This overlay adds a denominator; it must not restate the sum."""
    before = {"total_mw": 107.0, "facility_count": 91, "dcpi_score": 43.3}
    after = M.overlay_mw_coverage(before, {"facility_count": 95,
                                           "mw_reporting_count": 9,
                                           "total_mw": 120.0})
    assert after["total_mw"] == 107.0
    assert after["facility_count"] == 91
    assert after["dcpi_score"] == 43.3


def test_both_per_market_painters_render_the_denominator():
    """/markets/<slug> has three painters and two of them fire only when data
    degrades. The cached brief and the SEO shell both publish the sparse sum,
    so both must state its basis; the guard-neutral page publishes no measured
    facts at all and is correctly untouched.
    """
    src = open(M.__file__, encoding="utf-8").read()
    brief = re.search(r'Total MW<b>.*?</div>', src)
    assert brief and "_mw_cov_html" in brief.group(0), brief

    # ★ NOT `"_cov_for(lab)" in src` — that substring is satisfied by the
    # helper's own `def _cov_for(lab):` line, so deleting the call from the
    # tile left the assertion green. Measured: that mutation passed 8/8.
    # Anchor on the tile f-string itself.
    shell = re.search(r"_tiles = \[f'<div class=\"stat\">.*?\]", src, re.S)
    assert shell, "could not find the SEO shell's tile builder"
    assert "_cov_for(lab)" in shell.group(0), (
        "the shell's tile no longer renders the coverage note:\n"
        + shell.group(0))
    assert src.count("mw_coverage_note(") >= 3, (
        "expected one definition and a call in each of the two painters")


def test_the_small_element_is_styled_in_every_template_that_uses_it():
    """Both page templates carry their own CSS copy; a rule added to one leaves
    the other rendering an unstyled uppercase mono fragment."""
    src = open(M.__file__, encoding="utf-8").read()
    assert src.count(".stat small{{") == src.count(
        "letter-spacing:.06em;font-family:'JetBrains Mono',monospace}}") == 2
