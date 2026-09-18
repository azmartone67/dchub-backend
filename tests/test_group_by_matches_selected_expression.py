"""A GROUP BY expression must match the expression the SELECT projects.

routes/operator_brief.py::_section_market_concentration selected
`COALESCE(market, city, '')` (3-arg) while grouping by
`COALESCE(market, city)` (2-arg). Postgres matches GROUP BY to SELECT
expressions SYNTACTICALLY, not semantically -- equivalent-looking
COALESCE arities are two different expressions -- so it raised:

    column "discovered_facilities.market" must appear in the GROUP BY
    clause or be used in an aggregate function

The call site wrapped the execute in `except Exception: return []`, so the
error never surfaced and the published "market concentration" section
rendered EMPTY on every operator brief for an unknown period.
routes/operators.py::top_markets had the identical defect.
routes/quarterly_report.py had already hit this once (see its r48.1 note).

Three call sites, one bug class, found twice by accident. This guard finds
it on purpose.
"""
import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Floor: if the scanner stops finding SQL it must fail loudly rather than
# pass by looking at nothing. Measured against the tree at the time of
# writing; raise it, never lower it to make a change fit.
MIN_SQL_BLOCKS_SCANNED = 40
MIN_GROUPBY_COALESCE_FOUND = 5


def _strip_sql_comments(sql: str) -> str:
    """Drop `-- ...` to end of line.

    REQUIRED, not cosmetic: the fix for this very bug documents the broken
    SQL in a `--` comment inside the same string literal. Without stripping,
    this guard flags the comment that explains it.
    """
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())


def _coalesce_args(fragment: str) -> list[list[str]]:
    """Every COALESCE(...) in `fragment`, as a normalised list of its args.

    Paren-balanced so nested calls (e.g. COALESCE(NULLIF(x,''),'y')) are
    read as one argument rather than split on their inner comma.
    """
    out: list[list[str]] = []
    for m in re.finditer(r"\bCOALESCE\s*\(", fragment, re.I):
        i = m.end()
        depth, start, args = 1, i, []
        while i < len(fragment) and depth:
            ch = fragment[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    args.append(fragment[start:i])
                    break
            elif ch == "," and depth == 1:
                args.append(fragment[start:i])
                start = i + 1
            i += 1
        if args:
            out.append([" ".join(a.split()).lower() for a in args])
    return out


def _sql_strings():
    """Every string constant in routes/ that looks like a SELECT with a GROUP BY."""
    for path in sorted((ROOT / "routes").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:                                  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value
                if re.search(r"\bSELECT\b", s, re.I) and re.search(r"\bGROUP\s+BY\b", s, re.I):
                    yield path.relative_to(ROOT), getattr(node, "lineno", 0), s


def _violations():
    bad, blocks, groupby_coalesces = [], 0, 0
    for rel, lineno, raw in _sql_strings():
        blocks += 1
        sql = _strip_sql_comments(raw)
        sel = re.search(r"\bSELECT\b(.*?)\bFROM\b", sql, re.I | re.S)
        grp = re.search(
            r"\bGROUP\s+BY\b(.*?)(?:\bORDER\s+BY\b|\bHAVING\b|\bLIMIT\b|\bWINDOW\b|$)",
            sql, re.I | re.S)
        if not (sel and grp):
            continue
        sel_coalesces = _coalesce_args(sel.group(1))
        grp_coalesces = _coalesce_args(grp.group(1))
        groupby_coalesces += len(grp_coalesces)
        for g in grp_coalesces:
            for s in sel_coalesces:
                if g == s or len(g) == len(s):
                    continue
                short, long_ = (g, s) if len(g) < len(s) else (s, g)
                # Same leading arguments, different arity -> Postgres sees two
                # unrelated expressions and the GROUP BY does not cover the SELECT.
                if long_[:len(short)] == short:
                    bad.append(
                        f"{rel}:{lineno}: GROUP BY COALESCE({', '.join(g)}) does not "
                        f"match SELECT COALESCE({', '.join(s)}) -- same leading args, "
                        f"different arity; Postgres will raise GroupingError")
    return bad, blocks, groupby_coalesces


def test_scanner_actually_reads_sql():
    """A guard that can find nothing would pass forever."""
    _, blocks, groupby_coalesces = _violations()
    assert blocks >= MIN_SQL_BLOCKS_SCANNED, (
        f"only {blocks} SELECT-with-GROUP-BY blocks found in routes/ "
        f"(expected >= {MIN_SQL_BLOCKS_SCANNED}) -- the scanner stopped seeing SQL")
    assert groupby_coalesces >= MIN_GROUPBY_COALESCE_FOUND, (
        f"only {groupby_coalesces} GROUP BY COALESCE(...) expressions found "
        f"(expected >= {MIN_GROUPBY_COALESCE_FOUND}) -- the extractor is not "
        f"reaching the expressions this guard exists to check")


def test_comment_stripping_does_not_hide_real_sql():
    """Stripping `--` must remove prose, not the statement after it."""
    sql = "SELECT COALESCE(a, b, '') AS m -- GROUP BY COALESCE(a, b)\n  FROM t\n GROUP BY COALESCE(a, b)"
    stripped = _strip_sql_comments(sql)
    assert "GROUP BY COALESCE(a, b)" in stripped.split("FROM")[1]
    assert len(_coalesce_args(stripped.split("FROM")[0])) == 1


def test_coalesce_arity_mismatch_is_detected():
    """The detector fires on the exact shape this bug had."""
    sel = " COALESCE(market, city, '') AS m, COUNT(*) AS n "
    grp = " COALESCE(market, city) "
    s, g = _coalesce_args(sel)[0], _coalesce_args(grp)[0]
    assert len(s) == 3 and len(g) == 2
    assert s[:len(g)] == g, "leading-argument-prefix check must match this shape"


def test_nested_coalesce_is_one_argument():
    """COALESCE(NULLIF(x, ''), 'unknown') is 2 args, not 3."""
    args = _coalesce_args("COALESCE(NULLIF(mcp_client, ''), 'unknown')")[0]
    assert len(args) == 2, args


def test_no_group_by_select_coalesce_arity_mismatch_in_routes():
    bad, _, _ = _violations()
    assert not bad, "GROUP BY does not match the projected SELECT expression:\n" + "\n".join(bad)
