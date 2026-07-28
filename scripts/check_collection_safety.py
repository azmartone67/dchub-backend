#!/usr/bin/env python3
"""Fail if any tests/test_*.py would abort pytest COLLECTION.

★ WHY THIS EXISTS ALONGSIDE tests/test_tests_are_collectable.py.
That file checks the same property and has better failure messages — but it is
a pytest test, so it can only run if collection SUCCEEDS. Against the exact bug
it targets it is inert. Measured on main at 3143ca96, with one script-shaped
file added to tests/:

    $ python3 -m pytest tests/ -q
    INTERNALERROR> ... SystemExit: 0
    no tests ran in 0.03s        ← exit 3; the guard never executed

The two are complementary, and both are worth keeping:
  * the pytest guard catches LATENT exits — `if not DB: sys.exit()` does not
    fire when DB is set, so collection survives and the test reports the
    landmine before it goes off. That is precisely how test_targeted_evidence.py
    sat undetected.
  * this script catches the UNCONDITIONAL ones, and is the only one of the two
    that still runs once the suite is already dead.

So this runs in the `syntax-check` job, which does not depend on pytest.

THE BUG. A file named test_*.py in tests/ is IMPORTED by pytest during
collection, so its module body executes. A module-scope sys.exit() raises
SystemExit out of the collector and pytest abandons the whole session:

    INTERNALERROR> ... sys.exit(1 if fail else 0)
    INTERNALERROR> SystemExit: 0
    Process completed with exit code 3

Exit 3 means ZERO tests ran — not "some tests failed". It has shipped three
times (#1797, #1803, and the near-miss this script was written for), and every
time it was invisible: CI just said `unit-tests: FAILURE`, identical to main,
so it read as the known baseline while the backend ran with no gate at all.

THE FIX for a script you want to keep runnable: put the driver under
`if __name__ == "__main__":`, or drop the `test_` prefix. Several files in
tests/ already do the former correctly.

Usage:  python3 scripts/check_collection_safety.py [tests_dir]
Exit:   0 clean · 1 offender found · 2 the scan itself is broken
"""
import ast
import pathlib
import sys

# If the glob stops matching, every loop below iterates zero times and this
# exits 0 while checking NOTHING. A vacuous pass is the same class of bug the
# script exists to catch, so the count is asserted before any scanning.
MIN_TEST_FILES = 50


def _is_main_guard(node):
    """True for `if __name__ == "__main__":` — pytest never runs that body."""
    if not isinstance(node, ast.If):
        return False
    t = node.test
    return (isinstance(t, ast.Compare)
            and isinstance(t.left, ast.Name) and t.left.id == '__name__'
            and any(isinstance(c, ast.Constant) and c.value == '__main__'
                    for c in t.comparators))


def _exit_label(node):
    """Name the process-exit this node performs, or None."""
    if isinstance(node, ast.Raise):
        exc = node.exc
        name = getattr(exc, 'id', None) or getattr(getattr(exc, 'func', None), 'id', None)
        return 'raise SystemExit' if name == 'SystemExit' else None
    if isinstance(node, ast.Call):
        fn = node.func
        if isinstance(fn, ast.Attribute):
            mod = getattr(fn.value, 'id', '')
            if (mod, fn.attr) in {('sys', 'exit'), ('os', '_exit')}:
                return f'{mod}.{fn.attr}()'
        elif isinstance(fn, ast.Name) and fn.id in {'exit', 'quit'}:
            return f'{fn.id}()'
    return None


def offending_lines(tree):
    """(lineno, label) for every process-exit pytest WILL run on import.

    Descends through if/try/with/for/while — an exit inside `try:` or
    `if not DB:` still fires at import. Does NOT descend into function or class
    bodies: deferring execution is exactly the sanctioned fix.
    """
    found = []

    def visit(nodes):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.If):
                if _is_main_guard(node):
                    continue
                visit(node.body)
                visit(node.orelse)
                continue
            if isinstance(node, (ast.Try, ast.With, ast.AsyncWith,
                                 ast.For, ast.AsyncFor, ast.While)):
                for attr in ('body', 'orelse', 'finalbody'):
                    visit(getattr(node, attr, None) or [])
                for handler in getattr(node, 'handlers', None) or []:
                    visit(handler.body)
                continue
            label = _exit_label(node)
            if label:
                found.append((node.lineno, label))
            for sub in ast.iter_child_nodes(node):
                visit([sub])

    visit(tree.body)
    return sorted(set(found))


def main(argv):
    tests_dir = pathlib.Path(argv[1] if len(argv) > 1 else 'tests')
    files = sorted(tests_dir.glob('test_*.py'))
    if len(files) < MIN_TEST_FILES:
        print(f"::error::only {len(files)} test_*.py found under {tests_dir} — "
              f"expected at least {MIN_TEST_FILES}. The scan is broken, so a "
              f"clean result here would mean nothing.")
        return 2

    bad = []
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding='utf-8', errors='replace'), filename=str(f))
        except SyntaxError as e:
            bad.append((f, f'does not parse ({e.msg} at line {e.lineno}) — '
                           f'pytest cannot collect it either'))
            continue
        for lineno, label in offending_lines(tree):
            bad.append((f, f'{label} at module scope, line {lineno}'))

    for f, why in bad:
        print(f"::error file={f}::{why} — this aborts pytest COLLECTION "
              f"(exit 3, zero tests run). Move the script body under "
              f"`if __name__ == \"__main__\":` or drop the test_ prefix.")

    print(f"checked {len(files)} test modules — {len(bad)} would abort collection")
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
