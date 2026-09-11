#!/usr/bin/env python3
"""Pack credits never expire — against a REAL Postgres (2026-09-11).

tests/test_pack_credits_never_expire.py pins the SQL text against a fake cursor.
A fake cannot say whether the backfill's data-modifying CTE moves exactly the
pack rows, leaves the legacy tu- top-ups alone, restores a grant whose 90-day
clock already ran out, is idempotent, or runs on import — and it cannot say
whether a driver can load the instant back. This file runs the module's own
DDL and its own functions against a database.

Skips without PACK_EXPIRY_SQL_DSN. The db-parity job in pre-merge.yml sets it
and then FAILS if this file skipped. Owns and recreates only mcp_topups and
mcp_trial_emails.
"""
import datetime as dt
import importlib
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.mcp_conversion_plays as mcp  # noqa: E402

DSN = os.environ.get("PACK_EXPIRY_SQL_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="PACK_EXPIRY_SQL_DSN not set")

NEVER = dt.datetime(9999, 12, 31, tzinfo=dt.timezone.utc)


@pytest.fixture
def db(monkeypatch):
    import psycopg2
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mcp_topups, mcp_trial_emails CASCADE")
    monkeypatch.setattr(mcp, "_conn", lambda: psycopg2.connect(DSN))
    assert mcp.init_schema() is True, "the module's own DDL did not apply"
    yield conn
    conn.close()


def _seed(conn, key, source, expires_sql=None, remaining=1000):
    """One paid mcp_topups row as production holds it today. expires_sql=None
    takes the column DEFAULT, which is how the legacy tu- top-ups are written."""
    cols = "topup_token, api_key_hash, credits, price_cents, paid_at, credits_remaining, source"
    vals = "%s, %s, 1000, 1000, NOW() - INTERVAL '3 days', %s, %s"
    if expires_sql:
        cols += ", expires_at"
        vals += ", " + expires_sql
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO mcp_topups ({cols}) VALUES ({vals}) RETURNING id",
                    (f"{source or 'tu'}-{key}", mcp._hash_key(key), remaining, source))
        return cur.fetchone()[0]


def _expiry(conn, row_id):
    with conn.cursor() as cur:
        cur.execute("SELECT expires_at FROM mcp_topups WHERE id = %s", (row_id,))
        return cur.fetchone()[0]


def test_a_real_grant_never_expires_and_is_spendable(db):
    for i, source in enumerate(mcp.PACK_SOURCES):
        key = f"dch_live_grant_{i}"
        out = mcp.grant_credit_pack(key, None, 1000, stripe_session_id=f"cs_sql_{i}", source=source)
        assert out["ok"] and not out["idempotent"], (source, out)
        assert _expiry(db, out["topup_id"]).astimezone(dt.timezone.utc) == NEVER, source
        assert mcp.get_credit_balance(key, None) == 1000, source
        assert mcp.get_credit_status(key, None)["credits"] == 1000, source
        spent = mcp.consume_credits(key, None, 5)
        assert spent["ok"] and spent["remaining"] == 995, (source, spent)


def test_the_backfill_restores_exactly_the_pack_rows(db, capsys):
    rows = {
        "pack10_90d": _seed(db, "k-pack10", "pack10", "NOW() + INTERVAL '89 days'"),
        "keybound_lapsed": _seed(db, "k-lapsed", "pack5_keybound", "NOW() - INTERVAL '1 day'", remaining=700),
        "agentic": _seed(db, "k-agentic", "agentic_pack5", "NOW() + INTERVAL '60 days'"),
        "already_never": _seed(db, "k-never", "pack10", "'9999-12-31 00:00:00+00'::timestamptz"),
    }
    legacy_tu = _seed(db, "k-tu", None)                     # source NULL, 30-minute default
    tu_before = _expiry(db, legacy_tu)
    held = {row_id: _expiry(db, row_id) for row_id in rows.values()}
    assert mcp.get_credit_balance("k-lapsed", None) == 0, "control: the lapsed grant reads empty before"

    out = mcp.restore_pack_never_expires()
    assert out["ok"] and out["restored"] == 3, out            # pack10_90d, keybound_lapsed, agentic
    assert out["earliest_old_expiry"] < out["latest_old_expiry"], out

    # The audit trail: the log line names every moved id with the expiry it
    # held, read back from the database before the backfill — nothing else.
    line = next(l for l in capsys.readouterr().err.splitlines() if "restore_pack_never_expires" in l)
    logged = dict(item[len("id="):].split(":", 1)
                  for item in line.split("prior values: ", 1)[1].split(", "))
    moved = {rows["pack10_90d"], rows["keybound_lapsed"], rows["agentic"]}
    assert set(map(int, logged)) == moved, line
    for row_id in moved:
        assert dt.datetime.fromisoformat(logged[str(row_id)]) == held[row_id], (row_id, line)

    for name, row_id in rows.items():
        assert _expiry(db, row_id).astimezone(dt.timezone.utc) == NEVER, name
    assert _expiry(db, legacy_tu) == tu_before, "the backfill moved a legacy tu- top-up"
    assert mcp.get_credit_balance("k-lapsed", None) == 700, "the lapsed grant's credits did not come back"

    again = mcp.restore_pack_never_expires()
    assert again["ok"] and again["restored"] == 0, f"not idempotent: {again}"


def test_the_backfill_runs_on_import(db, monkeypatch):
    """The wire: nobody calls restore_pack_never_expires() by hand in production."""
    row_id = _seed(db, "k-import", "pack10", "NOW() + INTERVAL '30 days'")
    monkeypatch.setenv("DATABASE_URL", DSN)
    try:
        importlib.reload(mcp)
        assert mcp._PACK_EXPIRY_RESTORE.get("restored") == 1, mcp._PACK_EXPIRY_RESTORE
        assert _expiry(db, row_id).astimezone(dt.timezone.utc) == NEVER
    finally:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        importlib.reload(mcp)


def test_both_drivers_load_the_instant_and_infinity_would_not(db):
    """Why the instant is a date and not 'infinity'. The control half fails if a
    driver upgrade ever makes 'infinity' loadable — then the note is stale."""
    import psycopg
    out = mcp.grant_credit_pack("dch_live_drivers", None, 1000, stripe_session_id="cs_sql_drv",
                                source="pack10")
    with psycopg.connect(DSN) as c3:
        for tz in ("UTC", "Pacific/Kiritimati", "America/Phoenix"):
            c3.execute(f"SET TIME ZONE '{tz}'")
            loaded = c3.execute("SELECT expires_at FROM mcp_topups WHERE id = %s",
                                (out["topup_id"],)).fetchone()[0]
            assert loaded.astimezone(dt.timezone.utc) == NEVER, tz
        with pytest.raises(psycopg.DataError):
            c3.execute("SELECT 'infinity'::timestamptz").fetchone()
