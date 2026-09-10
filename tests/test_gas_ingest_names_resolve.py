r"""Every module-level name the gas ingest uses must actually be defined.

THE DEFECT (2026-09-07, shipped and live): rewriting `_log_sync` with the regex

    def _log_sync\(...\).*?(?=\n\ndef |\n\n@)

spanned to the NEXT top-level def and swallowed everything between — including

    _SVC = ("https://services2.arcgis.com/.../FeatureServer/0/query")

The module still imported and every test still passed, because `_SVC` is only
read INSIDE a function. It failed at request time instead:

    {"detail": "source fetch failed: name '_SVC' is not defined", "errors": 1}

so every gas ingest run died until it was restored. A splice bounded by "the
next thing that looks like an end" eats whatever sits in the middle.

★ WHY AN AST CHECK AND NOT `assert _SVC`. Pinning the three names I happen to
  know about would not catch the next constant a careless edit removes. This
  resolves EVERY bare uppercase global the module reads against what it
  defines, so any future deletion of a used constant fails here rather than in
  production. Python will not do this for you: an undefined global inside a
  function body is a runtime NameError, invisible to import and to any test
  that does not execute that exact line.
"""
import ast
import os

_MOD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "routes", "gas_pipeline_ingest.py")


def _tree():
    with open(_MOD, encoding="utf-8") as fh:
        return fh.read(), ast.parse(fh.read() if False else open(_MOD, encoding="utf-8").read())


def test_every_uppercase_global_it_reads_is_defined():
    src, tree = _tree()

    defined = {t.id for n in tree.body if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
    defined |= {n.target.id for n in tree.body
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    # names bound by imports are defined too
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            defined |= {(a.asname or a.name).split(".")[0] for a in n.names}

    # locals of each function are not globals — collect them to exclude
    local = set()
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                local.add(n.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                local |= {(a.asname or a.name).split(".")[0] for a in n.names}
        local |= {a.arg for a in fn.args.args}

    # ★ `n.id.isupper() is False` sat here and excluded every UPPERCASE name —
    #   i.e. exactly the ones this test exists to check. Deleting _SVC did not
    #   trip it; only the two specific tests below caught the mutation. A
    #   filter that removes the whole population is a vacuous guard.
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            and n.id.startswith("_") and n.id.upper() == n.id
            and any(c.isalpha() for c in n.id)}

    missing = sorted(u for u in used if u not in defined and u not in local)
    assert not missing, (
        f"{missing} are read but never defined at module level — a NameError "
        f"waiting for the first request. This is how _SVC was lost.")


def test_the_arcgis_service_url_is_present_and_whole():
    """The specific constant that was eaten. Its VALUE matters: a truncated
    URL would import fine and 404 at request time."""
    src, _ = _tree()
    assert "_SVC = (" in src, "_SVC is gone again"
    assert "services2.arcgis.com" in src and "FeatureServer/0/query" in src, (
        "the service URL is present but not whole")


def test_the_module_still_imports():
    import importlib
    m = importlib.import_module("routes.gas_pipeline_ingest")
    for name in ("_SVC", "_SRC", "_SYNC_SOURCE"):
        assert hasattr(m, name), f"{name} missing from the imported module"


# ── the second self-inflicted break, 2026-09-07 ──────────────────────────────

def test_no_exception_variable_is_read_outside_its_handler():
    """★ Python 3 UNBINDS `e` at the end of an `except ... as e` block.

    Inserting a _log_sync call above the fetch handler's `return` left the
    return at try/except level instead of inside the handler. Two faults from
    one dedent:
      · `e` is unbound there -> "cannot access local variable 'e'";
      · the return ran on the SUCCESS path too, so every call answered 502.

    Indentation is control flow. A patch that adds a line above a return must
    keep the return in its block, and `ast.parse` will not tell you it did not
    — the file stays perfectly valid Python.
    """
    _, tree = _tree()

    class V(ast.NodeVisitor):
        def __init__(self):
            self.bound, self.bad = [], []

        def visit_ExceptHandler(self, n):
            self.bound.append(n.name)
            self.generic_visit(n)
            self.bound.pop()

        def visit_Name(self, n):
            if (isinstance(n.ctx, ast.Load) and n.id in ("e", "exc", "err")
                    and n.id not in self.bound):
                self.bad.append((n.lineno, n.id))
            self.generic_visit(n)

    v = V()
    v.visit(tree)
    assert not v.bad, (
        f"exception variable read outside its handler at {v.bad} — unbound at "
        f"runtime, and a sign a return escaped its except block")


def test_the_fetch_handler_returns_inside_itself():
    """The success path must not fall into the failure return."""
    _, tree = _tree()
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "ingest_gas_pipelines")
    handlers = [h for t in ast.walk(fn) if isinstance(t, ast.Try) for h in t.handlers
                if h.name == "e"]
    assert handlers, "the fetch try/except is gone"
    for h in handlers:
        assert any(isinstance(x, ast.Return) for x in ast.walk(h)), (
            "an `except ... as e` handler with no return of its own means the "
            "failure return sits outside and fires on success too")
