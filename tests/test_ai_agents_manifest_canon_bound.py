"""ai-agents.json CANON-BINDING fence — the agent front door cannot restate a number.

`/api/v1/ai-agents.json` (and its `.well-known` twin) is the document every 404
`hint` in this service routes a blocked agent to. On 2026-08-30 it was the only
agent-facing surface NOT fed by a canonical bridge, and it published five figures
that nothing else corroborated:

    field                                 published    corroborated by
    data_coverage.news_articles           15,254       3,503  (/api/v1/stats/canonical)
    adoption.agent_requests               814,414      111,741 (/api/v1/agents/citations)
    authentication.free_tier.daily_calls  100          10     (canon + tier_registry + edge)
    rate_limits.free_tier                 "10/day ... 14 paid tools"   5 in the enforcer
    free_tier.paid_only_tools             3 names      5 in the enforcer

Two of those contradicted each other INSIDE THIS ONE FILE — `daily_calls: 100`
sat 200 lines from `rate_limits.free_tier: "10 calls/day"`. An agent budgeting
against 100 is cut off at 10 and cannot tell which half lied.

Design mirrors tests/test_canonical_counts_drift.py: static source reads, no DB
and no network. The helpers are extracted from main.py by AST and executed, so
this fences the SHIPPED derivation rather than a re-implementation of it — a
copy here could agree with itself while main.py drifted.

The source-of-truth modules are IMPORTED, never restated:
  ai_surface_canon.PINNED['free_tier_calls_per_day']  the ADVERTISED figure
  tier_registry.calls_per_day / price                  the tier registry
  mcp_upgrade_gate.PAID_ONLY_TOOLS                     the set the gate ENFORCES
"""
from __future__ import annotations

import ast
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(REPO, "main.py")

HELPERS = ("_canon_nums", "_canon_text", "_canon_int",
           "_paid_only_tools", "_rate_limit_text")


def _source() -> str:
    with open(MAIN, encoding="utf-8") as fh:
        return fh.read()


def _load_helpers() -> dict:
    """Execute main.py's OWN helper defs, extracted by AST.

    Importing main.py is not an option here (module-level DB/env work), and
    re-implementing the helpers would fence a copy instead of the shipped code.
    """
    tree = ast.parse(_source())
    ns: dict = {}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in HELPERS:
            exec(compile(ast.Module([node], []), "<main-helpers>", "exec"), ns)
            found.add(node.name)
    missing = set(HELPERS) - found
    assert not missing, (
        f"main.py no longer defines {sorted(missing)} at module level. The "
        f"ai-agents manifest binds its numbers through these; if they moved, "
        f"move this fence with them rather than deleting it."
    )
    return ns


def _handler_source() -> str:
    """Just the ai-agents.json handler body, so sweeps cannot match elsewhere."""
    src = _source()
    start = src.find("if path == '/.well-known/ai-agents.json'")
    assert start != -1, "ai-agents.json handler not found in main.py"
    end = src.find('"provider": {', start)
    assert end != -1 and end > start, "ai-agents handler end marker not found"
    return src[start:end]


def _handler_code() -> str:
    """The handler with whole-line comments removed.

    ★ The banned-literal sweeps below MUST read code, not prose. Written
    against the raw source, every one of them failed on the comment that
    explains why the literal was removed — the fence would have been arguing
    with its own documentation, and the only way to keep it green would have
    been to stop naming the bug. A comment describing 484364 is not a floor of
    484364. Whole-line comments are what this handler uses; a trailing `#` is
    left alone deliberately rather than risking a strip inside a string.
    """
    return "\n".join(
        ln for ln in _handler_source().splitlines()
        if not ln.lstrip().startswith("#")
    )


# --------------------------------------------------------------------------
# 1. the contradiction that shipped: one document, two free-tier numbers
# --------------------------------------------------------------------------

def test_free_tier_daily_calls_agrees_with_rate_limits_line():
    ns = _load_helpers()
    daily = ns["_canon_int"]("{canon_free_calls}", 10)
    line = ns["_rate_limit_text"]("free")
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", line)]
    assert nums, f"rate_limits.free_tier carried no number: {line!r}"
    assert nums[0] == daily, (
        f"authentication.free_tier.daily_calls={daily} but rate_limits.free_tier "
        f"says {nums[0]} ({line!r}). This is the exact 100-vs-10 contradiction "
        f"that shipped on 2026-08-30, in one document."
    )


def test_free_tier_matches_the_advertised_canon():
    ns = _load_helpers()
    import ai_surface_canon
    pinned = ai_surface_canon.PINNED.get("free_tier_calls_per_day")
    assert pinned, "canon lost free_tier_calls_per_day"
    assert ns["_canon_int"]("{canon_free_calls}", 10) == int(pinned), (
        "the manifest's free-tier figure no longer equals the ADVERTISED canon. "
        "10 is the figure every enforcement lane agrees on (tier_registry "
        "rate_limit + mcp_daily, edge MCP_TIERS.free). 100 is "
        "mcp_upgrade_gate.FREE_DAILY_LIMIT, which main.py's own note says must "
        "never be quoted on a public surface."
    )


def test_free_tier_never_quotes_the_legacy_enforcement_number():
    handler = _handler_code()
    assert '"daily_calls": 100' not in handler, (
        "the literal `\"daily_calls\": 100` is back on the agent front door. "
        "That is the legacy Flask gate's number, not the advertised one."
    )


# --------------------------------------------------------------------------
# 1b. WIRING — the manifest must actually CALL the derivations
# --------------------------------------------------------------------------
#
# ★ Found by mutating this fence: reverting `paid_only_tools` to its hand-typed
# three names left all thirteen assertions GREEN. Every test above exercised the
# HELPER, and the helper keeps returning the enforcer's set whether or not the
# manifest still calls it — testing a function is not testing that the surface
# uses it. These assertions read the handler's own source for the call sites, so
# unbinding a field reddens even when the helper beside it stays correct.

WIRED_FIELDS = (
    ('"daily_calls": _canon_int("{canon_free_calls}"',
     "authentication.free_tier.daily_calls"),
    ('"paid_only_tools": _paid_only_tools()',
     "authentication.free_tier.paid_only_tools"),
    ('"free_tier": _rate_limit_text("free")', "rate_limits.free_tier"),
    ('"developer": _rate_limit_text("developer")', "rate_limits.developer"),
    ('"pro": _rate_limit_text("pro")', "rate_limits.pro"),
    ('"enterprise": _rate_limit_text("enterprise")', "rate_limits.enterprise"),
)


@pytest.mark.parametrize("call,field", WIRED_FIELDS)
def test_manifest_field_is_wired_to_its_derivation(call, field):
    assert call in _handler_code(), (
        f"{field} is no longer derived — the call site `{call}` is gone from "
        f"the ai-agents.json handler. A literal here reads correct on the day "
        f"it is typed and silently rots after; that is how this surface came "
        f"to publish five figures nothing else corroborated."
    )


# --------------------------------------------------------------------------
# 2. paid-tool set: read the enforcer, do not restate it
# --------------------------------------------------------------------------

def test_paid_only_tools_is_the_set_the_gate_enforces():
    ns = _load_helpers()
    from mcp_upgrade_gate import PAID_ONLY_TOOLS
    assert ns["_paid_only_tools"]() == sorted(PAID_ONLY_TOOLS), (
        "free_tier.paid_only_tools no longer equals mcp_upgrade_gate."
        "PAID_ONLY_TOOLS. It shipped as a hand-typed 3 of the enforcer's 5 — "
        "omitting get_grid_intelligence and get_fiber_intel while the same "
        "document capped both two keys above."
    )


def test_paid_tool_count_in_rate_limits_matches_that_set():
    ns = _load_helpers()
    from mcp_upgrade_gate import PAID_ONLY_TOOLS
    line = ns["_rate_limit_text"]("free")
    m = re.search(r"across (\d+) paid tools", line)
    assert m, f"rate_limits.free_tier lost its paid-tool count: {line!r}"
    assert int(m.group(1)) == len(PAID_ONLY_TOOLS), (
        f"rate_limits.free_tier claims {m.group(1)} paid tools; the enforcer "
        f"holds {len(PAID_ONLY_TOOLS)}. The shipped string said 14, sourced "
        f"from nothing."
    )


# --------------------------------------------------------------------------
# 3. priced tiers derive from the registry
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["developer", "pro", "enterprise"])
def test_priced_tiers_derive_from_tier_registry(tier):
    ns = _load_helpers()
    import tier_registry
    line = ns["_rate_limit_text"](tier)
    assert line, f"rate_limits.{tier} resolved empty"
    calls = int(re.findall(r"[\d,]+", line)[0].replace(",", ""))
    assert calls == tier_registry.calls_per_day(tier), (
        f"rate_limits.{tier} quotes {calls}/day against registry "
        f"{tier_registry.calls_per_day(tier)}"
    )
    price = tier_registry.price(tier)
    if price:
        assert f"${int(price)}/mo" in line, (
            f"rate_limits.{tier} price drifted from tier_registry: {line!r}"
        )


# --------------------------------------------------------------------------
# 4. adoption: the gate above must not be undone below
# --------------------------------------------------------------------------

def test_agent_requests_carries_no_hardcoded_floor():
    handler = _handler_code()
    assert "484364" not in handler, (
        "the 484364 agent_requests floor is back. A floor that survives a "
        "truncated table also survives a CORRECTION — it published 814,414 "
        "against a gated 111,741 and could only ever ratchet up."
    )


def test_agent_requests_is_not_a_raw_call_log_count():
    handler = _handler_code()
    raw = re.search(r"COUNT\(\*\)\s+FROM\s+mcp_call_log", handler)
    assert raw is None, (
        "agent_requests is reading a raw COUNT(*) over mcp_call_log again. "
        "That counts every self-call, probe and internal request, and it "
        "OVERWRITES the _is_real_ai_platform-gated external total computed "
        "directly above — the block whose own comment promises 'never the "
        "inflated all-inclusive figure'."
    )


def test_adoption_still_passes_through_the_platform_gate():
    handler = _handler_code()
    assert "_is_real_ai_platform" in handler, (
        "the allowlist gate on adoption metrics is gone; ai_platforms and "
        "agent_requests would go back to publishing inflated junk."
    )


# --------------------------------------------------------------------------
# 5. news_articles: one field name, one table
# --------------------------------------------------------------------------

def test_news_articles_never_counts_the_abandoned_news_table():
    """The manifest must not bind to `news` — it is dead.

    ★ This assertion is INVERTED from how it first shipped, and the inversion
    is the lesson. The first version required `FROM news` and banned
    `FROM announcements`, reasoning that /api/v1/stats/canonical publishes
    `news_articles` from `news`, so the manifest should match it and stop the
    two disagreeing. One name, one number.

    `news` is a DEAD table — tests/test_news_freshness_watches_live_table.py
    has said so since 2026-08-13: its only writer is news_aggregator.py, which
    no workflow invokes, and four monitors once read it and reported a working
    pipeline as stale. That guard is what caught this, after auto-merge had
    already taken the change.

    So agreeing with a citable surface is NOT the invariant. Counting a table
    something still WRITES is. `announcements` is what /api/news serves.
    """
    handler = _handler_code()
    assert re.search(r"COUNT\(\*\)\s+FROM\s+announcements", handler), (
        "the manifest's news_articles no longer counts `announcements`, the "
        "table /api/news serves. If it moved, it must have moved to another "
        "LIVE table — never to `news`."
    )
    assert not re.search(r"COUNT\(\*\)\s+FROM\s+news\b(?!_)", handler), (
        "the manifest is counting the abandoned `news` table again. Nothing "
        "writes it, so the published figure freezes and can only get staler — "
        "and it is ~4x below the live count, so the manifest under-claims."
    )


def _code_only(text):
    """Drop whole-line comments before matching SQL.

    ★ Necessary, and found by mutation twice in one day. The prose in these
    files NAMES the tables and literals under discussion, so a search over raw
    source matches the explanation instead of the code — a guard arguing with
    its own documentation. Here, the comment block added beside the fix lists
    "COUNT(*) FROM announcements" three times, which kept this test green while
    the actual query was repointed at another table.
    """
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_manifest_and_canonical_now_count_the_same_live_table():
    """The disagreement is resolved by fixing the AUTHORITY, not the surfaces.

    Replaces a tripwire that asserted stats_canonical STILL read the dead
    `news` table, written to fire exactly when someone fixed it and to say that
    both surfaces should then be reconciled together. This is that change.

    The order is the lesson: on 2026-08-30 the manifest was pointed AT the dead
    table to agree with canonical, shipping a 4x under-claim to agents.
    Agreement is worth nothing when the thing agreed on is abandoned.
    """
    canon_path = os.path.join(REPO, "routes", "facilities_by_dims.py")
    with open(canon_path, encoding="utf-8") as fh:
        canon = _code_only(fh.read())

    assert not re.search(r"COUNT\(\*\)\s+FROM\s+news\b(?!_)", canon), (
        "routes/facilities_by_dims.stats_canonical is counting the abandoned "
        "`news` table again. It is the CITABLE surface, so a frozen count "
        "there propagates to everything that quotes us."
    )

    # Bind the table to the STAT, not merely to the file: another query in the
    # same module would otherwise satisfy a file-wide search.
    stmt = re.search(
        r'FROM (\w+)"\s*\)\s*\n\s*stats\["news_articles"\]', canon
    )
    assert stmt, (
        "could not find the statement assigning stats['news_articles']; if its "
        "shape changed, re-anchor this guard rather than deleting it."
    )
    assert stmt.group(1) == "announcements", (
        f"stats_canonical computes news_articles from `{stmt.group(1)}`. It "
        f"must be `announcements` — the table /api/health, /api/news and the "
        f"ai-agents manifest all count. A third table here recreates the "
        f"same-name-different-number split this change exists to end."
    )

    assert re.search(r"COUNT\(\*\)\s+FROM\s+announcements", _handler_code()), (
        "the ai-agents manifest and stats_canonical no longer count the same "
        "table. One name, one number — but only when that number is live."
    )
