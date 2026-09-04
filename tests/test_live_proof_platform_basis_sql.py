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
    # ── r-burst-vs-adoption (2026-09-04) ────────────────────────────────────
    # The query gained active_days + last_call, which decide whether a platform
    # is named as INTEGRATING or disclosed as a one-day trial. Both must be
    # filtered by the SAME self-traffic predicate as `n` — unfiltered, our own
    # calls manufacture the recurrence we are trying to measure.
    #
    # The dates below are chosen so an unfiltered column CANNOT pass:
    #   ours is the MOST RECENT row  -> unfiltered last_call would be ours
    #   ours falls on its OWN day    -> unfiltered active_days would be 3, not 2
    ours_day = now - dt.timedelta(days=1)      # newest, and ours
    day_a    = recent                          # now-2d, a prospect
    day_b    = now - dt.timedelta(days=5)      # a SECOND prospect day
    rows = [
        ("claude", "prospect-0001", True,  True,  day_a),    # real external
        ("claude", "prospect-0005", True,  True,  day_b),    # real, ANOTHER day
        ("claude", mine,            True,  True,  ours_day), # ours, and newest
        ("claude", "prospect-0002", True,  False, recent),   # not real-external
        ("claude", "prospect-0003", False, True,  recent),   # not a public IP
        ("grok",   mine,            True,  True,  recent),   # ONLY ever ours
        ("claude", "prospect-0004", True,  True,  old),      # outside 30d
    ]
    expected_last_call = day_a.date()
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
            out = {r[0]: {"n": int(r[1]), "gross": int(r[2]),
                          "active_days": int(r[3]), "last_call": r[4]}
                   for r in cur.fetchall()}
    finally:
        conn.close()
    out["_expected_last_call"] = expected_last_call
    return out


def test_the_query_runs_and_returns_platform_filtered_and_gross(counts):
    assert counts, "the query returned nothing — fixture or window is wrong"
    assert set(counts) - {"_expected_last_call"} == {"claude", "grok"}, counts


def test_declared_self_traffic_is_out_of_the_headline_figure(counts):
    """★ THE CONTROL. Two Claude rows survive the WHERE; one of them is ours.
    The headline figure must count ONE. Losing the FILTER makes it two — which
    is the defect, in miniature."""
    n, gross = counts["claude"]["n"], counts["claude"]["gross"]
    assert n == 2, f"self-traffic leaked into the headline count: {counts}"
    assert gross == 3, f"the gross sibling should still see ours: {counts}"


def test_a_platform_that_is_only_ever_us_scores_zero(counts):
    """It must not be nameable as an integrating platform. The route drops
    rows with calls == 0 and lists them under platforms_removed_entirely."""
    assert (counts["grok"]["n"], counts["grok"]["gross"]) == (0, 1), counts


def test_non_external_and_private_ip_rows_are_out_of_BOTH_figures(counts):
    # claude has five in-window rows; only three pass is_public_ip AND
    # is_real_external, so even the gross sibling must read 3, never 5.
    assert counts["claude"]["gross"] == 3, counts


def test_the_thirty_day_window_still_binds(counts):
    # The 40-day row is claude's sixth; if the window were dropped, gross
    # would read 4.
    assert counts["claude"]["gross"] == 3, counts


def test_active_days_is_filtered_by_the_same_self_traffic_predicate(counts):
    """★ THE NEW CONTROL. claude has THREE qualifying rows on three distinct
    days, one of which is ours. active_days must read 2. Losing the FILTER on
    this column makes it 3 — we would manufacture the recurrence the hero
    sentence rests on, out of our own traffic."""
    assert counts["claude"]["active_days"] == 2, (
        "active_days counts our own calls as the platform's activity: %r" % counts)


def test_last_call_is_filtered_too(counts):
    """Ours is the NEWEST claude row. An unfiltered MAX would report our own
    call as the platform's last contact — the freshness half of the same lie."""
    assert counts["claude"]["last_call"] == counts["_expected_last_call"], (
        "last_call reports our own traffic as the platform's: %r" % counts)


def test_a_platform_that_is_only_ever_us_reports_no_activity(counts):
    """grok's single row is ours, so it has zero real calls AND zero real
    active days — it must not be able to claim a day it never had."""
    assert counts["grok"]["active_days"] == 0, counts
    assert counts["grok"]["last_call"] is None, counts
