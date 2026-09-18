"""A finding whose suggested fix is WRONG is worse than no finding.

`js_field_fallback_missing` advises "add `|| item.company`". That advice is
correct where the read produces a VALUE and a bug where the read decides a
FILTER or a GUARD -- the same edit changes what the filter matches and what the
guard admits. Measured on live dchub-frontend main 2026-09-18, four of the
eight open rows were predicate reads, three of them inside `if (...)` deciding
a `return false`. So half an "important"-severity queue carried advice that
would introduce a bug if followed.

The eight lines below are the REAL lines from that scan, not fixtures. They are
the whole point: the false positive that started this (`</span>` parsed as a
relational operator) is invisible in invented test data and obvious in real
HTML.

Stdlib + pytest; no DB, no network.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.bug_squash import (  # noqa: E402
    JsFieldFallbackMissingPattern, is_predicate_read,
)

# capacity-pipeline.html / js/*.js on dchub-frontend origin/main, 2026-09-18.
VALUE_READS = [
    "        const operatorInitials = (item.operator || 'UN').substring(0, 2).toUpperCase();",
    "        <span class=\"operator-name\">${item.operator || 'Unknown'}</span>",
    "            item.operator || 'Unknown',",
    "      OPERATOR: item.operator || item.name,",
]
PREDICATE_READS = [
    "        if (item.operator && item.operator !== 'Unknown' && item.operator !== 'null') {",
    "        if (operator && item.operator !== operator) return false;",
    "            if (operator && item.operator !== operator) return false;",
    "        if (item.operator || item.location) {",
]


def _pattern():
    return JsFieldFallbackMissingPattern(
        id="js_field_fallback_missing", severity="important",
        title="t", why="w", reference="", files_glob=("*.js",), roots=(),
    )


def _detect(line):
    return _pattern().detect(line + "\n", Path("capacity-pipeline.html"))


# ── the rule itself ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("src", PREDICATE_READS)
def test_predicate_lines_are_recognised(src):
    assert is_predicate_read(src) is True


@pytest.mark.parametrize("src", VALUE_READS)
def test_value_lines_are_not_predicates(src):
    assert is_predicate_read(src) is False, (
        "a value read misclassified as a predicate throws away a real finding")


def test_markup_is_not_arithmetic():
    """★ The false positive that motivated the spaced relational alternative:
    a bare `[<>]=?` matches the `<` in `</span>`. Roughly half these findings
    live in .html, so this silently discards real findings."""
    assert is_predicate_read("<span>${item.operator || 'Unknown'}</span>") is False
    # ...while a genuine spaced comparison is still caught
    assert is_predicate_read("if (item.count > 3) { }") is True


# ── the detector's behaviour ────────────────────────────────────────────────
@pytest.mark.parametrize("src", PREDICATE_READS)
def test_detector_files_nothing_for_a_predicate_read(src):
    assert _detect(src) == [], (
        "filed a finding whose suggested fix would change what this "
        "filter matches")


@pytest.mark.parametrize("src", VALUE_READS)
def test_detector_still_files_every_value_read(src):
    """The precision fix must not become a silence fix. These four are exactly
    the rows the frontend lane patches."""
    found = _detect(src)
    assert len(found) == 1, f"lost a real finding: {src!r}"
    assert found[0].pattern_id == "js_field_fallback_missing"


def test_the_live_eight_split_four_and_four():
    """The ratio is the claim. If a later edit makes the detector greedier or
    quieter, this is the line that says so."""
    filed = [len(_detect(s)) for s in VALUE_READS + PREDICATE_READS]
    assert sum(filed) == 4, filed
    assert filed == [1, 1, 1, 1, 0, 0, 0, 0], filed


def test_a_bare_read_with_no_fallback_anywhere_is_still_filed():
    """The original incident shape -- a read with no fallback at all -- must
    survive. That is what the pattern exists for."""
    assert len(_detect("      const name = item.operator;")) == 1


def test_a_read_that_already_falls_back_is_still_not_filed():
    assert _detect("      const n = item.operator || item.company;") == []
