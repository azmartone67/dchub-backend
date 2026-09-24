"""routes/human_relay._paid_checkout, EXECUTED against a real Postgres.

r-post-pay-identify (2026-09-24). tests/test_post_pay_page.py stubs this lookup;
only a database shows which email it returns. Tables live in their own schema
(search_path set on the DSN), so this lane cannot clobber another's tables.

  C1  capture + conversion rows      capture's email wins
  C2  conversion only, user_email    conversion's user_email
  C3  conversion only, caller_id     caller_id when user_email is blank
  C4  paid, no email anywhere        paid, email ''
  C5  caller_id is not an email      paid, email ''
  C6  no payment row                 None

Set POST_PAY_SQL_DSN to run it. CI passes the db-parity service DSN and then
asserts this file did not skip.
"""
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from routes import human_relay  # noqa: E402

DSN = os.environ.get("POST_PAY_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="POST_PAY_SQL_DSN not set — no Postgres to run against")

SCHEMA = "post_pay_lookup_test"

DDL = """
CREATE TABLE mcp_checkout_payments (id BIGSERIAL PRIMARY KEY,
  stripe_session_id TEXT UNIQUE NOT NULL, client_reference_id TEXT, ref_kind TEXT);
CREATE TABLE relay_identify_captures (id BIGSERIAL PRIMARY KEY, mcp_session_id TEXT,
  email TEXT, source TEXT, stripe_session_id TEXT,
  captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE mcp_conversions (id BIGSERIAL PRIMARY KEY, caller_id TEXT,
  user_email TEXT, stripe_session_id TEXT);
"""


def _with_search_path(dsn):
    sep = "&" if "?" in dsn else "?"
    return dsn + sep + "options=-csearch_path%3D" + SCHEMA


@pytest.fixture(scope="module")
def looked_up():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS " + SCHEMA + " CASCADE")
    cur.execute("CREATE SCHEMA " + SCHEMA)
    cur.execute("SET search_path TO " + SCHEMA)
    cur.execute(DDL)
    for cs, kind in (("cs_test_c1", "pack_key"), ("cs_test_c2", "session"),
                     ("cs_test_c3", "sub_key"), ("cs_test_c4", "session"),
                     ("cs_test_c5", "session")):
        cur.execute("INSERT INTO mcp_checkout_payments (stripe_session_id, ref_kind)"
                    " VALUES (%s, %s)", (cs, kind))
    cur.execute("INSERT INTO relay_identify_captures (mcp_session_id, email, source,"
                " stripe_session_id) VALUES ('s1', 'Captured@Example.org', 'checkout',"
                " 'cs_test_c1') ON CONFLICT DO NOTHING")
    cur.executemany("INSERT INTO mcp_conversions (caller_id, user_email, stripe_session_id)"
                    " VALUES (%s, %s, %s)", [
                        ("conv-c1@example.org", "conv-c1@example.org", "cs_test_c1"),
                        ("anon-hash", "user@example.org", "cs_test_c2"),
                        ("caller@example.org", "", "cs_test_c3"),
                        ("anon-hash-5", None, "cs_test_c5")])
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", _with_search_path(DSN))
    try:
        yield {cs: human_relay._paid_checkout(cs)
               for cs in ("cs_test_c1", "cs_test_c2", "cs_test_c3",
                          "cs_test_c4", "cs_test_c5", "cs_test_c6")}
    finally:
        mp.undo()
        cur.execute("DROP SCHEMA IF EXISTS " + SCHEMA + " CASCADE")
        conn.close()


def test_capture_wins_over_the_conversion_row(looked_up):
    assert looked_up["cs_test_c1"] == {"ref_kind": "pack_key", "email": "captured@example.org"}


def test_conversion_user_email_then_caller_id(looked_up):
    assert looked_up["cs_test_c2"] == {"ref_kind": "session", "email": "user@example.org"}
    assert looked_up["cs_test_c3"] == {"ref_kind": "sub_key", "email": "caller@example.org"}


def test_paid_without_an_address_is_still_paid(looked_up):
    assert looked_up["cs_test_c4"] == {"ref_kind": "session", "email": ""}
    assert looked_up["cs_test_c5"] == {"ref_kind": "session", "email": ""}


def test_an_unrecorded_session_is_none(looked_up):
    assert looked_up["cs_test_c6"] is None
