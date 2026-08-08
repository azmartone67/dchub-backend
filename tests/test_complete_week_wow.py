"""The funnel headlined a -65% "collapse" that was window arithmetic.

Measured 2026-08-08, same population, same table:
    rolling 7d vs prior rolling 7d -> -65.3%
    complete ISO weeks             ->  62 -> 85 agents = +37%
The prior rolling window contained 2026-07-28 (30 distinct agents, ~3x its
neighbours); the current one did not. Nothing about the business changed.
"""
import re

from mcp_calls_deloop import (canonical_external_activity_sql,
                              canonical_external_complete_week_sql)


def test_complete_week_excludes_the_partial_current_week():
    # ★ The whole point: the current (partial) week can never be an operand.
    sql = canonical_external_complete_week_sql(0)
    assert "date_trunc('week', now())" in sql
    assert "created_at < date_trunc('week', now()) - interval '0 weeks'" in sql


def test_weeks_back_selects_the_PRIOR_complete_week():
    sql = canonical_external_complete_week_sql(1)
    assert "- interval '1 weeks'" in sql   # end boundary
    assert "- interval '2 weeks'" in sql   # start boundary


def test_window_is_exactly_one_week_wide_at_every_offset():
    # Kills: an off-by-one that compares a 1-week window against a 2-week one.
    for w in range(0, 5):
        sql = canonical_external_complete_week_sql(w)
        got = sorted(int(m) for m in re.findall(r"interval '(\d+) weeks'", sql))
        assert got == [w, w + 1], f"offset {w} produced {got}"


def test_same_population_predicate_as_the_canonical_rolling_query():
    # Both must count the SAME agents — only the WINDOW may differ, or the
    # comparison is two populations again (the drift this file's sibling
    # helper exists to retire).
    cw = canonical_external_complete_week_sql(0)
    roll = canonical_external_activity_sql(7)
    for frag in ("COUNT(DISTINCT agent_id) AS agents", "COUNT(*) AS calls",
                 "FROM mcp_calls_identity",
                 "WHERE is_public_ip AND is_real_external"):
        assert frag in cw and frag in roll, frag


def test_no_bound_parameters_anywhere():
    # The house rule: these fragments are literal-only. A stray % would hit
    # the psycopg2 empty-tuple trap.
    for w in (0, 1, 3):
        sql = canonical_external_complete_week_sql(w)
        assert "%" not in sql
        assert "$" not in sql


def test_weeks_back_is_int_coerced_against_injection():
    sql = canonical_external_complete_week_sql("2")
    assert "interval '2 weeks'" in sql and "interval '3 weeks'" in sql


def test_rolling_helper_is_UNCHANGED_byte_for_byte():
    # tests/test_canonical_counts_drift.py pins this; asserting it here too
    # means a future edit to the new helper cannot quietly reshape the old one.
    assert canonical_external_activity_sql(7) == (
        "SELECT COUNT(DISTINCT agent_id) AS agents, COUNT(*) AS calls "
        "FROM mcp_calls_identity "
        "WHERE is_public_ip AND is_real_external "
        "AND created_at >= now() - interval '7 days'")
