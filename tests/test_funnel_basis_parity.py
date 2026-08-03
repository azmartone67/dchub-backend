"""One card, one basis — the funnel agents/calls pair.

2026-08-03: the dashboard rendered `76 agents` (canonical identity query,
rolling 7d INCLUDING today) beside `8,652 calls` and `+323.7%` (mcp_tool_calls,
complete days EXCLUDING today, its own predicate). Two tables, two predicates,
two windows on one card — the growth rates described different populations and
the pair visibly drifted every afternoon.

canonical_external_activity_sql returns agents AND calls together and its own
docstring says dashboards should render that pair. These tests pin the offset
window that makes a same-basis WoW possible, and the byte-identical zero-offset
string that keeps the emitter-drift test meaningful.
"""
import re

import pytest

from mcp_calls_deloop import canonical_external_activity_sql as SQL


def test_zero_offset_output_is_unchanged():
    """test_canonical_counts_drift.py pins emitters against this exact string.
    A whitespace change here would read as every caller drifting at once."""
    s = SQL(7)
    assert s == (
        "SELECT COUNT(DISTINCT agent_id) AS agents, COUNT(*) AS calls "
        "FROM mcp_calls_identity "
        "WHERE is_public_ip AND is_real_external "
        "AND created_at >= now() - interval '7 days'"
    )
    # the default argument must still mean "last 7 days"
    assert SQL() == s


def test_offset_expresses_the_prior_window_not_a_longer_one():
    """The classic off-by-one here is emitting `interval '14 days'` alone,
    which returns the last FOURTEEN days — making the prior window include the
    current one and the WoW read ~0% forever."""
    s = SQL(7, 7)
    assert "interval '14 days'" in s
    assert "interval '7 days'" in s
    assert "created_at >=" in s and "created_at <" in s
    # the upper bound must be the SHORTER interval — >= 14d ago AND < 7d ago
    lower = re.search(r"created_at >= now\(\) - interval '(\d+) days'", s)
    upper = re.search(r"created_at < now\(\) - interval '(\d+) days'", s)
    assert lower and upper
    assert int(lower.group(1)) == 14
    assert int(upper.group(1)) == 7


def test_offset_windows_do_not_overlap():
    cur, prior = SQL(7), SQL(7, 7)
    # current has no upper bound (ends at now); prior ends where current begins
    assert "created_at <" not in cur
    assert "created_at < now() - interval '7 days'" in prior


def test_predicates_are_identical_across_windows():
    """A prior window that silently used different exclusions would produce a
    WoW comparing two populations — the exact bug this fixes."""
    for s in (SQL(7), SQL(7, 7), SQL(30), SQL(30, 30)):
        assert "FROM mcp_calls_identity" in s
        assert "WHERE is_public_ip AND is_real_external" in s
        assert "COUNT(DISTINCT agent_id) AS agents" in s
        assert "COUNT(*) AS calls" in s


def test_fragment_stays_literal_only():
    """No bound params — callers interpolate this straight into execute()."""
    for s in (SQL(7), SQL(7, 7)):
        assert "%s" not in s and "%(" not in s


def test_ints_are_coerced_so_the_fragment_cannot_carry_input():
    s = SQL("7", "7")  # strings must not reach the SQL
    assert "interval '14 days'" in s
    assert "'7'" not in s


def test_funnel_publishes_the_same_basis_pair():
    """The payload must expose prior + WoW on the canonical basis, or the
    dashboard has to borrow a WoW from the other basis again."""
    src = open("flask_mcp_endpoints.py").read()
    for field in ("real_external_calls_prior_7d", "real_external_calls_wow_pct",
                  "real_external_agents_prior_7d", "real_external_agents_wow_pct"):
        assert field in src, f"{field} not published by the funnel"
    assert "_canonical_activity_sql(7, 7)" in src, \
        "prior window must come from the canonical helper, not a hand-rolled query"


def test_dashboard_headline_uses_the_canonical_calls_figure():
    src = open("static/mcp-dashboard.html").read()
    # headline must prefer the canonical basis
    assert "d.real_external_calls_7d ??" in src
    # and the complete-days figure must still be present, as a labeled secondary
    assert "complete-days variant" in src
    assert "d.tool_calls_7d_complete_real" in src


def test_dashboard_pairs_both_wow_values_from_one_payload():
    src = open("static/mcp-dashboard.html").read()
    assert "d.real_external_agents_wow_pct" in src
    assert "d.real_external_calls_wow_pct" in src
