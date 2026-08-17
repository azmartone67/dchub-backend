"""Time-window SQL in news_engine / api_auto_discovery must be Postgres.

db_utils._translate_sql rewrites SQLite datetime() on the way to psycopg2,
but only by EXACT string match against SQLITE_TO_PG_FUNC — eight literal
spellings, all single-quoted, all with a space after the comma. Three
shapes slip past it, and all three shipped:

  · PARAMETERISED — `datetime('now', %s)`. The interval is a bind, so no
    literal can match. news_engine.get_latest_news (the news feed itself)
    and api_auto_discovery.api_change_events both drove off this.
  · UNLISTED SPELLING — `datetime('now','-168 hours')`: no space after
    the comma, and 168 hours is not one of the eight.
  · ESCAPED QUOTES — written inside a single-quoted Python string as
    datetime(\\'now\\', %s), which the obvious
    `grep -rn "datetime('now'"` sweep does not match at all. That is how
    the news feed's own query stayed hidden.

A fourth failure needs no datetime() at all: db_utils auto-casts a fixed
list of TEXT timestamp columns, and `checked_at` is not on it (only
`last_checked` is). api_auto_discovery's health-check join therefore
compared TEXT to timestamptz and raised "operator does not exist".

Every one of these raises before a single row is processed, and every one
sits under a bare except or a caller that logs success — so the symptom
was an empty feed, not an error.

WHAT THESE GUARDS PIN
  1. No SQL in either module calls datetime() in any spelling, escaped or
     not — the class, not the four instances.
  2. Every TEXT timestamp column these modules compare is cast, so a
     column falling off db_utils' auto-cast list cannot silently break it.

The TEXT-typed timestamp columns are PINNED constants measured against
the Neon read replica on 2026-08-17; CI runs with no DATABASE_URL and
must not need one. Every helper asserts it FOUND its target first — an
empty scan satisfies every "not in" below. Nothing runs at module scope.
"""

import ast
import os
import re

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ("news_engine.py", "api_auto_discovery.py")

# Columns these modules compare that are TEXT on the live DB, so a bare
# comparison against NOW() raises "operator does not exist: text > ...".
# Measured 2026-08-17.
TEXT_TIMESTAMP_COLS = {
    "fetched_at", "checked_at", "detected_at", "last_tested", "created_at",
    "discovered_at", "published_date", "first_seen", "timestamp",
}

# Any datetime( call whose first argument is 'now', however quoted or escaped.
_SQLITE_DATETIME = re.compile(r"datetime\s*\(\s*\\?[\"']now")


def _sql_strings(path):
    """(lineno, sql) for every non-docstring string constant holding SQL.

    Keyed on the AST, not on lines: this file's own prose names the very
    spellings it forbids, and a text sweep flags the explanation.
    """
    with open(os.path.join(_REPO, path), encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings \
                and re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", node.value, re.IGNORECASE):
            out.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for p in node.values:
                if isinstance(p, ast.Constant) and isinstance(p.value, str):
                    parts.append(p.value)
                elif isinstance(p, ast.FormattedValue):
                    parts.append(ast.unparse(p.value))
            joined = "".join(parts)
            if re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", joined, re.IGNORECASE):
                out.append((getattr(node, "lineno", 0), joined))
    return out


@pytest.mark.parametrize("module", MODULES)
def test_no_sqlite_datetime_in_any_spelling(module):
    sql = _sql_strings(module)
    assert sql, f"found no SQL in {module} — guard scanned nothing"
    offenders = [f"line {ln}: {s.strip()[:120]}"
                 for ln, s in sql if _SQLITE_DATETIME.search(s)]
    assert not offenders, (
        f"SQLite datetime() reached a Postgres cursor in {module}:\n  "
        + "\n  ".join(offenders))


def _db_utils_autocast_columns():
    """The TEXT timestamp columns db_utils._translate_sql casts for you.

    Read out of db_utils' own source rather than copied, so that a column
    leaving that tuple re-arms this guard instead of silently widening it.
    """
    with open(os.path.join(_REPO, "db_utils.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_translate_sql"), None)
    assert fn is not None, "db_utils._translate_sql not found — guard scanned nothing"

    for node in ast.walk(fn):
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            names = {e.value for e in node.iter.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            if "discovered_at" in names:      # the auto-cast loop, not some other tuple
                return names
    pytest.fail("db_utils auto-cast column tuple not found — guard scanned nothing")


@pytest.mark.parametrize("module", MODULES)
def test_text_timestamp_columns_are_cast_before_comparison(module):
    """A TEXT column compared to NOW() raises.

    db_utils auto-casts a fixed list on the way to psycopg2; anything OFF
    that list must carry its own ::timestamptz. `checked_at` and
    `fetched_at` are both off it, which is how two of these queries broke
    while their neighbours worked.
    """
    autocast = _db_utils_autocast_columns()
    must_cast = TEXT_TIMESTAMP_COLS - autocast
    assert must_cast, (
        "every TEXT timestamp column is now auto-cast by db_utils — if that is "
        "really so, delete this guard deliberately rather than let it pass vacuously")

    sql = _sql_strings(module)
    assert sql, f"found no SQL in {module} — guard scanned nothing"

    comparisons, offenders = 0, []
    for lineno, stmt in sql:
        for col in TEXT_TIMESTAMP_COLS:
            # Count cast AND uncast comparisons, so "found nothing" cannot be
            # mistaken for "found nothing wrong".
            for m in re.finditer(
                    r"\b" + col + r"\b(::timestamptz)?\s*(>=|<=|<>|!=|>|<)\s*([^\s,)]+)",
                    stmt):
                cast, op, rhs = m.group(1), m.group(2), m.group(3)
                if not re.match(r"NOW\(|CURRENT_|\(%s\)|%s|'-?\d|datetime", rhs, re.IGNORECASE):
                    continue          # not a time comparison
                comparisons += 1
                if not cast and col in must_cast:
                    offenders.append(
                        f"line {lineno}: {col} {op} {rhs} — TEXT column compared "
                        f"without ::timestamptz, and db_utils does not auto-cast it")
    assert comparisons, (
        f"no timestamp comparison found in {module} — guard scanned nothing")
    assert not offenders, (
        f"uncast TEXT timestamp comparison in {module}:\n  " + "\n  ".join(offenders))


def test_parameterised_windows_add_the_signed_interval():
    """`'-168 hours'` carries its own sign, so the SQL must ADD it.

    Writing NOW() - (%s)::interval with a negative bind looks FORWARD in
    time and silently returns rows from the future (i.e. none).
    """
    seen = 0
    for module in MODULES:
        for lineno, stmt in _sql_strings(module):
            for m in re.finditer(r"NOW\(\)\s*([+-])\s*\(%s\)::interval", stmt):
                seen += 1
                assert m.group(1) == "+", (
                    f"{module} line {lineno}: NOW() - (%s)::interval with a "
                    f"signed bind inverts the window")
    assert seen, "no parameterised interval found — guard scanned nothing"
