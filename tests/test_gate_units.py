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
    src = _src()
    assert re.search(r"mcp_share\s*=\s*\(mcp_calls\s*/\s*all_calls\)", src), \
        "mcp_share must be a FRACTION — the gate compares it against 0.8"
    assert not re.search(r"mcp_share\s*=\s*\(?\s*100\.?0?\s*\*", src), \
        "a percentage here silently trips every share threshold in the gate"


def test_the_empty_case_fails_closed():
    """No calls means the share is UNKNOWN, and unknown must not read as
    'attribution is perfect'. 1.0 (100%) keeps the gate shut; 0.0 would fling
    it open on zero evidence."""
    src = _src()
    m = re.search(r"mcp_share\s*=\s*\(mcp_calls / all_calls\) if all_calls else ([\d.]+)", src)
    assert m, "could not find the empty-case fallback"
    assert float(m.group(1)) >= 1.0, "empty case must fail CLOSED, not open"


def test_generic_bucket_family_is_imported_not_restated():
    """'mcp' was renamed 'mcp-generic-client' on 07-28. A lane that hardcodes
    either one desyncs from the gate the next time it changes."""
    src = _src()
    assert "GENERIC_BUCKETS" in src, "import the bucket family from the gate's owner"
    assert "platform = ANY(%s)" in src, "match the whole family, not one label"
    assert "platform = 'mcp'" not in src, "single-label match under-reports the share"


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
