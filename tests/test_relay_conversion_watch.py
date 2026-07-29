"""Relay conversion watch (2026-07-29) — guards on the funnel instrument.

This watch exists because the funnel was missing its FIRST stage: nothing counted
handoff emission, only opens. With no numerator, "never emitted" and "emitted and
ignored" both read as zero — and they demand opposite responses. That ambiguity
survived for months and was resolved only by calling the server from the client
seat.

So the properties worth guarding are not the arithmetic. They are the ones that
decide whether a future reader can tell what the numbers mean:

  * the proxy must announce itself as a proxy, everywhere it appears;
  * our own traffic must stay out (BOTH existing relay_opens rows are ours, so
    forgetting the filter reports a working funnel where none exists);
  * an unreadable stage must not read as zero;
  * a rate with no denominator must be None, not 0.0 — "nobody converted" and
    "nobody had the chance" are different findings.

Run:  python3 -m pytest tests/test_relay_conversion_watch.py -v
"""
from __future__ import annotations

import inspect

from routes import relay_conversion_watch as rw


def test_stage_one_is_labelled_a_proxy_wherever_it_appears():
    """THE POINT OF THE MODULE. Stage 1 is derived from call status, not from a
    record of emission — it over-counts whenever buildHumanRelay bails. If it
    ever ships unlabelled it becomes another number that looks like evidence."""
    src = inspect.getsource(rw.run_relay_watch)
    assert '"_basis": "proxy_upper_bound"' in src, "stage 1 lost its proxy label"
    # The derived RATES inherit the proxy's basis; labelling only the stage would
    # let a reader treat the rate as exact.
    assert "proxy_upper_bound —" in src, "rates no longer disclose their inherited basis"


def test_our_own_traffic_is_excluded_from_every_stage():
    """Both rows currently in relay_opens are ours ('dchub-ops-verify/1.0',
    'human-simulated/2.0'). Counting them shows a funnel that works."""
    # Checked PER QUERY, not by totals. The first version of this test compared
    # total _not_ours() occurrences against the relay_opens read count — but the
    # filter is also used on the mcp_call_log reads, so the totals stayed
    # satisfied when a relay_opens read lost its guard. It passed its own
    # must-fail control. Counting the right things globally is not the same as
    # checking each thing, which is the defect this whole watch descends from.
    src = inspect.getsource(rw.run_relay_watch)
    queries = [q for q in src.split('_scalar(c, f"""')[1:]]
    opens_queries = [q for q in queries if "FROM relay_opens" in q]
    assert opens_queries, "no relay_opens read found — the test is blind"
    for q in opens_queries:
        body = q.split('"""')[0]
        assert "_not_ours('user_agent')" in body, (
            "a relay_opens read has no self-traffic filter — both rows in that "
            "table are ours, so it would report our probes as real humans:\n"
            f"{body.strip()[:200]}")


def test_exclusion_patterns_are_bound_not_inlined():
    """psycopg2 trap, hit while building this: inlining LIKE patterns puts literal
    % into a query that also carries args, so the whole SQL string gets
    %-formatted and every call 500s. Binding removes the escaping question
    instead of answering it correctly once and hoping the next edit remembers."""
    frag = rw._not_ours("user_agent")
    assert "%s" in frag, "patterns are inlined again — literal % will break the query"
    assert "dchub%" not in frag, "a raw pattern leaked into the SQL fragment"
    assert rw._OURS_PARAM, "the bind list is empty — the filter would match nothing"
    assert set(rw._OURS_PARAM) == set(rw._OURS), "bind list drifted from _OURS"


def test_a_rate_with_no_denominator_is_none_not_zero():
    """'Nobody converted' and 'nobody had the chance' are different findings.
    Returning 0.0 for the second one is how a broken pipe reads as a weak offer."""
    src = inspect.getsource(rw.run_relay_watch)
    i = src.index("def rate(")
    blk = src[i:i + 200]
    assert "return None" in blk, (
        "rate() no longer returns None on a zero denominator — an unmeasurable "
        "rate would report as 0%")


def test_unreadable_stage_reports_indeterminate_not_ok():
    """A query that errors returns None from _scalar. That must sink the whole
    report, not silently count as zero."""
    src = inspect.getsource(rw.run_relay_watch)
    assert 'unreadable = [' in src, "the unreadable-stage tally is gone"
    assert '"INDETERMINATE" if unreadable else "OK"' in src, (
        "an unreadable stage no longer forces INDETERMINATE — it would read as a "
        "measured zero, which is the exact ambiguity this watch exists to remove")


def test_eligible_statuses_are_published_for_inspection():
    """The proxy's basis has to be checkable by whoever reads the number,
    otherwise 'proxy' is just a disclaimer."""
    assert rw.RELAY_ELIGIBLE_STATUSES, "eligible-status list is empty"
    src = inspect.getsource(rw.run_relay_watch)
    assert '"_statuses": list(RELAY_ELIGIBLE_STATUSES)' in src, (
        "the response no longer publishes which statuses the proxy counts")


def test_the_emission_gap_is_named_as_the_next_instrument():
    """The watch can prove the funnel MOVED but not why it didn't. That limit
    must ship with it, or the next reader re-derives it from scratch."""
    src = inspect.getsource(rw.run_relay_watch)
    assert "next_instrument" in src, "the watch no longer states its own blind spot"


def test_relay_live_date_is_carried_so_nobody_averages_across_it():
    """Every pre-2026-07-28 stage-1 number is structurally zero. A rate spanning
    that date mixes 'not shipped' with 'shipped'."""
    assert rw.RELAY_LIVE_SINCE == "2026-07-28"
    src = inspect.getsource(rw.run_relay_watch)
    assert '"relay_live_since": RELAY_LIVE_SINCE' in src
