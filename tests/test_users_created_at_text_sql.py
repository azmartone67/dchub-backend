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


def _load_blast_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "email_blast_developer_launch", ROOT / "tools" / "email_blast_developer_launch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("last_used_type", ["TIMESTAMPTZ", "TEXT"])
def test_email_blast_fetch_targets_runs_on_text_created_at(conn, last_used_type):
    # tools/email_blast_developer_launch.py filtered `u.created_at > NOW() - …`
    # in HAVING (and had bare '%' in its LIKEs), so it raised before sending
    # anything. api_keys.last_used_at's type differs between the DDLs that
    # create api_keys, so both are covered.
    blast = _load_blast_tool()
    now = dt.datetime.now(UTC)

    def ago(days, fmt="naive"):
        t = now - dt.timedelta(days=days)
        return (t.replace(tzinfo=None).isoformat() if fmt == "naive"
                else t.strftime("%Y-%m-%d %H:%M:%S.%f+00"))

    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE api_keys ADD COLUMN last_used_at {last_used_type}")
        blast.ensure_audit_table(conn)

        def user(uid, created_at, plan="free", email=None, used_days_ago=None):
            cur.execute("INSERT INTO users (id, email, password_hash, plan, created_at) "
                        "VALUES (%s, %s, 'x', %s, %s)",
                        (uid, email or f"{uid}@corp.io", plan, created_at))
            if used_days_ago is not None:
                cur.execute("INSERT INTO api_keys (user_id, last_used_at) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (uid, (now - dt.timedelta(days=used_days_ago)).isoformat()))

        user("recentnaive", ago(1))
        user("recentpg", ago(5, "pg"))
        user("oldused", ago(90), used_days_ago=10)       # old signup, recent use
        user("nulledused", None, used_days_ago=3)        # no signup date, recent use
        user("oldunused", ago(90), used_days_ago=100)
        user("stale31", ago(31))
        user("garbage", "not-a-date")
        user("blank", "")
        user("nulled", None)
        user("paid", ago(1), plan="developer")
        user("example", ago(1), email="a@example.com")
        user("testy", ago(1), email="qa-test@corp.io")
        user("mailed", ago(1))
        cur.execute("INSERT INTO email_blasts (user_id, email, campaign, status) "
                    "VALUES ('mailed', 'mailed@corp.io', 'c1', 'sent')")

    got = blast.fetch_targets(conn, "c1", 50, now=now)
    assert [t["user_id"] for t in got] == [
        "recentnaive", "recentpg", "oldused", "nulledused"]
    assert blast.humanize_signup(got[0]["created_at"]) == "1 days ago"

    assert [t["user_id"] for t in blast.fetch_targets(conn, "c1", 2, now=now)] == [
        "recentnaive", "recentpg"]
    # Another campaign's ledger does not exclude 'mailed'.
    assert "mailed" in [t["user_id"] for t in blast.fetch_targets(conn, "c2", 50, now=now)]
