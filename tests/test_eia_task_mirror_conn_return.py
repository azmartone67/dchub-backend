"""Guard: the EIA async-task mirror must RETURN its pooled connection.

Why this file exists — measured, not theorised. The 2026-08-18 06:38 run of
eia-pricing-ingest.yml polled `/api/v1/energy/eia-ingest/status` 28 times, got
404 every time, hit its 420s deadline and beat the dead-man board `error`. The
feed had been green the seven preceding days, so the obvious reading was "the
EIA API broke". It had not. From Railway deployment 60bf1ab7:

    🔪 FORCED RECLAIM: Connection 139725973437504 held 87s by thread ...
       Checkout stack:
         File "/app/main.py", line 19944, in _eia_task_persist
           conn = _gdb()

    🔪 FORCED RECLAIM: Connection 140402078827840 held 71s ...
       Checkout stack:
         File "/app/main.py", line 19966, in _eia_task_lookup

Both mirror helpers checked a connection out of the pool with `get_db()` and
never returned it — no `finally`, no `close()`. The pool watchdog reclaimed
each one and CLOSED it under the caller, so `_eia_task_persist`'s commit raised
into a `logger.debug` swallow and the `running` row was never written. With no
row, the cross-replica fallback the whole mechanism exists for had nothing to
read, and every poll 404'd.

Two invariants, because the fix has two halves and either can rot alone:
  1. the helpers return the connection (safe_db, whose contract is close() in
     finally) — otherwise the pool bleeds one connection per poll;
  2. the failure is logged at WARNING — a mirror that fails at debug level is
     precisely why this cost a live-log dig instead of showing up on the board.

AST, not grep: a comment mentioning safe_db must not satisfy this. And the
parse assertions come first — an AST test that finds nothing passes vacuously,
which is the failure mode this repo has paid for before.
"""
import ast
import os

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "main.py")

GUARDED = ("_eia_task_persist", "_eia_task_lookup")


def _funcs():
    with open(MAIN, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename="main.py")
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in GUARDED:
            found[node.name] = node
    return found


def test_both_helpers_are_present_and_parsed():
    """Non-vacuity gate. Every assertion below is scoped to these two
    functions; if a rename made them unfindable the rest of this file would
    pass while guarding nothing."""
    found = _funcs()
    missing = [n for n in GUARDED if n not in found]
    assert not missing, (
        "guarded helper(s) %s not found in main.py — this guard is now "
        "vacuous. If they were renamed, update GUARDED; do not delete the "
        "test." % missing)
    for name, node in found.items():
        # Body is [docstring, Try] by design, so statement count says nothing.
        # Node count does: a stubbed-out helper cannot carry a real query.
        size = sum(1 for _ in ast.walk(node))
        assert size > 40, f"{name} parsed but is a stub ({size} AST nodes)"
        assert any(isinstance(s, ast.Try) for s in ast.walk(node)), (
            f"{name} lost its fail-soft try block")


def _calls(node):
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Name):
                out.append(f.id)
            elif isinstance(f, ast.Attribute):
                out.append(f.attr)
    return out


def _imported_names(node):
    """Names pulled in by `from db_utils import X as Y` inside the function."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.ImportFrom) and sub.module == "db_utils":
            for a in sub.names:
                out.append(a.asname or a.name)
    return out


def test_helpers_acquire_through_safe_db_not_bare_get_db():
    """safe_db() guarantees close() in finally (db_utils). A bare get_db()
    here is the exact shape the pool watchdog reclaimed on 2026-08-18."""
    for name, node in _funcs().items():
        imported = _imported_names(node)
        called = _calls(node)

        assert "safe_db" in [i.lstrip("_") for i in imported] or "safe_db" in imported, (
            f"{name} no longer imports safe_db from db_utils — if it went back "
            f"to get_db(), every poll leaks a pooled connection again")

        # The alias actually has to be CALLED, and inside a `with`.
        alias = next((i for i in imported if "safe_db" in i), None)
        assert alias in called, f"{name} imports {alias} but never calls it"

        withs = [w for w in ast.walk(node) if isinstance(w, ast.With)]
        assert withs, f"{name} calls safe_db outside a `with` — no close() guarantee"

        assert "get_db" not in [i.lstrip("_") for i in imported], (
            f"{name} imports get_db again — bare checkout, no finally")


def test_mirror_failure_is_logged_at_warning_not_debug():
    """The commit failure was invisible for a day because it landed on
    logger.debug. WARNING is what puts it in the deploy log next to the
    FORCED RECLAIM that causes it."""
    for name, node in _funcs().items():
        handlers = [h for h in ast.walk(node) if isinstance(h, ast.ExceptHandler)]
        assert handlers, f"{name} lost its fail-soft except block"

        levels = set()
        for h in handlers:
            for sub in ast.walk(h):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == "logger"):
                    levels.add(sub.func.attr)

        assert levels, f"{name}'s except block logs nothing at all"
        assert "debug" not in levels, (
            f"{name} logs its mirror failure at debug — that is how a broken "
            f"cross-replica fallback stayed invisible while the feed read "
            f"'error' on the dead-man board")
        assert levels & {"warning", "error", "exception"}, (
            f"{name} logs at {sorted(levels)} — needs warning or louder")
