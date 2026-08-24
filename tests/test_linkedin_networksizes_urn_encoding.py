"""The org URN must be PERCENT-ENCODED in the networkSizes path.

2026-08-24: `fetch_linkedin_followers` built the path by concatenating
`urn:li:organization:<id>` in raw, so LinkedIn's Rest.li path parser returned
400 ILLEGAL_ARGUMENT "Syntax exception in path variables" on every call. The
function is fail-soft and its docstring predicts a 403 scope gap, so the
resulting reason="http_400" was read as that expected gap for weeks and
li_followers stayed null in every media_growth snapshot -- while the real
follower count (350) had already passed the 250 goal.

Measured both ways against the live API on 2026-08-24:
    raw     -> 400 {"code":"ILLEGAL_ARGUMENT"}
    encoded -> 200 {"firstDegreeSize":350}

This guard reads the SHIPPED source rather than importing the module, per the
repo convention (no test may pull in DB pools or keepalive threads at import).
"""
import ast
import os
import re
import textwrap

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "linkedin_poster.py")


def _fetch_followers_source() -> str:
    raw = open(_SRC, encoding="utf-8").read()
    tree = ast.parse(raw)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "fetch_linkedin_followers":
            return ast.get_source_segment(raw, node) or ""
    raise AssertionError("fetch_linkedin_followers not found in linkedin_poster.py")


def _url_expression() -> str:
    """The `url = ...` ASSIGNMENT, not the docstring's illustrative URL.

    The docstring also contains the literal `/rest/networkSizes/{org}?edgeType=`
    and an earlier draft of this guard matched THAT, so it failed against
    already-correct code. Walk the AST for the assignment instead of scanning
    text: whatever this returns is the expression that actually runs.
    """
    src = _fetch_followers_source()
    tree = ast.parse(textwrap.dedent(src))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "url" in targets:
                return ast.dump(node.value)
    raise AssertionError("no `url = ...` assignment in fetch_linkedin_followers")


def test_networksizes_path_percent_encodes_the_urn():
    """The URN must go through a quote()-family call, not raw concatenation."""
    expr = _url_expression()
    assert "networkSizes/" in expr, "networkSizes path is gone -- retarget this guard"
    assert re.search(r"quote", expr), (
        "the org URN is concatenated into the networkSizes PATH without "
        "percent-encoding. LinkedIn returns 400 ILLEGAL_ARGUMENT for raw "
        "colons in a path variable. Wrap it: _urlquote(org_urn, safe=\"\").\n"
        f"offending url expression: {expr}"
    )


def test_quote_call_uses_safe_empty():
    """quote() leaves ':' alone by default -- safe="" is the load-bearing part."""
    expr = _url_expression()
    assert "quote" in expr, "no quote() call found -- see test_networksizes_path_percent_encodes_the_urn"
    assert re.search(r"arg='safe'.*?value=''", expr) or "keyword(arg='safe'" in expr, (
        "quote() without safe=\"\" does NOT escape ':', so the URL is still "
        f"rejected with 400. url expression: {expr}"
    )


def test_the_400_is_not_swallowed_as_a_scope_gap():
    """A non-200 must still surface its status in `reason` (regression fence)."""
    src = _fetch_followers_source()
    assert "http_" in src, (
        "the fail-soft return no longer encodes the HTTP status in `reason`; "
        "without it a 400 is indistinguishable from the 403 scope gap the "
        "docstring predicts, which is how this bug hid."
    )
