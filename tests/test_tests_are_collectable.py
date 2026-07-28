"""Guard: no file in tests/ may abort pytest COLLECTION.

THE BUG THIS PREVENTS — it has now shipped THREE times:
  1. tests/test_climate_intel_cache.py   (fixed in #1797)
  2. tests/test_targeted_evidence.py     (fixed alongside this file)
  3. ...and whichever script lands next, unless something checks.

Several suites in this repo are written as standalone scripts — top-level
statements that print a report and end in a bare module-scope
`sys.exit(1 if fail else 0)`. That is a perfectly good script. But a file named
`test_*.py` inside `tests/` is IMPORTED by pytest during collection, so the
module body executes and the `SystemExit` propagates out of the collector:

    mainloop: caught unexpected SystemExit!
    INTERNALERROR> ... SystemExit: 0
    ##[error]Process completed with exit code 3.

pytest then abandons the whole session. Measured on main 2026-07-28: collection
died after 779 of 2,325 tests, so ~1,550 tests never ran — and because CI simply
reported `unit-tests: FAILURE`, identical to the pre-existing baseline, nobody
could tell the difference between "3 known failures" and "the suite never ran".
An entire test suite was dark for days.

WHY A GUARD AND NOT A THIRD MANUAL FIX: the same defect recurring three times is
a pattern, and the failure mode is silent — the damage is invisible precisely
because it looks like an ordinary red build. This test makes the next occurrence
name its own file on the first run.

THE RULE: keep the script if you like, but make importing it side-effect-free —
put the driver under `if __name__ == "__main__":`, or drop the `test_` prefix so
pytest ignores the file.
"""
from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# Sanity floor. If the glob silently stops matching (directory moved, rename,
# someone runs this from the wrong cwd), every loop below iterates zero times and
# the guard passes while checking NOTHING. That vacuous-pass is the same class of
# bug this file exists to catch, so the count is asserted before any scanning.
_MIN_TEST_FILES = 50


def _test_files() -> list[Path]:
    return sorted(TESTS_DIR.glob("test_*.py"))


def _is_dunder_main_guard(node: ast.If) -> bool:
    """True for `if __name__ == "__main__":` — the sanctioned script escape."""
    return any(
        isinstance(n, ast.Name) and n.id == "__name__"
        for n in ast.walk(node.test)
    )


def _exit_calls_at_module_scope(tree: ast.Module):
    """Yield (lineno, label) for every module-scope process-exit that pytest
    would execute at import time.

    Descends through if/try/with/for/while bodies — an exit nested in a `try` or
    an `if not DB:` still fires during collection (that is exactly how
    test_targeted_evidence.py died). Does NOT descend into function or class
    bodies: those only run when something calls them, which is the whole point of
    moving a script driver into a function.
    """
    found = []

    def visit(nodes):
        for node in nodes:
            # Deliberately not recursed into — deferred execution is the fix.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.If):
                if _is_dunder_main_guard(node):
                    continue          # sanctioned: only runs via `python tests/x.py`
                visit(node.body)
                visit(node.orelse)
                continue
            if isinstance(node, (ast.Try, ast.With, ast.AsyncWith,
                                 ast.For, ast.AsyncFor, ast.While)):
                for attr in ("body", "orelse", "finalbody"):
                    visit(getattr(node, attr, []) or [])
                for handler in getattr(node, "handlers", []) or []:
                    visit(handler.body)
                continue

            for sub in ast.walk(node):
                # `raise SystemExit` / `raise SystemExit(1)`
                if isinstance(sub, ast.Raise):
                    exc = sub.exc
                    name = getattr(exc, "id", None) or getattr(
                        getattr(exc, "func", None), "id", None)
                    if name == "SystemExit":
                        found.append((sub.lineno, "raise SystemExit"))
                    continue
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    # sys.exit(...) / os._exit(...)
                    if isinstance(fn, ast.Attribute):
                        mod = getattr(fn.value, "id", "")
                        if (mod, fn.attr) in {("sys", "exit"), ("os", "_exit")}:
                            found.append((sub.lineno, f"{mod}.{fn.attr}()"))
                    # bare exit(...) / quit(...)
                    elif isinstance(fn, ast.Name) and fn.id in {"exit", "quit"}:
                        found.append((sub.lineno, f"{fn.id}()"))

    visit(tree.body)
    return found


def test_the_scan_actually_sees_the_test_suite():
    """Anti-vacuous: prove the glob found files before trusting any clean run."""
    files = _test_files()
    assert len(files) >= _MIN_TEST_FILES, (
        f"only {len(files)} test_*.py files found under {TESTS_DIR} — expected at "
        f"least {_MIN_TEST_FILES}. The glob is broken, so every check in this "
        f"file would pass while scanning nothing."
    )


def test_no_test_file_exits_the_process_at_import_time():
    """A module-scope exit kills COLLECTION and takes the whole suite with it."""
    offenders = []
    parse_failures = []

    for path in _test_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:           # a file that cannot parse also breaks collection
            parse_failures.append(f"{path.name}:{exc.lineno} {exc.msg}")
            continue
        for lineno, label in _exit_calls_at_module_scope(tree):
            offenders.append(f"{path.name}:{lineno} — {label} at module scope")

    assert not parse_failures, (
        "test files that do not parse (pytest cannot collect these either):\n  "
        + "\n  ".join(parse_failures)
    )

    assert not offenders, (
        f"{len(offenders)} test file(s) call a process-exit at module scope. pytest "
        f"executes the module body during COLLECTION, so this raises SystemExit out "
        f"of the collector and aborts the ENTIRE run (exit code 3) — every other "
        f"test silently stops running.\n\n  "
        + "\n  ".join(offenders)
        + "\n\nFix: move the script driver under `if __name__ == \"__main__\":`, or "
          "rename the file without the `test_` prefix so pytest ignores it."
    )
