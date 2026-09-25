#!/usr/bin/env python3
"""users.created_at is TEXT — readers that date-filter it, on a real Postgres.

2026-09-25: POST /api/v1/admin/activation-emails/run failed on every
cron_heartbeat tick (:01 and :06 past each hour) with

    psycopg2.errors.UndefinedFunction: operator does not exist:
    text >= timestamp with time zone

because CANDIDATES_SQL filtered `u.created_at >= NOW() - …` and users.created_at
is declared TEXT (main.py's CREATE TABLE users). Nothing drifted: it had failed
since it shipped (2026-09-01). tests/test_activation_emails.py fed
fetch_candidates' OUTPUT to due_steps and never ran the SQL. crm_reverse_etl's
paid-conversion backfill had the same predicate with a datetime param, and its
except swallowed the error, so it always counted 0.

The users table is built from main.py's own DDL (read as text, never imported),
so the column type this file tests is the one production declares. Rows carry
the formats production holds: bare ISO (utcnow), Postgres '+00' text, another
offset, NULL, '' and garbage.

Skips without USERS_CREATED_AT_SQL_DSN; the db-parity job in pre-merge.yml sets
it and FAILS if this file skipped. Everything lives in its own schema, dropped
afterwards.
"""
import datetime as dt
import os
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("USERS_CREATED_AT_SQL_DSN")
SCHEMA = "users_created_at_text"
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _in_schema(dsn):
    sep = "&" if "?" in dsn else "?"
    return f"{dsn}{sep}options=-csearch_path%3D{SCHEMA}"


def _users_ddl() -> list[str]:
    """main.py's CREATE TABLE users plus every ALTER … ADD COLUMN it runs."""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    m = re.search(r"CREATE TABLE IF NOT EXISTS users \((.*?)\n\s*\)\s*\"\"\"", src, re.S)
    assert m, "main.py no longer declares CREATE TABLE IF NOT EXISTS users"
    stmts = [f"CREATE TABLE users ({m.group(1)})"]
    stmts += sorted(set(re.findall(
        r"\"(ALTER TABLE users ADD COLUMN IF NOT EXISTS [^\"]+)\"", src)))
    return stmts


@pytest.fixture
def conn(monkeypatch):
    if not DSN:
        pytest.skip("USERS_CREATED_AT_SQL_DSN not set")
    import psycopg2
    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=15000")
    admin = psycopg2.connect(DSN)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    c = psycopg2.connect(_in_schema(DSN))
    c.autocommit = True
    with c.cursor() as cur:
        for s in _users_ddl():
            cur.execute(s)
        cur.execute("CREATE TABLE api_keys (id SERIAL PRIMARY KEY, user_id TEXT, "
                    "calls_total INTEGER DEFAULT 0)")
        cur.execute("CREATE TABLE mcp_dev_keys (api_key TEXT PRIMARY KEY, email TEXT, "
                    "status TEXT, last_used_at TIMESTAMPTZ, "
                    "created_at TIMESTAMPTZ DEFAULT NOW())")
    try:
        yield c
    finally:
        c.close()
        with admin.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        admin.close()


def _user(cur, uid, created_at, plan="pro", stripe="cus_x", status="active"):
    cur.execute("INSERT INTO users (id, email, password_hash, plan, created_at, "
                "stripe_customer_id, subscription_status) "
                "VALUES (%s, %s, 'x', %s, %s, %s, %s)",
                (uid, f"{uid}@t.test", plan, created_at, stripe, status))


def test_fixture_column_is_text_like_production(conn):
    # If main.py ever declares timestamptz, this file must be revisited, not
    # silently start passing for a different reason.
    with conn.cursor() as cur:
        cur.execute("SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'users' "
                    "AND column_name = 'created_at'", (SCHEMA,))
        assert cur.fetchone()[0] == "text"


def _seed_activation(cur):
    _user(cur, "naive26h", "2026-09-24T10:00:00.123456")          # bare ISO, UTC
    _user(cur, "pg5d", "2026-09-20 12:00:00.5+00", plan="developer")
    _user(cur, "minus5", "2026-09-25 03:00:00-05")                 # = 08:00Z, 4h old
    _user(cur, "old24d", "2026-09-01T00:00:00")
    _user(cur, "nulled", None)
    _user(cur, "blank", "")
    _user(cur, "garbage", "not-a-date")
    _user(cur, "freeplan", "2026-09-24T10:00:00", plan="free")
    _user(cur, "nostripe", "2026-09-24T10:00:00", stripe=None)
    cur.execute("INSERT INTO mcp_dev_keys (api_key, email, status) "
                "VALUES ('dch_live_a', 'naive26h@t.test', 'active')")


@pytest.mark.parametrize("dict_rows", [False, True])
def test_fetch_candidates_runs_on_text_created_at(conn, dict_rows):
    from psycopg2.extras import RealDictCursor
    from routes import activation_emails as ae
    with conn.cursor() as cur:
        _seed_activation(cur)
    with conn.cursor(cursor_factory=RealDictCursor if dict_rows else None) as cur:
        got = ae.fetch_candidates(cur, now=NOW)
    assert [c["customer_id"] for c in got] == ["pg5d", "naive26h", "minus5"]
    assert [c["created_at"] for c in got] == [
        dt.datetime(2026, 9, 20, 12, 0, 0, 500000, tzinfo=UTC),
        dt.datetime(2026, 9, 24, 10, 0, 0, 123456, tzinfo=UTC),
        dt.datetime(2026, 9, 25, 8, 0, tzinfo=UTC),
    ]
    assert got[1]["mcp_key"] == "dch_live_a"


def test_disarmed_sweep_reports_would_send_and_writes_nothing(conn):
    from routes import activation_emails as ae
    with conn.cursor() as cur:
        _seed_activation(cur)
    sent = []
    out = ae.run_sweep(conn, sender=lambda *a: sent.append(a) or (True, ""),
                       now=NOW, armed=False)
    assert out["ok"] is True
    assert out["candidates"] == 3
    assert sorted((w["customer_id"], w["step"]) for w in out["would_send"]) == [
        ("naive26h", ae.STEP_DAY1), ("pg5d", ae.STEP_DAY3)]
    assert {(s["customer_id"], s["reason"]) for s in out["skips"]} == {
        ("pg5d", "no_mcp_key")}
    assert sent == []
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('activation_email_ledger')")
        assert cur.fetchone()[0] is None


def test_crm_paid_conversion_backfill_reads_text_created_at(conn, monkeypatch):
    from routes import crm_reverse_etl as crm
    now = dt.datetime.now(UTC)
    with conn.cursor() as cur:
        _user(cur, "recentnaive",
              (now - dt.timedelta(days=1)).replace(tzinfo=None).isoformat())
        _user(cur, "recentpg", (now - dt.timedelta(days=2)).strftime(
            "%Y-%m-%d %H:%M:%S.%f+00"))
        _user(cur, "stale", (now - dt.timedelta(days=30)).replace(tzinfo=None).isoformat())
        _user(cur, "nulled", None)
        _user(cur, "garbage", "not-a-date")
        _user(cur, "canceled", (now - dt.timedelta(days=1)).isoformat(), status="canceled")
    captured = []
    monkeypatch.setattr(crm, "_conn", lambda: conn)
    monkeypatch.setattr(crm, "_return", lambda c, error=False: None)
    monkeypatch.setattr(crm, "_ensure_schema", lambda c: None)
    monkeypatch.setattr(crm, "capture_event",
                        lambda et, p: captured.append((et, p["email"])) or {"ok": True})
    out = crm.backfill_last_n_days(days=7)
    assert out["ok"] is True
    assert sorted(e for et, e in captured if et == "paid_conversion") == [
        "recentnaive@t.test", "recentpg@t.test"]
    assert out["counts"]["paid_conversion"] == 2
