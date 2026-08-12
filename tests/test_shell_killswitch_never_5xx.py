"""tests/test_shell_killswitch_never_5xx.py — a kill switch must not fail the
site over (2026-08-12).

The CF worker's proxyWithRetry reads ANY 5xx from Railway as a dead origin and
fails over to the stale Render backend; two within 10s break the site for 30s.
So a master shell whose kill switch returns 503 turns "an operator disabled one
read-only diagnostic" into "the whole site is serving stale data".

graph_spine_master_shell documented this and returned 404. TWENTY-TWO other
shells returned 503 — the hazard was systemic, not a single stale shell, and it
was only found by sweeping for it rather than trusting the one note.

★This guard is repo-wide and AST-bounded ON PURPOSE. A regex for `503` would
match unrelated status codes elsewhere in a 1,700-line shell; walking the
`if _disabled():` blocks asks the only question that matters — what does this
module return when someone turns it off?

Run:  python3 -m pytest tests/test_shell_killswitch_never_5xx.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SHELLS = sorted((_ROOT / "routes").glob("*_master_shell.py"))


def _kill_status_codes(src: str) -> list:
    """Every integer literal returned from inside an `if _disabled():` block."""
    out = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not (isinstance(node, ast.If)
                and isinstance(node.test, ast.Call)
                and getattr(node.test.func, "id", None) == "_disabled"):
            continue
        for n in ast.walk(node):
            if isinstance(n, ast.Constant) and isinstance(n.value, int):
                out.append(n.value)
    return out


def test_there_are_shells_to_check():
    """★A sweep that silently matches nothing is the vacuous guard this
    codebase keeps rediscovering. Assert the population is non-empty."""
    assert len(_SHELLS) > 50, \
        "expected the full master-shell population, found %d" % len(_SHELLS)


@pytest.mark.parametrize("path", _SHELLS, ids=lambda p: p.name)
def test_kill_switch_never_returns_5xx(path):
    codes = _kill_status_codes(path.read_text(encoding="utf-8", errors="ignore"))
    bad = [c for c in codes if c >= 500]
    assert not bad, (
        "%s returns %s when disabled. The CF worker reads any 5xx from Railway "
        "as a dead origin and fails the site over to the stale Render backend — "
        "return 404 instead." % (path.name, bad))


def _route_kill_guards(src: str) -> list:
    """`if _disabled():` blocks inside FLASK ROUTE handlers only.

    ★Scope matters, and the first cut got it wrong. persistence_master_shell has
    a kill guard inside a tick HELPER that returns
    `{"shell": ..., "status": "DISABLED"}` — a payload, not an HTTP response.
    That is correct, and arguably better than a 404: the caller is told the
    shell is off rather than being handed a not-found. Demanding a status code
    there was my guard flagging a good pattern, the same false-positive shape
    lane 1 of shell #63 shipped with. A route handler is identified by a
    `.route(...)` decorator, which is the only place a status code applies."""
    out = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_route = any(
            isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "route"
            for d in fn.decorator_list)
        if not is_route:
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.If) and isinstance(node.test, ast.Call)
                    and getattr(node.test.func, "id", None) == "_disabled"):
                out.append(node)
    return out


@pytest.mark.parametrize("path", _SHELLS, ids=lambda p: p.name)
def test_a_route_kill_switch_states_a_status_code(path):
    """A route guard that falls through to Flask's default 200 would serve a
    live-looking board from a disabled shell — silence reading as health, the
    failure this codebase keeps rediscovering."""
    src = path.read_text(encoding="utf-8", errors="ignore")
    guards = _route_kill_guards(src)
    if not guards:
        pytest.skip("no route-level kill switch in this shell")
    for g in guards:
        codes = [n.value for n in ast.walk(g)
                 if isinstance(n, ast.Constant) and isinstance(n.value, int)]
        assert codes, (
            "%s has a ROUTE kill switch at line %d that returns no explicit "
            "status code — a disabled shell would answer 200"
            % (path.name, g.lineno))
