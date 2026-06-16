"""Repo-wide guard for the 'undefined logger' bug class (generalizes
tests/test_radar_logger_defined.py).

The 2026-06-15 brain_findings 22h freeze was caused by `logger.info(...)` in a
module that never bound `logger` — the NameError fired inside an except handler
(after a savepoint RELEASE) and aborted the transaction, so the trailing
conn.commit() silently discarded every write. This module had had the same class
of bug before (a missing `datetime` import).

This is a STATIC AST check (no import, no DB → CI-safe) that scans every brain
module: if a module USES `logger.<attr>` but never BINDS `logger` anywhere
(module-level assign/import, function arg, or local assign), it fails. That is
precisely the radar-bug shape, with no false positives for modules that legitimately
define or import their logger.
"""
import ast
import glob
import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _brain_modules():
    pats = [
        os.path.join(_ROOT, "routes", "brain_*.py"),
        os.path.join(_ROOT, "routes", "autopilot_outcomes.py"),
        os.path.join(_ROOT, "autonomous_brain.py"),
        os.path.join(_ROOT, "brain_*.py"),
    ]
    seen = set()
    for p in pats:
        for f in glob.glob(p):
            seen.add(os.path.abspath(f))
    return sorted(seen)


def _uses_logger_attr(tree: ast.AST) -> bool:
    # any `logger.<something>` (Attribute whose value is Name 'logger', Load ctx)
    for n in ast.walk(tree):
        if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id == "logger"):
            return True
    return False


def _binds_logger(tree: ast.AST) -> bool:
    for n in ast.walk(tree):
        # x = ...  /  x += ...  /  x: T = ...  with target Name 'logger'
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == "logger":
                    return True
        # import logging as logger  /  from x import logger
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                if (a.asname or a.name).split(".")[0] == "logger":
                    return True
        # def f(..., logger, ...)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = n.args
            for a in (list(args.args) + list(args.posonlyargs)
                      + list(args.kwonlyargs)
                      + ([args.vararg] if args.vararg else [])
                      + ([args.kwarg] if args.kwarg else [])):
                if a and a.arg == "logger":
                    return True
        # with ... as logger:  /  for logger in ...
        elif isinstance(n, ast.withitem) and isinstance(n.optional_vars, ast.Name) \
                and n.optional_vars.id == "logger":
            return True
        elif isinstance(n, ast.For) and isinstance(n.target, ast.Name) \
                and n.target.id == "logger":
            return True
    return False


def test_brain_modules_that_use_logger_define_it():
    offenders = []
    for path in _brain_modules():
        with open(path, "r", encoding="utf-8") as f:
            try:
                tree = ast.parse(f.read())
            except SyntaxError:
                continue  # not our concern here
        if _uses_logger_attr(tree) and not _binds_logger(tree):
            offenders.append(os.path.relpath(path, _ROOT))
    assert not offenders, (
        "These brain modules use `logger.*` but never bind `logger` (the "
        "undefined-name bug class that froze brain_findings for 22h). Add "
        "`import logging; logger = logging.getLogger(__name__)`:\n  - "
        + "\n  - ".join(offenders)
    )
