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


# ── the audit publishes itself ────────────────────────────────────────

def test_the_boot_log_names_every_missing_table():
    """★ The endpoint needs an admin key. Getting one into a terminal cost five
    round-trips and ended with the live key pasted into a chat transcript,
    which then had to be rotated. A finding that only exists behind a
    credential is a finding nobody reads — the same failure as a fix that is
    never wired. The boot log is the answer with no key and no curl."""
    from routes.ddl_audit import _boot_lines
    rep = {"ok": True, "frozen_functions": 2, "frozen_statements": 5,
           "counts": {"MISSING": 1, "EXISTS": 1},
           "entries": [
               {"path": "a.py", "function": "f", "line": 9, "verdict": "MISSING",
                "tables": [{"table": "gone", "exists": False}]},
               {"path": "b.py", "function": "g", "line": 3, "verdict": "EXISTS",
                "tables": [{"table": "here", "exists": True}]},
           ]}
    lines = _boot_lines(rep)
    joined = "\n".join(lines)
    assert all(l.startswith("[ddl-audit]") for l in lines), "greppable prefix"
    assert "MISSING a.py:9 f -> gone" in joined
    assert "MISSING=1" in joined and "EXISTS=1" in joined
    assert "b.py" not in joined, "EXISTS is a count, not a line"


def test_an_unmeasured_boot_audit_says_so_rather_than_nothing():
    """★ A silent boot audit is indistinguishable from a clean one, which is
    precisely the bug this module was built around."""
    from routes.ddl_audit import _boot_lines
    lines = _boot_lines({"ok": False, "error": "source scan failed: no scripts/"})
    assert len(lines) == 1 and "UNMEASURED" in lines[0]
    assert "source scan failed" in lines[0]


def test_a_clean_audit_says_clean_rather_than_going_quiet():
    from routes.ddl_audit import _boot_lines
    joined = "\n".join(_boot_lines(
        {"ok": True, "frozen_functions": 59, "frozen_statements": 214,
         "counts": {"EXISTS": 59}, "entries": []}))
    assert "no MISSING or PARTIAL tables" in joined


def test_a_dead_database_is_carried_into_the_log_not_dropped():
    from routes.ddl_audit import _boot_lines
    joined = "\n".join(_boot_lines(
        {"ok": True, "frozen_functions": 1, "frozen_statements": 1,
         "counts": {"UNKNOWN": 1}, "db_error": "no database — UNKNOWN, not EXISTS",
         "entries": [{"path": "a.py", "function": "f", "line": 1,
                      "verdict": "UNKNOWN", "tables": []}]}))
    assert "no database" in joined


def test_the_boot_audit_is_one_shot_and_killable():
    import routes.ddl_audit as m
    assert "DDL_AUDIT_NO_BOOT_LOG" in _src("routes", "ddl_audit.py")
    saved = m._boot_started
    try:
        m._boot_started = True          # already ran
        assert m.start_boot_audit() is False, "must not start twice"
    finally:
        m._boot_started = saved


def test_the_route_and_the_log_share_one_code_path():
    """Otherwise the log and the endpoint could disagree about what is
    MISSING, and we would be back to two sources of truth."""
    src = _src("routes", "ddl_audit.py")
    assert src.count("def audit_report") == 1
    assert "audit_report()" in src and "audit_report(refresh=refresh)" in src


def test_main_starts_the_boot_audit():
    src = _src("main.py")
    assert "start_boot_audit" in src


def test_partial_is_enumerated_and_names_only_the_absent_tables():
    """★ The first run of this log printed only MISSING and reported
    'PARTIAL=5' as a bare number — five functions with at least one absent
    table, invisible in the one place anyone reads. A count is not a finding."""
    from routes.ddl_audit import _boot_lines
    joined = "\n".join(_boot_lines(
        {"ok": True, "frozen_functions": 1, "frozen_statements": 2,
         "counts": {"PARTIAL": 1},
         "entries": [{"path": "a.py", "function": "f", "line": 4,
                      "verdict": "PARTIAL",
                      "tables": [{"table": "here", "exists": True},
                                 {"table": "gone", "exists": False}]}]}))
    assert "PARTIAL a.py:4 f -> gone" in joined
    assert "here" not in joined, "name what is absent, not what is fine"
