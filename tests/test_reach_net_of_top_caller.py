"""/api/v1/ai/reach publishes the net-of-top-caller companion (2026-08-25).

WHAT WAS MEASURED
=================
The /ai header badge reads THIS endpoint and renders "N distinct external
agents called MCP tools in the last 7 days" with no concentration context.
Live this evening:

    real_calls_7d          1,982
    top caller             Smithery Connect — 1,855 calls (93.6%)
    net of it              127 calls · 17 agents

So the badge headline tracked one caller — a registry GATEWAY that fronts us
rather than indexing us — and said nothing about it.

Verified against production before shipping:

    calls(1982) == top(1855) + net(127)          -> True
    matches real_calls_7d(1982)                  -> True
    matches real_agents_7d(18)                   -> True

THE CONTRACT being guarded
==========================
- The fields come from `canonical_top_caller_sql`, the SAME helper, window and
  predicates as `real_calls_7d`. This file's own history is a run of defects
  where two surfaces read two lineages and printed two different agent counts
  on one page; a second endpoint fetch for the badge would reproduce it.
- The identity `real_calls_7d == top_caller_calls_7d + real_calls_net_of_top_7d`
  holds BY CONSTRUCTION (one CTE), so a guard asserts the code cannot start
  computing either side separately.
- The block is fail-soft: on any error the fields stay ABSENT rather than
  emitting a confident zero. A fabricated 0 inside a sentence is the exact
  defect this module's r-one-chokepoint comments were written about.
- `net_of_top_basis` ships, and says the subtracted caller is NOT excluded
  elsewhere and that a gateway is not automatically a non-agent.

NO NETWORK, NO DB — the handler source is read as text.
"""
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "routes", "ai_reach.py")


def _src():
    with open(_SRC, encoding="utf-8") as fh:
        return fh.read()


def _block():
    """Only the net-of-top block, not the module's prose."""
    s = _src()
    start = s.index("from mcp_calls_deloop import canonical_top_caller_sql")
    return s[start:start + 2600]


def test_all_five_fields_ship():
    b = _block()
    for f in ("top_caller_calls_7d", "top_caller_pct_7d",
              "real_calls_net_of_top_7d", "real_agents_net_of_top_7d",
              "concentration_flag_7d"):
        assert f'"{f}"' in b, f"{f} stopped shipping"


def test_it_uses_the_canonical_helper_not_its_own_sql():
    """A second lineage is how this page printed two agent counts before."""
    b = _block()
    assert "canonical_top_caller_sql(7)" in b, (
        "the block no longer calls the canonical helper — a hand-rolled query "
        "here can drift from real_calls_7d and put two totals on one page"
    )
    assert "SELECT" not in b.upper().replace("CANONICAL_TOP_CALLER_SQL", ""), (
        "raw SQL appeared in the reach handler; the whole point of the helper "
        "is that numerator and denominator come from ONE query"
    )


def test_net_is_read_from_the_query_not_subtracted_by_hand():
    """calls - top computed here would be exactly the drift the CTE prevents."""
    b = _block()
    assert 'tc.get("calls_net_of_top")' in b, (
        "net calls is no longer read from the query result"
    )
    assert not re.search(r"_all\s*-\s*_top", b), (
        "net is being derived by hand from two fields instead of read from "
        "the single CTE that guarantees the identity"
    )


def test_fields_stay_absent_when_there_is_no_traffic():
    """An empty window must not publish a confident 0% concentration."""
    b = _block()
    assert "if _all > 0:" in b, (
        "the zero-denominator guard is gone — with no traffic this would "
        "divide by zero or publish a fabricated 0"
    )


def test_block_is_fail_soft():
    b = _block()
    assert "except Exception:" in b and "pass" in b, (
        "the block no longer degrades to absent fields; an error here would "
        "take down the whole reach payload"
    )


def test_basis_ships_and_names_the_gateway_caveat():
    b = _block()
    assert '"net_of_top_basis"' in b, "the basis stopped shipping"
    seg = b[b.index('"net_of_top_basis"'):]
    assert "by construction" in seg, "the identity guarantee is no longer stated"
    assert "gateway is not automatically a non-agent" in seg, (
        "the caveat that a top caller may be a legitimate customer was "
        "dropped — without it this reads as customer concentration"
    )


def test_threshold_is_published_not_left_to_the_renderer():
    b = _block()
    assert "concentration_flag_7d" in b and "25.0" in b, (
        "the concentration threshold is no longer published, so each renderer "
        "picks its own and they quietly disagree"
    )
