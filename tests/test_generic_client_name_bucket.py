"""2026-07-28 — a generic client_name is not an identity.

WHY THIS EXISTS
---------------
We reported for weeks that ~87% of call volume sat in an "unattributed" bucket
and reasoned about it as if its contents were unknowable. It was never
unattributed. 4,283 calls/7d arrive with `clientInfo.name = "mcp"` — the name
of the PROTOCOL, not of a client — and PLATFORM_CASE's first branch trusted any
non-UUID client_name verbatim. One generic string, read as a name.

Trusting it also PRE-EMPTED the internal-dchub branch, so our own probes
(dchub-fixwave-probe/1.0 and friends: 1,072 calls / 132 agent_ids in 7d) were
classified 'mcp' instead of self-traffic. PROBE_PLATFORMS never got to see them.

THE TRAP THESE TESTS EXIST TO HOLD
----------------------------------
The obvious fix — stop trusting it, fall through to the UA branches — routes
every one of these rows into `user_agent ILIKE '%node%'` => 'node-script',
which IS in PROBE_PLATFORMS. That silently deletes our LARGEST REAL COHORT
(3,164 calls / 63 agents of genuine work: 35 distinct tools, 11-50 calls per
episode, zero single-call episodes) and reads as a traffic collapse.

So the property under test is not "generic names fall through". It is
"generic names get a REAL bucket of their own, and never touch the node
heuristics". A future refactor that simplifies these two branches into a
fall-through would pass any test that only checked the first half.
"""
from __future__ import annotations

import re

from mcp_calls_deloop import PLATFORM_CASE, PROBE_PLATFORMS, real_calls_predicate

GENERIC_NAMES = ("mcp", "mcp-client", "client", "default")


def _branch_order(sql: str) -> list[str]:
    """Bucket labels in the order the CASE evaluates them. Derived from the
    SQL, never transcribed — the whole defect was a branch order nobody read."""
    return re.findall(r"THEN\s+'([^']+)'", sql)


def test_generic_client_names_are_classified_before_the_verbatim_branch():
    """Order is the entire bug. If the verbatim branch runs first it wins, and
    every generic name becomes its own 'platform' again."""
    order = _branch_order(PLATFORM_CASE)
    assert "mcp-generic-client" in order, "the generic-name bucket is gone"
    assert "internal-dchub" in order
    # LOWER(client_name) passthrough has no literal THEN, so the verbatim
    # branch is identified by its regex; assert the generic branches precede it.
    verbatim = PLATFORM_CASE.index("THEN LOWER(client_name)")
    for label in ("internal-dchub", "mcp-generic-client"):
        assert PLATFORM_CASE.index(f"'{label}'") < verbatim, (
            f"{label} is evaluated AFTER the verbatim client_name branch, so a "
            f"client_name of 'mcp' still wins and the bucket returns")


def test_every_generic_name_is_covered():
    """A name we forget to list silently becomes a platform of its own."""
    for name in GENERIC_NAMES:
        assert f"'{name}'" in PLATFORM_CASE, f"generic name {name!r} not handled"


def test_the_generic_bucket_counts_as_REAL_traffic():
    """THE TRAP. 'node-script' is in PROBE_PLATFORMS; the generic bucket must
    NOT be, or the fix deletes 3,164 calls of real agent work."""
    assert "mcp-generic-client" not in PROBE_PLATFORMS, (
        "mcp-generic-client became a probe platform — this silently removes "
        "the largest real cohort we have")
    assert "internal-dchub" in PROBE_PLATFORMS, (
        "our own probes stopped being excluded")


def test_generic_rows_never_reach_the_node_heuristics():
    """Guards against the 'simplify into a fall-through' refactor. Both node
    buckets are probes, so a generic row reaching them is data loss."""
    order = _branch_order(PLATFORM_CASE)
    for node_bucket in ("node-script", "node-http-client"):
        assert node_bucket in PROBE_PLATFORMS, (
            f"{node_bucket} left PROBE_PLATFORMS — re-check whether generic "
            f"rows falling through would now be counted, and why")
        assert order.index("mcp-generic-client") < order.index(node_bucket), (
            f"a generic client_name can reach {node_bucket} and be dropped "
            f"as a probe — this is the exact failure the fix exists to prevent")


def test_internal_ua_still_wins_over_the_generic_bucket():
    """Our probes send client_name='mcp' too. If the plain generic branch ran
    first they would be counted as real agents — which is what was happening."""
    order = _branch_order(PLATFORM_CASE)
    assert order.index("internal-dchub") < order.index("mcp-generic-client"), (
        "self-traffic with a generic client_name would be counted as a real "
        "agent — 1,072 calls / 132 agent_ids per week of us calling ourselves")


def test_predicate_still_renders_and_stays_percent_literal():
    """psycopg2 %-trap: these patterns are literal because no params are bound.
    A stray single % here 500s every call that uses the predicate."""
    pred = real_calls_predicate()
    assert pred and "mcp-generic-client" not in pred.split("NOT IN")[-1], (
        "mcp-generic-client leaked into the excluded set")
    assert "%" in PLATFORM_CASE, "ILIKE patterns vanished — classifier is inert"
