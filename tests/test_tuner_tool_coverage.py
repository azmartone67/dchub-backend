#!/usr/bin/env python3
"""tests/test_tuner_tool_coverage.py — which tools get per-platform descriptions,
and the two ways that list goes wrong.

NO NETWORK, NO DB.

WHAT THE LIST IS. ai_platform_tool_tuner generates a description per (tool,
platform) into mcp_tool_descriptions_per_platform. The MCP server prefers that
row over its inline text, so a tool ON this list is what tuned platforms
actually receive; a tool OFF it falls back to inline copy written for nobody in
particular.

★ THE SELECTION CRITERION IS BREADTH, NOT VOLUME, and this file records why.
Measured 2026-09-06 over mcp_tool_calls, 30d, net of chain-hire and our own
buckets:

    clients  calls  tool
         13    135  search_facilities          tuned
         10     92  get_market_dcpi_rank       ADDED here
          9    456  analyze_site               95% ONE caller
          -  1,481  search                     1,473 of them ONE caller

By raw volume `search` led by 10x. It is one automated client calling one tool
1,473 times — a description cannot change the behaviour of a caller that already
invokes the tool on a loop. It can only influence an agent CHOOSING between
tools, and distinct-client count is what measures that. Two rankings in this
investigation were wrong before the decomposition was done.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "routes", "ai_platform_tool_tuner.py")


def _src():
    return open(SRC, encoding="utf-8").read()


def test_the_widest_reach_untuned_tool_is_now_tuned():
    from routes.ai_platform_tool_tuner import TUNED_TOOLS
    assert "get_market_dcpi_rank" in TUNED_TOOLS, (
        "get_market_dcpi_rank left the list — it was the widest-reach untuned "
        "tool (10 distinct clients) and it answers 'should I build here' with a "
        "verdict and a quotable narrative that tuned platforms never see")


def _code_only():
    """Source with comment lines removed.

    ★ Needed because the rename comment NAMES the old constant to explain the
    rename — and a raw substring scan flags its own documentation, which then
    gets "fixed" by deleting the explanation. Established pattern in this repo;
    this check tripped on it first time out.
    """
    return "\n".join(l for l in _src().splitlines()
                      if not l.lstrip().startswith("#"))


def test_no_constant_names_its_own_length():
    """★ The old name became a lie the moment an 11th tool was added, and the
    next reader trusts the name over the contents."""
    src = _code_only()
    assert "TOP_10_TOOLS" not in src, (
        "the count-naming constant is back; a list called TOP_10 with 11 "
        "entries misleads every reader who does not count them")
    bad = re.findall(r"\b[A-Z_]*TOP_\d+[A-Z_]*\b|\b[A-Z_]*_\d+_TOOLS\b", src)
    assert not bad, "a constant still states its own length: %r" % bad


def test_the_documented_seed_bound_matches_the_real_one():
    """★ The docstring publishes the worst-case Claude spend of a /seed run.
    It already said '110 at 11 platforms' while there were 12 — a bound that
    drifts is worse than none, because it is quoted in the safety section."""
    from routes.ai_platform_tool_tuner import TUNED_TOOLS, TUNED_PLATFORMS
    real = len(TUNED_TOOLS) * len(TUNED_PLATFORMS)
    src = _src()
    i = src.index("Seed is bounded")
    claim = src[i:i + 220]
    nums = [int(n) for n in re.findall(r"\b(\d{2,4})\b", claim)]
    assert real in nums, (
        "the documented seed bound %r does not contain the real worst case "
        "%d (%d tools x %d platforms)"
        % (claim.split("\n")[1:3], real, len(TUNED_TOOLS), len(TUNED_PLATFORMS)))


def test_every_listed_tool_is_a_real_tool_name():
    """A typo here costs a whole (tool x platform) column silently: the seed
    generates copy for a name no tools/list will ever return."""
    from routes.ai_platform_tool_tuner import TUNED_TOOLS
    for t in TUNED_TOOLS:
        assert re.fullmatch(r"[a-z][a-z0-9_]{3,40}", t), "bad tool name %r" % t
    assert len(set(TUNED_TOOLS)) == len(TUNED_TOOLS), (
        "a tool is listed twice — the seed would spend double on it")


def test_the_breadth_rationale_is_recorded_beside_the_list():
    """The next person to extend this list will reach for call volume, because
    that is the obvious metric and it is on every dashboard. The counter-example
    has to live here, not in a commit message."""
    src = _src()
    i = src.index("TUNED_TOOLS = [")
    block = src[i:src.index("\n]", i)]
    assert "chain-hire" in block, (
        "the worked counter-example is gone — volume-ranking put a "
        "single-caller tool at the top by 10x and it must stay documented")
    assert "clients" in block.lower(), (
        "the selection criterion (distinct clients, not calls) is not stated "
        "where the list is edited")
