#!/usr/bin/env python3
"""Fail CI if anything under tests/ can abort pytest COLLECTION.

THE BUG THIS PREVENTS
---------------------
A file in tests/ that calls sys.exit() at module scope does not fail a test --
it kills the ENTIRE RUN. pytest imports the module during collection and
re-raises SystemExit instead of recording it as a per-file error:

    _pytest/runner.py:  collect, "collect", reraise=(KeyboardInterrupt, SystemExit)

The session tears down and pytest exits 3:

    INTERNALERROR> File "tests/test_targeted_evidence.py", line 61, in <module>
    INTERNALERROR>   sys.exit(1 if fail else 0)
    INTERNALERROR> SystemExit: 0
    Process completed with exit code 3

Exit 3 means ZERO tests ran -- not "some tests failed". It is easy to miss,
because a red unit-tests job on a PR looks exactly like a red one on main, so it
reads as "the baseline" and nobody opens it.

This happened TWICE on 2026-07-28, four hours apart, from the same copy-pasted
script shape:  #1797 (tests/test_climate_intel_cache.py) and #1803
(tests/test_targeted_evidence.py).

WHY THIS LIVES IN syntax-check AND NOT IN THE TEST SUITE
--------------------------------------------------------
A pytest test cannot guard this class. Collection runs over every file before
any test executes, so an abort in ANY file pre-empts a guard written as a test
-- it would be killed by the very bug it exists to catch. The guard has to run
in a job that never imports the test modules.

WHAT IS ALLOWED
---------------
Exiting inside `if __name__ == "__main__":` is fine: pytest never runs that block
on import, and it is the correct way to keep a file runnable as a script.
Exits inside a function or class body are fine for the same reason -- importing
defines them, it does not call them.

Run locally:  python3 scripts/check_test_collection_safety.py
"""
import ast
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"

# Calls that raise SystemExit. `exit`/`quit` are the site builtins -- the same
# trap, and what a copy-pasted script tends to reach for.
BANNED_CALLS = {
    "sys.exit": "sys.exit()",
    "os._exit": "os._exit()",
    "exit": "exit()",
    "quit": "quit()",
}

# Bodies that importing the module does NOT execute.
NEW_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _dotted(node):
    """Best-effort dotted name for a call target: sys.exit, os._exit, exit."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return "{0}.{1}".format(base, node.attr) if base else node.attr
    return ""


def _is_main_guard(node):
    """True for `if __name__ == "__main__":` -- the legitimate script hatch."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    # Accept either order: __name__ == "__main__" or "__main__" == __name__
    for a, b in ((test.left, test.comparators[0]),
                 (test.comparators[0], test.left)):
        if (isinstance(a, ast.Name) and a.id == "__name__"
                and isinstance(b, ast.Constant) and b.value == "__main__"):
            return True
    return False


def _offence(node):
    """(what,) if this expression node raises SystemExit, else None."""
    if isinstance(node, ast.Call):
        name = _dotted(node.func)
        if name in BANNED_CALLS:
            return BANNED_CALLS[name]
    elif isinstance(node, ast.Raise):
        exc = node.exc
        target = exc.func if isinstance(exc, ast.Call) else exc
        if _dotted(target) in ("SystemExit", "builtins.SystemExit"):
            return "raise SystemExit"
    return None


def _scan_stmts(stmts, found):
    """Recurse over statements that RUN at import time.

    Descends through module-level control flow (if / try / for / while / with,
    including except handlers) because an exit nested in one of those still
    fires on import. Stops at def/class bodies and at the __main__ guard.
    """
    for node in stmts:
        if isinstance(node, NEW_SCOPE) or _is_main_guard(node):
            continue

        what = _offence(node)
        if what:
            found.append((node.lineno, what))

        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                _scan_stmts([child], found)
            elif isinstance(child, ast.excepthandler):
                _scan_stmts(child.body, found)
            else:
                # An expression: search it, but never cross into a lambda or a
                # comprehension-nested def/class.
                for sub in ast.walk(child):
                    if isinstance(sub, NEW_SCOPE):
                        continue
                    w = _offence(sub)
                    if w:
                        found.append((sub.lineno, w))
    return found


def check_file(path):
    """Return [(lineno, what, source_line)] for import-time exits in `path`."""
    src = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []          # the AST-parse step of syntax-check owns that failure

    lines = src.splitlines()
    out = []
    for lineno, what in sorted(set(_scan_stmts(tree.body, []))):
        text = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
        out.append((lineno, what, text))
    return out


def main():
    if not TESTS_DIR.is_dir():
        print("no tests/ directory at {0} -- nothing to check".format(TESTS_DIR))
        return 0

    files = sorted(TESTS_DIR.rglob("*.py"))
    offenders = []
    for path in files:
        for lineno, what, text in check_file(path):
            offenders.append((path.relative_to(REPO_ROOT), lineno, what, text))

    if not offenders:
        print("collection-safety: {0} files under tests/ clean "
              "(no import-time exits)".format(len(files)))
        return 0

    for rel, lineno, what, text in offenders:
        # GitHub Actions annotation -- renders inline on the PR diff.
        print('::error file={f},line={l}::{w} at module scope aborts pytest '
              'collection (exit 3, ZERO tests run). Move it inside '
              '`if __name__ == "__main__":` or out of tests/.'.format(
                  f=rel, l=lineno, w=what))
        print("  {f}:{l}  {t}".format(f=rel, l=lineno, t=text))

    print("\ncollection-safety FAILED: {n} import-time exit(s) under tests/.\n"
          "\n"
          "pytest imports every test module during collection and re-raises\n"
          "SystemExit, so ONE of these kills the whole run -- not one test, all\n"
          "~2,300 of them, with exit code 3.\n"
          "\n"
          'Fix: put the exit inside `if __name__ == "__main__":`, or move the\n'
          "script out of tests/ (or drop its `test_` prefix).\n"
          "History: #1797 and #1803 were both this exact bug.".format(
              n=len(offenders)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
