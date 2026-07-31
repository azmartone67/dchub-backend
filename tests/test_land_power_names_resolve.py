"""Guard: every module-level name land_power_crawler CALLS actually exists.

THE BUG THIS EXISTS FOR (shipped in #2005, caught in production the same run)
────────────────────────────────────────────────────────────────────────────
#2005 added a helper, `_ingest_conn(get_db)`, and pointed the six long-running
crawlers at it. A follow-up pass then narrowed the change back to those six by
rewriting lines that contained the substring `_ingest_conn(get_db)` — which the
DEFINITION LINE also contains:

    def _ingest_conn(get_db):      ->   def get_db():

So the helper stopped existing, every crawler called a name that was gone, and
production reported:

    fetched=28103  upserted=0  errors=1  "Fatal: name '_ingest_conn' is not defined"
    fetched=89744  upserted=0  errors=1  "Fatal: name '_ingest_conn' is not defined"

Those fetch counts are the good news buried inside the failure: the EIA route
(#1990) really does pull all 28,103 generator rows, and the self-healing HIFLD
resolver (#1996) really does pull all 89,744 transmission features. Only the
write was broken, by a rename.

★ THE SCRIPT SAID SO AND I READ PAST IT. Its own summary printed
  "reverted to pool: ['_ingest_conn', ...]" — the helper's name in a list of
  things it had rewritten, which is only possible if it rewrote the definition.
  A mechanical edit that reports what it touched is only useful if the report is
  read; this test is the version that cannot be skimmed.

★ WHY A SUBSTRING EDIT WAS THE WRONG TOOL. `def f(x):` contains `f(x)`. Any
  line-level search-and-replace over `name(arg)` will hit the definition as well
  as the call sites. Rewrite CALL NODES via the AST, or match on a leading
  `conn = ` — never the bare `name(arg)` substring.

THE CONTRACT
────────────
  N1. Every plain-function name called at module scope in this file resolves to
      something the module defines, imports, or inherits from builtins. This is
      the general check — it would have failed instantly on the rename, and it
      catches the next one regardless of which name it is.
  N2. `_ingest_conn` specifically exists and takes get_db (regression pin).
  N3. The module does not define a top-level `get_db` — that name arrives as a
      parameter, and a module-level one shadows it confusingly.
  N4. Each long-running crawler routes its connection through `_ingest_conn`,
      and the request handlers do NOT (they must keep the pool).

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (origin/main @ 5b28e1a6, carrying the rename):
    3 failed, 1 passed, 1 xfailed
    N4 passes unpatched: the crawlers DO all call `_ingest_conn(get_db)` — that
    was never the problem. Only the definition was renamed away. That asymmetry
    is the shape of this bug: every call site correct, the callee gone.
PATCHED (this branch):
    0 failed, 4 passed, 1 xfailed

`1 xfailed` in both runs — strict-xfail must-fail control.

No network, no DB, no main.py import; nothing runs at module scope.

Run:  python3 -m pytest tests/test_land_power_names_resolve.py -v
"""
import ast
import builtins
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "land_power_crawler.py")

# The six long-running functions that must own a direct connection, and the
# handlers that must keep the pool.
INGEST_FUNCS = ("crawl_power_plants", "crawl_substations",
                "crawl_transmission_lines", "crawl_gas_pipelines")
POOLED_FUNCS = ("land_power_status", "market_profiles", "market_profile_detail")


def _tree():
    src = open(MOD).read()
    t = ast.parse(src)
    assert isinstance(t, ast.Module), "parse did not produce a Module"
    assert t.body, "parsed module body is EMPTY — extraction read nothing"
    return t, src


def _module_names(t):
    names = set(dir(builtins))
    for n in ast.walk(t):
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
        elif isinstance(n, ast.alias):
            names.add((n.asname or n.name).split(".")[0])
    return names


# ── N1 ────────────────────────────────────────────────────────────────────────
def test_every_called_name_resolves():
    """The general check — not tied to any one helper.

    A call to a name nothing defines is a NameError that only fires on the code
    path that reaches it, which for an ingest means "in production, once a day".
    """
    t, _ = _tree()
    known = _module_names(t)
    missing = {}
    for n in ast.walk(t):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if n.func.id not in known:
                missing.setdefault(n.func.id, []).append(n.lineno)
    assert not missing, (
        "these names are CALLED but never defined, imported, or bound — each is "
        "a NameError waiting for the code path that reaches it: "
        + "; ".join(f"{k} at lines {v}" for k, v in sorted(missing.items())))


# ── N2 ────────────────────────────────────────────────────────────────────────
def test_ingest_conn_exists_and_takes_get_db():
    t, _ = _tree()
    fn = next((n for n in t.body if isinstance(n, ast.FunctionDef)
               and n.name == "_ingest_conn"), None)
    assert fn is not None, (
        "_ingest_conn is gone. #2005's narrowing pass rewrote its DEFINITION "
        "line — `def _ingest_conn(get_db):` contains the substring "
        "`_ingest_conn(get_db)` — into `def get_db():`")
    assert fn.body, "_ingest_conn parsed with an EMPTY body"
    args = [a.arg for a in fn.args.args]
    assert args == ["get_db"], f"_ingest_conn signature changed: {args}"


# ── N3 ────────────────────────────────────────────────────────────────────────
def test_no_module_level_get_db():
    t, _ = _tree()
    defs = [n.name for n in t.body if isinstance(n, ast.FunctionDef)]
    assert "get_db" not in defs, (
        "a module-level `get_db` exists here. get_db arrives as a PARAMETER "
        "from main.py; a top-level definition shadows it and is exactly the "
        "artefact the #2005 rename left behind")


# ── N4 ────────────────────────────────────────────────────────────────────────
def test_crawlers_own_their_connection_and_handlers_keep_the_pool():
    t, src = _tree()
    lines = src.split("\n")

    def body_of(name):
        for n in ast.walk(t):
            if isinstance(n, ast.FunctionDef) and n.name == name:
                return "\n".join(lines[n.lineno - 1:n.end_lineno])
        return None

    for name in INGEST_FUNCS:
        b = body_of(name)
        assert b is not None, f"{name} not found"
        assert "_ingest_conn(get_db)" in b, (
            f"{name} takes a pooled connection. It holds it across a paged "
            f"fetch plus a bulk write (130-160s measured) and the pool's 60s "
            f"forced reclaim closes it mid-run")
    for name in POOLED_FUNCS:
        b = body_of(name)
        if b is None:
            continue
        assert "_ingest_conn(" not in b, (
            f"{name} is a short request handler and must keep the pool — a "
            f"direct connection here leaks one per request")


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
