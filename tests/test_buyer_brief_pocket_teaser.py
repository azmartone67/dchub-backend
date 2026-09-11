"""/api/v1/brief/buyer must list pocket listings as TEASERS.

The pocket-listing program puts asking price behind an auth wall
(routes/exclusive_listings.py). The buyer brief read the same table with its own
SELECT and shipped `asking_price` per row, and `soft_gate(truncate_to=3)` still
served three full rows to anonymous callers — so the wall had a side door.

Asserted on the AST of `buyer_brief`, not on source text: the dict appended to
`listings` and the SQL bound to `sql_li`. A substring check would also match
this docstring and the explanatory comment in the route.
"""
import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parent.parent / "routes" / "persona_briefs.py"
_WALLED = {"asking_price", "asking_currency", "contact", "latitude", "longitude", "detail"}


def _buyer_brief():
    tree = ast.parse(SRC.read_text())
    fns = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "buyer_brief"]
    assert len(fns) == 1, "buyer_brief not found — this guard would check nothing"
    return fns[0]


def _pocket_row_keys(fn):
    keys = []
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "listings" and node.args
                and isinstance(node.args[0], ast.Dict)):
            keys.append({k.value for k in node.args[0].keys if isinstance(k, ast.Constant)})
    return keys


def _pocket_sql_columns(fn):
    for node in ast.walk(fn):
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "sql_li"
                                                  for t in node.targets)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            select = node.value.value.split("FROM")[0]
            return set(re.findall(r"[a-z_]+", select.replace("SELECT", "").lower()))
    return None


def test_pocket_rows_in_the_buyer_brief_are_teasers():
    fn = _buyer_brief()
    rows = _pocket_row_keys(fn)
    assert len(rows) == 1, f"expected one pocket-row dict, found {len(rows)}"
    assert {"slug", "title", "market", "capacity_mw"} <= rows[0]     # the right dict
    assert not rows[0] & _WALLED, f"walled fields in the buyer brief: {rows[0] & _WALLED}"


def test_the_buyer_brief_does_not_select_walled_columns():
    cols = _pocket_sql_columns(_buyer_brief())
    assert cols and {"slug", "capacity_mw"} <= cols, f"pocket SELECT not found: {cols}"
    assert not cols & _WALLED, f"walled columns selected: {cols & _WALLED}"
