"""Let a DROP-and-recreate fixture survive a PROD-SHAPED database.

These fixtures were written against an empty `postgres:` service, where
`DROP TABLE IF EXISTS x` always succeeds. Pointed at a `schema-only` Neon branch
cut from production — the `ephemeral-db` db-parity lane — the same statement
fails two ways. On 2026-09-19 it errored the SETUP of 40 tests across three
files:

    DependentObjectsStillExist: cannot drop table transmission_lines because
      other objects depend on it
    DETAIL:  view global_infrastructure depends on table transmission_lines

    WrongObjectType: "mcp_calls_identity" is not a table

★ `DROP ... CASCADE` IS NOT THE FIX. All 19 lanes share ONE branch, so cascading
would delete production's views out of the shared schema — `global_infrastructure`
among them, whose definition exists in no file in this repo — and every later
lane reading them would fail, or pass vacuously. What this module does instead,
per relation:

  * ABSENT  -> say so, and let the fixture CREATE it. The empty-postgres path is
    unchanged, which is where these tests still run outside the parity lane.
  * TABLE   -> TRUNCATE it and keep PRODUCTION's shape. That is the entire point
    of a parity lane: the test then exercises the real column list instead of
    the fixture's idea of it. `TRUNCATE ... CASCADE` removes ROWS from
    referencing tables and drops NOT ONE OBJECT — that is the whole difference
    from the trap above.
  * VIEW    -> the fixture's model of the schema disagrees with production and
    nothing here can reconcile them. Skip, naming the object, so the
    disagreement is reported rather than raised as a setup error or, worse,
    silently satisfied.

This deliberately does NOT cover a table a fixture wants ABSENT. A drop can be
an assertion: `test_transmission_readers_sql.py` drops
`discovered_transmission_lines` and never recreates it, so any reader still
touching it raises. Truncating that would hand such a reader an empty table and
the control test would stop controlling anything. Those drops stay as they are.
"""
from __future__ import annotations

import pytest

# relkind values that TRUNCATE accepts: ordinary and partitioned tables.
_TABLE = ("r", "p")
_VIEWS = {"v": "view", "m": "materialized view"}

_RELKIND = """
    SELECT c.relkind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = %s
"""


def reset_tables(cur, *names: str) -> tuple[str, ...]:
    """Empty each named table, or report it absent so the caller creates it.

    Returns the names that do not exist yet. Skips the test when production
    carries one of them as a view.
    """
    # Imported here, not at module scope: every caller reaches this module via
    # `pytest.importorskip("psycopg2")`, and a top-level import would turn that
    # clean skip into a collection error wherever the driver is absent.
    from psycopg2 import sql

    absent = []
    for name in names:
        cur.execute(_RELKIND, (name,))
        row = cur.fetchone()
        kind = row[0] if row else None
        if kind is None:
            absent.append(name)
            continue
        if kind in _VIEWS:
            pytest.skip(
                f"this database carries `{name}` as a {_VIEWS[kind]}, not a "
                f"table, so this fixture's model of the schema disagrees with "
                f"it. `{name}` is rendered by scripts/render_identity_views.py "
                f"in production. Reconcile the model — do not delete the view "
                f"to make the fixture fit.")
        if kind not in _TABLE:
            pytest.skip(f"`{name}` is relkind {kind!r}, which cannot be reset "
                        f"as a table")
        cur.execute(sql.SQL("TRUNCATE TABLE {} CASCADE").format(
            sql.Identifier("public", name)))
    return tuple(absent)
