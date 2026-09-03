"""Read the live-proof / funnel queries out of flask_mcp_endpoints.py.

A shared helper rather than a copy in each test, for the reason this repo keeps
relearning: two copies of the same extraction drift, and the one that drifts
keeps passing while describing a query that no longer ships. Same reason
tests/_scan_floors.py is a module and not a paste.

flask_mcp_endpoints pulls Flask and psycopg2 at import, and tests/ imports
neither — so everything here is source-text and ast, never an import.
"""
from __future__ import annotations

import ast
import io
import pathlib
import tokenize

REPO = pathlib.Path(__file__).resolve().parents[1]
SOURCE = REPO / "flask_mcp_endpoints.py"


def source():
    return SOURCE.read_text(encoding="utf-8")


def body_of(fn_name, src=None):
    src = src if src is not None else source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{fn_name}() not found — the caller proves nothing")


def code_only(src):
    """The source with COMMENTS STRIPPED, string literals kept.

    ★ Written because the first reltuples ban red on the comment that EXPLAINS
    the ban. A guard that cannot tell code from prose forces a fix to delete
    its own reasoning; this repo has paid for that twice. String literals stay
    — the SQL lives in them.
    """
    return "\n".join(t.string for t in
                     tokenize.generate_tokens(io.StringIO(src).readline)
                     if t.type != tokenize.COMMENT)


def _flatten(node, subs):
    """Rebuild a concatenated SQL literal from the AST.

    The query is assembled as string literals plus the self-traffic predicate,
    so a plain Constant lookup will not find it. Every node type is handled
    explicitly and anything else RAISES — a silent fallback would let a caller
    test a query that is not the shipped one.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten(node.left, subs) + _flatten(node.right, subs)
    if isinstance(node, ast.Name):
        if node.id not in subs:
            raise AssertionError(f"query interpolates unknown name {node.id!r}")
        return subs[node.id]
    raise AssertionError(f"unreconstructable SQL node: {type(node).__name__}")


def execute_queries(fn_name, subs=None, src=None):
    """Every SQL string `fn_name` passes to cur.execute(), reconstructed."""
    src = src if src is not None else source()
    subs = subs or {}
    out = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == fn_name):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "execute" and call.args):
                continue
            try:
                out.append(_flatten(call.args[0], subs))
            except AssertionError:
                continue          # not a reconstructable literal; not ours
    return out


def platforms_query(predicate, src=None):
    """THE query behind platforms_30d — the homepage's platform headline.

    Identified by the two columns only it selects, so it cannot be confused
    with the other reads in the same endpoint (which is exactly how the first
    version of this guard passed a mutation that moved it back to the raw,
    unfiltered table).
    """
    for sql in execute_queries("stats_live_proof", {"_not_self": predicate}, src):
        if "n_gross" in sql and "platform" in sql:
            return sql
    raise AssertionError(
        "the platforms_30d query was not found in stats_live_proof() — it no "
        "longer selects n_gross beside a platform, or it stopped being a "
        "reconstructable literal. Either way this guard proves nothing.")
