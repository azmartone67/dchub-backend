"""capacity_tracking has no writer — keep it that way, or match the real schema.

`deep_learning_engine` used to hold the only `INSERT INTO capacity_tracking`
in the repo. It could never execute: it named `location` and `confidence`,
and neither column exists on the live table. The INSERT sat behind a bare
`except:` + `note_swallowed_write(...)`, so it failed silently for as long as
it has existed. The SELECT that fed it was broken independently — it filtered
on `datetime('now', '-30 days')`, which is SQLite, not Postgres.

Measured against the live Neon read replica on 2026-08-17:

    to_regclass('public.capacity_tracking')  -> capacity_tracking   (exists)
    SELECT COUNT(*) FROM capacity_tracking   -> 0                   (empty)

    the SELECT   -> UndefinedFunction: function datetime(unknown, unknown)
                    does not exist
    the INSERT   -> UndefinedColumn: column "location" of relation
                    "capacity_tracking" does not exist

The INSERT error is the column list, not the replica being read-only: the same
INSERT restricted to columns that do exist fails later and differently, with
ReadOnlySqlTransaction.

That is why the lane was deleted rather than repaired. Fixing only the
`datetime()` would have produced a function reporting `tracked: N, new: 0`
forever — strictly worse than the visible error it replaced, because it looks
like it works.

These guards are cheap and source-only; they hold the two facts that made
deletion the right call.
"""

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The live column set, measured 2026-08-17 (see module docstring). Any code
#: that reads or writes capacity_tracking must stay inside this set. Re-measure
#: before editing — do not widen it to make a query pass.
LIVE_COLUMNS = frozenset({
    "id", "operator", "market", "region", "capacity_mw", "phase",
    "status", "source", "source_url", "tracked_at", "raw_data",
})

#: Columns the deleted lane invented. None of these exist on the live table;
#: their presence next to capacity_tracking means someone rebuilt the phantom
#: schema from the old code rather than from the database.
PHANTOM_COLUMNS = frozenset({
    "location", "confidence", "facility_id", "expected_online",
    "discovered_at", "verified", "notes",
})

#: Reads that name a column capacity_tracking does not have, so they raise
#: UndefinedColumn on every call. Each entry is the measurement that showed it
#: dead (Neon read replica, 2026-08-17). They are recorded rather than repaired
#: because the table is empty AND writerless: repairing the column names would
#: turn "raises, swallowed" into "returns nothing" — same data, live surfaces
#: touched for no gain. Fix them when something actually writes the table, and
#: delete the entry in the same change.
_MEASURED_DEAD_READS = {
    "marketing_stats_route.py":
        "the 'recent highlight' query selects `location` and orders by "
        "`created_at`; neither column exists. Measured 2026-08-17: "
        'UndefinedColumn: column "location" does not exist.',
    "intelligence_engine.py":
        "two queries filter `WHERE discovered_at LIKE %s`; the live table has "
        "`tracked_at`, not `discovered_at`. Measured 2026-08-17: "
        'UndefinedColumn: column "discovered_at" does not exist.',
}

_SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    "tests", "site-packages", "build", "dist", "migrations",
}

#: One-time maintenance scripts that are not deployed and not imported. Listed
#: in pre_deploy_check.py's own exclusion set. They predate the measurement and
#: are not worth rewriting; they must not grow.
_UNDEPLOYED_SCRIPTS = {"fix_capacity.py"}


def _python_sources():
    """Yield (relpath, source) for every deployed .py file in the repo."""
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, ROOT)
            if rel in _UNDEPLOYED_SCRIPTS:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    yield rel, f.read()
            except (OSError, UnicodeDecodeError):
                continue


def _sql_literals_naming(tree, table):
    """Yield each string literal in `tree` that is SQL naming `table`.

    Keys on the string constant itself rather than on a line window: a window
    sweeps in comments and whatever unrelated query happens to sit nearby,
    which is how this guard produced three false positives on its first run.
    f-string pieces (JoinedStr) are joined so an interpolated query still reads
    as one statement.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                p.value for p in node.values
                if isinstance(p, ast.Constant) and isinstance(p.value, str)
            )
        else:
            continue
        if table not in text:
            continue
        if not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", text, re.I):
            continue
        yield text


def test_capacity_tracking_has_no_writer():
    """No INSERT INTO capacity_tracking anywhere in deployed code.

    The table is writerless by measurement, not by preference. A new writer is
    allowed — but it has to be written against LIVE_COLUMNS and it has to drop
    this guard deliberately, having re-measured the table first.
    """
    offenders = [
        rel for rel, src in _python_sources()
        if re.search(r"INSERT\s+INTO\s+capacity_tracking", src, re.IGNORECASE)
    ]
    assert not offenders, (
        "something now INSERTs into capacity_tracking: "
        f"{offenders}. The table was empty and had no working writer when "
        "measured 2026-08-17. If you are adding a real writer, use only "
        f"{sorted(LIVE_COLUMNS)} — the previous writer died on `location`, "
        "which does not exist — and re-measure the table before deleting "
        "this test."
    )


def test_no_phantom_capacity_tracking_columns():
    """Nothing names capacity_tracking alongside a column that does not exist.

    Catches the specific way this broke: code written from the old CREATE
    TABLE in deep_learning_engine rather than from the live schema.
    """
    offenders = []
    for rel, src in _python_sources():
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for sql in _sql_literals_naming(tree, "capacity_tracking"):
            hit = sorted(
                c for c in PHANTOM_COLUMNS
                if re.search(rf"\b{c}\b", sql)
            )
            if hit:
                offenders.append((rel, hit, " ".join(sql.split())[:160]))
    new = [o for o in offenders if o[0] not in _MEASURED_DEAD_READS]
    assert not new, (
        "capacity_tracking is referenced with columns that do not exist on "
        f"the live table (measured 2026-08-17; real columns are "
        f"{sorted(LIVE_COLUMNS)}):\n" + "\n".join(
            f"  {rel}: {hit} in {stmt!r}" for rel, hit, stmt in new
        ) + "\nThis raises UndefinedColumn on every call. Use the real "
        "columns, or add an entry to _MEASURED_DEAD_READS with the "
        "measurement that shows the path is dead."
    )


def test_measured_dead_reads_are_still_dead():
    """_MEASURED_DEAD_READS must not go stale.

    If someone repairs one of these files, the entry has to go with it —
    otherwise the registry quietly grants a permanent exemption to code that
    no longer needs one.
    """
    still_bad = set()
    for rel, src in _python_sources():
        if rel not in _MEASURED_DEAD_READS:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for sql in _sql_literals_naming(tree, "capacity_tracking"):
            if any(re.search(rf"\b{c}\b", sql) for c in PHANTOM_COLUMNS):
                still_bad.add(rel)
    fixed = sorted(set(_MEASURED_DEAD_READS) - still_bad)
    assert not fixed, (
        f"these files no longer use phantom capacity_tracking columns: "
        f"{fixed}. Remove them from _MEASURED_DEAD_READS — the exemption is "
        "no longer earned."
    )


def test_deep_learning_engine_has_no_sqlite_datetime():
    """deep_learning_engine.py runs against Postgres — no SQLite datetime().

    Scoped to this module on purpose. `datetime('now', ...)` appears at ~68
    sites across the repo; that backlog is real but is not this change. This
    guard only stops the file that was just cleaned from regressing.
    """
    path = os.path.join(ROOT, "deep_learning_engine.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    hits = [
        m.group(0)
        for m in re.finditer(r"datetime\(\s*['\"]now['\"]", src, re.IGNORECASE)
    ]
    assert not hits, (
        f"deep_learning_engine.py uses SQLite datetime(): {hits}. Against the "
        "live Neon Postgres this raises UndefinedFunction and the cursor dies "
        "before any row is processed. Use "
        "`<col>::timestamptz > NOW() - INTERVAL '30 days'` — published_date "
        "and the other timestamp columns on these tables are TEXT."
    )


def test_deleted_capacity_functions_stay_deleted():
    """The five removed symbols are gone from deep_learning_engine.

    Named individually so a revert shows up as this test rather than as a
    silent zero on a dashboard.
    """
    path = os.path.join(ROOT, "deep_learning_engine.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    defined = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    removed = {
        "track_capacity_pipeline",   # SELECT died on SQLite datetime()
        "_save_capacity_update",     # INSERT died on a column that does not exist
        "_detect_capacity_updates",  # fed the dead writer
        "_extract_capacity_info",    # fed the dead writer
        "get_capacity_pipeline",     # SELECT died on `location`; nothing imported it
    }
    back = sorted(removed & defined)
    assert not back, (
        f"deleted capacity_tracking functions are back: {back}. They were "
        "removed because every one of them fails against the live schema "
        "(measured 2026-08-17). Re-measure before restoring any of them."
    )
