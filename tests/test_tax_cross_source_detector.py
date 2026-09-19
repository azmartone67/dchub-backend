"""GUARD — the cross-source detector must never read an EMPTY side as agreement.

FENCES _cross_source_checks in routes/ingestion_freshness_master_shell.py.

The freshness lane answers "how old is this row?". It cannot answer "is this
row WRONG?" — a row verified yesterday can still be wrong today. This lane
answers that by pointing permitting_intel's class=tax records at the incentive
rows: a state with a published tax record but no status on its row means DC Hub
is holding evidence against its own published answer.

──────────────────────────────────────────────────────────────────────────
★ THE VACUOUS-PASS TRAP (test_empty_tax_side_is_unknown_not_agreement)

permitting_intel today holds NINE records, all class=moratorium, and ZERO
class=tax — the tax lane has never been populated, though the API advertises
the class. A cross-check that reports "0 disagreements -> PASS" against an
empty side would go green precisely BECAUSE nobody is feeding it, and would
stay green through every future miss. Zero evidence on one side is not
agreement. That case must be None, never True.

THE PROPERTIES, each with a MUST-FAIL CONTROL:

1. A CONTRADICTION CONVICTS. Tax record for OH + no status on OH -> False.
   Control: same record with a status present -> True.
2. AN EMPTY TAX SIDE IS UNKNOWN. -> None, asserted `is None` (not falsy).
3. UNREADABLE IS NOT AGREEMENT. Either side None -> None with a reason.
4. COVERAGE NEVER CONVICTS. A flagged state with no tax record is a gap in
   the jurisdiction store, not an error in our row — a guard that reds here
   earns deletion.
"""
import routes.ingestion_freshness_master_shell as sh


def _by(checks):
    return {c["id"]: c for c in checks}


def _rows(*specs):
    return [dict(abbr=a, **({"status": s} if s else {})) for a, s in specs]


def _recs(*states):
    return [dict(state=s, jurisdiction=f"{s} (statewide)", **{"class": "tax"})
            for s in states]


# ── 1. a contradiction convicts ─────────────────────────────────────────────
def test_tax_record_with_no_status_on_the_row_convicts():
    """THE detector. permitting_intel knows Ohio changed; the row does not."""
    c = _by(sh._cross_source_checks(
        _rows(("OH", None), ("AZ", "paused_new_applicants")), None,
        _recs("OH"), None))
    assert c["xsrc_contradiction"]["pass"] is False
    assert "OH" in c["xsrc_contradiction"]["detail"]


def test_status_present_does_not_convict():
    """CONTROL for 1 — a detector that convicted everything fails here."""
    c = _by(sh._cross_source_checks(
        _rows(("OH", "paused_new_applicants")), None, _recs("OH"), None))
    assert c["xsrc_contradiction"]["pass"] is True


# ── ★ 2. an empty tax side is UNKNOWN, never agreement ──────────────────────
def test_empty_tax_side_is_unknown_not_agreement():
    """★ THE VACUOUS-PASS TRAP — today's real production state."""
    c = _by(sh._cross_source_checks(
        _rows(("OH", None), ("AZ", None), ("TX", None)), None, [], None))
    assert c["xsrc_contradiction"]["pass"] is None, \
        "an empty tax side must never vindicate the incentive rows"
    assert "ZERO" in c["xsrc_contradiction"]["detail"]


def test_empty_tax_side_makes_the_whole_lane_indeterminate():
    checks = sh._cross_source_checks(_rows(("OH", None)), None, [], None)
    assert sh._lane_verdict(checks) == "?", "empty side must not render PASS"


def test_a_real_comparison_can_still_pass():
    """CONTROL for 2 — proves '?' is not hardcoded for every input."""
    checks = sh._cross_source_checks(
        _rows(("OH", "paused_new_applicants")), None, _recs("OH"), None)
    assert sh._lane_verdict(checks) == "PASS"


# ── 3. unreadable is not agreement ──────────────────────────────────────────
def test_unreadable_incentive_side_is_indeterminate():
    checks = sh._cross_source_checks(None, "URLError: timed out", _recs("OH"), None)
    assert len(checks) == 1
    assert checks[0]["pass"] is None          # `is None`, not falsy
    assert checks[0]["critical"] is True
    assert "timed out" in checks[0]["detail"]


def test_unreadable_permitting_side_is_indeterminate():
    checks = sh._cross_source_checks(_rows(("OH", None)), None, None, "boom")
    assert checks[0]["pass"] is None
    assert "permitting_intel" in checks[0]["detail"]


# ── 4. coverage never convicts ──────────────────────────────────────────────
def test_coverage_gap_never_convicts():
    """Our row can be right and the jurisdiction store simply thinner."""
    c = _by(sh._cross_source_checks(
        _rows(("OH", "paused_new_applicants"), ("AZ", "paused_new_applicants")),
        None, _recs("OH"), None))
    assert c["xsrc_coverage"]["pass"] is True
    assert "AZ" in c["xsrc_coverage"]["detail"]
    assert c["xsrc_contradiction"]["pass"] is True


def test_a_tax_record_for_an_unknown_state_is_ignored():
    """A record for a state we do not carry must not invent a contradiction."""
    c = _by(sh._cross_source_checks(
        _rows(("OH", "paused_new_applicants")), None, _recs("OH", "ZZ"), None))
    assert c["xsrc_contradiction"]["pass"] is True
