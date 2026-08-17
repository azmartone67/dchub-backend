"""No SQLite time function may reach the Postgres cursor.

This is the class guard for the sweep that produced #2802, #2808, #2814,
#2816 and #2817. Everything here is one bug wearing five disguises, and
the disguises are the point — each one defeated the sweep that found the
previous one.

  1. datetime('now', '-7 days')      the canonical form
  2. datetime("now", "-7 days")      DOUBLE quotes. Not in db_utils'
                                     lookup dict, and in Postgres the
                                     double-quoted strings are IDENTIFIERS.
  3. datetime(\\'now\\', %s)           ESCAPED quotes inside a single-quoted
                                     Python string. `grep "datetime('now'"`
                                     does not match this line at all — it is
                                     how the news feed's own query hid.
  4. datetime('now', %s)             PARAMETERISED. No literal can match, so
                                     db_utils' string table cannot help.
  5. date('now', '-30 days')         date(), not datetime(). db_utils rewrites
                                     only datetime(), so this reaches Postgres
                                     untouched and raises
                                     `function date(unknown, unknown) does not exist`.

Single-argument `date('now')` is NOT an offence: Postgres casts the
literal and returns today. Only the two-argument form is SQLite-only.
Files that genuinely run SQLite (they call sqlite3.connect) are exempt —
there datetime() is correct, and rewriting it would be the bug.

WHAT THIS GUARD PINS
  Every Postgres-backed module in the tree is free of all five spellings,
  enforced against the AST so that a comment explaining the bug cannot be
  mistaken for the bug. New violations fail; the allow-list below is the
  explicit, shrinking record of what has not been converted yet.

Pure source analysis — CI runs with no DATABASE_URL and must not need
one. Every helper asserts it FOUND its target first: an empty scan
satisfies every "not in" below. Nothing runs at module scope.
"""

import ast
import os
import re

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Modules that genuinely run SQLite — datetime() is CORRECT there.
SQLITE_BACKED = {
    "static/auto_pilot.py",
}

# Files whose datetime()/date() strings are data, not SQL: translation
# tables, the migration script, brain detectors that match on the pattern.
NOT_SQL = {
    "db_utils.py",                            # the translation table itself
    "fix_all_sqlite_to_pg.py",                # migration tool, operates on text
    "pre_deploy_check.py",                    # detector description
    "kmz_auto_discovery.py",                  # module docstring
    "routes/brain_mechanical_classifier.py",  # detector patterns
    "routes/brain_autonomy_loop.py",          # detector patterns
    "routes/brain_v2_layer5.py",              # detector patterns
}

# Sites not yet converted. Every entry is a live bug; the list must only
# shrink. Adding to it requires the same evidence a fix does.
KNOWN_UNCONVERTED = {
    # Empty, and it must stay that way. Every site in the tree now names an
    # explicit Postgres interval, so none of them depends on db_utils
    # matching a literal string — the coupling that let five different
    # spellings through. A new entry here is a regression, not a TODO.
}

_DATETIME_NOW = re.compile(r"\bdatetime\s*\(\s*\\?[\"']now", re.IGNORECASE)
# date('now', <something>) — two-arg only; bare date('now') is valid Postgres.
_DATE_NOW_2ARG = re.compile(r"\bdate\s*\(\s*\\?[\"']now\\?[\"']\s*,", re.IGNORECASE)


def _python_files():
    out = []
    for root, dirs, files in os.walk(_REPO):
        dirs[:] = [d for d in dirs
                   if d not in {".git", "__pycache__", "node_modules", "tests", ".claude"}]
        for f in files:
            if f.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, f), _REPO)
                out.append(rel)
    return sorted(out)


def _sql_constants(path):
    """(lineno, text) for string constants that look like SQL, docstrings excluded."""
    full = os.path.join(_REPO, path)
    with open(full, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        # Do NOT swallow this. A file this guard cannot parse is a file it
        # cannot clear, and returning [] would hand it a silent pass — the
        # exact shape of failure this whole sweep is about.
        pytest.fail(f"{path} does not parse, so it cannot be cleared: {e}")

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    inner = {id(p) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
             for p in ast.walk(n) if isinstance(p, ast.Constant)}

    sqlish = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings or id(node) in inner:
                continue
            if sqlish.search(node.value):
                out.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            parts = []
            for p in node.values:
                if isinstance(p, ast.Constant) and isinstance(p.value, str):
                    parts.append(p.value)
                elif isinstance(p, ast.FormattedValue):
                    parts.append(ast.unparse(p.value))
            joined = "".join(parts)
            if sqlish.search(joined):
                out.append((getattr(node, "lineno", 0), joined))
    return out


def _offenders():
    """(path, lineno, spelling, sql) for every SQLite time call in live SQL."""
    found = []
    scanned = 0
    for path in _python_files():
        if path in SQLITE_BACKED or path in NOT_SQL:
            continue
        for lineno, sql in _sql_constants(path):
            scanned += 1
            if _DATETIME_NOW.search(sql):
                found.append((path, lineno, "datetime('now', ...)", sql))
            elif _DATE_NOW_2ARG.search(sql):
                found.append((path, lineno, "date('now', ...)", sql))
    return found, scanned


def test_no_sqlite_time_function_reaches_postgres():
    offenders, scanned = _offenders()
    assert scanned > 200, (
        f"only {scanned} SQL strings scanned — the walker is not finding the "
        f"tree, so this guard proves nothing")

    unexpected = [o for o in offenders if o[0] not in KNOWN_UNCONVERTED]
    assert not unexpected, (
        "SQLite time function in Postgres-bound SQL:\n  "
        + "\n  ".join(f"{p}:{ln} [{kind}] {s.strip()[:100]}"
                      for p, ln, kind, s in unexpected))


def test_allow_list_has_no_stale_entries():
    """A fixed file must leave the allow-list, or the list stops meaning anything."""
    offenders, _ = _offenders()
    offending_paths = {o[0] for o in offenders}
    stale = sorted(set(KNOWN_UNCONVERTED) - offending_paths)
    assert not stale, (
        f"these files are listed as unconverted but are clean — remove them "
        f"from KNOWN_UNCONVERTED: {stale}")


def test_sqlite_backed_files_really_use_sqlite():
    """The exemption must be earned, not asserted."""
    assert SQLITE_BACKED, "no exempt files — guard scanned nothing"
    for path in SQLITE_BACKED:
        full = os.path.join(_REPO, path)
        assert os.path.exists(full), f"exempt file {path} no longer exists"
        with open(full, encoding="utf-8") as fh:
            src = fh.read()
        assert "sqlite3.connect" in src, (
            f"{path} is exempted as SQLite-backed but never calls "
            f"sqlite3.connect — its datetime() calls would hit Postgres")


def test_not_sql_files_have_no_executed_sql_with_datetime():
    """Files exempted as 'pattern, not SQL' must not actually execute any."""
    assert NOT_SQL, "no NOT_SQL entries — guard scanned nothing"
    for path in NOT_SQL:
        full = os.path.join(_REPO, path)
        if not os.path.exists(full):
            pytest.fail(f"NOT_SQL entry {path} no longer exists — remove it")
        for lineno, sql in _sql_constants(path):
            if _DATETIME_NOW.search(sql) or _DATE_NOW_2ARG.search(sql):
                pytest.fail(
                    f"{path}:{lineno} is exempted as non-SQL but contains an "
                    f"executable-looking statement with a SQLite time call: "
                    f"{sql.strip()[:120]}")


def test_bare_date_now_is_not_flagged():
    """Single-arg date('now') is valid Postgres; flagging it would be noise."""
    assert not _DATE_NOW_2ARG.search("SELECT date(created_at) = date('now')")
    assert _DATE_NOW_2ARG.search("SELECT * FROM t WHERE d > date('now', '-30 days')")
    assert _DATE_NOW_2ARG.search("SELECT * FROM t WHERE d > date('now', %s)")


def test_every_disguise_is_actually_matched():
    """The five spellings this class hid behind."""
    cases = [
        ("canonical", "SELECT 1 FROM t WHERE a > datetime('now', '-7 days')", _DATETIME_NOW),
        ("double-quoted", 'SELECT 1 FROM t WHERE a > datetime("now", "-7 days")', _DATETIME_NOW),
        ("escaped", "SELECT 1 FROM t WHERE a > datetime(\\'now\\', %s)", _DATETIME_NOW),
        ("parameterised", "SELECT 1 FROM t WHERE a > datetime('now', %s)", _DATETIME_NOW),
        ("date 2-arg", "SELECT 1 FROM t WHERE a > date('now', '-30 days')", _DATE_NOW_2ARG),
    ]
    for label, sql, pattern in cases:
        assert pattern.search(sql), f"{label} spelling is not detected: {sql}"
