"""ddl-audit — which frozen table-creates actually matter (2026-08-04).

House rule: tests NEVER import main. Everything here imports the leaf module or
reads files directly, and nothing runs at module scope.

`scripts/check_ddl_through_pool.py` proved 59 functions never create their
table. It cannot say whether that MATTERS: a migration or a pre-SKIP_DDL deploy
may have created the table anyway, in which case the lazy CREATE is dead weight
and converting it would be churn. Only a MISSING table is a live bug.

★ The temptation this module exists to resist is "just fix all 59". That would
fire ~214 dormant DDL statements at production in one deploy — ~140 tables that
do not exist today, ALTER TABLE against live schemas, and CREATE INDEX (not
CONCURRENTLY) taking a write lock on whatever it touches. A blanket fix is a
schema change wearing a lint cleanup's clothes. Measure, then fix what is
broken.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


# ── the verdict, which is the whole product ───────────────────────────

def _frozen(path="m.py", fn="f", table="t", line=1):
    return {"path": path, "function": fn, "table": table, "line": line,
            "sql": "CREATE TABLE " + table}


def test_a_present_table_means_the_lazy_create_is_dead_weight():
    from routes.ddl_audit import verdicts
    rows = verdicts([_frozen(table="leads")], {"leads": True})
    assert rows[0]["verdict"] == "EXISTS"


def test_a_missing_table_is_the_live_bug():
    from routes.ddl_audit import verdicts
    rows = verdicts([_frozen(table="nope")], {"nope": False})
    assert rows[0]["verdict"] == "MISSING"


def test_unaskable_is_unknown_never_exists():
    """★ The failure mode that would make this endpoint worse than nothing:
    reading 'we could not check' as 'the table is fine', and quietly closing 59
    findings on the strength of a dead connection."""
    from routes.ddl_audit import verdicts
    rows = verdicts([_frozen(table="t")], {})          # no answer at all
    assert rows[0]["verdict"] == "UNKNOWN"
    rows = verdicts([_frozen(table="t")], {"t": None})  # explicit non-answer
    assert rows[0]["verdict"] == "UNKNOWN"


def test_one_function_with_mixed_tables_is_partial_not_exists():
    from routes.ddl_audit import verdicts
    rows = verdicts([_frozen(fn="f", table="a"), _frozen(fn="f", table="b")],
                    {"a": True, "b": False})
    assert len(rows) == 1 and rows[0]["verdict"] == "PARTIAL"


def test_missing_sorts_above_exists():
    """The reader should not have to scroll past 40 non-problems."""
    from routes.ddl_audit import verdicts
    rows = verdicts([_frozen(fn="ok", table="a"), _frozen(fn="bad", table="b")],
                    {"a": True, "b": False})
    assert [r["verdict"] for r in rows] == ["MISSING", "EXISTS"]


# ── the existence query ───────────────────────────────────────────────

class _Cur:
    def __init__(self, present):
        self.present, self.args = present, None

    def execute(self, sql, args=None):
        self.args = args

    def fetchall(self):
        return [(p,) for p in self.present]


def test_existence_is_one_query_over_distinct_names():
    from routes.ddl_audit import table_existence
    cur = _Cur(["leads"])
    out = table_existence(cur, ["leads", "leads", "gone", ""])
    assert out == {"leads": True, "gone": False}
    assert cur.args == (["gone", "leads"],)


def test_no_tables_asks_nothing():
    from routes.ddl_audit import table_existence
    cur = _Cur([])
    assert table_existence(cur, ["", None]) == {}
    assert cur.args is None


# ── the scan behind it ────────────────────────────────────────────────

def test_the_audit_reuses_the_guard_rather_than_reimplementing_it():
    """★ A second implementation would drift from the guard and start
    disagreeing about what is frozen — auditing a list nobody enforces."""
    src = _src("routes", "ddl_audit.py")
    assert "check_ddl_through_pool.py" in src
    assert "_DDL_PREFIXES" not in src, "no private copy of the rule"


def test_a_vacuous_scan_is_refused_not_reported_clean():
    """If the file walk stops matching, 'zero frozen entries' looks identical
    to 'everything fixed'."""
    src = _src("routes", "ddl_audit.py")
    assert "MIN_FILES" in src and "vacuous" in src


def test_the_auditor_creates_nothing():
    """It would be a poor joke otherwise."""
    body = "\n".join(l for l in _src("routes", "ddl_audit.py").splitlines()
                     if not l.lstrip().startswith("#"))
    up = body.upper()
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert verb not in up, f"the auditor must be read-only: {verb}"
    # It names CREATE TABLE constantly in prose; what matters is that no
    # cursor is ever handed one.
    assert "cur.execute" in body
    assert 'cur.execute("SELECT' in body or "cur.execute(\n" in body


def test_it_reads_its_own_database_directly():
    """The module telling everyone else to stop using the wrapped path should
    not be using it."""
    src = _src("routes", "ddl_audit.py")
    assert "psycopg2.connect" in src
    assert "safe_db" not in src and "try_get_db" not in src


def test_blueprint_is_registered_in_main():
    src = _src("main.py")
    assert "from routes.ddl_audit import ddl_audit_bp" in src
    assert "app.register_blueprint(ddl_audit_bp)" in src


def test_it_is_admin_gated_and_killable():
    src = _src("routes", "ddl_audit.py")
    assert "X-Admin-Key" in src and "DDL_AUDIT_DISABLE" in src


# ── the table-name extraction the audit depends on ────────────────────

def test_table_names_are_pulled_from_every_ddl_shape():
    """Without the name there is nothing to ask the database about."""
    import importlib.util
    path = os.path.join(ROOT, "scripts", "check_ddl_through_pool.py")
    spec = importlib.util.spec_from_file_location("_ddl_guard_t", path)
    g = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(g)
    cases = {
        "CREATE TABLE IF NOT EXISTS leads (id INT)": "leads",
        "CREATE TABLE users (id INT)": "users",
        "ALTER TABLE announcements ADD COLUMN category TEXT": "announcements",
        "CREATE INDEX IF NOT EXISTS ix_a ON obs_metrics (m)": "obs_metrics",
        "CREATE UNIQUE INDEX u ON gas_pipelines(source_id)": "gas_pipelines",
        "CREATE TABLE public.scoped (id INT)": "scoped",
        "SELECT 1": "",
    }
    for sql, want in cases.items():
        assert g.target_table(sql) == want, sql
