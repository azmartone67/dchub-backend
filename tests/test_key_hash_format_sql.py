#!/usr/bin/env python3
"""Key-hash format normalization on the credit ledger — against a REAL Postgres.

mcp_topups.api_key_hash is _hash_key(key): the first 32 hex chars of
sha256(key), the form every reader hashes the caller's key into. A key-bound
pack ref can carry the full 64-hex digest instead, and the webhook passes the
ref's hash straight to grant_credit_pack. These tests grant through that full
form, and seed a row already stored in it, then read and burn with the raw key
the way GET /api/v1/mcp/credits/balance (get_credit_status) and
POST /api/v1/mcp/credits/burn (consume_credits) do.

Last, two burns of a pack's LAST credit are raced on a real row lock. Only a
real Postgres re-checks an UPDATE's conditions on the row a blocked statement
finally gets, so only here can a double spend show (REST started burning
credits too, frontend#1534).

The database tests skip without PACK_EXPIRY_SQL_DSN. The db-parity job in
pre-merge.yml sets it and then FAILS if this file skipped. Owns and recreates
only mcp_topups and mcp_trial_emails.
"""
import hashlib
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.mcp_conversion_plays as mcp  # noqa: E402

DSN = os.environ.get("PACK_EXPIRY_SQL_DSN")

KEY = "dch_live_keyhash_format_buyer"
OTHER = "dch_live_keyhash_format_other"
SESSION = "sess-keyhash-format"


def _digest(key):
    """The full 64-hex sha256, computed here rather than by the module."""
    return hashlib.sha256(key.encode()).hexdigest()


@pytest.fixture
def db(monkeypatch):
    if not DSN:
        pytest.skip("PACK_EXPIRY_SQL_DSN not set")
    import psycopg2
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mcp_topups, mcp_trial_emails CASCADE")
    monkeypatch.setattr(mcp, "_conn", lambda: psycopg2.connect(DSN))
    assert mcp.init_schema() is True, "the module's own DDL did not apply"
    yield conn
    conn.close()


def _stored_hash(conn, row_id):
    with conn.cursor() as cur:
        cur.execute("SELECT api_key_hash FROM mcp_topups WHERE id = %s", (row_id,))
        return cur.fetchone()[0]


def _seed(conn, api_key_hash, token, session=None, credits=1000):
    """A paid pack row written straight to the table, in whatever hash form
    the caller gives — how rows granted before the normalization are held."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO mcp_topups
                   (topup_token, api_key_hash, credits, price_cents, paid_at,
                    expires_at, credits_remaining, stripe_session_id,
                    mcp_session_id, source)
               VALUES (%s, %s, %s, 1000, NOW() ON CONFLICT DO NOTHING - INTERVAL '2 days',
                       %s::timestamptz, %s, %s, %s, 'pack10_keybound')
               RETURNING id""",
            (token, api_key_hash, credits, mcp.PACK_NEVER_EXPIRES, credits,
             "cs_" + token, session))
        return cur.fetchone()[0]


def test_the_two_forms_under_test_are_what_they_claim():
    full = _digest(KEY)
    assert len(full) == 64 and len(mcp._hash_key(KEY)) == 32
    assert mcp._hash_key(KEY) == full[:32]


def test_normalization_folds_only_a_full_hex_digest():
    full = _digest(KEY)
    short = full[:32]
    assert mcp.normalize_key_hash(full) == short
    assert mcp.normalize_key_hash("  " + full.upper() + "\n") == short
    assert mcp.normalize_key_hash(short) == short
    not_a_digest = "g" + full[1:]
    assert mcp.normalize_key_hash(not_a_digest) == not_a_digest, "a non-hex value is never cut"
    assert mcp.normalize_key_hash(full + "0") == full + "0", "65 chars is not a digest"


def test_a_grant_through_the_full_digest_is_read_and_burned_by_the_raw_key(db):
    out = mcp.grant_credit_pack(None, None, 1000, stripe_session_id="cs_keyhash_grant",
                                source="pack10_keybound", api_key_hash=_digest(KEY))
    assert out["ok"] and not out["idempotent"], out
    seen = {
        "status": mcp.get_credit_status(KEY, None),
        "balance": mcp.get_credit_balance(KEY, None),
        "burn": mcp.consume_credits(KEY, None, 1),
        "stored": _stored_hash(db, out["topup_id"]),
        "other_key": mcp.get_credit_status(OTHER, None),
    }
    assert seen == {
        "status": {"credits": 1000, "had_pack": True},
        "balance": 1000,
        "burn": {"ok": True, "remaining": 999, "burned": 1},
        "stored": _digest(KEY)[:32],
        "other_key": {"credits": 0, "had_pack": False},
    }, seen


def test_a_row_already_held_in_the_full_digest_is_read_and_burned(db):
    _seed(db, _digest(KEY), "pack10_keybound-held")
    # A session-only grant for someone else: the no-key arm must still work
    # with the key arm bound to NULL, and must not leak into KEY's balance.
    _seed(db, _digest(OTHER)[:32], "pack10-session", session=SESSION, credits=50)
    seen = {
        "status": mcp.get_credit_status(KEY, None),
        "balance": mcp.get_credit_balance(KEY, None),
        "burn": mcp.consume_credits(KEY, None, 2),
        "topup_burn": mcp.consume_topup_credit(KEY, 3),
        "after": mcp.get_credit_balance(KEY, None),
        "session_only": mcp.get_credit_status(None, SESSION),
        "session_burn": mcp.consume_credits(None, SESSION, 1),
        "stranger": mcp.get_credit_balance("dch_live_keyhash_format_stranger", None),
    }
    assert seen == {
        "status": {"credits": 1000, "had_pack": True},
        "balance": 1000,
        "burn": {"ok": True, "remaining": 998, "burned": 2},
        "topup_burn": True,
        "after": 995,
        "session_only": {"credits": 50, "had_pack": True},
        "session_burn": {"ok": True, "remaining": 49, "burned": 1},
        "stranger": 0,
    }, seen


# ── two burns of the last credit ────────────────────────────────────────────

class _HeldCommit:
    """A real connection whose `with conn:` commit waits for `release`.

    Both burns run `with c, c.cursor() as cur:`: the UPDATE executes inside the
    block, taking the row lock, and commits on exit. Holding that exit open is
    exactly "burn #1 has run but not committed"."""

    def __init__(self, executed, release):
        import psycopg2
        self._c = psycopg2.connect(DSN)
        self._executed, self._release = executed, release

    def __enter__(self):
        self._c.__enter__()
        return self

    def __exit__(self, *exc):
        self._release.wait(20)
        return self._c.__exit__(*exc)

    def cursor(self):
        return _SignallingCursor(self._c.cursor(), self._executed)

    def close(self):
        self._c.close()


class _SignallingCursor:
    def __init__(self, cur, executed):
        self._cur, self._executed = cur, executed

    def __enter__(self):
        self._cur.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cur.__exit__(*exc)

    def execute(self, *a, **k):
        self._cur.execute(*a, **k)
        self._executed.set()

    def fetchone(self):
        return self._cur.fetchone()


BURNS = {
    "consume_credits": lambda: mcp.consume_credits(KEY, None, 1),
    "consume_topup_credit": lambda: mcp.consume_topup_credit(KEY, 1),
}


def _burned(result):
    return result is True or (isinstance(result, dict) and result.get("ok") is True)


@pytest.mark.parametrize("burn", sorted(BURNS))
def test_two_burns_of_the_last_credit_spend_it_once(db, monkeypatch, burn):
    import threading
    import time
    import psycopg2

    row = _seed(db, _digest(KEY)[:32], "pack10_keybound-race", credits=1)
    executed, release = threading.Event(), threading.Event()
    held = [_HeldCommit(executed, release)]
    lock = threading.Lock()

    def _conn():
        with lock:
            return held.pop() if held else psycopg2.connect(DSN)

    monkeypatch.setattr(mcp, "_conn", _conn)
    results = {}
    first = threading.Thread(target=lambda: results.__setitem__("first", BURNS[burn]()))
    first.start()
    assert executed.wait(10), "burn #1 never ran its UPDATE"
    second = threading.Thread(target=lambda: results.__setitem__("second", BURNS[burn]()))
    second.start()
    staged = False
    deadline = time.time() + 10
    while time.time() < deadline and not staged:
        with db.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_stat_activity "
                        "WHERE wait_event_type = 'Lock' AND datname = current_database()")
            staged = cur.fetchone()[0] >= 1
        if not staged:
            time.sleep(0.05)
    release.set()
    first.join(20)
    second.join(20)
    assert staged, "burn #2 never waited on burn #1's row lock, so the race was not staged"
    with db.cursor() as cur:
        cur.execute("SELECT credits_remaining FROM mcp_topups WHERE id = %s", (row,))
        left = cur.fetchone()[0]
    seen = (_burned(results.get("first")), _burned(results.get("second")), left)
    assert seen == (True, False, 0), (results, left)
