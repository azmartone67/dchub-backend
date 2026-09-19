#!/usr/bin/env python3
"""The alias-repoint ON CONFLICT clause, against a REAL Postgres.

`routes.facility_slug_freeze._ALIAS_REPOINT` is ONE clause shared by three
alias emitters — provider-dedupe (`backfill_canonical_slugs`), id-scheme
(`backfill_id_scheme_aliases`) and stored-slug (`backfill_stored_slug_aliases`).
It replaced `ON CONFLICT (old_slug) DO NOTHING`, which froze a stale 301 target
forever once a re-mint moved a facility from slug S to S'.

What it has to do is NARROW: repoint an alias only where the SAME emitter wrote
it for the SAME facility, and touch nothing else.

    ON CONFLICT (old_slug) DO UPDATE
       SET canonical_slug = EXCLUDED.canonical_slug
     WHERE facility_slug_aliases.source = EXCLUDED.source
       AND facility_slug_aliases.facility_id IS NOT DISTINCT FROM EXCLUDED.facility_id
       AND facility_slug_aliases.canonical_slug IS DISTINCT FROM EXCLUDED.canonical_slug
    RETURNING 1

★ WHY THIS FILE EXISTS. That scoping was guarded only by source-text assertions
in tests/test_stored_slug_alias_gap.py and tests/test_seo_slug_and_soft404.py.
They prove the characters are present in the file. They cannot prove PostgreSQL
AGREES — that a 'gsc' row really is left alone by a 'stored-slug' emit, that
`IS NOT DISTINCT FROM` really does match NULL to NULL where `=` does not, or
that a re-emit of an unchanged target really returns ZERO rows so the caller's
`inserted += len(got or [])` cannot overstate what it moved. The semantics were
verified once by hand against a local PostgreSQL 18.6 cluster; a hand run is not
a lane. This file is the lane.

★ THE CLAUSE IS IMPORTED, NEVER RETYPED. A retyped copy keeps passing while the
real clause drifts — the exact way a parity test goes vacuous. Everything here
executes `_ALIAS_REPOINT` itself, through `execute_values(..., fetch=True)` with
the same `template` the emitters pass, so the statement under test is the
statement that ships.

Set ALIAS_REPOINT_SQL_DSN to run. CI's db-parity job sets it with
ALIAS_REPOINT_SQL_REQUIRE=1 and fails the job if anything here skipped.

Run:  ALIAS_REPOINT_SQL_DSN=postgresql:///alias_repoint_parity \\
        python3 -m pytest tests/test_alias_repoint_scoping_sql.py -v
"""
import os
import pathlib
import sys
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import execute_values            # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.facility_slug_freeze as freeze          # noqa: E402

DSN = os.environ.get("ALIAS_REPOINT_SQL_DSN", "").strip()
REQUIRE = os.environ.get("ALIAS_REPOINT_SQL_REQUIRE") == "1"

# The VALUES head the three emitters send. The part under test is the clause
# appended to it, and that comes from the module — never from this file.
_EMIT_HEAD = """
    INSERT INTO facility_slug_aliases (old_slug, canonical_slug, facility_id, source)
    VALUES %s
"""


@pytest.fixture
def db():
    if not DSN:
        pytest.skip("ALIAS_REPOINT_SQL_DSN not set — no Postgres to run against")
    name = f"alias_repoint_{uuid.uuid4().hex[:10]}"
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {name}")
    conn = psycopg2.connect(DSN, options=f"-c search_path={name}")
    # The table is built by the MODULE's own schema step, not by a retyped DDL
    # here: the clause conflicts on old_slug, and old_slug is only the arbiter
    # because that schema makes it the primary key.
    freeze.ensure_freeze_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"{name}.facility_slug_aliases",))
        assert cur.fetchone()[0] is not None, (
            "ensure_freeze_schema did not create facility_slug_aliases in the "
            "test schema — every case below would test an absent table")
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        admin.close()


def _seed(conn, rows):
    """Establish the pre-existing alias with a PLAIN insert.

    Deliberately not the clause under test: a mutation to _ALIAS_REPOINT must
    not be able to change the precondition it is then measured against.
    """
    with conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO facility_slug_aliases "
            "(old_slug, canonical_slug, facility_id, source) VALUES %s",
            rows, template="(%s, %s, %s, %s)")
    conn.commit()


def _emit(conn, rows, clause=None):
    """Run the emitters' OWN statement and return the rows it reports.

    Same shape as backfill_stored_slug_aliases: execute_values, the four-column
    template, fetch=True. `clause` overrides the module constant ONLY for the
    explicit `=` control in the NULL case.
    """
    with conn.cursor() as cur:
        got = execute_values(cur, _EMIT_HEAD + (clause or freeze._ALIAS_REPOINT),
                             rows, template="(%s, %s, %s, %s)", fetch=True)
    conn.commit()
    return list(got or [])


def _row(conn, old_slug):
    with conn.cursor() as cur:
        cur.execute("SELECT canonical_slug, facility_id, source "
                    "FROM facility_slug_aliases WHERE old_slug = %s", (old_slug,))
        return cur.fetchone()


# ── 1. it repoints what it owns ─────────────────────────────────────────────

def test_an_emitter_repoints_its_own_alias_for_its_own_facility(db):
    """The whole point: a re-mint moved F1 from canon-1 to canon-2, and the
    301 has to follow. Under DO NOTHING this row stayed on canon-1 forever."""
    _seed(db, [("old-a", "canon-1", "F1", "stored-slug")])

    got = _emit(db, [("old-a", "canon-2", "F1", "stored-slug")])

    assert len(got) == 1, f"the repoint reported {len(got)} rows, want 1"
    assert _row(db, "old-a") == ("canon-2", "F1", "stored-slug")


# ── 2. it does not cross sources ────────────────────────────────────────────

def test_a_gsc_alias_is_not_repointed_by_a_stored_slug_emit(db):
    """A 'gsc' alias records a URL Google actually indexed. A stored-slug
    backfill has no standing to move it, and must not report that it did."""
    _seed(db, [("old-b", "canon-1", "F1", "gsc")])

    got = _emit(db, [("old-b", "canon-2", "F1", "stored-slug")])

    assert got == [], f"a stored-slug emit moved a gsc alias: {got}"
    assert _row(db, "old-b") == ("canon-1", "F1", "gsc"), (
        "the gsc row changed — source is meant to fence the update, and the "
        "SET touches canonical_slug only")


# ── 3. it does not cross facilities ─────────────────────────────────────────

def test_another_facilitys_alias_is_not_repointed(db):
    """Same emitter, same old_slug, DIFFERENT facility. Repointing here would
    hand F1's inbound links to F2."""
    _seed(db, [("old-c", "canon-1", "F1", "stored-slug")])

    got = _emit(db, [("old-c", "canon-2", "F2", "stored-slug")])

    assert got == [], f"an emit for F2 moved F1's alias: {got}"
    assert _row(db, "old-c") == ("canon-1", "F1", "stored-slug")


# ── 4. the counter cannot overstate ─────────────────────────────────────────

def test_a_re_emit_of_the_same_target_returns_zero_rows(db):
    """`inserted += len(got or [])` is the emitters' counter. A no-op re-emit
    that still RETURNED would report movement on every idle run — the #4830
    counter-bug shape. The `IS DISTINCT FROM` predicate is what stops it.

    The fresh insert in the same statement is the control: a 0 that comes from
    a statement returning nothing at all would prove nothing.
    """
    _seed(db, [("old-d", "canon-1", "F1", "stored-slug")])

    got = _emit(db, [("old-d", "canon-1", "F1", "stored-slug"),
                     ("old-d-new", "canon-9", "F1", "stored-slug")])

    assert len(got) == 1, (
        f"want exactly the ONE genuinely new row, got {len(got)} — an "
        f"unchanged re-emit is being counted as movement")
    assert _row(db, "old-d-new") == ("canon-9", "F1", "stored-slug"), (
        "the fresh insert did not land, so the count above is vacuous")


# ── 5. why the predicate is IS NOT DISTINCT FROM ────────────────────────────

def test_null_facility_id_on_both_sides_still_matches(db):
    """id-scheme aliases can carry a NULL facility_id. Under plain `=` the
    predicate evaluates to NULL, the WHERE is not satisfied, and those rows
    freeze exactly as they did under DO NOTHING.

    The `=` control below is DERIVED from the shipping clause, so it cannot
    drift into agreeing with it by accident.
    """
    eq_control = freeze._ALIAS_REPOINT.replace("IS NOT DISTINCT FROM", "=")
    assert eq_control != freeze._ALIAS_REPOINT, (
        "the control is identical to the shipping clause — _ALIAS_REPOINT no "
        "longer spells 'IS NOT DISTINCT FROM' the way this control rewrites, "
        "so this case would compare the clause against itself")

    _seed(db, [("old-e", "canon-1", None, "id-scheme"),
               ("old-f", "canon-1", None, "id-scheme")])

    # The control first, on its own row: `=` must NOT match NULL to NULL.
    frozen = _emit(db, [("old-f", "canon-2", None, "id-scheme")], clause=eq_control)
    assert frozen == [], f"`=` matched NULL to NULL, which Postgres does not do: {frozen}"
    assert _row(db, "old-f")[0] == "canon-1"

    # The shipping clause on the same shape: it must match.
    got = _emit(db, [("old-e", "canon-2", None, "id-scheme")])
    assert len(got) == 1, (
        f"the shipping clause reported {len(got)} rows for a NULL/NULL "
        f"facility_id — a NULL-id alias can never be repointed")
    assert _row(db, "old-e") == ("canon-2", None, "id-scheme")


def test_a_null_facility_id_does_not_match_a_real_one(db):
    """IS NOT DISTINCT FROM is exact, not permissive: NULL matches only NULL.
    Without this, case 5 would also pass for a clause that dropped the
    facility_id predicate entirely."""
    _seed(db, [("old-g", "canon-1", None, "id-scheme")])

    got = _emit(db, [("old-g", "canon-2", "F1", "id-scheme")])

    assert got == [], f"an emit carrying F1 moved a NULL-id alias: {got}"
    assert _row(db, "old-g") == ("canon-1", None, "id-scheme")


# ── anti-skip ───────────────────────────────────────────────────────────────

def test_a_database_is_configured_when_ci_requires_one():
    if not REQUIRE:
        pytest.skip("ALIAS_REPOINT_SQL_DSN not set — no Postgres to run against")
    assert DSN, ("ALIAS_REPOINT_SQL_REQUIRE=1 but ALIAS_REPOINT_SQL_DSN is empty "
                 "— every test in this file would skip and prove nothing")
