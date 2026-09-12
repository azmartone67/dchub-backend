"""A count printed next to an INSERT must name rows the database stored.

2026-09-12, follow-up to #4453. That PR fixed db_utils.PGCursorWrapper, whose
execute() fired a `SELECT lastval()` of its own after any INSERT with no
RETURNING clause — so `cursor.rowcount` described that SELECT (one row, always)
and an `ON CONFLICT DO NOTHING` which inserted nothing still counted as saved.
news_engine.save_articles reported "322 new" for ~172 rows that way.

The wrapper fix settles the READ for every caller. This file covers the call
sites, for two reasons the wrapper cannot address:

  * a call site carrying RETURNING skips the lastval probe entirely, so it is
    honest even if the wrapper's rowcount regresses again, and it is immune to
    the probe aborting its transaction (db_utils trap #2);
  * `ON CONFLICT DO UPDATE` affects one row whether it inserted or refreshed,
    so no rowcount reading can name inserts there — the wrapper fix does not
    and cannot help those, and the fix is to say "upserted".

Measured before writing any of this, through the real wrapper on a real
Postgres 18, per statement shape:

    ON CONFLICT DO NOTHING   pre-fix rowcount 1 on a conflict   WRONG
                             post-fix               0           ok
    ON CONFLICT DO UPDATE    1 either way, both versions        cannot distinguish
    plain INSERT, no clause  1, and a duplicate RAISES          ok, via the except

So of the 11 wrapper-cursor call sites the scan found, 3 were actually wrong
(and are fixed), 2 are upserts that were never distinguishable, and 6 were
already accurate because a duplicate raises instead of returning zero rows.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

WRAPPER_FACTORIES = {"get_db", "get_bg_db", "get_read_db", "try_get_db", "safe_db"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "tests",
             "site-packages", ".claude"}


def _python_files():
    out = []
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                out.append(Path(dirpath) / fn)
    return out


def _sql_literal(call):
    """The literal SQL of an execute() call, or None when it is composed."""
    if not call.args:
        return None
    parts = []

    def walk(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            parts.append(node.value)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            walk(node.left)
            walk(node.right)
        elif isinstance(node, ast.JoinedStr):
            for v in node.values:
                walk(v)
        else:
            parts.append("<<DYN>>")

    walk(call.args[0])
    return "".join(parts)


def _wrapper_rowcount_after_do_nothing():
    """Every `rowcount` read that follows an ON CONFLICT DO NOTHING insert on a
    cursor this function got from get_db()/get_bg_db(), with no RETURNING.

    Cursors are resolved inside the ENCLOSING FUNCTION. A file-wide name map is
    wrong in both directions in a 30k-line module: it put main.py's Stripe
    duplicate-webhook guard in the wrapper column because some other function in
    main.py assigns `_cur` from get_db(), when pg_connection() actually yields a
    raw pooled psycopg2 connection and that guard reads the truth.
    """
    offenders, files_scanned = [], 0
    for path in _python_files():
        files_scanned += 1
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        rel = path.relative_to(REPO).as_posix()
        for fnode in [n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            conn_kind, cur_kind = {}, {}
            for n in ast.walk(fnode):
                if (isinstance(n, ast.Assign) and len(n.targets) == 1
                        and isinstance(n.targets[0], ast.Name)
                        and isinstance(n.value, ast.Call)):
                    target = n.targets[0].id
                    f = n.value.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                    if name in WRAPPER_FACTORIES:
                        conn_kind[target] = "wrapper"
                    elif name in ("connect", "pg_connection", "get_pg_connection"):
                        conn_kind[target] = "direct"
                    elif name == "cursor":
                        base = getattr(n.value.func.value, "id", None)
                        cur_kind[target] = conn_kind.get(base, "unknown")
            executes, reads = [], []
            for n in ast.walk(fnode):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr in ("execute", "executemany")):
                    var = getattr(n.func.value, "id", None)
                    if var:
                        executes.append((n.lineno, var, _sql_literal(n)))
                if (isinstance(n, ast.Attribute) and n.attr == "rowcount"
                        and isinstance(n.ctx, ast.Load)):
                    var = getattr(n.value, "id", None)
                    if var:
                        reads.append((n.lineno, var))
            executes.sort()
            for lineno, var in sorted(reads):
                if cur_kind.get(var) != "wrapper":
                    continue
                prior = [e for e in executes if e[1] == var and e[0] <= lineno]
                if not prior:
                    continue
                sql = " ".join((prior[-1][2] or "").split()).upper()
                if not sql.startswith("INSERT"):
                    continue
                if "ON CONFLICT" in sql and "DO NOTHING" in sql and "RETURNING" not in sql:
                    offenders.append(f"{rel}:{lineno}  {var}.rowcount")
    return offenders, files_scanned


def test_no_wrapper_caller_counts_a_do_nothing_insert_by_rowcount():
    """★ The class floor. An ON CONFLICT DO NOTHING whose result is read as a
    rowcount on a pooled cursor is the exact shape that reported 322 for 172."""
    offenders, files_scanned = _wrapper_rowcount_after_do_nothing()
    assert files_scanned > 400, (
        f"scanned only {files_scanned} python files — the walk collapsed, so a "
        f"green result here would mean nothing")
    assert offenders == [], (
        "these count a skipped ON CONFLICT DO NOTHING as inserted; add "
        "RETURNING and count the rows handed back:\n  " + "\n  ".join(offenders))


def test_the_scan_can_actually_find_this_shape():
    """★ CONTROL. The assertion above is worthless if the matcher cannot see the
    shape it bans, so the shape is built here and fed to the same matcher."""
    src = '''
def writer():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO t (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (1,))
    if cur.rowcount > 0:
        n += 1
'''
    tree = ast.parse(src)
    fnode = tree.body[0]
    # same resolution the scanner uses, applied to this one function
    conn_kind, cur_kind = {}, {}
    for n in ast.walk(fnode):
        if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and isinstance(n.targets[0], ast.Name)):
            f = n.value.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in WRAPPER_FACTORIES:
                conn_kind[n.targets[0].id] = "wrapper"
            elif name == "cursor":
                cur_kind[n.targets[0].id] = conn_kind.get(
                    getattr(n.value.func.value, "id", None), "unknown")
    assert cur_kind.get("cur") == "wrapper", (
        "the cursor resolver no longer recognises `conn = get_db(); "
        "cur = conn.cursor()` — the repo scan above is blind")
    sql = [_sql_literal(n) for n in ast.walk(fnode)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
           and n.func.attr == "execute"][0].upper()
    assert "ON CONFLICT" in sql and "DO NOTHING" in sql and "RETURNING" not in sql, (
        "the SQL extractor cannot read a plain triple-quoted INSERT literal")


def _function_source(path, name):
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path} has no {name}()")


HARDENED = [
    ("main.py", "seed_serverfarm_facilities", "facilities"),
    ("autonomous_brain.py", "extract_gas_infrastructure_from_news", "gas_pipelines"),
    ("autonomous_brain.py", "extract_transmission_infrastructure_from_news", "transmission_lines"),
    ("api_auto_discovery.py", "seed_known_apis", "discovered_apis"),
]


@pytest.mark.parametrize("path,func,table", HARDENED,
                         ids=[f"{p}:{f}" for p, f, _ in HARDENED])
def test_the_do_nothing_writers_carry_returning_and_do_not_read_rowcount(path, func, table):
    node = _function_source(path, func)
    inserts = [n.value for n in ast.walk(node)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and f"INSERT INTO {table}" in n.value]
    assert inserts, f"{func}() no longer writes {table} with a literal INSERT"
    for sql in inserts:
        flat = " ".join(sql.split()).upper()
        assert "ON CONFLICT" in flat, f"dedup left the statement: {flat[:90]}"
        assert "RETURNING" in flat, (
            f"an INSERT with no RETURNING makes the pooled wrapper probe "
            f"lastval and overwrite rowcount: {flat[:90]}")
    assert not [n for n in ast.walk(node)
               if isinstance(n, ast.Attribute) and n.attr == "rowcount"], (
        f"{func}() reads rowcount again — on a pooled cursor that number can "
        f"describe a SELECT lastval(), not the INSERT")


def test_seed_known_apis_omits_the_conflict_target():
    """★ `ON CONFLICT (url)` would raise 42P10 wherever the live table has no
    unique constraint on url. The CREATE TABLE in this module declares
    `url TEXT UNIQUE`, but it runs through a POOLED cursor, which drops DDL when
    SKIP_DDL=1 — its default — so that declaration is not evidence about the
    live table. Measured on real Postgres: targeted form raises when the
    constraint is absent, untargeted form does not."""
    node = _function_source("api_auto_discovery.py", "seed_known_apis")
    inserts = [n.value for n in ast.walk(node)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "INSERT INTO discovered_apis" in n.value]
    assert inserts
    for sql in inserts:
        flat = " ".join(sql.split()).upper()
        assert not re.search(r"ON CONFLICT\s*\(", flat), (
            "a conflict target here depends on a unique constraint this repo "
            f"cannot prove exists in the live table: {flat[:90]}")


def test_the_upsert_writers_say_upserted_not_inserted():
    """ON CONFLICT DO UPDATE affects one row whether it inserted or refreshed.
    Nothing read from rowcount can name inserts there, so the count must not
    claim to. subsea_cable_ingestion published 'upserted' while counting into a
    variable called `inserted` — the gap that invites a fix to the wrong half."""
    src = (REPO / "subsea_cable_ingestion.py").read_text(encoding="utf-8")
    code = "\n".join(line.split("#")[0] for line in src.splitlines())
    stray = [i + 1 for i, line in enumerate(code.splitlines())
             if re.search(r"\binserted\b", line)]
    assert stray == [], (
        f"subsea_cable_ingestion.py lines {stray} call an upsert count "
        f"'inserted'; it is published as 'upserted'")


# ── the seeder, against a real database ──────────────────────────────────────
# NOT coverage: skips without a DSN, so CI proves nothing here. It exists
# because the AST guards above cannot show that results['existing'] is REACHABLE
# or that the table stops growing, and both were measured this way:
#
#   SEED_TEST_PG_DSN="dbname=postgres" python3 -m pytest \
#       tests/test_insert_counts_name_rows_inserted.py -v
#
# Run the WHOLE file, not `-k real_postgres`: the coverage-floor mechanism in
# tests/_scan_floors.py checks this file's pinned repo scan after it finishes,
# and a selection that skips the scanning tests trips it as a COVERAGE COLLAPSE.
real_pg = pytest.mark.skipif(
    not os.environ.get("SEED_TEST_PG_DSN"),
    reason="set SEED_TEST_PG_DSN to drive the seeder against real Postgres")


@real_pg
def test_real_postgres_seed_counts_and_stops_growing():
    """Two runs. The second must add nothing and store nothing new.

    Before this change the second run reported added=13/existing=0 — a plain
    INSERT affects one row or raises, so `rowcount > 0` was always true and the
    else branch was dead — while 12 of the 13 statements actually raised into
    the except and the 13th (null url) stored a duplicate row.
    """
    import psycopg2
    import db_utils
    import api_auto_discovery as mod

    dsn = os.environ["SEED_TEST_PG_DSN"]
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    cur = admin.cursor()
    opened = []

    class Pooled:
        def __init__(self):
            self.conn = psycopg2.connect(dsn)
            opened.append(self.conn)

        def cursor(self):
            return db_utils.PGCursorWrapper(self.conn.cursor())

        def commit(self):
            self.conn.commit()

        def rollback(self):
            self.conn.rollback()

        def close(self):
            pass

    def rows():
        c = admin.cursor()
        c.execute("SELECT COUNT(*) FROM discovered_apis")
        n = c.fetchone()[0]
        c.close()
        return n

    original = mod.get_db
    try:
        cur.execute("DROP TABLE IF EXISTS discovered_apis")
        cur.execute("""CREATE TABLE discovered_apis (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, category TEXT,
            api_type TEXT, url TEXT UNIQUE, record_count TEXT, fields TEXT,
            status TEXT DEFAULT 'discovered', last_tested TEXT,
            test_result TEXT, integrated INTEGER DEFAULT 0,
            discovered_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        mod.get_db = lambda *a, **k: Pooled()
        agent = mod.APIAutoDiscovery.__new__(mod.APIAutoDiscovery)
        agent.db_path = "pg"

        total = len(mod.KNOWN_API_SOURCES)
        seedable = sum(1 for a in mod.KNOWN_API_SOURCES if a.get("url"))

        first = agent.seed_known_apis()
        after_first = rows()
        second = agent.seed_known_apis()
        after_second = rows()

        assert sum(first.values()) == total, (
            f"{first} does not account for all {total} sources")
        assert first["added"] == seedable, f"first run: {first}"
        assert after_first == seedable, (
            f"first run stored {after_first} rows for {seedable} seedable sources")

        assert second["added"] == 0, (
            f"second run reported {second['added']} added; every source was "
            f"already there")
        assert second["existing"] == seedable, (
            f"second run reported existing={second['existing']} — the branch is "
            f"unreachable again")
        assert after_second == after_first, (
            f"the table grew from {after_first} to {after_second} on a re-seed; "
            f"a source with no url conflicts with nothing, because UNIQUE does "
            f"not constrain NULLs")
    finally:
        mod.get_db = original
        for conn in opened:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
        try:
            cur.execute("DROP TABLE IF EXISTS discovered_apis")
        finally:
            admin.close()
