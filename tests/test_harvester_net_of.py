"""r-harvester-split (2026-09-01) — guards for the harvester DECOMPOSITION.

The thing this file has to prevent is a net-of-harvester figure that is a
no-op reading as a fix. Two ways that happens:

  1. a harvester name is ALREADY outside is_real_external (caught by an
     internal/crawler family, or too short), so "net of harvesters" equals the
     headline and the payload claims a subtraction it never made;
  2. the two halves stop being complementary, so harvester_calls + calls no
     longer reconciles to the headline a reader is dividing by.

Both are checked by EXECUTING the rendered SQL against sqlite over rows built
for the purpose, not by looking for substrings in it — the predicates are
plain LOWER/TRIM/COALESCE/IN/LIKE/LENGTH and run unmodified there.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_calls_deloop import (  # noqa: E402
    HARVESTER_PLATFORMS,
    _AMBIGUOUS_NOT_EXCLUDED,
    canonical_harvester_split_sql,
    external_platform_predicate,
    harvester_predicate,
)


def test_harvesters_are_not_the_ambiguous_class():
    """A name cannot be both 'deliberately kept' and 'split out'."""
    overlap = set(HARVESTER_PLATFORMS) & set(_AMBIGUOUS_NOT_EXCLUDED)
    assert overlap == set(), overlap


@pytest.mark.parametrize("name", sorted(HARVESTER_PLATFORMS))
def test_harvester_is_still_inside_is_real_external(name):
    """★ THE LOAD-BEARING GUARD.

    Netting out a name that the canonical view already dropped subtracts
    nothing while telling the reader it subtracted something. Executes the
    real external_platform_predicate() over a row carrying the name.
    """
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (platform TEXT)")
    con.execute("INSERT INTO t VALUES (?)", (name,))
    kept = con.execute(
        f"SELECT COUNT(*) FROM t WHERE {external_platform_predicate()}"
    ).fetchone()[0]
    assert kept == 1, (
        f"{name!r} is already excluded by external_platform_predicate(), so "
        "every net-of-harvester figure for it is a no-op"
    )


def test_the_guard_above_can_actually_fail():
    """Mutation check: the same assertion must REJECT a name the predicate
    does drop, or it proves nothing about the names that pass."""
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (platform TEXT)")
    con.execute("INSERT INTO t VALUES ('dchub-selfheal')")
    kept = con.execute(
        f"SELECT COUNT(*) FROM t WHERE {external_platform_predicate()}"
    ).fetchone()[0]
    assert kept == 0


@pytest.mark.parametrize(
    "value,is_harvester",
    [("chain-hire", 1), ("datacolo", 1),
     ("  Chain-Hire  ", 1), ("CHAIN-HIRE", 1),
     ("smithery", 0), ("claude", 0), ("chatgpt", 0),
     ("chain-hire-two", 0), ("", 0)],
)
def test_harvester_predicate_matches_exactly_the_named_tags(value, is_harvester):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (platform TEXT)")
    con.execute("INSERT INTO t VALUES (?)", (value,))
    got = con.execute(
        f"SELECT COUNT(*) FROM t WHERE {harvester_predicate()}"
    ).fetchone()[0]
    assert got == is_harvester


def test_harvester_predicate_is_bound_param_safe():
    """Inlined beside LIKE forms — a stray %s or literal % would break the
    psycopg2 callers this module warns about."""
    pred = harvester_predicate()
    assert "%" not in pred, pred


def _run_split(rows):
    """Execute the SELECT list of the canonical split query over `rows`."""
    sql = canonical_harvester_split_sql(7)
    head, sep, _tail = sql.partition(" WHERE ")
    assert sep, sql
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE mcp_calls_identity (platform TEXT, agent_id TEXT)")
    con.executemany("INSERT INTO mcp_calls_identity VALUES (?, ?)", rows)
    cols = [d[0] for d in con.execute(head).description]
    return dict(zip(cols, con.execute(head).fetchone()))


def test_calls_are_additive_by_construction():
    """headline_calls == harvester_calls + calls_net_of_harvesters, which is
    the identity the payload prints and a reader divides by."""
    got = _run_split([("chain-hire", "a")] * 1473
                     + [("claude", "b")] * 40
                     + [("smithery", "c")] * 5
                     + [(None, "d")] * 3)
    assert got["harvester_calls"] == 1473
    assert got["calls_net_of_harvesters"] == 48
    assert got["calls"] == got["harvester_calls"] + got["calls_net_of_harvesters"]


def test_agents_are_deliberately_not_a_partition():
    """One agent_id on BOTH sides is counted on both — the payload says so and
    this pins that it is real behaviour, not an accident to 'fix' later."""
    got = _run_split([("chain-hire", "same"), ("claude", "same")])
    assert got["agents"] == 1
    assert got["harvester_agents"] == 1
    assert got["agents_net_of_harvesters"] == 1
    assert got["harvester_agents"] + got["agents_net_of_harvesters"] > got["agents"]


def test_harvesters_are_not_removed_from_the_real_population():
    """The decomposition must never become an exclusion: a harvester row still
    counts toward the published headline."""
    got = _run_split([("chain-hire", "a")] * 10 + [("claude", "b")] * 2)
    assert got["calls"] == 12


def test_offset_window_matches_the_sibling_helpers():
    """Prior-period form, same signature as canonical_external_activity_sql /
    canonical_top_caller_sql, so a WoW cannot be built across two bases."""
    cur = canonical_harvester_split_sql(7)
    prior = canonical_harvester_split_sql(7, 7)
    assert cur.endswith("created_at >= now() - interval '7 days'")
    assert prior.endswith(
        "created_at >= now() - interval '14 days' "
        "AND created_at < now() - interval '7 days'")


def test_platform_attribution_does_not_keep_a_second_copy():
    """The drift this PR exists to close: the tuple lived in one module while
    the headline read another."""
    from routes import platform_attribution as pa
    assert pa.HARVESTER_PLATFORMS is HARVESTER_PLATFORMS
    assert pa.classify_platform("chain-hire") == "harvester"
