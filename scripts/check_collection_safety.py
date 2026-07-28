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
    """True for `if __name__ == "__main__":` — pytest never runs that body.

    Either operand order counts. `if "__main__" == __name__:` is the same guard
    and was previously reported as an offence, which is the kind of false
    positive that gets a guard deleted rather than fixed.
    """
    if not isinstance(node, ast.If):
        return False
    t = node.test
    if not isinstance(t, ast.Compare):
        return False
    operands = [t.left] + list(t.comparators)
    return (any(isinstance(o, ast.Name) and o.id == '__name__' for o in operands)
            and any(isinstance(o, ast.Constant) and o.value == '__main__'
                    for o in operands))


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

    # EVERY .py under tests/, not just top-level test_*.py. pytest also imports
    # conftest.py and the package __init__.py during collection, and an exit in
    # conftest is WORSE than one in a test module:
    #
    #   test module   sys.exit() -> INTERNALERROR, exit 3, run dies LOUDLY
    #   conftest.py   sys.exit(0) -> exit 0, NO output at all, zero tests run
    #
    # Measured on main: a guaranteed-failing test gives `1 failed, 2342 passed`
    # (rc=1); append `sys.exit(0)` to tests/conftest.py and the same suite gives
    # rc=0 and zero bytes of output. CI goes GREEN having run nothing. The old
    # glob could not see either file. rglob also picks up subdirectories and the
    # *_test.py naming, which pytest collects and the old glob missed too.
    files = sorted(tests_dir.rglob('*.py'))

    # The floor still counts TEST MODULES specifically -- that is what makes a
    # clean result meaningful. Counting all .py would let the floor be satisfied
    # by helpers while the test glob silently matched nothing.
    modules = [f for f in files
               if f.name.startswith('test_') or f.name.endswith('_test.py')]
    if len(modules) < MIN_TEST_FILES:
        print(f"::error::only {len(modules)} test modules found under "
              f"{tests_dir} — expected at least {MIN_TEST_FILES}. The scan is "
              f"broken, so a clean result here would mean nothing.")
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
        # conftest.py / __init__.py fail DIFFERENTLY and far more quietly than a
        # test module, so do not tell the reader to expect a red exit 3.
        if f.name in ('conftest.py', '__init__.py'):
            effect = ("pytest exits 0 with NO output and runs zero tests — a "
                      "SILENT GREEN, worse than the exit-3 a test module gives")
        else:
            effect = "this aborts pytest COLLECTION (exit 3, zero tests run)"
        print(f"::error file={f}::{why} — {effect}. Move the script body under "
              f"`if __name__ == \"__main__\":` or drop the test_ prefix.")

    print(f"checked {len(files)} importable files under {tests_dir} "
          f"({len(modules)} test modules, incl. conftest/__init__) — "
          f"{len(bad)} would abort collection")
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
