"""The retention cohort and the headline agent count disagreed ~3x (2026-08-03).

House rule: tests NEVER import main. Everything here reads files directly, and
nothing runs at module scope.

Live dashboard, same page, same moment:
    headline "Agents (rolling 7d · distinct external)" = 76   (canonical grain)
    retention table, wk 2026-07-27, "New ext IPs"      = 246  (raw ip_address)

"Inflow is fine; retention is the leak" is computed as returning/new. With a
denominator inflated by Cloudflare POPs and registry crawlers, that sentence
cannot be evaluated — it reads as broken retention whether or not retention is
broken.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(*parts) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_a_canonical_cohort_is_computed():
    src = _src("routes", "mcp_retention.py")
    assert '"agent_cohort"' in src
    assert "FROM mcp_calls_identity" in src
    assert "is_public_ip AND is_real_external" in src


def test_it_is_added_beside_the_legacy_series_not_instead_of_it():
    """★The two together are the evidence; either alone is an assertion. And
    silently swapping the series under a page people already read would change
    a headline number with no way to see why."""
    src = _src("routes", "mcp_retention.py")
    assert '"ip_cohort"' in src, "the legacy series must survive"
    i, j = src.index('"ip_cohort"'), src.index('"agent_cohort"')
    assert i < j, "agent_cohort should be additive, after ip_cohort"


def test_the_canonical_series_counts_agents_not_ips():
    src = _src("routes", "mcp_retention.py")
    block = src[src.index('out["agent_cohort"]') - 1800:src.index('out["agent_cohort"]')]
    assert "COUNT(DISTINCT agent_id)" in block
    assert "COUNT(DISTINCT ip_address)" not in block


def test_a_missing_view_is_unmeasured_not_zero_returning():
    """★An empty agent_cohort must never read as 'no returning agents' — that
    is a retention cliff invented by a missing view."""
    src = _src("routes", "mcp_retention.py")
    i = src.index('out["agent_cohort"] = []')
    window = src[i:i + 500]
    assert "UNMEASURED" in window
    assert "NOT zero" in window


def test_the_failure_arm_rolls_back_on_the_real_connection():
    """A failed SELECT aborts the transaction; without a rollback the queries
    after it fail too and one added metric takes out the whole page."""
    src = _src("routes", "mcp_retention.py")
    i = src.index('out["agent_cohort"] = []')
    assert "c.rollback()" in src[i:i + 700]


def test_the_note_says_which_number_to_trust():
    src = _src("routes", "mcp_retention.py")
    assert "the population you sell to" in src
    assert "inflated denominator" in src
