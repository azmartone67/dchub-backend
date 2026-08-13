#!/usr/bin/env python3
"""tests/test_near_converter_tool_scope.py — the conversion funnel must not
define "paid" by hand.

NO NETWORK, NO DB.

r-nearconv (2026-08-13). `PAID_TOOLS` in routes/mcp_usage_self.py was a
hardcoded set of five names, and `_fetch_near_converters` filters on it. It had
drifted in both directions. Measured against 30 days of live signals:

  · it matched 22 of 1,012 paid_tool_blocked signals — 2%. The other 990 were
    invisible, 684 of them on get_interconnection_queue alone: the single
    largest paywall signal in the product, by a factor of thirteen.
  · it listed get_dchub_recommendation as paid. TOOL_TIER says FREE.

So the conversion-outreach job reported `near_converter_count: 0` on every run
and sent nothing, while demand piled up on tools it could not see. A hand-kept
list of what costs money will always drift from the gate that charges for it.

★ PAID and IDENTIFIED are separate on purpose. IDENTIFIED is free-with-a-key: a
caller blocked there converts by calling claim_free_key, not by paying. Merging
them produces outreach that asks for money to fix something free.

Run standalone:   python3 tests/test_near_converter_tool_scope.py
Run under pytest: pytest tests/test_near_converter_tool_scope.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SRC_PATH = os.path.join(ROOT, "routes", "mcp_usage_self.py")


def _src():
    with open(SRC_PATH, encoding="utf-8") as fh:
        return fh.read()


def _code():
    """Source minus comment lines — the comments name the old hardcoded tools
    on purpose, and a substring match on prose would read the history as the
    defect. Several guards in this repo have already made that mistake."""
    return "\n".join(l for l in _src().splitlines()
                     if not l.lstrip().startswith("#"))


def test_paid_tools_is_derived_not_hardcoded():
    """★ The defect. A literal set here cannot track the gate."""
    code = _code()
    assert "from mcp_gatekeeper import" in code and "TOOL_TIER" in code, (
        "PAID_TOOLS must derive from mcp_gatekeeper.TOOL_TIER — the map the gate "
        "itself enforces"
    )
    # A re-introduced literal set of tool names is the regression.
    assert not re.search(r'^PAID_TOOLS\s*=\s*\{\s*$\s*"', code, re.MULTILINE), (
        "PAID_TOOLS was hardcoded again"
    )


def test_paid_and_identified_are_not_conflated():
    from routes import mcp_usage_self as m
    assert m.PAID_TOOLS, "paid set is empty — the import guard failed open"
    assert m.IDENTIFIED_TOOLS, "identified set is empty"
    assert not (m.PAID_TOOLS & m.IDENTIFIED_TOOLS), (
        "a tool cannot be both paid and free-with-a-key; outreach would ask for "
        "money to unlock something free"
    )


def test_the_specific_mislabel_is_gone():
    """get_dchub_recommendation was listed as paid; TOOL_TIER says FREE."""
    from routes import mcp_usage_self as m
    assert "get_dchub_recommendation" not in m.PAID_TOOLS, (
        "get_dchub_recommendation is FREE in TOOL_TIER — listing it as paid is "
        "how the old hardcoded set was wrong in the generous direction"
    )


def test_real_paid_tools_are_actually_covered():
    from routes import mcp_usage_self as m
    for t in ("analyze_site", "compare_sites"):
        assert t in m.PAID_TOOLS, f"{t} is a paid tier and must be in scope"
    assert len(m.PAID_TOOLS) > 5, (
        f"only {len(m.PAID_TOOLS)} paid tools in scope — the old hardcoded set "
        f"had 5 and matched 2% of real blocks"
    )


def test_untiered_blocking_tools_are_reported_not_dropped():
    """★ 772 of 1,012 blocks (76%) were on tools with NO TOOL_TIER entry —
    get_interconnection_queue alone was 684. A tier-derived query drops those
    silently, and a number missing from a funnel is indistinguishable from
    demand that does not exist."""
    code = _code()
    assert "_blocked_but_untiered" in code, (
        "the funnel must report tools that block callers but carry no tier"
    )
    assert '"blocked_but_untiered"' in code, (
        "the gap must appear in the response, not just be computed"
    )


def test_an_import_failure_is_visible_not_silent():
    """Empty sets would make the funnel report zero — the exact failure being
    fixed. It must at least say so."""
    import ast
    code = _code()
    assert '"tool_tier_import_failed"' in code, "the flag must reach the response"

    # Presence of the NAME is not enough — a weaker version of this test passed
    # while the except branch had been gutted to `pass`, because the name still
    # appeared in the else branch and in the response dict. Assert the FAILURE
    # PATH actually raises the flag.
    tree = ast.parse(_src())
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
                for h in n.handlers
                if "TOOL_TIER" in ast.unparse(n.body)]
    assert handlers, "the gatekeeper import is not wrapped in a try"
    tried = handlers[0]
    setters = [
        t.id
        for h in tried.handlers for stmt in ast.walk(h)
        if isinstance(stmt, ast.Assign)
        for t in stmt.targets if isinstance(t, ast.Name)
    ]
    assert "TOOL_TIER_IMPORT_FAILED" in setters, (
        "the except branch must SET TOOL_TIER_IMPORT_FAILED — otherwise an "
        "import failure empties the sets and the funnel reports zero silently, "
        "which is the exact bug being fixed"
    )


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✓ {_name}")
            except AssertionError as _e:
                _failed += 1
                print(f"✗ {_name}: {_e}")
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
