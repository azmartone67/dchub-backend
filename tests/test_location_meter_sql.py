#!/usr/bin/env python3
"""The exact-location meter against a REAL Postgres (2026-09-21).

A fake cursor cannot say whether the meter's statement enforces the monthly
limit, frees a re-view, resets on the 1st, or — the part that matters most —
stays within the limit when two reveals race. So this file applies the shipped
migration and drives the shipped util/location_meter.py functions against a
database.

★ THE RACE. The obvious single statement (INSERT ... SELECT ... WHERE count <
limit ON CONFLICT DO NOTHING) lets two concurrent reveals of DIFFERENT
facilities both pass at used=9: each counts with its own READ COMMITTED
snapshot. Measured on Postgres 18 while writing this: with the advisory lock
removed, the interleaving below ends the month at 11 rows and eight concurrent
reveals at used=9 all succeed (17 rows). With it, 10 and exactly one.

It also runs the reveal endpoint's operator-withheld query
(routes/facility_location_reveal._withheld) against the redaction registry's
real SQL functions, inside a private schema.

Skips without LOCATION_METER_SQL_DSN. The db-parity job in pre-merge.yml sets
it and then FAILS if this file skipped. Owns and recreates only
facility_location_reveals and the schema reveal_redaction_test.
"""
import datetime as dt
import os
import pathlib
import sys
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from util import location_meter as m  # noqa: E402

DSN = os.environ.get("LOCATION_METER_SQL_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="LOCATION_METER_SQL_DSN not set")

MIGRATION = ROOT / "migrations" / "2026-09-21_facility_location_reveals.sql"
SEPT = dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.timezone.utc)
OCT = dt.datetime(2026, 10, 1, 0, 0, 1, tzinfo=dt.timezone.utc)
ACCT = "reveal-test@example.com"


def _connect(autocommit=False):
    import psycopg2
    c = psycopg2.connect(DSN)
    c.autocommit = autocommit
    return c


def _apply_migration():
    with _connect(True) as c:
        c.cursor().execute(MIGRATION.read_text(encoding="utf-8"))


@pytest.fixture
def db(monkeypatch):
    monkeypatch.delenv(m.LIMIT_ENV, raising=False)
    with _connect(True) as c:
        c.cursor().execute("DROP TABLE IF EXISTS facility_location_reveals")
    _apply_migration()
    # The module's own connection factory imports main; hand it a real one.
    monkeypatch.setattr(m, "_write_conn", lambda: _connect())
    yield
    with _connect(True) as c:
        c.cursor().execute("DROP TABLE IF EXISTS facility_location_reveals")


def _rows(account=ACCT, period="2026-09"):
    with _connect(True) as c:
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM facility_location_reveals "
                    "WHERE account = %s AND period = %s", (account, period))
        return cur.fetchone()[0]


def _seed(n, account=ACCT, period="2026-09"):
    with _connect(True) as c:
        cur = c.cursor()
        for i in range(n):
            cur.execute("INSERT INTO facility_location_reveals "
                        "(account, facility_key, period, channel) "
                        "VALUES (%s, %s, %s, 'web')", (account, f"seed-{i:02d}", period))


def test_the_migration_is_idempotent(db):
    _apply_migration()                       # a second apply must be a no-op
    assert m.consume(ACCT, "fac-a", "web", now=SEPT)["allowed"] is True
    _apply_migration()
    assert _rows() == 1, "re-applying the migration must not touch existing rows"


def test_the_tenth_distinct_reveal_is_allowed_and_the_eleventh_refused(db):
    results = [m.consume(ACCT, f"fac-{i:02d}", "web", now=SEPT) for i in range(11)]
    for i, r in enumerate(results[:10]):
        assert r["allowed"] and r["consumed"] and r["measured"], (i, r)
        assert r["used"] == i + 1 and r["remaining"] == 10 - (i + 1), (i, r)
    eleventh = results[10]
    assert eleventh["allowed"] is False and eleventh["consumed"] is False, eleventh
    assert eleventh["measured"] is True, "a refusal is a measurement, not an outage"
    assert (eleventh["used"], eleventh["remaining"]) == (10, 0), eleventh
    assert _rows() == 10, "a refused reveal must not write"


def test_a_re_reveal_in_the_same_month_is_free(db):
    for i in range(10):
        assert m.consume(ACCT, f"fac-{i:02d}", "web", now=SEPT)["allowed"]
    again = m.consume(ACCT, "fac-03", "api", now=SEPT)
    assert again["allowed"] and again["already_revealed"], again
    assert again["consumed"] is False and again["used"] == 10, again
    assert _rows() == 10
    # ...and peek agrees, while a NEW facility at the limit is refused.
    assert m.peek(ACCT, "fac-03", now=SEPT)["allowed"] is True
    assert m.peek(ACCT, "fac-new", now=SEPT)["allowed"] is False


def test_the_same_facility_twice_below_the_limit_costs_one(db):
    first = m.consume(ACCT, "fac-a", "web", now=SEPT)
    second = m.consume(ACCT, "fac-a", "mcp", now=SEPT)
    assert first["consumed"] and not second["consumed"]
    assert second["already_revealed"] and second["used"] == 1
    assert _rows() == 1


def test_a_new_month_resets_the_allowance(db):
    for i in range(10):
        m.consume(ACCT, f"fac-{i:02d}", "web", now=SEPT)
    assert m.consume(ACCT, "fac-new", "web", now=SEPT)["allowed"] is False
    october = m.consume(ACCT, "fac-new", "web", now=OCT)
    assert october["allowed"] and october["used"] == 1, october
    assert october["period"] == "2026-10"
    assert october["resets_at"] == "2026-11-01T00:00:00Z"
    # A facility revealed in September is NOT free in October.
    assert m.peek(ACCT, "fac-00", now=OCT)["already_revealed"] is False


def test_accounts_do_not_share_an_allowance(db):
    _seed(10)
    other = m.consume("key:0123456789abcdef", "fac-a", "api", now=SEPT)
    assert other["allowed"] and other["used"] == 1


def test_peek_never_writes(db):
    for _ in range(3):
        r = m.peek(ACCT, "fac-a", now=SEPT)
        assert r["allowed"] and r["used"] == 0 and r["measured"]
    assert _rows() == 0


def test_the_limit_comes_from_the_environment(db, monkeypatch):
    monkeypatch.setenv(m.LIMIT_ENV, "2")
    assert m.consume(ACCT, "a", "web", now=SEPT)["allowed"]
    assert m.consume(ACCT, "b", "web", now=SEPT)["allowed"]
    third = m.consume(ACCT, "c", "web", now=SEPT)
    assert third["allowed"] is False and third["limit"] == 2
    monkeypatch.setenv(m.LIMIT_ENV, "0")
    assert m.consume(ACCT, "a", "web", now=OCT)["allowed"] is False


def test_the_channel_is_recorded(db):
    m.consume(ACCT, "fac-a", "mcp", now=SEPT)
    m.consume(ACCT, "fac-b", "bogus", now=SEPT)      # coerced, never rejected
    with _connect(True) as c:
        cur = c.cursor()
        cur.execute("SELECT facility_key, channel FROM facility_location_reveals "
                    "ORDER BY facility_key")
        assert cur.fetchall() == [("fac-a", "mcp"), ("fac-b", "api")]


def test_two_concurrent_reveals_at_nine_only_one_succeeds(db):
    """The race, forced deterministically.

    A charges facility A inside a transaction it has not committed; B then tries
    facility B. B must WAIT on the (account, period) lock, and once A commits it
    must see 10 and be refused. Without the lock B does not wait, counts 9 with
    its own snapshot, inserts, and the month ends at 11."""
    _seed(9)
    a = _connect()
    cur = a.cursor()
    cur.execute(m._CONSUME_SQL, (m._LOCK_NAMESPACE, m._lock_key(ACCT, "2026-09"),
                                 ACCT, "fac-A", "2026-09", "web", 10))
    assert cur.fetchone() == (9, False, True), "A should be the 10th reveal"
    out = {}

    def reveal_b():
        out["b"] = m.consume(ACCT, "fac-B", "web", now=SEPT)

    t = threading.Thread(target=reveal_b)
    t.start()
    time.sleep(0.7)
    waited = t.is_alive()
    a.commit()
    a.close()
    t.join(10)
    assert waited, "B did not wait for A — the reveals are not serialised"
    assert out["b"]["allowed"] is False, out["b"]
    assert out["b"]["used"] == 10, out["b"]
    assert _rows() == 10, "the month went over its limit"


def test_many_concurrent_reveals_at_nine_grant_exactly_one(db):
    _seed(9)
    n = 8
    barrier = threading.Barrier(n)
    allowed = []

    def worker(i):
        c = _connect()
        try:
            barrier.wait()
            allowed.append(m.consume(ACCT, f"fac-{i}", "api", conn=c, now=SEPT)["allowed"])
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert sorted(allowed) == [False] * (n - 1) + [True], allowed
    assert _rows() == 10


def test_a_lock_wait_is_bounded_and_fails_closed(db):
    """SET LOCAL lock_timeout must take effect even on an autocommit connection
    (the statements are one implicit transaction) — else a stuck holder parks
    every reveal for that account behind statement_timeout."""
    holder = _connect()
    holder.cursor().execute("SELECT pg_advisory_xact_lock(%s, %s)",
                            (m._LOCK_NAMESPACE, m._lock_key(ACCT, "2026-09")))
    try:
        started = time.time()
        r = m.consume(ACCT, "fac-a", "web", conn=_connect(True), now=SEPT)
        waited = time.time() - started
    finally:
        holder.rollback()
        holder.close()
    assert r["allowed"] is False and r["measured"] is False, r
    assert 4.0 <= waited < 15.0, f"lock wait was {waited:.1f}s, not the 5s bound"


def test_a_missing_table_fails_closed_and_never_raises(db):
    with _connect(True) as c:
        c.cursor().execute("DROP TABLE facility_location_reveals")
    for call in (lambda: m.consume(ACCT, "fac-a", "web", now=SEPT),
                 lambda: m.peek(ACCT, "fac-a", now=SEPT)):
        r = call()
        assert r["allowed"] is False, r
        assert r["measured"] is False, r
        assert r["used"] is None and r["remaining"] == 0, r


# ── the operator-withheld check, against the registry's own SQL ─────────────
# migrations/2026-09-21_facility_location_redactions.sql owns these functions.
# Until that migration is on this branch the test creates the two functions
# from the same definitions (registry identities: df:/f:<id>, slug:, src: with
# the pdb_ prefix stripped, url:). Once the file exists it is applied verbatim.
REDACTION_MIGRATION = ROOT / "migrations" / "2026-09-21_facility_location_redactions.sql"
REDACTION_FUNCTIONS = """
CREATE TABLE IF NOT EXISTS facility_location_redactions (
    match_key TEXT PRIMARY KEY, reason TEXT NOT NULL DEFAULT 'operator_request',
    note TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE OR REPLACE FUNCTION facility_location_redaction_keys(
        id_prefix TEXT, row_id TEXT, canonical_slug TEXT,
        source TEXT, source_id TEXT, source_url TEXT)
RETURNS TEXT[] LANGUAGE sql IMMUTABLE AS $fn$
    SELECT ARRAY[
        id_prefix || ':' || btrim(row_id),
        'slug:' || NULLIF(btrim(canonical_slug), ''),
        'src:' || lower(btrim(source)) || ':'
               || regexp_replace(NULLIF(btrim(source_id), ''), '^pdb_', ''),
        'url:' || NULLIF(btrim(source_url), '')]
$fn$;
CREATE OR REPLACE FUNCTION facility_location_is_redacted(keys TEXT[])
RETURNS boolean LANGUAGE sql STABLE AS $fn$
    SELECT EXISTS (SELECT 1 FROM facility_location_redactions
                    WHERE match_key = ANY (keys))
$fn$;
"""
SCHEMA = "reveal_redaction_test"


def _schema_conn(autocommit=True):
    import psycopg2
    c = psycopg2.connect(DSN, options=f"-c search_path={SCHEMA}")
    c.autocommit = autocommit
    return c


@pytest.fixture
def redaction(monkeypatch):
    import routes.facility_location_reveal as rev
    with _connect(True) as c:
        cur = c.cursor()
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    with _schema_conn() as c:
        cur = c.cursor()
        cur.execute("""
            CREATE TABLE discovered_facilities (
                id SERIAL PRIMARY KEY, name TEXT, provider TEXT, canonical_slug TEXT,
                source TEXT, source_id TEXT, source_url TEXT,
                latitude DOUBLE PRECISION, longitude DOUBLE PRECISION, address TEXT,
                raw_data JSONB, substation_band TEXT);
            CREATE TABLE facilities (
                id TEXT PRIMARY KEY, name TEXT, provider TEXT, canonical_slug TEXT,
                source TEXT, source_id TEXT, source_url TEXT,
                latitude DOUBLE PRECISION, longitude DOUBLE PRECISION,
                lat DOUBLE PRECISION, lon DOUBLE PRECISION, address TEXT,
                raw_data JSONB, substation_band TEXT);
            CREATE TABLE carrier_facility_presence (
                dchub_facility_id TEXT, facility_pdb_id TEXT,
                facility_lat DOUBLE PRECISION, facility_lng DOUBLE PRECISION);
        """)
        cur.execute(REDACTION_MIGRATION.read_text(encoding="utf-8")
                    if REDACTION_MIGRATION.exists() else REDACTION_FUNCTIONS)
        cur.execute("""
            INSERT INTO discovered_facilities (id, name, canonical_slug, source, source_id, latitude, longitude)
            VALUES (1, 'A', 'acme-alpha-one-1a2b3c4d', 'osm', 'n1', 1.5, 2.5) ON CONFLICT DO NOTHING,
                   (2, 'B', 'acme-beta-2b3c4d5e', 'PeeringDB', 'pdb_123', 1.5, 2.5),
                   (3, 'C', 'acme-gamma-3c4d5e6f', 'osm', 'n3', 1.5, 2.5);
            INSERT INTO facilities (id, name, canonical_slug, source, source_id, source_url, latitude, longitude)
            VALUES ('legacy-7', 'D', NULL, 'osm', 'n7', NULL, 1.5, 2.5) ON CONFLICT DO NOTHING,
                   ('legacy-8', 'E', NULL, 'osm', 'n8', 'https://example.invalid/withheld', 1.5, 2.5),
                   ('legacy-9', 'F', NULL, 'osm', 'n9', 'https://example.invalid/ok', 1.5, 2.5);
        """)
        # Registered AFTER the rows exist, so the migration's write-side
        # triggers (when present) have not yet cleared anything: this tests
        # that the READ query builds every identity. Once a redacted row is
        # touched, the trigger NULLs its source_url, and a redaction registered
        # by url: ALONE can no longer be recognised on read — the row then
        # answers 'unknown' (its coordinates are NULLed too), never a location.
        cur.execute("INSERT INTO facility_location_redactions (match_key) VALUES "
                    "('slug:acme-alpha-one-1a2b3c4d'), ('src:peeringdb:123'), "
                    "('url:https://example.invalid/withheld'), ('f:legacy-7')")
    monkeypatch.setattr(rev, "_read_conn", lambda: _schema_conn(False))
    rev._probe.update(ok=None, at=0.0)
    yield rev
    rev._probe.update(ok=None, at=0.0)
    with _connect(True) as c:
        c.cursor().execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


@pytest.mark.parametrize("table,row_id,expected", [
    ("discovered_facilities", 1, True),     # slug: identity
    ("discovered_facilities", 2, True),     # src:peeringdb:<id>, pdb_ prefix stripped
    ("discovered_facilities", 3, False),
    ("facilities", "legacy-7", True),       # f:<id> identity
    ("facilities", "legacy-8", True),       # url: identity
    ("facilities", "legacy-9", False),
])
def test_the_withheld_query_matches_every_registry_identity(redaction, table, row_id, expected):
    assert redaction._withheld({"_src_table": table, "id": row_id}) is expected


def test_the_withheld_check_fails_soft_without_the_registry(redaction):
    with _schema_conn() as c:
        c.cursor().execute("DROP FUNCTION facility_location_is_redacted(text[]) CASCADE")
    redaction._probe.update(ok=None, at=0.0)
    assert redaction._withheld({"_src_table": "discovered_facilities", "id": 1}) is False
    assert redaction._probe["ok"] is False, "the probe must say the registry is absent"


def test_an_unknown_table_or_missing_id_is_never_withheld(redaction):
    assert redaction._withheld({"_src_table": "somewhere_else", "id": 1}) is False
    assert redaction._withheld({"_src_table": "discovered_facilities", "id": None}) is False
