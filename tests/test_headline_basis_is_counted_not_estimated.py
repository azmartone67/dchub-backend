"""Two published headline numbers, and what they are allowed to be made of.

Both defects were found by diffing two snapshots of the live page eight hours
apart on 2026-09-03. Neither was a test failure — that is why they shipped.

1. ALL-TIME TOOL CALLS WAS A PLANNER ESTIMATE. /api/v1/mcp/funnel derived
   tool_calls_total from pg_class.reltuples, which VACUUM / ANALYZE /
   CREATE INDEX update and INSERT never does. Both snapshots read exactly
   260,147 while the 7d rolling window over the same events moved — ~9
   calls/hour the "all-time" figure denied. The guards (== 0, < tool_calls_7d)
   catch an unanalyzed table and an absurd estimate; staleness trips neither,
   so an approximation published to the unit, in the field whose own comment
   invites press to quote it.

2. THE PLATFORM HEADLINE COUNTED OUR OWN TRAFFIC. /api/v1/stats/live-proof
   counted raw mcp_tool_calls with no is_real_external and no self-traffic
   exclusion, while /api/ai/tracking counted the same 30 days with both. Claude
   read 1,071 against 492 on its card. The gap was never a double-count — both
   sides collapse vendor aliases — it was the operator's own agent client,
   which writes mcp_client 'claude' / user_agent 'node' and is byte-identical
   to a prospect's. The funnel on the same page already excludes it under
   DEFINITION v4; the headline did not.

Source-text on purpose: tests/ never imports Flask, and flask_mcp_endpoints
pulls psycopg2 at import. Reading the source is also what makes these
regression guards rather than a restatement of behaviour.
"""
import pytest

from tests._live_proof_sql import (body_of, code_only, platforms_query,
                                   source)

SRC = source()
# The predicate's exact text does not matter to these guards — only that the
# query interpolates it — so a stand-in keeps them independent of the seed list.
PREDICATE = "(COALESCE(session_id,'') !~* '^(deadbeef)')"


@pytest.fixture(scope="module")
def funnel():
    return body_of("mcp_funnel", SRC)


@pytest.fixture(scope="module")
def live_proof():
    return body_of("stats_live_proof", SRC)


@pytest.fixture(scope="module")
def platforms_sql():
    """THE platforms_30d query itself — not the endpoint that contains it.

    ★ The first version of these guards asserted on the whole function, and a
    mutation that moved this one query back to raw mcp_tool_calls PASSED:
    mcp_calls_identity still appeared elsewhere in the same endpoint. Assert on
    the query, or assert nothing.
    """
    return platforms_query(PREDICATE, SRC)


# ── 1. the all-time figure is counted ────────────────────────────────────
def test_all_time_tool_calls_never_comes_from_a_planner_estimate(funnel):
    """★ THE CONTROL. reltuples does not move on INSERT, so a total built on it
    is frozen between ANALYZE runs and cannot be told apart from 'no traffic'.

    Checked against CODE, not prose — the comment explaining why it is banned
    names it, and must be allowed to."""
    code = code_only(funnel)
    assert "reltuples" not in code, (
        "tool_calls_total is back on pg_class.reltuples — a planner statistic "
        "published as an exact lifetime total")
    assert "pg_class" not in code, "pg_class re-entered the funnel endpoint"


def test_all_time_tool_calls_is_a_real_count(funnel):
    assert 'cur.execute("SELECT COUNT(*) FROM mcp_tool_calls")' in funnel, (
        "the unqualified COUNT(*) that produces tool_calls_total is gone")


def test_the_all_time_figure_publishes_what_it_was_made_of(funnel):
    # A number cited in press states its basis, so "exact" and "the count
    # failed" cannot both render as a bare integer.
    # ★ BOTH branches. Asserting only that the key appears somewhere let a
    # mutation delete the success-path basis and still pass, because the error
    # branch kept the key alive.
    assert "COUNT(*) mcp_tool_calls — exact" in funnel, (
        "the successful count no longer publishes its basis")
    assert "unavailable — the count failed" in funnel, (
        "a failed count must say so rather than substituting an estimate")


# ── 2. the platform headline excludes our own traffic ────────────────────
def test_platform_counts_come_from_the_filtered_identity_view(platforms_sql):
    assert "FROM mcp_calls_identity" in platforms_sql, (
        "platforms_30d is back on a table without the identity filters")
    assert "is_public_ip AND is_real_external" in platforms_sql
    assert "mcp_tool_calls" not in platforms_sql


def test_platform_counts_apply_the_declared_self_traffic_predicate(live_proof):
    """★ Reused, never re-declared. A second hand-maintained prefix list here
    is the regex-twin drift mcp_calls_deloop exists to stop."""
    assert "external_session_predicate" in live_proof
    assert "self_traffic_session_prefixes" in live_proof
    assert f"COUNT(*) FILTER (WHERE {PREDICATE})" in platforms_query(PREDICATE, SRC), (
        "the headline count no longer filters the declared self-traffic")


def test_the_thirty_day_window_is_still_in_the_query(platforms_sql):
    assert "INTERVAL '30 days'" in platforms_sql


def test_the_route_delegates_the_judgement_it_cannot_test_inline(live_proof):
    """Who counts as a platform is decided in live_proof_platforms, which
    tests/ can import. Inline in the route it was untestable, and a mutation
    putting our-traffic-only platforms back in the headline passed every guard
    written here."""
    assert "from live_proof_platforms import shape_platforms" in live_proof
    assert "shape_platforms(" in live_proof
    assert '"platforms_30d_excluded"' in live_proof


def test_the_gross_figure_ships_beside_the_filtered_one():
    # Removing a number from a headline is not the same as hiding it.
    from live_proof_platforms import shape_platforms
    rows, excluded = shape_platforms([("claude", 492, 1071)], ("88e20dac",))
    assert rows[0]["calls_including_self_traffic"] == 1071
    assert excluded["calls_removed"] == 579
    assert excluded["self_traffic_session_prefixes"] == ["88e20dac"]
    assert "platforms_removed_entirely" in excluded


def test_a_missing_predicate_fails_open_and_says_so(live_proof):
    """Fail OPEN — nothing removed — but never silently, so a reader can tell
    'no exclusion applied' from 'applied, found nothing'."""
    assert '_not_self, _prefixes = "TRUE", []' in live_proof
    assert "platforms_self_traffic_filter" in live_proof


def test_the_published_basis_matches_what_actually_runs(live_proof):
    """The source_columns string is the contract a reader quotes. If it still
    advertises the old lineage, the payload lies about itself even though the
    query is right — the exact shape of the defect one funnel over."""
    assert "COUNT(DISTINCT recognized platform) mcp_tool_calls (30d, allowlist only)" \
        not in SRC, "source_columns still publishes the pre-fix lineage"
    assert "minus declared operator self-traffic" in SRC
