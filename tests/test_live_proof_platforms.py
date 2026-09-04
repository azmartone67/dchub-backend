"""Who counts as a platform that calls our tools — the headline's judgement.

/api/v1/stats/live-proof publishes platforms_30d, the source of the homepage
line naming which platforms call MCP tools and how often. Until 2026-09-03 it
counted raw mcp_tool_calls with neither the is_real_external filter nor the
self-traffic exclusion that /api/ai/tracking applied to the same 30 days.
Claude read 1,071 in the headline against 492 on its card.

The gap was never a double-count — both sides collapse vendor aliases. It was
our own traffic: the operator's agent client writes mcp_client 'claude' /
user_agent 'node', byte-identical to a prospect's.

★ WHY THIS FILE EXISTS AT ALL. The fix first put the rule inline in the route,
and a mutation that kept our-traffic-only platforms in the headline PASSED
every source-text guard aimed at that endpoint. A judgement inline in a route
is a judgement nobody can test. It moved to live_proof_platforms so these could
be written against behaviour instead of against the text of a function.
"""
import pytest

from live_proof_platforms import shape_platforms


def test_a_platform_whose_every_call_was_ours_is_not_a_platform():
    """★ THE CONTROL. This is the defect in miniature: 7 calls, all of them us.
    Naming it as an integrating platform is the claim that must not survive."""
    rows, excluded = shape_platforms([("grok", 0, 7)], ("88e20dac",))
    assert rows == []
    assert excluded["platforms_removed_entirely"] == ["grok"]
    assert excluded["calls_removed"] == 7


def test_a_platform_with_real_calls_survives_with_both_figures():
    rows, excluded = shape_platforms([("claude", 492, 1071)])
    # ★ 2026-09-04: this compared the WHOLE dict with ==, so it failed the
    # moment r-burst-vs-adoption added active_days / last_call / recurring —
    # additive fields that break nothing. The test's own name says what it is
    # for: both figures survive. Pin that, and pin the absent-column contract
    # separately, so a new field is welcome and a REMOVED one still fails.
    assert len(rows) == 1
    got = rows[0]
    for k, v in (("platform", "claude"), ("calls", 492),
                 ("calls_including_self_traffic", 1071)):
        assert got[k] == v, "%s changed: %r" % (k, got.get(k))
    # A 3-column row measures no recency, and unmeasured must never read as
    # "this platform called on a single day".
    assert got["active_days"] is None and got["recurring"] is None
    assert excluded["calls_removed"] == 579


def test_removal_is_published_never_silent():
    _rows, excluded = shape_platforms([("claude", 492, 1071)], ("88e20dac",))
    # "no exclusion applied" must be distinguishable from "applied, found
    # nothing" — the fail-open contract the endpoint promises.
    assert excluded["self_traffic_session_prefixes"] == ["88e20dac"]
    _rows2, open_ = shape_platforms([("claude", 492, 492)], ())
    assert open_["self_traffic_session_prefixes"] == []
    assert open_["calls_removed"] == 0


def test_the_basis_names_the_population_and_the_rule():
    _rows, excluded = shape_platforms([("claude", 1, 1)])
    b = excluded["basis"]
    assert "is_public_ip AND is_real_external" in b
    assert "Declared, never inferred" in b
    # The gross sibling must not be mistaken for an unfiltered total.
    assert "not an unfiltered" in b


def test_a_zero_call_platform_with_no_gross_is_not_reported_as_removed():
    # Nothing was observed, so nothing was removed. Claiming otherwise would
    # publish a statement about traffic that never existed.
    rows, excluded = shape_platforms([("kimi", 0, 0)])
    assert rows == []
    assert excluded["platforms_removed_entirely"] == []


def test_platform_names_are_normalized_and_blanks_dropped():
    rows, _ = shape_platforms([("  Claude  ", 5, 5), ("   ", 9, 9), (None, 9, 9)])
    assert [r["platform"] for r in rows] == ["claude"]


@pytest.mark.parametrize("bad", [
    ("claude",), (), None, ("claude", "x", 1), ("claude", 1),
])
def test_malformed_rows_are_dropped_not_counted_as_removed(bad):
    # A wiring fault is not a platform, and must not be published as one that
    # was "removed entirely" — that is a claim about observed traffic.
    rows, excluded = shape_platforms([bad])
    assert rows == []
    assert excluded["platforms_removed_entirely"] == []
    assert excluded["calls_removed"] == 0


def test_a_gross_below_the_filtered_count_cannot_manufacture_a_removal():
    # calls is a SUBSET of gross by construction. A gross below it means the
    # two were paired from different queries — the cross-basis error this
    # whole area exists to end. It must never read as negative removal.
    rows, excluded = shape_platforms([("claude", 500, 10)])
    assert rows[0]["calls_including_self_traffic"] == 500
    assert excluded["calls_removed"] == 0


def test_order_is_deterministic_so_two_requests_cannot_reorder_ties():
    rows, _ = shape_platforms([("grok", 33, 33), ("chatgpt", 33, 33),
                               ("claude", 492, 1071)])
    assert [r["platform"] for r in rows] == ["claude", "chatgpt", "grok"]


def test_empty_input_is_empty_output_not_an_error():
    rows, excluded = shape_platforms([])
    assert rows == []
    assert excluded["calls_removed"] == 0
