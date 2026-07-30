"""Guards for ai_testimonials volunteered-quote capture (2026-07-30).

The live table carried a broad UNIQUE (platform, context) constraint
(ai_testimonials_platform_context_unique — live-only drift: no repo CREATE
TABLE ever declared it). Both volunteered-quote paths write context=<company>,
so the SECOND quote from the same (platform, company) pair hit the constraint:
/api/v1/keys/identify swallowed it (quote_captured=False) and
/api/v1/keys/claim/quote surfaced an opaque storage_failed. Zero
source='claim_quote' rows ever landed.

The fix scopes dedup to the auto-capture sources that were designed around it
(partial unique index over source IN ('mcp-auto', 'auto'); main.py's BARE
target-less ON CONFLICT DO NOTHING arbitrates against partial indexes) and
drops the broad constraint. These tests lock:

  · the migration SQL's shape and ORDER (partial index created BEFORE the
    constraint drop — auto-dedup must never have a gap);
  · that the ensure helper runs each statement exactly once per process;
  · that no writer names (platform, context) as an ON CONFLICT target — with
    the constraint gone that arbiter no longer exists and such an INSERT
    hard-errors ("no unique or exclusion constraint matching");
  · that the new duplicate guards match on QUOTE, never on context — a
    second, different quote from the same company must insert.
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _module_node(src, name, kind):
    for node in ast.parse(src).body:
        if kind == "assign" and isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return node.value
        if kind == "func" and isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found at module scope of flask_mcp_endpoints")


def _schema_sql():
    src = _read("flask_mcp_endpoints.py")
    value = _module_node(src, "_TESTIMONIAL_QUOTE_SCHEMA_SQL", "assign")
    stmts = ast.literal_eval(value)
    assert isinstance(stmts, tuple), "schema SQL must be an immutable tuple"
    return [" ".join(s.split()) for s in stmts]


# ── migration SQL shape + order ─────────────────────────────────────────────

def test_schema_sql_shape_and_order():
    stmts = _schema_sql()
    assert len(stmts) == 2, "exactly two statements: create partial index, drop constraint"

    create, drop = stmts
    # Statement 1: the auto-scoped replacement dedup rail, idempotent.
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ai_testimonials_auto_dedup" in create
    assert "ON ai_testimonials (platform, context)" in create
    assert "WHERE source IN ('mcp-auto', 'auto')" in create, (
        "partial index must cover ONLY the auto-capture sources — scoping it "
        "wider re-caps human-volunteered quotes"
    )
    # Statement 2: the broad constraint goes away, idempotent.
    assert ("ALTER TABLE ai_testimonials DROP CONSTRAINT IF EXISTS "
            "ai_testimonials_platform_context_unique") in drop

    # ORDER: index first, then drop — otherwise there's a window where the
    # mcp-auto writers have no DB-level dedup at all.
    assert stmts.index(create) < stmts.index(drop)


def test_ensure_helper_runs_each_statement_once():
    """Execute the real _ensure_testimonial_quote_schema against a stub pool:
    both statements run in tuple order on the first call, none on the second.
    """
    src = _read("flask_mcp_endpoints.py")
    fn = _module_node(src, "_ensure_testimonial_quote_schema", "func")

    executed = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append(" ".join(sql.split()))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Pool:
        def connection(self):
            return _Conn()

    ns = {
        "_pool": _Pool(),
        "_TESTIMONIAL_QUOTE_SCHEMA_SQL": tuple(_schema_sql()),
        "_testimonial_quote_schema_done": False,
    }
    exec(ast.unparse(fn), ns)

    ns["_ensure_testimonial_quote_schema"]()
    assert executed == list(ns["_TESTIMONIAL_QUOTE_SCHEMA_SQL"])
    ns["_ensure_testimonial_quote_schema"]()
    assert len(executed) == 2, "second call must be a no-op (once per process)"


# ── no writer may target the dropped constraint ─────────────────────────────

_CONFLICT_TARGET = re.compile(
    r"ON\s+CONFLICT\s*\(\s*platform\s*,\s*context\s*\)", re.IGNORECASE)
_CONFLICT_CONSTRAINT = re.compile(
    r"ON\s+CONFLICT\s+ON\s+CONSTRAINT\s+ai_testimonials_platform_context",
    re.IGNORECASE)


def test_no_insert_targets_the_dropped_constraint():
    """The broad UNIQUE (platform, context) constraint is dropped on live.
    An INSERT naming it (or its column pair) as an ON CONFLICT target now
    raises 'no unique or exclusion constraint matching' — the bare
    target-less DO NOTHING form is the only allowed one on this table.
    """
    offenders = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in
                       ("venv", "node_modules", "__pycache__")]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    src = f.read()
            except OSError:
                continue
            if "ai_testimonials" not in src:
                continue
            if _CONFLICT_TARGET.search(src) or _CONFLICT_CONSTRAINT.search(src):
                offenders.append(os.path.relpath(path, ROOT))
    assert not offenders, (
        f"ON CONFLICT targeting (platform, context) found in {offenders} — "
        "that constraint was dropped 2026-07-30; use the bare target-less "
        "form (arbitrates against the ai_testimonials_auto_dedup partial "
        "index) or dedup app-side"
    )


# ── duplicate guards must be quote-scoped, never company-scoped ─────────────

def test_claim_quote_dup_guards_match_on_quote_not_context():
    src = _read("flask_mcp_endpoints.py")
    guards = [
        " ".join(m.split())
        for m in re.findall(
            r"SELECT\s+(?:1|id)\s+FROM\s+ai_testimonials\s+WHERE[^\"]*?LIMIT\s+1",
            src)
        if "claim_quote" in m
    ]
    assert len(guards) == 2, (
        f"expected the identify + claim/quote idempotency guards, got {guards!r}")
    for g in guards:
        assert "quote = %s" in g, f"guard must dedup on the quote text: {g!r}"
        assert "context" not in g, (
            f"guard must NOT filter on context/company — one quote per company "
            f"was the bug this replaces: {g!r}")


def test_claim_quote_inserts_stay_plain():
    """Both claim_quote INSERTs must stay ON CONFLICT-free: a target-less DO
    NOTHING here would silently eat rows again the day someone widens the
    partial index, and there is no constraint left to target."""
    src = " ".join(_read("flask_mcp_endpoints.py").split())
    inserts = re.findall(
        r"INSERT INTO ai_testimonials \(platform, agent_name, quote, context, "
        r"category, source, approved\).*?(?:\"\"\")", src)
    assert len(inserts) == 2, "expected exactly the two claim_quote INSERTs"
    for ins in inserts:
        assert "ON CONFLICT" not in ins.upper()
