"""Capacity Source rename guard (2026-09-13): every MCP tool name the listing
API hands an agent is a tool the public gate advertises.

Pocket listings became DC Hub Capacity Source, and dchub-mcp-server renamed its
tools: get_pocket_listings -> source_capacity, request_listing_intro ->
request_capacity_intro. The old names keep resolving there as call-time
aliases, so a stale pointer in this API would never error. It would keep
teaching agents a name tools/list no longer shows, and a typo would point at
nothing at all. So every `"mcp_tool": "<name>"` literal in
routes/exclusive_listings.py is bound to ai_surface_canon.PINNED["tool_manifest"].
Both files are read by AST; neither module is imported.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _dict_values_for_key(tree, key):
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == key:
                    yield v


def _advertised_tools():
    tree = ast.parse((ROOT / "ai_surface_canon.py").read_text(encoding="utf-8"))
    lists = [v for v in _dict_values_for_key(tree, "tool_manifest") if isinstance(v, ast.List)]
    assert len(lists) == 1, f"expected one tool_manifest list, found {len(lists)}"
    names = {e.value for e in lists[0].elts
             if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    assert len(names) > 50, "tool_manifest parsed to almost nothing"
    return names


def _listing_api_tool_pointers():
    tree = ast.parse((ROOT / "routes" / "exclusive_listings.py").read_text(encoding="utf-8"))
    values = list(_dict_values_for_key(tree, "mcp_tool"))
    assert all(isinstance(v, ast.Constant) and isinstance(v.value, str) for v in values), (
        "an mcp_tool value is not a string literal, so this guard cannot read it")
    return [v.value for v in values]


def test_every_mcp_tool_the_listing_api_names_is_advertised():
    advertised = _advertised_tools()
    pointers = _listing_api_tool_pointers()
    # Floor: the program block and the listing's introduction block both name a tool.
    assert len(pointers) >= 2, pointers
    assert set(pointers) <= advertised, sorted(set(pointers) - advertised)


def test_the_retired_listing_tool_names_are_not_advertised():
    # The control that lets the test above fail on a stale pointer: the old
    # names must be absent from the advertised set, and the new ones present.
    advertised = _advertised_tools()
    assert {"source_capacity", "request_capacity_intro"} <= advertised
    assert not {"get_pocket_listings", "request_listing_intro"} & advertised
