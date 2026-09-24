"""routes/warm_key_cohort._gather, EXECUTED against a real Postgres.

r-optin-consents (2026-09-24). tests/test_warm_key_cohort.py stubs _gather;
this runs its statements. Its own schema, so it cannot clobber another lane.

  W1  identified key, key flag true                       consent (as before)
  W2  identified key, confirmed opt_in_consents row       consent (NEW: address-held)
  W3  identified key, opt_in_consents row NOT confirmed   no consent: a request is not consent
  W4  identified key, no consent anywhere                 no consent
  last_tool_wall is the MOST RECENT signal's tool; top_tool_wall the most-hit.

Set WARM_KEYS_SQL_DSN to run it. CI passes the db-parity service DSN and then
asserts this file did not skip.
"""
import os

import pytest

psycopg2 = pytest.importorskip("psycopg2")

import routes.warm_key_cohort as wk  # noqa: E402

DSN = os.environ.get("WARM_KEYS_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="WARM_KEYS_SQL_DSN not set — no Postgres to run against")

SCHEMA = "warm_keys_sql_test"

DDL = """
CREATE TABLE email_suppression (email TEXT PRIMARY KEY);
CREATE TABLE mcp_dev_keys (api_key TEXT PRIMARY KEY, email TEXT, tier TEXT,
  status TEXT, metadata JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE mcp_call_log (api_key TEXT, "timestamp" TIMESTAMPTZ);
CREATE TABLE mcp_upgrade_signals (user_email TEXT, tool_requested TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW());
CREATE TABLE opt_in_consents (email TEXT PRIMARY KEY, confirmed_at TIMESTAMPTZ);
"""


@pytest.fixture(scope="module")
def rows():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA IF EXISTS " + SCHEMA + " CASCADE")
    cur.execute("CREATE SCHEMA " + SCHEMA)
    cur.execute("SET search_path TO " + SCHEMA)
    cur.execute(DDL)
    tier = sorted(wk.NON_PAID_TIERS)[0]
    for key, email, flag in (("k1", "w1@acme.io", True), ("k2", "W2@Acme.io", False),
                             ("k3", "w3@acme.io", False), ("k4", "w4@acme.io", False)):
        cur.execute("INSERT INTO mcp_dev_keys (api_key, email, tier, status, metadata)"
                    " VALUES (%s, %s, %s, 'active', %s)",
                    (key, email, tier, '{"marketing_opt_in": "true"}' if flag else "{}"))
    cur.execute("INSERT INTO opt_in_consents VALUES (' w2@acme.io', now()), ('w3@acme.io', NULL)")
    cur.executemany("INSERT INTO mcp_upgrade_signals VALUES (%s, %s, now() - %s::interval)", [
        ("w4@acme.io", "get_fiber_intel", "3 days"),
        ("w4@acme.io", "get_fiber_intel", "2 days"),
        ("w4@acme.io", "get_grid_intelligence", "1 hour"),
    ])
    wconn = psycopg2.connect(DSN, options="-csearch_path=" + SCHEMA)
    mp = pytest.MonkeyPatch()
    mp.setattr(wk, "_conn", lambda: wconn)
    mp.setattr(wk, "_release", lambda c, error=False: None)
    try:
        got, meta = wk._gather()
        yield {r["email"]: r for r in got}, meta
    finally:
        mp.undo()
        wconn.close()
        cur.execute("DROP SCHEMA IF EXISTS " + SCHEMA + " CASCADE")
        conn.close()


def test_every_subread_ran(rows):
    _, meta = rows
    assert "errors" not in meta, meta


def test_consent_is_the_key_flag_or_a_confirmed_address(rows):
    by, _ = rows
    assert by["w1@acme.io"]["marketing_opt_in"] is True
    assert by["w2@acme.io"]["marketing_opt_in"] is True
    assert by["w3@acme.io"]["marketing_opt_in"] is False, "a request is not consent"
    assert by["w4@acme.io"]["marketing_opt_in"] is False


def test_last_wall_is_the_latest_and_top_wall_the_most_hit(rows):
    by, _ = rows
    assert by["w4@acme.io"]["top_tool_wall"] == "get_fiber_intel"
    assert by["w4@acme.io"]["last_tool_wall"] == "get_grid_intelligence"
    assert by["w1@acme.io"]["last_tool_wall"] == ""


def test_the_csv_carries_the_last_wall():
    assert "last_tool_wall" in wk._CSV_FIELDS
