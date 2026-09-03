"""The live-proof platform query, executed against a real Postgres.

WHAT THIS PINS. /api/v1/stats/live-proof publishes platforms_30d — the source
of the homepage line naming which platforms call MCP tools, and how often.
Until 2026-09-03 it counted RAW mcp_tool_calls: no is_real_external, no
self-traffic exclusion. /api/ai/tracking's cards counted the same 30 days
through mcp_calls_identity with both filters. Same vendor collapsing on both
sides, so the gap was never a double-count — it was OUR OWN traffic. Claude
read 1,071 in the headline against 492 on its card, and the 579-call gap grew
~81 in eight hours.

The operator's agent client writes mcp_client 'claude' / user_agent 'node',
byte-identical to a prospect's. That is exactly why the funnel on the same page
carries DEFINITION v4 and excludes it from "Human acted". The headline never
learned it.

★ The query is READ OUT OF flask_mcp_endpoints.py, not copied here. A copy
drifts, and a drifted copy passing is worse than no test: it would report that
the shipped query behaves in a way the shipped query no longer does.

The fixture gives every clause a row that fails without it:

  claude    external session               -> counted in BOTH n and n_gross
  claude    session '88e20dac…' (declared) -> n_gross ONLY
  claude    is_real_external = false       -> neither
  claude    is_public_ip = false           -> neither
  grok      declared self-traffic ONLY     -> n = 0, so it is not a platform
  claude    40 days old                    -> outside the 30-day window

Set LIVE_PROOF_SQL_DSN to run it. CI passes the db-parity service DSN and then
asserts this file did not skip — a skipped proof is not a proof.
"""
import datetime as dt
import os

import pytest

from tests._live_proof_sql import platforms_query

psycopg2 = pytest.importorskip("psycopg2")

DSN = os.environ.get("LIVE_PROOF_SQL_DSN", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN, reason="LIVE_PROOF_SQL_DSN not set — no Postgres to run against")

DDL = """
DROP TABLE IF EXISTS mcp_calls_identity;
-- In production this is a VIEW over the call log; only the five columns the
-- query reads are modelled here, with their production types.
CREATE TABLE mcp_calls_identity (
    platform         TEXT,
    session_id       TEXT,
    is_public_ip     BOOLEAN,
    is_real_external BOOLEAN,
    created_at       TIMESTAMP
);
"""


@pytest.fixture(scope="module")
def counts():
    from mcp_calls_deloop import (external_session_predicate,
                                  self_traffic_session_prefixes)
    prefixes = list(self_traffic_session_prefixes())
    assert prefixes, "no declared self-traffic prefixes — nothing to exclude"
    mine = prefixes[0] + "-0001"
    sql = platforms_query(external_session_predicate("session_id"))

    now = dt.datetime.utcnow()
    recent, old = now - dt.timedelta(days=2), now - dt.timedelta(days=40)
    rows = [
        ("claude", "prospect-0001", True,  True,  recent),   # real external
        ("claude", mine,            True,  True,  recent),   # ours
        ("claude", "prospect-0002", True,  False, recent),   # not real-external
        ("claude", "prospect-0003", False, True,  recent),   # not a public IP
        ("grok",   mine,            True,  True,  recent),   # ONLY ever ours
        ("claude", "prospect-0004", True,  True,  old),      # outside 30d
    ]
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(DDL)
            cur.executemany(
                "INSERT INTO mcp_calls_identity "
                "(platform, session_id, is_public_ip, is_real_external, created_at)"
                " VALUES (%s,%s,%s,%s,%s)", rows)
            cur.execute(sql)
            out = {p: (int(n), int(g)) for (p, n, g) in cur.fetchall()}
    finally:
        conn.close()
    return out


def test_the_query_runs_and_returns_platform_filtered_and_gross(counts):
    assert counts, "the query returned nothing — fixture or window is wrong"
    assert set(counts) == {"claude", "grok"}, counts


def test_declared_self_traffic_is_out_of_the_headline_figure(counts):
    """★ THE CONTROL. Two Claude rows survive the WHERE; one of them is ours.
    The headline figure must count ONE. Losing the FILTER makes it two — which
    is the defect, in miniature."""
    n, gross = counts["claude"]
    assert n == 1, f"self-traffic leaked into the headline count: {counts}"
    assert gross == 2, f"the gross sibling should still see ours: {counts}"


def test_a_platform_that_is_only_ever_us_scores_zero(counts):
    """It must not be nameable as an integrating platform. The route drops
    rows with calls == 0 and lists them under platforms_removed_entirely."""
    n, gross = counts["grok"]
    assert (n, gross) == (0, 1), counts


def test_non_external_and_private_ip_rows_are_out_of_BOTH_figures(counts):
    # claude has four in-window rows; only two pass is_public_ip AND
    # is_real_external, so even the gross sibling must read 2, never 4.
    assert counts["claude"][1] == 2, counts


def test_the_thirty_day_window_still_binds(counts):
    # The 40-day row is claude's fifth; if the window were dropped, gross
    # would read 3.
    assert counts["claude"][1] == 2, counts
