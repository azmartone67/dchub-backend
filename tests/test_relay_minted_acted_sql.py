"""r-acted-outside-minted (2026-09-24) — relay_minted_acted, executed on Postgres.

The funnel published relay_mint→human_acted (7d 267→37) as a same-population
"86% lost". Measured on prod read-only the same day: only 3 of the 37
human_acted sessions were relay_minted sessions. human_acted's /go/c lane reads
mcp_checkout_clicks and never joins mcp_high_intent_sessions, so it is not a
subset of relay_minted and the ratio is not a conversion rate.

relay_minted_acted_count_sql() is the same-population numerator. This file
runs it against a real Postgres and pins:
  · it counts ONLY relay_minted sessions (claim minted, in the window);
  · on either human artifact (relay open, signed /go/c click), real UA only;
  · operator self-traffic excluded;
  · a session that acted but was never minted is in human_acted and NOT here —
    the exact case the published rung could not tell apart.

Runs in the pre-merge db-parity job on RELAYED_CHECKOUT_SQL_DSN.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp_calls_deloop import self_traffic_session_prefixes  # noqa: E402
from routes.handoff_definition import (  # noqa: E402
    LEAK_LADDER,
    biggest_leak_detail,
    human_acted_count_sql,
    relay_minted_acted_count_sql,
)

DSN = os.environ.get("RELAYED_CHECKOUT_SQL_DSN", "").strip()
IV = "7 days"
REAL_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
           "(KHTML, like Gecko) Version/17.5 Safari/605.1.15")
PROBE_UA = "curl/8.7.1"

M_OPEN, M_CLICK, M_IDLE, M_PROBE, M_OLD = (
    "a11ce%03d-0000-4000-8000-%012d" % (i, i) for i in range(1, 6))
NOT_MINTED, OUTSIDE = ("b0b0%04d-0000-4000-8000-%012d" % (i, i) for i in (1, 2))
OP = self_traffic_session_prefixes()[0] + "-0000-4000-8000-00000000000c"

DDL = """
DROP TABLE IF EXISTS mcp_checkout_clicks;
DROP TABLE IF EXISTS relay_opens;
DROP TABLE IF EXISTS mcp_high_intent_sessions;
CREATE TABLE mcp_high_intent_sessions (
    mcp_session_id             TEXT NOT NULL,
    first_hit_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claim_minted_at            TIMESTAMPTZ,
    human_view_first_opened_at TIMESTAMPTZ,
    human_view_first_ua        TEXT
);
CREATE TABLE relay_opens (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ DEFAULT NOW(),
    session_id TEXT,
    user_agent TEXT,
    valid      BOOLEAN
);
CREATE TABLE mcp_checkout_clicks (
    id          SERIAL PRIMARY KEY,
    clicked_at  TIMESTAMPTZ DEFAULT NOW(),
    plan        TEXT,
    ref         TEXT,
    ref_kind    TEXT,
    sig_ok      BOOLEAN DEFAULT TRUE,
    user_agent  TEXT,
    session_id  TEXT,
    referrer    TEXT
);
"""


def test_the_published_rung_declares_it_is_not_a_conversion_rate():
    d = biggest_leak_detail({"paywall_hit": 300, "relay_minted": 267,
                             "human_acted": 37, "identified": 4,
                             "paid_attributed": 0})
    assert d["label"] == "relay_mint→human_acted"
    assert d["lost_pct"] == 86.1, "the number is still published"
    assert d["same_population"] is False
    basis = d["population_basis"] or ""
    for token in ("NOT a conversion rate", "mcp_checkout_clicks",
                  "relay_minted_acted"):
        assert token in basis, token


def test_the_later_rungs_stay_same_population():
    flagged = {(s, t) for s, t, _l, b in LEAK_LADDER if b is not None}
    assert flagged == {("paywall_hit", "relay_minted"),
                       ("relay_minted", "human_acted")}, flagged


@pytest.fixture(scope="module")
def counts():
    if not DSN:
        pytest.skip("RELAYED_CHECKOUT_SQL_DSN not set — no Postgres to run against")
    import psycopg2

    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(DDL)
        minted = [(M_OPEN, "2 hours"), (M_CLICK, "2 hours"), (M_IDLE, "2 hours"),
                  (M_PROBE, "2 hours"), (M_OLD, "10 days"), (OP, "2 hours")]
        for sid, ago in minted:
            cur.execute(
                "INSERT INTO mcp_high_intent_sessions"
                " (mcp_session_id, first_hit_at, claim_minted_at)"
                " VALUES (%s, now() - %s::interval, now() - %s::interval)",
                (sid, ago, ago))
        # In the table, but no relay link was ever minted for it.
        cur.execute("INSERT INTO mcp_high_intent_sessions"
                    " (mcp_session_id, first_hit_at)"
                    " VALUES (%s, now() - interval '2 hours')", (NOT_MINTED,))
        for sid in (M_OPEN, OP):
            cur.execute("INSERT INTO relay_opens (session_id, user_agent, valid)"
                        " VALUES (%s, %s, true)", (sid, REAL_UA))
        clicks = [(M_CLICK, REAL_UA), (M_PROBE, PROBE_UA), (M_OLD, REAL_UA),
                  (NOT_MINTED, REAL_UA), (OUTSIDE, REAL_UA)]
        for sid, ua in clicks:
            cur.execute(
                "INSERT INTO mcp_checkout_clicks"
                " (plan, ref, ref_kind, sig_ok, user_agent, session_id)"
                " VALUES ('metered', %s, 'session', true, %s, %s)",
                (sid, ua, sid))

        def one(sql):
            cur.execute(sql)
            return cur.fetchone()[0]

        out = {
            "minted_acted": one(relay_minted_acted_count_sql(IV)),
            "human_acted": one(human_acted_count_sql(IV)),
            "minted": one("select count(distinct mcp_session_id)"
                          " from mcp_high_intent_sessions"
                          " where claim_minted_at is not null"
                          " and first_hit_at > now() - interval '" + IV + "'"),
        }
        cur.execute("select distinct u.sid from ("
                    + human_acted_count_sql(IV)[len("select count(distinct u.sid) from ("):])
        out["human_acted_sids"] = sorted(r[0] for r in cur.fetchall())
        yield out
    finally:
        conn.close()


def test_counts_only_minted_sessions_that_acted(counts):
    # M_OPEN (relay open) + M_CLICK (signed /go/c click). Not M_IDLE (no act),
    # M_PROBE (curl UA), M_OLD (outside the window), NOT_MINTED (never minted),
    # OUTSIDE (not in the table), OP (operator).
    assert counts["minted_acted"] == 2, counts


def test_is_a_subset_of_relay_minted(counts):
    assert counts["minted_acted"] <= counts["minted"], counts
    assert counts["minted"] == 5, "M_OPEN M_CLICK M_IDLE M_PROBE OP"


def test_human_acted_counts_sessions_relay_minted_never_saw(counts):
    """The defect the rung's population_basis now declares: sessions that
    acted without ever being minted are in human_acted, not in relay_minted."""
    assert OUTSIDE in counts["human_acted_sids"], counts
    assert NOT_MINTED in counts["human_acted_sids"], counts
    assert counts["human_acted"] > counts["minted_acted"], counts
