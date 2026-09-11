#!/usr/bin/env python3
"""A paid legacy top-up never expires — against a REAL Postgres (2026-09-11).

tests/test_topup_paid_credits_never_expire.py pins the UPDATE's text against a
fake cursor. Only a database can show the bug itself: a paid tu- row leaving the
balance when its 30-minute checkout window closes. This file runs the module's
own DDL and its own functions — redeem_topup_token and the three readers that
filter on expires_at — on tokens written exactly as topup_start wrote them.

Elapsed time is simulated by moving a row's timestamps into the past together,
which is what real minutes do to a NOW()-relative predicate.

Skips without TOPUP_EXPIRY_SQL_DSN. The db-parity job in pre-merge.yml sets it and
then FAILS if this file skipped. Owns and recreates only mcp_topups and
mcp_trial_emails.
"""
import datetime as dt
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.mcp_conversion_plays as mcp  # noqa: E402

DSN = os.environ.get("TOPUP_EXPIRY_SQL_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TOPUP_EXPIRY_SQL_DSN not set")

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


def _mint(conn, token, key):
    """An unpaid tu- token as topup_start wrote one until 2026-09-11: these six
    columns, everything else from the column DEFAULTs."""
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO mcp_topups
                         (topup_token, api_key_hash, credits, price_cents,
                          credits_remaining, referring_agent)
                       VALUES (%s, %s, 50, 500, 50, 'claude')
                       RETURNING source, paid_at, expires_at - created_at""",
                    (token, mcp._hash_key(key)))
        # the premise: an unpaid token carries a 30-minute checkout window
        assert cur.fetchone() == (None, None, dt.timedelta(minutes=30))


def _age(conn, token, minutes):
    """Move every timestamp on the row `minutes` into the past."""
    with conn.cursor() as cur:
        cur.execute("""UPDATE mcp_topups
                          SET created_at = created_at - make_interval(mins => %s),
                              expires_at = expires_at - make_interval(mins => %s),
                              paid_at    = paid_at    - make_interval(mins => %s)
                        WHERE topup_token = %s""", (minutes, minutes, minutes, token))
        assert cur.rowcount == 1


def _row(conn, token):
    with conn.cursor() as cur:
        cur.execute("SELECT paid_at, stripe_session_id, expires_at, credits_remaining "
                    "FROM mcp_topups WHERE topup_token = %s", (token,))
        return cur.fetchone()


def test_a_paid_topup_outlives_its_checkout_window(db):
    key, token = "dch_live_topup_paid", "tu-paidpaid01"
    _mint(db, token, key)
    assert mcp.get_credit_balance(key, None) == 0, "control: an unpaid token is not credit"

    out = mcp.redeem_topup_token(token, stripe_session_id="cs_sql_paid")
    assert out["ok"] and out["credits"] == 50, out
    assert _row(db, token)[2].astimezone(dt.timezone.utc) == NEVER
    assert mcp.get_credit_balance(key, None) == 50

    _age(db, token, 31)                          # where the checkout window closed
    assert mcp.get_credit_balance(key, None) == 50
    assert mcp.get_credit_status(key, None)["credits"] == 50
    spent = mcp.consume_credits(key, None, 1)
    assert spent["ok"] and spent["remaining"] == 49, spent

    _age(db, token, 400 * 24 * 60)               # and more than a year on
    assert mcp.get_credit_balance(key, None) == 49


def test_a_late_payment_is_worth_its_credits(db):
    key, token = "dch_live_topup_late", "tu-latelate01"
    _mint(db, token, key)
    _age(db, token, 31)                          # the human paid after the window closed
    assert mcp.redeem_topup_token(token, stripe_session_id="cs_sql_late")["ok"]
    assert mcp.get_credit_balance(key, None) == 50


def test_a_webhook_retry_changes_nothing(db):
    key, token = "dch_live_topup_retry", "tu-retryretr1"
    _mint(db, token, key)
    assert mcp.redeem_topup_token(token, stripe_session_id="cs_sql_first")["ok"]
    first = _row(db, token)
    assert mcp.redeem_topup_token(token, stripe_session_id="cs_sql_retry")["ok"]
    assert _row(db, token) == first, "a retry moved paid_at, the session id, the expiry or the credits"
    assert first[1] == "cs_sql_first" and first[2].astimezone(dt.timezone.utc) == NEVER, first
    assert mcp.get_credit_balance(key, None) == 50
