"""`POST /api/agents/enrichment/submit` writes to a table that was never created.

Measured against production information_schema on 2026-09-11: `submissions` is
absent. Its CREATE lives in `discovery_nexus.NexusDatabase._init_db`, which runs
on a `db_utils` cursor — and `PGCursorWrapper.execute` drops CREATE TABLE while
SKIP_DDL is set, which it is by default. So the statement never reached
Postgres and every enrichment submission has been failing on an undefined
relation. Same mechanism as news_articles.publisher_url (#4438); this one is on
a public, unauthenticated endpoint.

`api_server.py` carries a second copy of the route. It is not deployed —
start_web.sh runs `gunicorn main:app` — so main.py's is the live one.
"""
from __future__ import annotations

import ast
import functools
import os

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@functools.lru_cache(maxsize=1)
def _main_tree():
    with open(os.path.join(_ROOT, "main.py"), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _fn(name):
    for node in ast.walk(_main_tree()):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name}() is gone from main.py — it is the fix for a "
                         "public endpoint that writes to a missing table")


def _strings(node, skip_docstring=True):
    """String constants in `node`, WITHOUT its docstring by default.

    The docstring of _ensure_submissions_table quotes the dead foreign key in
    order to explain it. A check that scanned the docstring too would match
    that explanation and report the bug it documents — the comment explaining
    the drift is not the drift."""
    doc = ast.get_docstring(node, clean=False) if skip_docstring else None
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and not (doc is not None and n.value == doc)]


def _calls(node):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.append(f.id if isinstance(f, ast.Name)
                       else getattr(f, "attr", ""))
    return out


def test_the_ensure_never_borrows_a_pooled_connection():
    """A pooled cursor silently drops CREATE TABLE. That is the whole bug."""
    calls = _calls(_fn("_ensure_submissions_table"))
    for pooled in ("get_db", "get_bg_db", "get_read_db", "try_get_db", "safe_db"):
        assert pooled not in calls, (
            f"_ensure_submissions_table calls {pooled}() — a pooled connection "
            "cannot run this CREATE, which is how submissions went missing")
    assert "ddl_cursor" in calls, (
        "the CREATE does not run on db_utils.ddl_cursor(), the one connection "
        "in this codebase that actually executes DDL")


def test_the_ensure_reads_the_schema_back():
    """`CREATE TABLE IF NOT EXISTS` reports success whether or not it did
    anything. Without a read-back, a fix that did nothing looks identical to a
    fix that worked — which is the failure mode this whole class is made of."""
    sql = " ".join(_strings(_fn("_ensure_submissions_table"))).lower()
    assert "information_schema.tables" in sql, (
        "no read-back — the CREATE's own success report is not evidence")
    assert "table_name = 'submissions'" in sql, (
        "the read-back does not name the table it is supposed to verify")


def test_the_ensure_does_not_reproduce_the_unbuildable_foreign_key():
    """discovery_nexus declares FOREIGN KEY (api_key) REFERENCES api_keys(key).
    Production api_keys has key_hash, not key — so that CREATE could not have
    succeeded even with SKIP_DDL off, and both writers pass the literal
    'crowdsource', which is a tag and matches no key. Copying the constraint
    would turn "relation does not exist" into "violates foreign key
    constraint" and still 500."""
    sql = " ".join(_strings(_fn("_ensure_submissions_table"))).lower()
    assert "foreign key" not in sql, (
        "the dead FK came back; it makes the table unbuildable")
    assert "references api_keys" not in sql


def test_the_ensure_bounds_its_lock():
    """ADD COLUMN/CREATE IF NOT EXISTS still requests ACCESS EXCLUSIVE even
    when it is a no-op, and a pending exclusive request queues every lock
    behind it (util/ddl_once documents the outage this caused)."""
    sql = " ".join(_strings(_fn("_ensure_submissions_table"))).lower()
    assert "lock_timeout" in sql, "an unbounded DDL lock can stall the pool"


def test_the_ensure_is_latched_once_per_process():
    calls = _calls(_fn("_ensure_submissions_table"))
    assert "ensure_once_call" in calls or "ensure_once" in calls, (
        "without a latch this requests an exclusive lock on every submission")


def test_the_route_ensures_before_it_inserts():
    """A fix that is never called is not a fix."""
    route = _fn("enrichment_submit")
    assert "_ensure_submissions_table" in _calls(route), (
        "enrichment_submit does not call _ensure_submissions_table — the "
        "INSERT still names a table nothing creates")

    # and it must come BEFORE the write, not after it
    ensure_line = min(n.lineno for n in ast.walk(route)
                      if isinstance(n, ast.Call)
                      and getattr(n.func, "id", "") == "_ensure_submissions_table")
    insert_lines = [n.lineno for n in ast.walk(route)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and "INSERT INTO SUBMISSIONS" in n.value.upper()]
    assert insert_lines, "the INSERT this guards is gone; delete the guard too"
    assert ensure_line < min(insert_lines), (
        "the ensure runs AFTER the INSERT it is supposed to make possible")


def test_discovery_nexus_create_is_still_the_stale_one():
    """A floor, not a wish. If someone fixes discovery_nexus' CREATE to drop
    the dead FK, this test fails and the docstrings above need rewriting —
    better a red build than two schemas that quietly disagree."""
    with open(os.path.join(_ROOT, "discovery_nexus.py"), encoding="utf-8") as fh:
        src = fh.read()
    i = src.find("CREATE TABLE IF NOT EXISTS submissions")
    assert i != -1, (
        "discovery_nexus no longer declares submissions — if the schema moved, "
        "main.py's _ensure_submissions_table is now the only definition and "
        "its docstring should say so")
    block = src[i:i + 700]
    assert "REFERENCES api_keys(key)" in block, (
        "discovery_nexus' dead FK is gone — re-check whether main.py's "
        "FK-free CREATE is still the right shape")
