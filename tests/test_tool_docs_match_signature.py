#!/usr/bin/env python3
"""tests/test_tool_docs_match_signature.py — a tool must not advertise a
parameter it does not accept.

r-infraargs (2026-08-12): get_infrastructure's own docstring said
`Example: market='Loudoun County, VA'`, the main.py catalog said "by state",
and qa_mcp_test.py called it with {"state","data_type"} — none of which are
real parameters. Its real signature is (lat, lon, radius_km, layer,
min_voltage_kv, limit). A paying customer's agent read that documentation and
failed seven times in three hours before discovering coordinates on its own.

Agents read these strings as the contract. When the strings lie, the agent
cannot recover by trying harder — it retries the same wrong shape.

Run standalone:   python3 tests/test_tool_docs_match_signature.py
Run under pytest: pytest tests/test_tool_docs_match_signature.py
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Names that have repeatedly been hallucinated onto coordinate tools.
PHANTOM = ("market=", "state=", "region=", "data_type=", '"state"', '"data_type"')


def _read(rel):
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as fh:
        return fh.read()


def _signature_params(src, func_name):
    """Real parameter names, parsed from the AST — not from prose."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            a = node.args
            return {p.arg for p in (a.posonlyargs + a.args + a.kwonlyargs)}
    raise AssertionError(f"{func_name} not found")


def test_get_infrastructure_signature_is_coordinate_based():
    params = _signature_params(_read("dchub_mcp_server.py"), "get_infrastructure")
    assert {"lat", "lon"} <= params, "lat/lon must be real parameters"
    for phantom in ("market", "state", "region", "data_type"):
        assert phantom not in params, (
            f"'{phantom}' is not a real parameter — if that changed, update this test"
        )


def test_get_infrastructure_docstring_advertises_no_phantom_params():
    src = _read("dchub_mcp_server.py")
    tree = ast.parse(src)
    doc = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "get_infrastructure":
            doc = ast.get_docstring(node) or ""
    assert doc is not None, "get_infrastructure must have a docstring"
    for bad in ("market=", "market='", "state=", "data_type="):
        assert bad not in doc, (
            f"docstring advertises non-existent parameter ({bad}) — this is what "
            f"made an agent retry seven times without ever trying coordinates"
        )
    assert "lat" in doc and "lon" in doc, "docstring must name the required coordinates"


def test_catalog_descriptions_do_not_promise_place_name_lookup():
    """The short descriptions an agent sees in tools/list must not say 'by state'."""
    for rel in ("main.py", "mcp_gatekeeper.py"):
        for line in _read(rel).splitlines():
            if "get_infrastructure" not in line or "description" not in line.lower():
                continue
            low = line.lower()
            assert "by state" not in low, f"{rel}: catalog still promises state lookup"
            assert "per region" not in low, f"{rel}: catalog still promises region lookup"


def test_qa_suite_calls_the_tool_the_way_it_actually_works():
    """A QA probe using phantom params tests nothing and teaches the wrong shape."""
    for line in _read("qa_mcp_test.py").splitlines():
        if '"get_infrastructure"' not in line:
            continue
        assert '"state"' not in line and "data_type" not in line, (
            "qa_mcp_test calls get_infrastructure with parameters it does not accept"
        )
        assert '"lat"' in line and '"lon"' in line, (
            "qa_mcp_test must exercise the real coordinate signature"
        )


def test_invalid_args_response_is_actionable():
    """An error an agent cannot act on costs more than the call it replaces."""
    src = _read("dchub_mcp_server.py")
    i = src.index("async def get_infrastructure")
    body = src[i: i + 6000]
    assert "working_example" in body, "the error must show a call that works"
    assert "valid_layers" in body, "the error must enumerate the valid layers"
    assert "_VALID_LAYERS" in body, "an unknown layer must be rejected, not silently empty"


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
