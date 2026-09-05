"""r-restore-helpers (2026-09-05) — a route called a helper that no longer existed.

THE BUG, and it was mine. #3894 replaced routes/enterprise.py's _ensure_table
by splicing out everything between `def _ensure_table():` and the route
decorator that follows it. Two helpers lived in that gap:

    _rate_limited(src_ip)        rate limiting for the public lead form
    _relay_to_webhook(payload)   the sales-webhook relay

Nothing references them at import time, so the module imported clean, the full
suite passed 15101, regression_lint --mode delta reported no new violations,
and CI merged it. The endpoint 500'd the first time a VALID submission got past
validation — measured live at the Railway origin minutes after deploy:

    POST /api/v1/enterprise/contact
    -> 500 {"error":"name '_rate_limited' is not defined","success":false}

That is WORSE than the bug #3894 fixed. Before it, a failed submission returned
503 naming the sales address to write to. After it, an internal error naming
nothing.

★ WHY EVERY EXISTING GATE MISSED IT. A deleted function is not a syntax error,
not an import error, and not a changed line anywhere near the call site — so
AST-parses, module imports and delta-diff linting all pass. The call is only
reached at request time, on a path no test exercises because it writes to a
live table. pyflakes catches it (`undefined name '_rate_limited'`) but is not
installed in CI, and the repo carries 187 pre-existing undefined-name findings,
so turning it on wholesale is a separate project.

THE CHECK: a call to a MODULE-PRIVATE name — `_foo()`, single underscore — must
resolve to something defined in that same module. Private by convention means
locally defined, so there is no import to chase and no false-positive class to
tune. Measured before pinning: 0 violations across every file in routes/, and
2 on the broken enterprise.py, so this enforces cleanly with no baseline.
"""
import ast
import builtins
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
ROUTE_FILES = sorted(REPO.glob("routes/*.py"))


def _defined_names(tree):
    """Every name a module binds: defs, classes, imports, assignments, args,
    comprehension targets, with-targets and except-as."""
    names = set(dir(builtins))
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, ast.Global):
            names.update(n.names)
    return names


def _unresolved_private_calls(path):
    src = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []          # syntax is another guard's job
    defined = _defined_names(tree)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            nm = n.func.id
            # Single leading underscore only. Dunders are builtins/protocol
            # names, and a bare `foo()` may legitimately come from a star
            # import — neither is this bug.
            if nm.startswith("_") and not nm.startswith("__") and nm not in defined:
                out.append((nm, n.lineno))
    return out


def test_the_scan_actually_reads_the_routes_tree():
    """A glob that stopped matching would make every assertion below pass on
    an empty set — the exact green-because-it-found-nothing shape."""
    assert len(ROUTE_FILES) > 100, f"only found {len(ROUTE_FILES)} route modules"
    assert any(p.name == "enterprise.py" for p in ROUTE_FILES)


@pytest.mark.parametrize("path", ROUTE_FILES, ids=lambda p: p.name)
def test_every_private_helper_call_resolves(path):
    """`_foo()` must be defined in the module that calls it."""
    bad = _unresolved_private_calls(path)
    assert not bad, (
        f"{path.name} calls module-private helper(s) that do not exist: "
        + ", ".join(f"{nm}() at line {ln}" for nm, ln in bad)
        + " — this is a NameError at request time, not at import"
    )


def test_the_check_catches_a_deleted_helper():
    """★ NON-VACUITY, executed. The detector must fire on the real shape of
    the bug — a module that calls a private helper it does not define. Without
    this, a detector that silently returned [] would pass every case above."""
    broken = ast.parse(
        "from flask import jsonify\n"
        "def handler():\n"
        "    if _rate_limited('1.2.3.4'):\n"
        "        return jsonify(ok=False)\n"
        "    return _relay_to_webhook({})\n"
    )
    defined = _defined_names(broken)
    found = [n.func.id for n in ast.walk(broken)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id.startswith("_") and n.func.id not in defined]
    assert sorted(found) == ["_rate_limited", "_relay_to_webhook"]


def test_a_defined_helper_is_not_flagged():
    """The other direction — a detector that flags everything is no better."""
    ok = ast.parse(
        "def _rate_limited(ip):\n    return False\n"
        "def handler():\n    return _rate_limited('1.2.3.4')\n"
    )
    defined = _defined_names(ok)
    found = [n.func.id for n in ast.walk(ok)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id.startswith("_") and n.func.id not in defined]
    assert found == []
