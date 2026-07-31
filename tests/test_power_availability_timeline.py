"""Guards for routes/power_availability_timeline.py — timing signals, honestly.

Pure-function tests over SQL strings and contract constants. No DB, no
network, never imports main. The property under guard is the honesty line:
this tool reports WHEN power gets easier (dated, sourced, confidence-split)
and must never mutate into "your load can energize by <date>" — the overclaim
competitors sell and LLMs fabricate.
"""
import re

import pytest

pat = pytest.importorskip("routes.power_availability_timeline")


def test_definition_version_and_changelog():
    assert pat.DEFINITION_VERSION >= 1
    for v in range(1, pat.DEFINITION_VERSION + 1):
        assert v in pat.DEFINITION_CHANGELOG, f"version {v} undocumented"


def test_confidence_classes_are_never_blended():
    """The one derived number must exclude speculative 'planned' capacity —
    blending permitting-stage MW into a firm signal is the overclaim."""
    src = open(pat.__file__, encoding="utf-8").read()
    assert "cumulative_firm_signal_mw" in src
    firm = src[src.index("cum += "):src.index("cum += ") + 120]
    assert "under_construction_mw" in firm and "testing_mw" in firm \
           and "retiring_mw" in firm
    assert "planned_mw" not in firm, \
        "speculative capacity leaked into the firm signal"


def test_status_classes_match_eia_prefixes():
    s = pat._STATUS_CLASS_SQL
    for prefix, cls in (("U%%", "under_construction"), ("V%%", "under_construction"),
                        ("TS%%", "testing"), ("P%%", "planned"),
                        ("L%%", "planned"), ("T%%", "planned")):
        assert prefix in s and cls in s
    # TS must be classed BEFORE the bare-T planned branch or testing units
    # silently become 'planned'.
    assert s.index("TS%%") < s.index("'P%%' OR status ILIKE 'L%%'")


def test_queue_lane_carries_no_dates_and_reuses_dead_status():
    """The queue feed has no CODs; the lane is congestion context only, and
    its dead-status verdict is IMPORTED from retirement_headroom, not a second
    hand-kept list."""
    src = open(pat.__file__, encoding="utf-8").read()
    assert "from routes.retirement_headroom import _DEAD_STATUS_RE" in src
    assert "congestion pressure only" in src
    assert "never completes" in src


def test_constraint_coverage_names_the_unknowables():
    joined = " ".join(pat.CONSTRAINT_COVERAGE).lower()
    for phrase in ("deliverable load", "study timelines", "large-load",
                   "substation", "ppa"):
        assert phrase in joined, f"coverage lost the {phrase!r} declaration"
    assert len(pat.CONSTRAINT_COVERAGE) >= 5


def test_parameterised_sql_binds_cleanly():
    """Emulate psycopg2 substitution: the %%-doubled ILIKEs in the status CASE
    must survive alongside real bound params (the /api/v1/map outage class)."""
    sql = f"""
        SELECT planned_year::int, ({pat._STATUS_CLASS_SQL}), SUM(capacity_mw)
          FROM planned_generators WHERE state = %s
           AND planned_year BETWEEN %s AND %s GROUP BY 1, 2
    """
    out = sql % ("__A__", "__B__", "__C__")
    for s in ("__A__", "__B__", "__C__"):
        assert s in out
    for m in re.finditer(r"%(.)", sql):
        assert m.group(1) in ("s", "%"), f"bare percent before {m.group(1)!r}"


def test_state_is_validated_and_iso_never_guessed():
    src = open(pat.__file__, encoding="utf-8").read()
    assert "state.isalpha()" in src
    assert "fail-open" in src, \
        "the deliberate non-use of the taxonomy default must stay documented"
    assert "iso_context" in src


def test_mw_is_context_only():
    """A requested MW must never produce an energize-by date."""
    src = open(pat.__file__, encoding="utf-8").read()
    assert "does not and" in src and "cannot state when" in src
