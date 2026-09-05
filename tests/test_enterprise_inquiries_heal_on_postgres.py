"""The enterprise_inquiries heal, EXECUTED against a real Postgres.

r-inquiry-schema-fork (2026-09-05). tests/test_enterprise_inquiries_schema_fork.py
reasons about the SQL as text: it checks that every inserted column is declared
and that the heal adds each one. That catches a column going missing, but it
cannot tell you the DDL is even valid, that ALTER ... DROP NOT NULL is legal on
a column that arrived NOT NULL, or that running the heal twice is safe. Those
are claims about Postgres, and only Postgres can settle them.

This matters more than usual here because the heal runs LAZILY: both callers
invoke it inside a POST handler, so nothing executes this SQL until a real
enterprise lead arrives. Without this file, a live customer submission would
be the first thing ever to run it.

OPT-IN. Set DCHUB_PG_TEST_DSN to a throwaway database and it runs; otherwise
it skips. It DROPs and recreates enterprise_inquiries, so never point it at
anything you care about — it refuses a DSN that looks like production.

    initdb -D /tmp/pgt -U postgres --auth=trust
    LC_ALL=C pg_ctl -D /tmp/pgt -o "-p 55432 -k /tmp/pgsock" -l /tmp/pgt/log start
    DCHUB_PG_TEST_DSN="host=127.0.0.1 port=55432 user=postgres dbname=postgres" \
      python3 -m pytest tests/test_enterprise_inquiries_heal_on_postgres.py -v

Recorded result 2026-09-05 (PostgreSQL 18.6), both directions:

    enterprise.py won         pre-heal inquiry.py   INSERT -> UndefinedColumn: tier_requested
    enterprise_inquiry.py won pre-heal enterprise.py INSERT -> UndefinedColumn: org_name
    after heal, both writers insert from either starting schema; 18 columns
    either way; heal re-run is a no-op; status backfill covers pre-existing rows.
"""
import os

import pytest

DSN = os.environ.get("DCHUB_PG_TEST_DSN", "")
if DSN and any(x in DSN.lower() for x in ("neon", "azure", "amazonaws",
                                          "railway", "prod")):
    raise RuntimeError(
        "DCHUB_PG_TEST_DSN looks like a real database; this file DROPs tables")

pytestmark = pytest.mark.skipif(
    not DSN, reason="set DCHUB_PG_TEST_DSN to a throwaway Postgres to run")

psycopg2 = pytest.importorskip("psycopg2")

from util.enterprise_inquiries_schema import (  # noqa: E402
    HEAL_SQL, INDEX_SQL, SCHEMA_SQL, declared_columns)

# The two June definitions, verbatim in shape.
DDL_ENTERPRISE = """
CREATE TABLE enterprise_inquiries (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    org_name TEXT NOT NULL,
    email TEXT NOT NULL,
    use_case TEXT NOT NULL,
    expected_volume TEXT NOT NULL,
    source_ip TEXT, user_agent TEXT, relay_status TEXT
);"""
DDL_INQUIRY = """
CREATE TABLE enterprise_inquiries (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tier_requested TEXT, name TEXT, email TEXT, firm TEXT, use_case TEXT,
    notes TEXT, source TEXT, ip_hash TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    contacted_at TIMESTAMPTZ, notes_admin TEXT
);"""

INS_ENTERPRISE = """INSERT INTO enterprise_inquiries
  (org_name, email, use_case, expected_volume, source_ip, user_agent, relay_status)
  VALUES ('Acme','a@b.co','sizing','10k','1.2.3.4','ua','sent')"""
INS_INQUIRY = """INSERT INTO enterprise_inquiries
  (tier_requested, name, email, firm, use_case, notes, source, ip_hash)
  VALUES ('pro','Jo','j@b.co','Firm','sizing','n','enterprise_page','h')"""


@pytest.fixture()
def cur():
    c = psycopg2.connect(DSN)
    c.autocommit = True
    with c.cursor() as k:
        yield k
    c.close()


def _columns(k):
    k.execute("SELECT column_name FROM information_schema.columns "
              "WHERE table_name='enterprise_inquiries'")
    return {r[0] for r in k.fetchall()}


def _heal(k):
    k.execute(SCHEMA_SQL)
    k.execute(HEAL_SQL)
    k.execute(INDEX_SQL)


def _start_from(k, ddl, seed):
    k.execute("DROP TABLE IF EXISTS enterprise_inquiries CASCADE")
    k.execute(ddl)
    k.execute(seed)          # a pre-existing real submission


@pytest.mark.parametrize("label,ddl,seed,broken", [
    ("enterprise.py won", DDL_ENTERPRISE, INS_ENTERPRISE, INS_INQUIRY),
    ("enterprise_inquiry.py won", DDL_INQUIRY, INS_INQUIRY, INS_ENTERPRISE),
])
def test_the_fork_really_breaks_the_other_writer(cur, label, ddl, seed, broken):
    """★ The bug, reproduced. Whichever DDL won, the OTHER writer's INSERT
    raises UndefinedColumn. If this ever stops failing, the premise of the
    whole fix is gone and the heal tests below prove nothing."""
    _start_from(cur, ddl, seed)
    with pytest.raises(psycopg2.errors.UndefinedColumn):
        cur.execute(broken)


@pytest.mark.parametrize("label,ddl,seed", [
    ("enterprise.py won", DDL_ENTERPRISE, INS_ENTERPRISE),
    ("enterprise_inquiry.py won", DDL_INQUIRY, INS_INQUIRY),
])
def test_the_heal_converges_from_either_history(cur, label, ddl, seed):
    """Both starting schemas end at the same table, and BOTH writers work."""
    _start_from(cur, ddl, seed)
    _heal(cur)
    missing = declared_columns() - _columns(cur)
    assert not missing, f"{label}: heal left {sorted(missing)} missing"
    cur.execute(INS_ENTERPRISE)
    cur.execute(INS_INQUIRY)


@pytest.mark.parametrize("ddl,seed", [(DDL_ENTERPRISE, INS_ENTERPRISE),
                                      (DDL_INQUIRY, INS_INQUIRY)])
def test_the_heal_is_idempotent(cur, ddl, seed):
    """It runs on a request path; a second run must be a no-op, not an error."""
    _start_from(cur, ddl, seed)
    _heal(cur)
    first = _columns(cur)
    _heal(cur)
    assert _columns(cur) == first


def test_a_preexisting_row_gets_a_status(cur):
    """Rows written before the heal have no status column at all. They must
    come out with one, or the histogram reads NULL for real submissions."""
    _start_from(cur, DDL_ENTERPRISE, INS_ENTERPRISE)
    _heal(cur)
    cur.execute("SELECT count(*), count(status) FROM enterprise_inquiries")
    total, with_status = cur.fetchone()
    assert total >= 1 and total == with_status


def test_an_explicitly_null_status_is_backfilled(cur):
    """★ What the UPDATE is actually for, and the case that was missing.

    On the ADD COLUMN path Postgres backfills existing rows from the column's
    DEFAULT by itself, so deleting the UPDATE left every other test in this
    file green — it survived mutation. The statement earns its place only
    when `status` already EXISTS and holds NULLs, which a row written
    straight through the nullable column does. Without this case the backfill
    is unguarded, and a reader would get {None: n} back into
    util.status_taxonomy.status_histogram."""
    _start_from(cur, DDL_INQUIRY, INS_INQUIRY)
    cur.execute("ALTER TABLE enterprise_inquiries ALTER COLUMN status DROP NOT NULL")
    cur.execute("INSERT INTO enterprise_inquiries (email, status) VALUES ('n@b.co', NULL)")
    cur.execute("SELECT count(*) FROM enterprise_inquiries WHERE status IS NULL")
    assert cur.fetchone()[0] == 1, "the NULL seed did not land; test proves nothing"
    _heal(cur)
    cur.execute("SELECT count(*) FROM enterprise_inquiries WHERE status IS NULL")
    assert cur.fetchone()[0] == 0, (
        "a NULL status survived the heal — status_histogram would key it None")


def test_the_heal_preserves_existing_rows(cur):
    """No DROP COLUMN, no data loss — both column sets carry real leads."""
    _start_from(cur, DDL_ENTERPRISE, INS_ENTERPRISE)
    cur.execute("SELECT count(*) FROM enterprise_inquiries")
    before = cur.fetchone()[0]
    _heal(cur)
    cur.execute("SELECT count(*), org_name FROM enterprise_inquiries GROUP BY org_name")
    after, org = cur.fetchone()
    assert after == before and org == "Acme"
