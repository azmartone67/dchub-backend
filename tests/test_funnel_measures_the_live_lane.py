"""The conversion funnel must measure the lane production actually serves.

2026-08-24: stages 2-5 of /api/v1/mcp/conversion-funnel read `mcp_pair_codes`
and returned 0/0/0/0 for all 25 tools, which reads as "the paywall converts
nobody". It was not measuring the paywall at all -- the pair-code lane was
retired in production on 2026-07-07 (last real mint id 1098 @ 18:30Z).

What the live gate hands an agent, verified by calling get_fiber_intel
anonymously against https://dchub.cloud/mcp on 2026-08-24:

    https://dchub.cloud/upgrade/h/<payload>.<sig>

built by server.mjs buildHumanRelay -> for_your_human. Its opens log to
`relay_opens`. flask_mcp_endpoints.py already reached this conclusion for
human_acted DEFINITION v3; this endpoint had not.

The self-traffic filter is load-bearing, not hygiene: of 50 all-time
relay_opens rows, exactly ONE is on a real user-agent. Publishing the
unfiltered 50 would report our own QA harness as customer demand.
"""
import os
import re

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "routes", "mcp_funnel.py")
_TEXT = open(_SRC, encoding="utf-8").read()


def test_funnel_reads_the_live_lane():
    assert "relay_opens" in _TEXT, (
        "the funnel does not read relay_opens, so it cannot see the "
        "/upgrade/h/<payload>.<sig> lane the live paywall actually serves. "
        "Stages above 1 will read 0 for every tool regardless of traffic."
    )


def test_live_lane_open_count_filters_self_traffic():
    """The relay_opens count must carry a real-UA predicate in the same query."""
    m = re.search(r"FROM\s+relay_opens(.*?)\"\"\"", _TEXT, re.S | re.I)
    assert m, "no relay_opens query found -- retarget this guard"
    where = m.group(1)
    assert ("_ua_ok" in where) or re.search(r"user_agent", where, re.I), (
        "the relay_opens count has no user-agent filter. 49 of 50 all-time rows "
        "are our own probes (44 x dchub-qa-superuser), so an unfiltered count "
        f"publishes QA traffic as demand. WHERE clause was: {where!r}"
    )


def test_self_filter_fails_closed():
    """If the canonical predicate can't be imported, we must NOT count everything."""
    i = _TEXT.find("real_ua_predicate")
    assert i != -1, "canonical real_ua_predicate is no longer used"
    # the except branch that follows the import must still install a predicate
    tail = _TEXT[i:i + 1400]
    assert "except" in tail, "no fallback around the real_ua_predicate import"
    exc = tail[tail.find("except"):]
    assert "_ua_ok" in exc and ("!~*" in exc or "NOT ILIKE" in exc or "not ilike" in exc), (
        "the fallback for a failed real_ua_predicate import does not install a "
        "restrictive predicate. Failing OPEN here is how a probe becomes a "
        "customer in a published number -- fail closed."
    )


def test_retired_lane_is_labelled_not_presented_as_the_funnel():
    for k in ("legacy_2_codes_minted", "legacy_3_redeem_viewed",
              "legacy_4_stripe_clicked", "legacy_5_converted"):
        assert k in _TEXT, (
            f"{k} missing -- the retired pair-code lane must be reported under a "
            "legacy_ prefix so no reader mistakes it for the live funnel."
        )
    assert not re.search(r'"[2-4]_(codes_minted|redeem_viewed|stripe_clicked)"', _TEXT), (
        "an un-prefixed pair-code stage key is back in the stages dict; that is "
        "the exact shape that reported a fake 100% leak on every tool."
    )


def test_redeem_viewed_is_not_sold_as_a_human_view():
    """984 of 1,098 rows get redeem_viewed_at within 2s of created_at."""
    assert re.search(r"NOT a human view|not a human view", _TEXT), (
        "the legend/comments no longer warn that redeem_viewed_at is written by "
        "the mint itself. Without that warning it reads as human intent."
    )
