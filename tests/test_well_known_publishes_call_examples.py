"""2026-09-05 — /.well-known/mcp.json published 83 tool names and no way to call one.

`_merged_tools()` yields (name, category, tier, summary, EXAMPLE) and every one
of the 83 entries carries a fully-formed call with the real parameter names:

    get_market_intel(market="northern-virginia")
    rank_markets(criteria="fastest_growing", region="us", limit=10, min_capacity_mw=100)
    compare_isos(isos="PJM,ERCOT,CAISO")

`tools_for_well_known()` destructured that field to `_ex` and dropped it. So the
public discovery manifest — the document registries and AI crawlers scan, and
the only tool surface readable WITHOUT opening an MCP session — said what each
tool does and never how to call it.

★ WHY THAT COSTS CALLS, MEASURED. Live 2026-09-05, anonymous free tier, same
session seconds apart:

    get_market_intel(market_slug: "dallas")  -> no market data. The undeclared
        argument is stripped before the handler, `market` is empty, and the tier
        gate answers with an upgrade envelope + a for_your_human relay link.
    get_market_intel(market: "dallas")       -> _gated:false, 372 facilities,
        cities, top providers, related intel.

An agent that guesses tells its human DC Hub charges for data it returns free.
The correct call was sitting in this catalog tuple the whole time, unpublished.

★ WHY THIS CANNOT DRIFT. The example comes from the same catalog entry as the
name and the description, which the manifest already publishes. It is not a
second hand-maintained list — that is the failure this manifest has had twice
(9 phantom tools + 11 missing, r-fix 2026-06-06).

★ WHICH BUILDER IS LIVE. main.py defines /.well-known/mcp.json TWICE. The
before_request hook (~line 7221) intercepts BEFORE the @app.route registered
later, and says so in its own comment — it is the actual responder, and it is
the one that calls tools_for_well_known(). The @app.route builder's
_canonical_mcp_manifest() is a separate list. This test asserts on the function
the live responder uses.
"""
import re

import pytest

from routes.mcp_tool_catalog import _merged_tools, tools_for_well_known

# name(arg=value, ...) — a call an agent can copy, not prose.
CALL_SHAPE = re.compile(r"^[a-z][a-z0-9_]*\((?:[a-z][a-z0-9_]*=.*)?\)$")


@pytest.fixture(scope="module")
def published():
    return tools_for_well_known()


def test_every_published_tool_carries_a_call_example(published):
    """The defect: the field was computed and discarded."""
    missing = [t["name"] for t in published if not t.get("example")]
    assert not missing, (
        f"{len(missing)} tools published with no call example, so an agent "
        f"reading the manifest cannot construct a call: {missing[:10]}")


def test_the_published_example_is_the_catalog_example(published):
    """Not a second list — the same tuple the name and description come from."""
    catalog = {name: ex for name, _c, _t, _s, ex in _merged_tools()}
    assert {t["name"]: t["example"] for t in published} == catalog, (
        "the manifest example diverged from the catalog it is derived from")


def test_every_example_is_shaped_like_a_call(published):
    """Prose in this field would be worse than nothing — it would look callable."""
    bad = [(t["name"], t["example"]) for t in published
           if not CALL_SHAPE.match(t["example"].strip())]
    assert not bad, f"examples that are not a callable shape: {bad[:5]}"


def test_the_example_names_its_own_tool(published):
    """A copy-paste slip here teaches a call to the wrong tool."""
    wrong = [(t["name"], t["example"]) for t in published
             if not t["example"].strip().startswith(t["name"] + "(")]
    assert not wrong, f"example does not call its own tool: {wrong[:5]}"


def test_the_market_intel_example_uses_the_schema_argument(published):
    """The measured case. `market_slug` is the name of the VALUE; passing it as
    the argument is silently stripped and returns no market data."""
    ex = next(t["example"] for t in published if t["name"] == "get_market_intel")
    assert "market=" in ex, f"get_market_intel example must pass `market`: {ex!r}"
    assert "market_slug=" not in ex, (
        f"get_market_intel example passes market_slug, which is stripped: {ex!r}")


def test_the_manifest_keys_stay_a_superset_of_the_old_contract(published):
    """Adding a key must not drop one — registries bind to name/tier/description."""
    for t in published:
        assert {"name", "tier", "description"} <= set(t), (
            f"{t.get('name')} lost a manifest key: {sorted(t)}")
