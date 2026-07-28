#!/usr/bin/env python3
"""Fail if any tests/test_*.py would abort pytest COLLECTION.

★ WHY THIS IS NOT A PYTEST TEST.
A module in tests/ under a `test_` prefix is IMPORTED by pytest at collection
time. If its module body calls sys.exit(), the SystemExit propagates out of the
collector and tears down the entire session:

    INTERNALERROR> ... sys.exit(1 if fail else 0)
    INTERNALERROR> SystemExit: 0
    Process completed with exit code 3

Exit 3 means ZERO tests ran — not "some tests failed". A guard written as a test
CANNOT catch this, because the abort kills the guard along with everything else.
So this runs in the syntax-check job, which does not depend on pytest.

It has happened twice in 48 hours (tests/test_climate_intel_cache.py → PR #1797,
tests/test_targeted_evidence.py → this one), and both times it was invisible:
every PR went red identically to main, so the failure read as the known baseline
and the backend ran with no unit-test gate at all.

A file in tests/ may still be runnable as a script — put the script body under
`if __name__ == "__main__":` and pytest will never execute it. Three files here
already do that correctly.

Usage:  python3 scripts/check_collection_safety.py [tests_dir]
"""
import ast
import pathlib
import sys


def _is_main_guard(node):
    """True for `if __name__ == "__main__":` — pytest never runs that body."""
    if not isinstance(node, ast.If):
        return False
    t = node.test
    return (isinstance(t, ast.Compare)
            and isinstance(t.left, ast.Name) and t.left.id == '__name__'
            and any(isinstance(c, ast.Constant) and c.value == '__main__'
                    for c in t.comparators))


def _is_sys_exit(node):
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    # sys.exit(...)
    if isinstance(fn, ast.Attribute) and fn.attr == 'exit' \
            and isinstance(fn.value, ast.Name) and fn.value.id == 'sys':
        return True
    # a bare `exit(...)` / `quit(...)` at module scope does the same thing
    return isinstance(fn, ast.Name) and fn.id in ('exit', 'quit')


def offending_lines(tree):
    """Line numbers of sys.exit() calls pytest WILL execute on import."""
    hits = []
    for stmt in tree.body:
        # function/class bodies are definitions, not executed at import
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if _is_main_guard(stmt):
            continue
        hits.extend(n.lineno for n in ast.walk(stmt) if _is_sys_exit(n))
    return sorted(hits)


def main(argv):
    tests_dir = pathlib.Path(argv[1] if len(argv) > 1 else 'tests')
    files = sorted(tests_dir.glob('test_*.py'))
    if not files:
        print(f"::error::no test_*.py found under {tests_dir} — wrong path?")
        return 2                      # an empty scan must never pass silently

    bad = []
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding='utf-8', errors='replace'))
        except SyntaxError as e:
            bad.append((f, f'syntax error: {e}'))
            continue
        hits = offending_lines(tree)
        if hits:
            bad.append((f, f'module-scope sys.exit() at line(s) '
                           f'{", ".join(str(h) for h in hits)}'))

    for f, why in bad:
        print(f"::error file={f}::{why} — this aborts pytest COLLECTION "
              f"(exit 3, zero tests run). Move the script body under "
              f"`if __name__ == \"__main__\":` or rewrite it as test functions.")

    print(f"checked {len(files)} test modules — "
          f"{len(bad)} would abort collection")
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
