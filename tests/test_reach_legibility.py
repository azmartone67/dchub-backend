"""`reach 0.00` had to stop reading as "nobody uses DC Hub".

The RAG board published `rag_agents_7d: 0` with no denominator. Measured
2026-09-19, /api/v1/ai/reach read real_agents_7d 70 and real_calls_7d 534 over
45 distinct tools — from the SAME table, under the SAME is_public_ip +
is_real_external predicate, in the same week — while both RAG doors sat at
zero.

So the zero was true and the sentence was misleading. "No agents" and "70
agents, none of which walked through a RAG door" are opposite problems with
opposite remedies, and the old flag could not tell them apart. A north-star
that reads 0 beside another surface's 70 gets dismissed as broken telemetry,
and this one is not broken.

The rule these tests enforce is narrow: the flag may only assert what the tick
actually measured.

Stdlib + pytest; no DB, no network.
"""
import re
import pathlib

import pytest

from routes import rag_master_shell as rag

ROOT = pathlib.Path(__file__).resolve().parents[1]
TRAFFIC_CLAIM = "The platform has the traffic"


def _m(agents=0, total=70, tools=45, packs=0):
    return {"rag_agents_7d": agents, "rag_context_packs_7d": packs,
            "real_agents_7d": total, "tools_in_use_7d": tools}


# ── the flag carries its denominator ────────────────────────────────────────
def test_the_flag_names_numerator_and_denominator():
    f = rag._reach_flag(_m())
    assert "0 of 70 real external agents" in f
    assert "45 distinct tools" in f
    for tool in rag._REACH_TOOLS:
        assert tool in f, "the flag must name the doors it is counting"


def test_the_flag_asserts_the_traffic_verdict_only_when_it_measured_traffic():
    assert TRAFFIC_CLAIM in rag._reach_flag(_m(agents=0, total=70))


def test_an_unreadable_denominator_is_said_out_loud_and_claims_nothing():
    """★ 'could not measure agents' must not render as 'no agents'. And with no
    denominator the flag has NOT earned the traffic verdict -- asserting it
    anyway is precisely the unsupported claim this change removes."""
    f = rag._reach_flag(_m(total=None, tools=None))
    assert "not readable" in f
    assert "nobody is calling DC Hub" in f
    assert TRAFFIC_CLAIM not in f, (
        "claimed the platform has traffic without having measured any")


def test_a_genuinely_empty_platform_does_not_claim_traffic():
    """0 of 0 is the one case where 'the platform has the traffic' is FALSE."""
    f = rag._reach_flag(_m(agents=0, total=0, tools=0))
    assert TRAFFIC_CLAIM not in f


def test_no_flag_once_reach_meets_target():
    assert rag._reach_flag(_m(agents=rag._REACH_TARGET)) is None
    assert rag._reach_flag(_m(agents=rag._REACH_TARGET + 5)) is None


def test_no_flag_when_the_numerator_itself_is_unknown():
    assert rag._reach_flag({"rag_agents_7d": None}) is None


# ── the pair cannot drift ───────────────────────────────────────────────────
def test_both_numbers_come_from_one_query_so_they_cannot_disagree():
    """★ The containment rag_agents_7d <= real_agents_7d must hold BY
    CONSTRUCTION, not by two surfaces happening to agree. Reading the total
    from /ai/reach instead would put a second FROM, a second WHERE and a second
    instant behind the same sentence."""
    src = (ROOT / "routes/rag_master_shell.py").read_text()
    i = src.index("COUNT(*) FILTER (WHERE tool_name IN ('get_market_context'")
    stmt = src[i - 400: i + 900]
    frm = re.findall(r"FROM\s+mcp_calls_identity", stmt)
    whr = re.findall(r"WHERE\s+created_at", stmt)
    assert len(frm) == 1, f"reach reads mcp_calls_identity {len(frm)}x — must be one query"
    assert len(whr) == 1, "the denominator must share the numerator's WHERE"
    for agg in ("COUNT(DISTINCT agent_id),", "COUNT(DISTINCT tool_name)"):
        assert agg in stmt, f"missing {agg} — the denominator is not in this query"


def test_the_denominator_defaults_to_none_not_zero():
    """A default of 0 would render '0 of 0 agents' on an unreadable tick, which
    is the exact misreading this change exists to prevent."""
    src = (ROOT / "routes/rag_master_shell.py").read_text()
    assert "default=(0, 0, 0, None, None)" in src
    assert '"real_agents_7d"] = None if rr[3] is None' in src
    assert '"tools_in_use_7d"] = None if rr[4] is None' in src


@pytest.mark.parametrize("key", ["real_agents_7d", "tools_in_use_7d"])
def test_the_measure_skeleton_carries_the_new_keys(key):
    """The skeleton is what a failed query falls back to; a key absent there
    reads as KeyError-or-zero downstream instead of 'unmeasured'."""
    src = (ROOT / "routes/rag_master_shell.py").read_text()
    i = src.index('"rag_agents_7d": None')
    assert f'"{key}": None' in src[i - 200: i + 300]
