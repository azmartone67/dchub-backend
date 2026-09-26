#!/usr/bin/env python3
"""had_pack reads every pack source — against a REAL Postgres (2026-09-11).

tests/test_had_pack_reads_every_pack_source.py pins the SQL and binds
PACK_SOURCES to it against a fake cursor. A fake cannot say whether
`source = ANY(%s)` matches a pack10 row once psycopg2 has adapted the list, or
whether bool_or over a caller's tu- top-ups — source NULL — reads false. This
file writes every row through the module's own writers (grant_credit_pack,
consume_credits, redeem_topup_token) and reads it back through
get_credit_status. The one row no writer can produce any more — a legacy tu-
top-up, whose start route was retired 2026-09-11 (#4383) — is inserted with
exactly the columns that route wrote, then paid through redeem_topup_token.

Skips without HAD_PACK_SQL_DSN. The db-parity job in pre-merge.yml sets it and
then FAILS if this file skipped. Owns and recreates only mcp_topups and
mcp_trial_emails.
"""
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.mcp_conversion_plays as mcp  # noqa: E402

DSN = os.environ.get("HAD_PACK_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="HAD_PACK_SQL_DSN not set — no Postgres to run against")

# What the retired POST /api/v1/mcp/topup/start wrote for every token (the
# DCHUB_TOPUP_CREDITS default); the module constant went with the route.
LEGACY_TOPUP_CREDITS = 50


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


def _buy_and_spend_pack(key, source, session=None):
    """A pack bought through grant_credit_pack with the call shape its real
    caller uses, then spent to zero through consume_credits."""
    if source.endswith("_keybound"):
        # main.py's pk- branch: the Stripe ref carries only the key's hash
        out = mcp.grant_credit_pack(None, None, 1000, stripe_session_id=f"cs_{source}_{key}",
                                    source=source, api_key_hash=mcp._hash_key(key))
    else:
        out = mcp.grant_credit_pack(key, session, 1000, stripe_session_id=f"cs_{source}_{key}",
                                    source=source)
    assert out["ok"] and not out["idempotent"], (source, out)
    spent = mcp.consume_credits(key, None, 1000)
    assert spent["ok"] and spent["remaining"] == 0, (source, spent)


def _buy_topup(key):
    """A legacy tu- top-up as production can still hold one: minted before the
    start route was retired, with exactly the six columns that route wrote (every
    other column from its DEFAULT, source NULL), then paid by the Stripe
    webhook's real redeem_topup_token."""
    token = "tu-" + mcp._hash_key(key)[:10]
    c = mcp._conn()
    try:
        with c, c.cursor() as cur:
            cur.execute("""INSERT INTO mcp_topups
                             (topup_token, api_key_hash, credits, price_cents,
                              credits_remaining, referring_agent)
                           VALUES (%s, %s, %s, 500, %s, 'claude') ON CONFLICT DO NOTHING""",
                        (token, mcp._hash_key(key), LEGACY_TOPUP_CREDITS, LEGACY_TOPUP_CREDITS))
    finally:
        c.close()
    paid = mcp.redeem_topup_token(token, stripe_session_id=f"cs_tu_{key}")
    assert paid["ok"], paid


def test_a_pack10_buyer_with_no_credits_left_reads_had_pack(db):
    """The live $10 SKU — the buyer `LIKE 'pack5%'` read as never having bought."""
    _buy_and_spend_pack("dch_live_hp_pack10", "pack10", session="mcp-sess-hp-pack10")
    assert mcp.get_credit_status("dch_live_hp_pack10", None) == {"credits": 0, "had_pack": True}
    assert mcp.get_credit_status(None, "mcp-sess-hp-pack10") == {"credits": 0, "had_pack": True}


def test_a_tu_topup_buyer_does_not_read_had_pack(db):
    _buy_topup("dch_live_hp_tu")
    status = mcp.get_credit_status("dch_live_hp_tu", None)
    # Control: the top-up's credits are counted, so the lookup DID find the paid
    # row — had_pack is False because of its source, not because nothing matched.
    assert status["credits"] == LEGACY_TOPUP_CREDITS > 0, status
    assert status["had_pack"] is False, status


def test_a_caller_who_bought_nothing_reads_no_pack(db):
    assert mcp.get_credit_status("dch_live_hp_nobody", None) == {"credits": 0, "had_pack": False}


@pytest.mark.parametrize("source", mcp.PACK_SOURCES)
def test_every_pack_source_reads_had_pack_once_spent(db, source):
    key = f"dch_live_hp_{source}"
    _buy_and_spend_pack(key, source)
    assert mcp.get_credit_status(key, None) == {"credits": 0, "had_pack": True}, source


def test_a_topup_beside_a_spent_pack_reads_had_pack(db):
    key = "dch_live_hp_both"
    _buy_topup(key)
    _buy_and_spend_pack(key, "pack10")
    assert mcp.get_credit_status(key, None) == {"credits": LEGACY_TOPUP_CREDITS, "had_pack": True}
