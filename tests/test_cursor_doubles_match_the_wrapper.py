"""A cursor double must not be LESS capable than the cursor it replaces.

The bug class
-------------
`PGCursorWrapper.execute()` returns early for DDL and forwards everything else.
On the forwarding path it reads `self._cur.description` and `self._cur.rowcount`
unconditionally (db_utils.py ~341/344). A test double that omits them is:

  - green on every DDL case, because those return at line 324 and never look; and
  - `AttributeError` on every non-DDL case.

That is exactly how `tests/test_dropped_ddl_is_loud.py` landed red on main: its
double had `execute` and `description` but no `rowcount`, so its three DDL tests
passed and its three forwarding tests raised. The failure looked like a defect in
`db_utils`; it was a defect in the double. A real `psycopg2.extensions.cursor`
carries both attributes always — `rowcount` is -1 when no statement has run.

Ten doubles across eight files were missing one or both when this was written.
None were failing, because none happened to exercise a path that read them — so
the next attribute the wrapper starts reading would have broken all of them at
once, in files whose authors never touched the wrapper. This pins the floor.

Why only these two
------------------
`connection` is read only in `execute()`'s `except` branch; `fetchone`,
`fetchall` and `close` only when a test calls the matching wrapper method. Those
are genuinely optional, and a double that omits them fails LOUDLY at the call.
`description` and `rowcount` are different on two counts: they are read whether
or not the test asks for them, and they are plain data attributes. Requiring a
default for them is free. Requiring `fetchall` would NOT be — a stub returning
`[]` converts a loud `AttributeError` into a silent empty result, which is the
very failure mode this file exists to prevent. Do not "complete" the floor by
adding methods to it.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import db_utils

# Every double found when this floor was introduced. A scan that suddenly finds
# far fewer has broken its own matching, not cleaned up the suite.
MIN_DOUBLES = 10

FORWARDING_FLOOR = ("description", "rowcount")


def _wrapper_reads() -> set[str]:
    """Attributes `PGCursorWrapper` reads off its raw cursor, by AST.

    Not a text scan: `self._cur.rowcount` appears in prose in these docstrings,
    so a regex would match a file that never reads it and report a floor that
    is really just a comment.
    """
    src = pathlib.Path(db_utils.__file__).read_text(encoding="utf-8")
    return {n.attr for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Attribute) and n.value.attr == "_cur"
            and isinstance(n.value.value, ast.Name) and n.value.value.id == "self"}


def _class_attributes(node: ast.ClassDef) -> set[str]:
    """Names the class provides: methods, class-level assignments, and
    `self.X = ...` bound in `__init__`."""
    have: set[str] = set()
    for body in node.body:
        if isinstance(body, (ast.FunctionDef, ast.AsyncFunctionDef)):
            have.add(body.name)
            if body.name == "__init__":
                for n in ast.walk(body):
                    if (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
                            and isinstance(n.value, ast.Name) and n.value.id == "self"):
                        have.add(n.attr)
        elif isinstance(body, ast.Assign):
            for target in body.targets:
                if isinstance(target, ast.Name):
                    have.add(target.id)
        elif isinstance(body, ast.AnnAssign) and isinstance(body.target, ast.Name):
            have.add(body.target.id)
    return have


def _cursor_doubles():
    """Every class in tests/ that stands in for a raw cursor.

    Scoped to files naming PGCursorWrapper so this does not drag in unrelated
    fakes that happen to define an `execute`.
    """
    found = []
    tests_dir = pathlib.Path(__file__).parent
    for path in sorted(tests_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "PGCursorWrapper" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ClassDef):
                have = _class_attributes(node)
                if "execute" in have:
                    found.append((path.name, node.name, have))
    return found


def test_the_scan_finds_the_doubles_it_is_supposed_to_police():
    """A repo-wide scan that matches nothing passes every assertion below."""
    doubles = _cursor_doubles()
    assert len(doubles) >= MIN_DOUBLES, (
        f"found only {len(doubles)} cursor doubles, expected >= {MIN_DOUBLES}. "
        f"Either the matching broke, or the doubles moved out of tests/*.py — "
        f"in both cases the floor below is no longer being enforced on them: "
        f"{[f'{f}::{c}' for f, c, _ in doubles]}")


def test_the_forwarding_floor_is_still_what_the_wrapper_reads():
    """Pins the floor to the CODE, so it cannot quietly become wrong.

    If someone adds another unconditional read to `execute()`'s forwarding path,
    this fails and names it — rather than the ten doubles failing one by one in
    files whose authors never touched db_utils.
    """
    src = pathlib.Path(db_utils.__file__).read_text(encoding="utf-8")
    execute = next(
        fn for node in ast.walk(ast.parse(src))
        if isinstance(node, ast.ClassDef) and node.name == "PGCursorWrapper"
        for fn in node.body
        if isinstance(fn, ast.FunctionDef) and fn.name == "execute")

    # Reads that happen before/outside the `except` handler, excluding the call
    # to .execute() itself, are what every forwarded statement must satisfy.
    handlers = [h for n in ast.walk(execute) if isinstance(n, ast.Try) for h in n.handlers]
    excepted = {id(n) for h in handlers for n in ast.walk(h)}
    unconditional = {
        n.attr for n in ast.walk(execute)
        if isinstance(n, ast.Attribute) and id(n) not in excepted
        and isinstance(n.value, ast.Attribute) and n.value.attr == "_cur"
        and isinstance(n.value.value, ast.Name) and n.value.value.id == "self"
        and n.attr != "execute"}

    assert unconditional == set(FORWARDING_FLOOR), (
        f"PGCursorWrapper.execute() now reads {sorted(unconditional)} off the raw "
        f"cursor on the forwarding path, but FORWARDING_FLOOR is "
        f"{sorted(FORWARDING_FLOOR)}. Add the new attribute to the floor AND to "
        f"the doubles, or every non-DDL test using a double will AttributeError.")


@pytest.mark.parametrize(
    "filename,classname,have",
    [pytest.param(f, c, h, id=f"{f}::{c}") for f, c, h in _cursor_doubles()])
def test_every_cursor_double_carries_the_forwarding_floor(filename, classname, have):
    """The floor itself, per double, so a failure names the exact class."""
    missing = [a for a in FORWARDING_FLOOR if a not in have]
    assert not missing, (
        f"{filename}::{classname} stands in for a raw cursor but does not define "
        f"{missing}. PGCursorWrapper.execute() reads those off it on every "
        f"forwarded (non-DDL) statement, and a real psycopg2 cursor always has "
        f"them. As written this double is green on DDL and AttributeError on "
        f"everything else. Add `description = None` / `rowcount = -1`.")


def test_a_real_cursor_would_pass_this_floor():
    """The floor is only meaningful if it describes the real object.

    Guards against the inverse mistake: a floor invented from the doubles rather
    than from psycopg2, which would let the suite drift away from production.
    """
    psycopg2_ext = pytest.importorskip(
        "psycopg2.extensions", reason="psycopg2 absent; cannot check the real cursor")
    missing = [a for a in FORWARDING_FLOOR if not hasattr(psycopg2_ext.cursor, a)]
    assert not missing, (
        f"the floor requires {missing}, which a real psycopg2 cursor does not "
        f"have — the floor is wrong, not the doubles")
    assert FORWARDING_FLOOR and set(FORWARDING_FLOOR) <= _wrapper_reads()
