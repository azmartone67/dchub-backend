#!/usr/bin/env python3
"""No public surface may publish a free-tier daily cap that nothing enforces.

NO NETWORK, NO DB.

Four surfaces in this service published `daily_calls: 100` for the free tier.
Nothing enforces 100. The split is recorded at main.py's MCP_FREE_DAILY_LIMIT:

    ADVERTISED (public gate)  10   ai_surface_canon.PINNED, tier_registry
                                   TIER_LIMITS['free'], edge worker MCP_TIERS
    ENFORCED (legacy Flask)   25   MCP_FREE_DAILY_LIMIT, deliberately looser,
                                   and its own note says DO NOT quote it publicly

100 is neither. It reached the claim response — the first thing a partner
integration reads — and the key-bind response, which is the moment we ask for
an email and therefore the moment the before/after has to be true.

This pins the PUBLISHED numbers against canon. It deliberately says nothing
about MCP_FREE_DAILY_LIMIT: tightening that is a live quota cut and an owner
call, not a copy fix.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _canon(key):
    from ai_surface_canon import PINNED
    return int(PINNED[key])


def test_canon_and_registry_agree_on_the_advertised_free_cap():
    """If these two ever disagree, every surface below inherits the ambiguity."""
    import tier_registry
    assert _canon("free_tier_calls_per_day") == int(tier_registry.calls_per_day("free"))
    assert _canon("identified_calls_per_day") == int(tier_registry.calls_per_day("identified"))


def test_the_advertised_free_cap_is_not_a_number_nothing_enforces():
    assert _canon("free_tier_calls_per_day") != 100, (
        "100 is advertised by canon but enforced by no gate"
    )


def test_claim_and_bind_responses_do_not_hardcode_a_daily_cap():
    """Every published site must resolve the cap, not type it.

    Scanned via AST, not text. A regex over the source flagged the COMMENT
    that explains this very change, because the comment quotes the literal it
    replaced — the guard would have been unsatisfiable by any wording that
    records what happened. The AST sees code only.
    """
    import ast

    tree = ast.parse((ROOT / "flask_mcp_endpoints.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        # daily_calls=<int literal> as a call keyword
        if isinstance(node, ast.keyword) and node.arg == "daily_calls":
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, int):
                offenders.append(f"kw:{node.value.value}")
        # {"daily_calls": <int literal>} in a dict
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "daily_calls"
                        and isinstance(v, ast.Constant) and isinstance(v.value, int)):
                    offenders.append(f"dict:{v.value}")
    assert not offenders, (
        f"published daily_calls is an int literal in flask_mcp_endpoints.py: "
        f"{offenders} — resolve it from tier_registry/canon/auto_trial so a "
        f"repriced tier heals every surface at once"
    )


def test_the_worked_example_in_the_tool_catalog_is_not_a_literal():
    """A worked example is the line a reader copies."""
    src = (ROOT / "routes" / "mcp_tool_catalog.py").read_text(encoding="utf-8")
    assert '"daily_calls":100' not in src.replace(" ", ""), (
        "the tool-catalog quick-start still publishes a hardcoded free-tier cap"
    )
    assert "_free_calls" in src, "the example no longer resolves the canon value"
