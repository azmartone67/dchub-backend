"""The attribution gate takes a FRACTION. Feed it a percentage and it lies.

★ 2026-08-04. Shell #45 lane 2 computed `100.0 * mcp_calls / all_calls` and
passed 27.4 into `_attribution_gate`, which compares against
MCP_BUCKET_MAX_SHARE_TO_PUBLISH = 0.8. 27.4 > 0.8, so the lane rendered
"GATED — GATED_ATTRIBUTION_UNVERIFIED" on the same day the public report,
running the SAME gate function on the same data with the correct units,
rendered "MEASURED / passed: true".

Two readings of one question, disagreeing by a factor of 100, each looking
authoritative. That is the two-sources-of-truth class the whole shell fleet
exists to prevent — reached this time through units rather than through a
second query.

Second defect in the same call: the lane counted only `platform = 'mcp'`,
while the gate's own definition of generic is GENERIC_BUCKETS — 'mcp' AND
'mcp-generic-client' (the 07-28 rename). Counting one of two under-reports
the share and feeds the gate a number that does not mean what it thinks.

★ 2026-08-05 — AND IT HAPPENED AGAIN, ONE LAYER DOWN. The 08-04 fix imported
the bucket LIST and left the lane its own hand-written query. The list could
not police the two things that then diverged: the lane matched the RAW
`platform` column where the report applies PLATFORM_CASE, and it counted a
different population (is_public_ip). Measured live: this tick published
"generic bucket 21.6%" while /api/v1/reports/agent-success published 25.1% for
the same question five seconds apart, stable across two rounds — two
computations, not drift. Third reading of one question in two days.

So the fences below moved up a level with the fix: importing the INGREDIENTS
is no longer enough to satisfy them, because importing the ingredients is
exactly what shipped a desync. The lane must import the QUERY —
agent_success_report.measure_generic_bucket_share — and run no aggregate of
its own. Nothing restated is nothing that can drift.
"""
import inspect
import re

import pytest

MOD = "routes.agent_expansion_master_shell"


def _src():
    """Lane source with COMMENTS STRIPPED.

    ★ The first cut of this asserted `"MCP_BUCKET_MAX_SHARE_TO_PUBLISH" not in
    src` and failed — because the comment I had just written to EXPLAIN the
    bug names that constant. A test matching its own warning text is this
    repo's most-repeated test bug (documented twice before this). Assertions
    here run against code, never prose."""
    m = pytest.importorskip(MOD)
    src = inspect.getsource(m._lane_planner_adoption)
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in src.splitlines())


def test_share_is_a_fraction_not_a_percentage():
    """The units defect, re-anchored: the lane no longer divides at all.

    It publishes the fraction the exported measure returned — the same value
    /api/v1/reports/agent-success publishes as generic_bucket_share_7d. A
    quantity computed once cannot be computed twice in two units.
    """
    src = _src()
    assert not re.search(r"mcp_share\s*=\s*\(?\s*100\.?0?\s*\*", src), \
        "a percentage here silently trips every share threshold in the gate"
    assert not re.search(r"mcp_share\s*=\s*\(?mcp_calls\s*/\s*all_calls", src), \
        ("the lane divides the counts itself again — the fraction must come "
         "from measure_generic_bucket_share, which is where the report gets it")
    assert re.search(r"mcp_share\s*=\s*_share_frac", src), \
        "mcp_share must be the exported measure's own fraction"


def test_the_empty_case_fails_closed():
    """No calls means the share is UNKNOWN, and unknown must not read as
    'attribution is perfect'.

    Checked behaviourally now, not by matching a literal in the lane: the
    exported measure returns None on an empty window, and the gate's None
    branch is what must keep the door shut. The old `else 1.0` said "100%
    generic" — right verdict, wrong reason, and it would have read as a real
    measurement on the board.
    """
    mod = pytest.importorskip("routes.agent_success_report")
    passed, status, reason = mod._attribution_gate(
        mod.ATTRIBUTION_MIN_ACCUMULATION_DAYS, None)
    assert passed is False, "an unmeasured share must fail CLOSED, not open"
    assert "UNVERIFIED" in status
    assert "could not be measured" in reason.lower(), (
        "the empty case must say it is unmeasured, not report a 100% share")
    src = _src()
    assert not re.search(r"if all_calls else\s*1\.0", src), \
        "the lane invents a 1.0 share again instead of passing None through"


def test_the_share_query_itself_is_imported_not_restated():
    """'mcp' was renamed 'mcp-generic-client' on 07-28. A lane that restates
    ANY part of the measurement desyncs from the gate the next time a part of
    it changes — the list on 08-04, the population and the canonicaliser on
    08-05.
    """
    src = _src()
    assert "measure_generic_bucket_share" in src, (
        "import the QUERY from the gate's owner; importing GENERIC_BUCKETS "
        "alone was tried on 08-04 and the desync moved to the population")
    assert "platform = ANY(%s)" not in src, "the private query is back"
    assert "platform = 'mcp'" not in src, "single-label match under-reports the share"
    assert "mcp_calls_identity" not in src, (
        "the lane runs its own identity-view aggregate again")


def test_the_shared_measure_matches_the_whole_bucket_family():
    """The property the deleted lane-side assertions were protecting, now
    asserted where the query actually lives."""
    mod = pytest.importorskip("routes.agent_success_report")
    sql = mod._SQL_MCP_SHARE
    for bucket in mod.GENERIC_BUCKETS:
        assert f"'{bucket}'" in sql, f"{bucket} is not matched by the share query"
    assert "is_public_ip" in sql, (
        "the share must count the canonical population — the 08-05 divergence "
        "was entirely this filter")


def test_the_gate_itself_is_imported_not_reimplemented():
    """The lane must run the REAL gate. A local copy of the thresholds would
    drift from the published report the first time either moved."""
    src = _src()
    assert "from routes.agent_success_report import _attribution_gate" in src
    assert "MCP_BUCKET_MAX_SHARE_TO_PUBLISH" not in src, \
        "thresholds belong to the gate, not to this lane"


def test_gate_contract_holds_for_fraction_inputs():
    """Behavioural check on the gate itself, so this test fails if the gate's
    units ever change under us."""
    mod = pytest.importorskip("routes.agent_success_report")
    gate = mod._attribution_gate
    days = mod.ATTRIBUTION_MIN_ACCUMULATION_DAYS
    passed_low, _, _ = gate(days, 0.2984)      # today's real generic share
    assert passed_low is True, "a 29.8% generic share must PASS the gate"
    passed_pct, status_pct, _ = gate(days, 29.84)  # the same number as a percentage
    assert passed_pct is False, "a percentage must trip the gate — that was the bug"
    assert "UNVERIFIED" in status_pct
