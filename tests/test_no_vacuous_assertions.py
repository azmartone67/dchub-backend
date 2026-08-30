"""A test whose assertion is caught by its own handler cannot fail.

★ THE ONE THIS WAS WRITTEN FOR. tests/test_auth_context.py carried:

    try:
        ctx.tier = "enterprise"
        assert False, "AuthContext should be frozen (immutable)"
    except Exception:
        pass  # expected — dataclass(frozen=True) raises FrozenInstanceError

`assert False` raises AssertionError, and AssertionError IS an Exception, so
the handler caught THE TEST'S OWN ALARM. Proven vacuous by mutation on
2026-08-30: flipping routes/auth_context.py to @dataclass(frozen=False) makes
the assignment succeed, fires the assert, and the handler swallows it — the
test still passed. It could not fail in either direction.

It mattered here more than most: a caller that can set `ctx.tier` can escalate
itself from "free" to "enterprise".

★ SCOPE, deliberately narrow. This fences the shape that is ALWAYS wrong — a
real assertion sitting inside a broad, swallowing handler — not every broad
`except`. A handler that catches a SPECIFIC exception is fine and common
(tests/test_csp_canonical.py does exactly that, correctly, with
`except FileNotFoundError`). The right way to assert a raise is
`pytest.raises`, which cannot swallow AssertionError.

★ MEASURED YIELD: across 620 test files, exactly one match. Cheap to keep.
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.dirname(os.path.abspath(__file__))

_BROAD = {"Exception", "BaseException"}


def _is_broad(handler):
    if handler.type is None:
        return True                                   # bare `except:`
    if isinstance(handler.type, ast.Name):
        return handler.type.id in _BROAD
    if isinstance(handler.type, ast.Tuple):
        return any(isinstance(e, ast.Name) and e.id in _BROAD
                   for e in handler.type.elts)
    return False


def _is_swallowing(handler):
    """Body that lets execution continue as though nothing failed. A handler
    that re-raises, fails, or skips is NOT swallowing."""
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, (ast.Raise, ast.Assert)):
            return False
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in ("fail", "skip", "xfail", "exit"):
                return False
    return True


def test_no_assertion_is_caught_by_its_own_handler():
    offenders = []
    for dirpath, _dirs, files in os.walk(TESTS):
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    tree = ast.parse(fh.read())
            except SyntaxError:
                continue          # syntax-check owns that failure, not this fence
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                asserts = [x for stmt in node.body for x in ast.walk(stmt)
                           if isinstance(x, ast.Assert)]
                if not asserts:
                    continue
                for handler in node.handlers:
                    if _is_broad(handler) and _is_swallowing(handler):
                        offenders.append(
                            f"  {os.path.relpath(path, ROOT)}:{node.lineno} — "
                            f"{len(asserts)} assertion(s) inside "
                            f"`except {ast.unparse(handler.type) if handler.type else ''}`"
                            .rstrip())
    assert not offenders, (
        "an assertion inside a broad swallowing handler cannot fail — "
        "AssertionError is an Exception, so the handler catches the test's own "
        "alarm. Use pytest.raises, or catch the SPECIFIC exception:\n"
        + "\n".join(offenders))
