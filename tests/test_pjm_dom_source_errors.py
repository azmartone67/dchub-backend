"""Regression guard: pjm_dom_zone's fail-closed marker must say WHY.

2026-07-31: gridstatus.io was rejecting every call (403 "API requests limit
reached. Usage: 375, Limit: 250") but _gridstatus_dom computed the per-dataset
errors and then returned bare None — the source_unavailable marker carried no
error at all, making a provider quota exhaustion indistinguishable from a
missing API key. Diagnosing it required replaying the upstream calls by hand.

Static AST checks (pjm_dataminer imports nothing heavy, but stay consistent
with the house no-import rule): the marker path must attach source_errors, and
_gridstatus_dom must accept + populate the errs channel.
"""
import ast
import os

_SRC = os.path.join(os.path.dirname(__file__), "..", "pjm_dataminer.py")


def _tree() -> ast.Module:
    with open(os.path.abspath(_SRC), "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    assert isinstance(tree, ast.Module) and len(tree.body) > 5, (
        "pjm_dataminer.py parsed to a degenerate module")
    return tree


def _fn(name: str) -> ast.FunctionDef:
    for node in _tree().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"pjm_dataminer.py no longer defines {name}()")


def test_marker_carries_source_errors():
    consts = {n.value for n in ast.walk(_fn("pjm_dom_zone"))
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "source_errors" in consts, (
        "pjm_dom_zone's source_unavailable marker no longer attaches "
        "source_errors — a provider 403 becomes indistinguishable from a "
        "missing key again")


def test_gridstatus_dom_populates_errs_channel():
    node = _fn("_gridstatus_dom")
    args = [a.arg for a in node.args.args]
    assert "errs" in args, (
        "_gridstatus_dom lost its errs parameter — per-dataset failures are "
        "dropped on the floor when it returns None")
    consts = {n.value for n in ast.walk(node)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert {"gridstatus_pjm_load", "gridstatus_pjm_lmp"} <= consts, (
        "_gridstatus_dom no longer records both dataset failures into errs")
