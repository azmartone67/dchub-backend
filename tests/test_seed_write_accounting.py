"""The seed must count ROWS WRITTEN, and must not hold a pooled connection
across its network phase.

★ THE BUG THIS PINS. On 2026-09-07 a force=1 reseed returned
`written=132 skipped=0 failed=0 ok=True` and the workflow went green, while 17
of the 132 cells were never written. Two independent defects combined:

  1 seed_variants took ONE pooled connection and held it through ~88s of
    Claude API calls. main.py's watchdog force-reclaims any connection held
    past _CONN_MAX_HOLD_SECONDS (60, swept every 30s), so the connection was
    closed mid-run and every subsequent write raised
    "connection already closed".
  2 _upsert swallowed that exception and returned None, while the caller
    appended to `written` unconditionally — so `written` counted rewrites
    produced, not rows stored.

Read from the shipped source with `ast`; routes/* import flask and a DB pool.
"""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "routes" / "ai_platform_tool_tuner.py"


def _tree():
    return ast.parse(SRC.read_text(encoding="utf-8"))


def _func(name):
    for node in ast.walk(_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_upsert_reports_failure_instead_of_swallowing_it():
    fn = _func("_upsert")
    returns = [r for r in ast.walk(fn) if isinstance(r, ast.Return)]
    assert returns, "_upsert has no return statements"
    bare = [r.lineno for r in returns if r.value is None]
    assert not bare, (
        f"_upsert returns None at lines {bare}; the caller treats a falsy "
        "result as a failed write, so a bare return misreports a real write")
    assert isinstance(fn.body[-1], ast.Return), (
        "_upsert can fall off its end and return None after a SUCCESSFUL "
        "commit — that reports a stored row as a failure")


def test_written_is_appended_only_under_an_upsert_check():
    """Every `written.append` must be guarded by a call to _upsert. Binding the
    AST rather than grepping: a nearby `if` in the source proves nothing."""
    fn = _func("seed_variants")
    appends = [n for n in ast.walk(fn)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute)
               and n.func.attr == "append"
               and isinstance(n.func.value, ast.Name)
               and n.func.value.id == "written"]
    assert appends, "no written.append found — did seed_variants change shape?"

    guarded = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        calls = [c for c in ast.walk(node.test)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "_upsert"]
        if not calls:
            continue
        for sub in ast.walk(node):
            if sub in appends:
                guarded.append(sub)
    assert len(guarded) == len(appends), (
        f"{len(appends) - len(guarded)} of {len(appends)} `written.append` "
        "calls are not inside an `if _upsert(...)` — those count a rewrite "
        "that may never have committed")


def test_connection_is_released_before_the_network_phase():
    """_put_db must be called before the ThreadPoolExecutor block, and a fresh
    _get_db taken after it, so no pooled connection spans the Claude calls."""
    fn = _func("seed_variants")
    pool_line = None
    for node in ast.walk(fn):
        if isinstance(node, ast.With):
            for item in node.items:
                call = item.context_expr
                if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "ThreadPoolExecutor":
                    pool_line = node.lineno
    assert pool_line, "ThreadPoolExecutor block not found in seed_variants"

    puts = [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_put_db"]
    gets = [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_get_db"]
    assert any(l < pool_line for l in puts), (
        "no _put_db before the ThreadPoolExecutor — the pooled connection is "
        "held across the network phase and will be force-reclaimed mid-run")
    assert any(l > pool_line for l in gets), (
        "no _get_db after the ThreadPoolExecutor — the write phase has no "
        "connection of its own")


def test_no_upsert_inside_the_network_phase():
    """The writes must happen after the pool block, not inside it."""
    fn = _func("seed_variants")
    for node in ast.walk(fn):
        if isinstance(node, ast.With) and any(
                isinstance(i.context_expr, ast.Call)
                and getattr(i.context_expr.func, "id", "") == "ThreadPoolExecutor"
                for i in node.items):
            inner = [c for c in ast.walk(node)
                     if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "_upsert"]
            assert not inner, (
                "_upsert is called inside the ThreadPoolExecutor block, which "
                "is exactly the window where the connection gets reclaimed")


def test_cleanup_tolerates_a_released_connection():
    """`c` is deliberately None during the network phase, so the finally block
    must not call _put_db(None)."""
    fn = _func("seed_variants")
    for node in ast.walk(fn):
        if isinstance(node, ast.Try) and node.finalbody:
            src = ast.unparse(ast.Module(body=node.finalbody, type_ignores=[]))
            if "_put_db" in src:
                assert "is not None" in src or "if c" in src, (
                    "the finally block calls _put_db unconditionally; `c` is "
                    f"None during the network phase. Got: {src!r}")
