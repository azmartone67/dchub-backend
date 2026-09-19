"""Let a DROP-and-recreate fixture survive a PROD-SHAPED database.

These fixtures were written against an empty `postgres:` service, where
`DROP TABLE IF EXISTS x` always succeeds. Pointed at a `schema-only` Neon branch
cut from production -- the `ephemeral-db` db-parity lane -- the same statement
fails two ways. On 2026-09-19 it errored the SETUP of 40 tests across three
files:

    DependentObjectsStillExist: cannot drop table transmission_lines because
      other objects depend on it
    DETAIL:  view global_infrastructure depends on table transmission_lines

    WrongObjectType: "mcp_calls_identity" is not a table

So: DROP first, exactly as before, and fall back only when the drop is REFUSED.

  * ABSENT              -> report it; the fixture's CREATE makes it.
  * TABLE, droppable    -> DROP and report it, so the fixture recreates it in
    its own shape. This is the empty-postgres path and it is deliberately
    unchanged: `test_continuation_compliance_sql` and
    `test_live_proof_platform_basis_sql` both build `mcp_calls_identity` with
    DIFFERENT columns, and the drop is what reconciles them. Truncating instead
    made the second file inherit the first one's table and errored 8 tests --
    measured, not hypothetical.
  * TABLE, drop refused -> TRUNCATE ... CASCADE and keep PRODUCTION's shape.
    That removes ROWS from referencing tables and drops NOT ONE OBJECT -- the
    whole difference from the trap below. The fixture's `CREATE TABLE IF NOT
    EXISTS` then no-ops and the test runs against production's real column list.
  * VIEW                -> the fixture's model of the schema disagrees with
    production and nothing here can reconcile them. Skip, naming the object, so
    the disagreement is reported rather than raised as a setup error or, worse,
    silently satisfied.

* `DROP ... CASCADE` IS NOT THE FALLBACK. All 19 lanes share ONE branch, so
cascading would delete production's views out of the shared schema --
`global_infrastructure` among them, whose definition exists in no file in this
repo -- and every later lane reading them would fail, or pass vacuously.

This deliberately does NOT cover a table a fixture wants ABSENT. A drop can be
an assertion: `test_transmission_readers_sql.py` drops
`discovered_transmission_lines` and never recreates it, so any reader still
touching it raises. Truncating that would hand such a reader an empty table and
the control test would stop controlling anything. Those drops stay as they are.
"""
from __future__ import annotations

import pytest

# relkind values TRUNCATE accepts: ordinary and partitioned tables.
_TABLE = ("r", "p")
_VIEWS = {"v": "view", "m": "materialized view"}

_RELKIND = """
    SELECT c.relkind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = %s
"""


def reset_tables(cur, *names: str) -> tuple[str, ...]:
    """Clear each named table; return the ones the caller must now CREATE.

    A name comes back when it does not exist or was dropped here. A name does
    NOT come back when production's dependencies forced a truncate instead --
    it still exists, so the caller's `CREATE TABLE IF NOT EXISTS` no-ops.
    Skips the test when the relation is a view.
    """
    # Imported here, not at module scope: every caller reaches this module via
    # `pytest.importorskip("psycopg2")`, and a top-level import would turn that
    # clean skip into a collection error wherever the driver is absent.
    from psycopg2 import errors, sql

    if not cur.connection.autocommit:
        raise RuntimeError(
            "reset_tables needs an autocommit connection: it DROPs first and "
            "falls back to TRUNCATE when a dependent object refuses the drop. "
            "Inside a transaction that first failure aborts the session and "
            "every later statement fails with InFailedSqlTransaction instead.")

    to_create = []
    for name in names:
        cur.execute(_RELKIND, (name,))
        row = cur.fetchone()
        kind = row[0] if row else None
        if kind is None:
            to_create.append(name)
            continue
        if kind in _VIEWS:
            pytest.skip(
                f"this database carries `{name}` as a {_VIEWS[kind]}, not a "
                f"table, so this fixture's model of the schema disagrees with "
                f"it. `{name}` is rendered by scripts/render_identity_views.py "
                f"in production. Reconcile the model -- do not delete the view "
                f"to make the fixture fit.")
        if kind not in _TABLE:
            pytest.skip(f"`{name}` is relkind {kind!r}, which cannot be reset "
                        f"as a table")
        ident = sql.Identifier("public", name)
        try:
            cur.execute(sql.SQL("DROP TABLE {}").format(ident))
        except errors.DependentObjectsStillExist:
            cur.execute(sql.SQL("TRUNCATE TABLE {} CASCADE").format(ident))
        else:
            to_create.append(name)
    return tuple(to_create)
