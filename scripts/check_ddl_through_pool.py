#!/usr/bin/env python3
"""Fail when a module runs DDL through the WRAPPED cursor, which silently drops it.

★ THE TRAP. `db_utils.py:13` sets `SKIP_DDL = os.environ.get('SKIP_DDL', '1')
== '1'` — DEFAULT ON, and the var is absent from prod config. So
`PGCursorWrapper.execute()` returns early for any statement starting with one
of `db_utils._DDL_PREFIXES`:

    def execute(self, sql, params=None):
        if _is_ddl(sql):
            return self          # ← no error, no log, no table

Nothing raises. Nothing is logged. The call returns the cursor as if it had
worked. Every lazy `CREATE TABLE IF NOT EXISTS` written against `db_utils`'
`get_db()`, `safe_db()`, `try_get_db()` or `safe_write()` is a no-op in
production, and the first INSERT after it fails with an undefined-relation
error — inside whatever `try/except: pass` the caller wrapped its logging in.

★ WHY A GUARD AND NOT A COMMENT. Twenty-five modules already carry a
hand-written warning about this in their docstrings, which is exactly the
problem: the knowledge lives in prose, is rediscovered by whoever gets bitten
next, and does nothing for the module written tomorrow. It cost three months of
silently-missing `mcp_sessions` rows (#2196), and `free_tier_limiter`,
`intelligence_engine`, `linkedin_posts_schema` and `seo_promotion_engine` each
carry their own postmortem of the same bug.

★★ `main.get_db` IS NOT `db_utils.get_db`, AND THAT DISTINCTION IS THE WHOLE
SCRIPT. main.py:7613 imports the db_utils one, then main.py:7625 REBINDS the
name to a function returning `get_pg_connection()` — a raw psycopg2 connection
straight off the pool, with no wrapper and therefore no DDL skip. So:

    from main import get_db          → DDL RUNS.       Not an offence.
    from db_utils import get_db      → DDL IS DROPPED. Offence.

69 files in this tree import `get_db` from main. A guard that treated the two
as the same name would have reported ~60 false offences on day one and been
switched off by the end of the week, so resolution here is import-aware:
every getter is judged by where the name came from, module scope and
function-local imports both.

★ THE FIX, when this fires. Use `db_utils.ddl_cursor()` — its own direct
autocommit psycopg2 connection, no wrapper, so the DDL really runs:

    from db_utils import ddl_cursor
    with ddl_cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS ...")

That helper was added with this guard, and for the same reason the guard
exists: 25 modules had each hand-rolled their own `psycopg2.connect` to escape
the trap, because there was no blessed alternative to reach for. A trap with no
marked path around it gets walked into.

Hand-rolled `psycopg2.connect` still works and is still recognised
(`routes/email_suppression._ensure_table`, `main._persist_mcp_session`), as is
unwrapping to the underlying cursor — `getattr(conn.cursor(), "_cur", ...)`, as
`routes/paywall_hint_middleware` does. Or move the DDL to a migration and leave
the module read-only against a table it does not own.

★ THE PREFIX LIST IS IMPORTED, NOT COPIED. A guard with its own copy of
`_DDL_PREFIXES` would keep passing after someone adds a new prefix to the
wrapper. Note the corollary: `DROP TABLE`, `CREATE SCHEMA` and `CREATE OR
REPLACE VIEW` are NOT in that list, so they really do execute through the
wrapper — this script deliberately says nothing about them, because its
subject is the silent skip, not DDL hygiene generally.

Usage:  python3 scripts/check_ddl_through_pool.py [root]
Exit:   0 clean · 1 a new offender · 2 the scan itself is broken
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

# Imported, never copied — see the header. The fallback exists only so the
# script still runs from a checkout where db_utils is unimportable, and it
# names itself in the output when it is used.
try:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from db_utils import _DDL_PREFIXES  # type: ignore
    _PREFIX_SOURCE = "db_utils._DDL_PREFIXES"
except Exception:  # pragma: no cover - only when db_utils itself is broken
    _DDL_PREFIXES = ('CREATE TABLE', 'CREATE INDEX', 'ALTER TABLE',
                     'CREATE UNIQUE INDEX')
    _PREFIX_SOURCE = "fallback copy (db_utils unimportable)"

# Names in db_utils that hand back a PGConnectionWrapper / PGCursorWrapper.
POOLED_GETTERS = frozenset({
    "get_db", "try_get_db", "get_read_db", "get_bg_db", "safe_db",
    "safe_db_cursor", "_get_pg_connection",
})
# db_utils helpers that take the SQL directly and run it on a wrapped cursor,
# so DDL passed to one is an offence regardless of what else the function does.
POOLED_EXECUTORS = frozenset({
    "safe_write", "safe_executemany", "safe_write_returning",
    "safe_transaction",
})
# Everything the wrapper reaches, by db_utils name.
DB_UTILS_WRAPPED = POOLED_GETTERS | POOLED_EXECUTORS
# ★ The blessed way OUT. db_utils.ddl_cursor() opens its own direct psycopg2
# connection with autocommit and no wrapper, so DDL on it really executes. It
# exists because 25 modules each hand-rolled psycopg2.connect to escape the
# trap — a trap with no marked path around it gets walked into. Recognised here
# so the guard points at a fix that lives in the same module as the problem.
DB_UTILS_DIRECT = frozenset({"ddl_cursor"})
# main.py rebinds these to raw-connection functions (main.py:7625/7629), so the
# same identifier imported from main is NOT an offence. main's `safe_write` is
# a straight re-export of the db_utils one and is deliberately absent here.
MAIN_RAW = frozenset({"get_db", "get_read_db", "get_pg_connection",
                      "try_get_pg_connection"})

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
              "site-packages", ".mypy_cache", ".pytest_cache", "tests"}

# If the walk stops matching, every loop below runs zero times and this exits 0
# having checked nothing — the same class of silent pass it exists to catch.
MIN_FILES = 200

ALLOWLIST = "scripts/ddl_through_pool_allowlist.txt"


# ── name resolution ───────────────────────────────────────────────────────

def _import_binds(node) -> dict:
    """{local_name: 'pooled' | 'raw' | 'db_utils' | 'main'} from one statement.

    'pooled'/'raw' tag a directly-imported function; 'db_utils'/'main' tag a
    module alias so `db_utils.get_db()` resolves later.
    """
    binds = {}
    if isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        for a in node.names:
            local = a.asname or a.name
            if mod == "db_utils" and a.name in DB_UTILS_DIRECT:
                binds[local] = "raw"
            elif mod == "db_utils" and a.name in DB_UTILS_WRAPPED:
                binds[local] = "pooled"
            elif mod == "main" and a.name in MAIN_RAW:
                binds[local] = "raw"
            elif mod == "main" and a.name in DB_UTILS_WRAPPED:
                # re-exported unchanged from db_utils (e.g. safe_write)
                binds[local] = "pooled"
    elif isinstance(node, ast.Import):
        for a in node.names:
            if a.name in ("db_utils", "main"):
                binds[a.asname or a.name] = a.name
    return binds


def _collect_binds(node) -> dict:
    """Import bindings anywhere under `node`, not descending into nested defs
    when `node` is itself a def — callers pass either a Module or a function."""
    binds = {}
    stack = list(ast.iter_child_nodes(node))
    while stack:
        cur = stack.pop()
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        binds.update(_import_binds(cur))
        stack.extend(ast.iter_child_nodes(cur))
    return binds


def _rebound_names(node) -> set:
    """Module-scope names REBOUND by a def/class/assignment after import.

    ★ This is not pedantry, it is main.py. Line 7613 does
    `from db_utils import get_db`; line 7625 then defines `def get_db(...)`
    returning a raw pooled connection. Inside main.py the name means the raw
    one, so every `get_db()` in its own 40k lines is NOT an offence — without
    this, main.py alone contributes six false reports.
    """
    out = set()
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            out.add(child.name)
        elif isinstance(child, ast.Assign):
            for t in child.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
    return out


def _module_binds(tree) -> dict:
    """Module-scope imports, including those nested in try/except — this tree
    imports half its dependencies inside a `try: ... except ImportError:` —
    minus any name a later def or assignment took over."""
    binds = _collect_binds(tree)
    for name in _rebound_names(tree):
        binds.pop(name, None)
    return binds


def _resolve(call: ast.Call, binds: dict) -> str:
    """'pooled' | 'raw' | '' for the target of one call."""
    f = call.func
    if isinstance(f, ast.Name):
        return binds.get(f.id, "") if binds.get(f.id) in ("pooled", "raw") else ""
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        origin = binds.get(f.value.id, "")
        if origin == "db_utils" and f.attr in DB_UTILS_DIRECT:
            return "raw"
        if origin == "db_utils" and f.attr in DB_UTILS_WRAPPED:
            return "pooled"
        if origin == "main":
            if f.attr in MAIN_RAW:
                return "raw"
            if f.attr in DB_UTILS_WRAPPED:
                return "pooled"
    return ""


def _call_name(node) -> str:
    """Rightmost identifier of a call target: `db_utils.get_db()` -> 'get_db'."""
    if not isinstance(node, ast.Call):
        return ""
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _is_direct_connect(node) -> bool:
    if not isinstance(node, ast.Call):
        return False
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr == "connect":
        base = f.value
        if isinstance(base, ast.Name) and base.id in ("psycopg2", "pg", "pg8000"):
            return True
        if isinstance(base, ast.Attribute) and base.attr in ("psycopg2", "pool"):
            return True
    if isinstance(f, ast.Name) and f.id == "connect":
        return True
    return False


def _is_cur_unwrap(node) -> bool:
    """`getattr(c, "_cur", c)` — the documented escape hatch out of the
    wrapper and onto the raw psycopg2 cursor, where DDL really executes."""
    if isinstance(node, ast.Call) and _call_name(node) == "getattr":
        for a in node.args[1:2]:
            if isinstance(a, ast.Constant) and a.value == "_cur":
                return True
    return False


# ── DDL detection ─────────────────────────────────────────────────────────

_TABLE_RE = (
    re.compile(r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
               r"[\"']?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)", re.I),
    re.compile(r"^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?"
               r"[\"']?([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)", re.I),
    re.compile(r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
               r"(?:IF\s+NOT\s+EXISTS\s+)?\S+\s+ON\s+[\"']?"
               r"([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)", re.I),
)


def target_table(stmt: str) -> str:
    """The table a DDL statement acts on, schema-qualifier stripped.

    ★ This is what makes the allowlist decidable. A frozen entry means "this
    CREATE has never run"; whether that MATTERS depends entirely on whether the
    table exists anyway — created by a migration or by a deploy that predates
    SKIP_DDL. Without the name there is no way to ask. `routes/ddl_audit.py`
    takes these names to the live database and answers it.
    """
    flat = " ".join((stmt or "").split())
    for rx in _TABLE_RE:
        m = rx.match(flat)
        if m:
            return m.group(1).split(".")[-1].lower()
    return ""


def _ddl_statements(node):
    """DDL in a string-ish expression, as (snippet, table) pairs.

    Handles plain constants and f-strings (literal chunks only), and splits on
    ';' so an `executescript`-style blob is examined statement by statement.
    """
    text = None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            parts.append(v.value if isinstance(v, ast.Constant)
                         and isinstance(v.value, str) else " ")
        text = "".join(parts)
    if not text:
        return []
    out = []
    for stmt in text.split(";"):
        s = stmt.strip().upper()
        if any(s.startswith(p) for p in _DDL_PREFIXES):
            out.append((stmt.strip().split("\n")[0][:70], target_table(stmt)))
    return out


class _FnScan(ast.NodeVisitor):
    """What one function does, WITHOUT descending into nested defs — a closure
    that opens its own direct connection is judged on its own body rather than
    inheriting its parent's."""

    def __init__(self, binds):
        self.binds = binds
        self.pooled = set()
        self.direct = False
        self.ddl = []
        self.executor_ddl = []
        self.calls = set()

    def visit_FunctionDef(self, node):
        return

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        name = _call_name(node)
        if name:
            self.calls.add(name)
        kind = _resolve(node, self.binds)
        if kind == "pooled":
            self.pooled.add(name)
        elif kind == "raw":
            self.direct = True
        if _is_direct_connect(node) or _is_cur_unwrap(node):
            self.direct = True
        if kind == "pooled" and name in POOLED_EXECUTORS:
            for arg in node.args[:3]:
                for snip, tbl in _ddl_statements(arg):
                    self.executor_ddl.append((node.lineno, snip, tbl))
        if name in ("execute", "executescript", "executemany") and node.args:
            for snip, tbl in _ddl_statements(node.args[0]):
                self.ddl.append((node.lineno, snip, tbl))
        self.generic_visit(node)


def _functions(tree):
    """(qualified_name, node) for every def in the module."""
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                out.append((qual, child))
                walk(child, qual + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def scan_source(src: str, path: str = "<src>"):
    """Offences in one module. Each is a dict; an empty list means clean."""
    tree = ast.parse(src)
    mbinds = _module_binds(tree)

    scans = {}
    for qual, node in _functions(tree):
        binds = dict(mbinds)
        binds.update(_collect_binds(node))   # function-local imports win
        s = _FnScan(binds)
        for child in ast.iter_child_nodes(node):
            s.visit(child)
        scans[qual] = s

    # One level of local-helper resolution, both directions. A module-level
    # `_conn()` wrapping psycopg2.connect and a `_get_db()` wrapping
    # db_utils.get_db are both common here, and a guard that misreads either is
    # a guard that gets deleted rather than fixed.
    local_direct = {q.split(".")[-1] for q, s in scans.items() if s.direct}
    local_pooled = {q.split(".")[-1] for q, s in scans.items() if s.pooled}

    offences = []
    for qual, s in scans.items():
        direct = s.direct or bool(s.calls & local_direct)
        pooled = bool(s.pooled) or bool(s.calls & local_pooled)
        for lineno, snip, tbl in s.executor_ddl:
            offences.append({
                "path": path, "line": lineno, "function": qual, "sql": snip,
                "table": tbl,
                "why": "DDL handed to a db_utils safe_* helper, which always "
                       "runs on the wrapped cursor",
            })
        if not s.ddl or direct or not pooled:
            # direct  → a raw connection is in play; the DDL rides it.
            # !pooled → no connection source visible; the cursor was handed in
            #           and is unresolvable from here. Guessing produces the
            #           false positives that get guards switched off.
            continue
        via = (", ".join("db_utils." + n for n in sorted(s.pooled))
               if s.pooled else "a local helper that returns a db_utils "
                                "connection")
        for lineno, snip, tbl in s.ddl:
            offences.append({
                "path": path, "line": lineno, "function": qual, "sql": snip,
                "table": tbl,
                "why": f"DDL on a cursor from {via}",
            })
    return offences


def scan_tree(root: str):
    files, offences = 0, []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, encoding="utf-8") as fh:
                    src = fh.read()
            except Exception:
                continue
            files += 1
            try:
                offences.extend(scan_source(src, os.path.relpath(full, root)))
            except SyntaxError:
                # syntax-check owns that failure; not this script's job to
                # double-report it.
                continue
    return files, offences


def load_allowlist(root: str) -> set:
    """Pre-existing offences, one `path::function` per line.

    ★ A FREEZE, NOT AN AMNESTY. Every line is a module whose lazy CREATE has
    never run in production. The list exists so the guard can land today rather
    than behind a 30-file migration, and it is only ever meant to shrink.
    """
    out = set()
    try:
        with open(os.path.join(root, ALLOWLIST), encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0].strip()
                if line:
                    out.add(line)
    except FileNotFoundError:
        pass
    return out


# ── MUST-FAIL CONTROL ────────────────────────────────────────────────────────
# This guard's whole subject is a failure that is SILENT: db_utils' cursor
# wrapper returns early on DDL, nothing raises, nothing logs, and the table is
# never created. It hid mcp_sessions for three months (#2196). A silent-failure
# guard that has itself gone silent is indistinguishable from a clean tree.
#
# The control drives scan_source() — the real scanner — over planted modules.
# The GOOD cases matter as much as the BAD ones, and one of them is the entire
# subtlety of this script: `from main import get_db` is a raw pooled connection
# and DDL through it RUNS, while `from db_utils import get_db` is the wrapper
# and DDL through it is dropped. A control that did not pin that distinction
# would pass a scanner that flagged all 69 main-importers and got deleted.
_SELFTEST_MUST_FIRE = {
    "DDL through db_utils.get_db":
        "from db_utils import get_db\n"
        "def make():\n"
        "    with get_db() as c, c.cursor() as cur:\n"
        "        cur.execute('CREATE TABLE IF NOT EXISTS x (id int)')\n",
    "DDL through db_utils.safe_db":
        "import db_utils\n"
        "def make():\n"
        "    with db_utils.safe_db() as c, c.cursor() as cur:\n"
        "        cur.execute('CREATE INDEX IF NOT EXISTS i ON x (id)')\n",
}
_SELFTEST_MUST_STAY_SILENT = {
    "DDL through a raw psycopg2.connect":
        "import psycopg2\n"
        "def make():\n"
        "    with psycopg2.connect(DSN) as c, c.cursor() as cur:\n"
        "        cur.execute('CREATE TABLE IF NOT EXISTS x (id int)')\n",
    "DDL through main.get_db (rebound to a raw pooled conn)":
        "from main import get_db\n"
        "def make():\n"
        "    with get_db() as c, c.cursor() as cur:\n"
        "        cur.execute('CREATE TABLE IF NOT EXISTS x (id int)')\n",
    "a plain INSERT through db_utils":
        "from db_utils import get_db\n"
        "def ins():\n"
        "    with get_db() as c, c.cursor() as cur:\n"
        "        cur.execute('INSERT INTO x VALUES (1)')\n",
}


def self_test():
    """Prove the scanner still refuses DDL through the wrapper, and still does
    NOT flag the sanctioned ways of running it. Exit 1 = the GUARD is broken."""
    dead, noisy = [], []
    for name, src in _SELFTEST_MUST_FIRE.items():
        try:
            if not scan_source(src, "<selftest>"):
                dead.append(name)
        except Exception as e:  # noqa: BLE001
            dead.append(f"{name} (raised {type(e).__name__})")
    for name, src in _SELFTEST_MUST_STAY_SILENT.items():
        try:
            if scan_source(src, "<selftest>"):
                noisy.append(name)
        except Exception as e:  # noqa: BLE001
            noisy.append(f"{name} (raised {type(e).__name__})")
    if dead or noisy:
        if dead:
            print("SELF-TEST FAILED — scanner no longer fires on: "
                  + ", ".join(dead), file=sys.stderr)
        if noisy:
            print("SELF-TEST FAILED — false positive on sanctioned shape: "
                  + ", ".join(noisy), file=sys.stderr)
        return 1
    print(f"self-test ok: {len(_SELFTEST_MUST_FIRE)} wrapper-DDL shapes fire, "
          f"{len(_SELFTEST_MUST_STAY_SILENT)} sanctioned shapes stay silent")
    return 0


def main(argv):
    if "--self-test" in argv:
        return self_test()
    root = argv[1] if len(argv) > 1 else str(
        pathlib.Path(__file__).resolve().parent.parent)
    files, offences = scan_tree(root)
    if files < MIN_FILES:
        print(f"SCAN BROKEN: only {files} .py files under {root} "
              f"(expected >= {MIN_FILES}). Refusing to report a vacuous pass.")
        return 2

    allowed = load_allowlist(root)
    keys = {f"{o['path']}::{o['function']}" for o in offences}
    new = [o for o in offences
           if f"{o['path']}::{o['function']}" not in allowed]
    stale = sorted(allowed - keys)

    print(f"scanned {files} files · DDL prefixes from {_PREFIX_SOURCE}")
    print(f"{len(offences)} DDL-through-wrapper site(s) in {len(keys)} "
          f"function(s) · {len(allowed)} allowlisted · {len(new)} new")
    for o in new:
        print(f"::error file={o['path']},line={o['line']}::{o['function']} "
              f"runs DDL through the db_utils wrapper, which drops it silently "
              f"under SKIP_DDL: {o['sql']}")
        print(f"  {o['path']}:{o['line']}  {o['function']}  — {o['why']}")
    if stale:
        # Not a failure. An entry with no matching offence means someone FIXED
        # it, and a guard that punishes the fix is worse than no guard — but
        # the line should be deleted so the list keeps shrinking.
        print(f"\nallowlist entries with no matching offence (fixed — delete "
              f"the line from {ALLOWLIST}):")
        for k in stale:
            print(f"  {k}")
    if new:
        print("\nDDL must run on a RAW psycopg2 cursor, not a db_utils-wrapped "
              "one. See this script's header for the pattern, or "
              "routes/email_suppression._ensure_table for a live example.")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:  # pragma: no cover
        print(f"SCAN BROKEN: {exc!r}")
        sys.exit(2)
