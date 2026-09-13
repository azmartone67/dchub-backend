"""human_acted v8 — the link agents actually relay can move the headline.

Measured 2026-09-09 from outside: a gated tools/call puts /go/c/<token> in
content[0].text and the /upgrade/h/ link only in structuredContent. Through v5
the headline read relay_opens alone, which only /upgrade/h/ writes. v7 counted
/go/c/ clicks from 2026-09-10, but only BESIDE the headline, so a human clicking
the link their agent showed them still could not move steps.human_acted.

Every check binds to a value a builder produces, or to the endpoint's AST
(flask_mcp_endpoints imports a DB pool, so it is parsed, never imported).
"""
from __future__ import annotations

import ast
import os
import re

import pytest

from mcp_calls_deloop import external_session_predicate
from routes import handoff_definition as H

_ENDPOINT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "flask_mcp_endpoints.py")
_IV = "30 days"


def _lanes(sql: str) -> list:
    head, sep, rest = sql.partition(" from (")
    assert sep and head == "select count(distinct u.sid)", sql[:120]
    assert rest.endswith(") u"), sql[-60:]
    return rest[:-len(") u")].split(" union ")


def _anchor(sql: str) -> str:
    return re.search(r"\bfrom ([a-z_]+) ", sql).group(1)


def test_the_headline_is_the_relay_lane_union_the_relayed_checkout_lane():
    lanes = _lanes(H.human_acted_count_sql(_IV))
    assert len(lanes) == 2, lanes
    assert _anchor(lanes[0]) == _anchor(H.human_acted_v5_count_sql(_IV)) \
        == "mcp_high_intent_sessions"
    # The /go/c/ table comes from the v7 builder, which
    # test_human_acted_v7_relayed_checkout proves is the table /go/c/ INSERTs.
    assert _anchor(lanes[1]) == _anchor(H.human_acted_v7_count_sql(_IV))
    assert _anchor(lanes[1]) != "relay_opens"


def test_each_lane_is_the_published_instrument_not_a_copy():
    """Byte-bound to the counts published beside the headline, so a lane
    cannot drift from the figure a reader checks it against."""
    relay, checkout = _lanes(H.human_acted_count_sql(_IV))
    assert relay == H.human_acted_v5_count_sql(_IV).replace(
        "select count(distinct s.mcp_session_id) ",
        "select s.mcp_session_id as sid ", 1)
    assert checkout == H.human_acted_v7_count_sql(_IV).replace(
        "select count(distinct cc.ref) ", "select cc.ref as sid ", 1)


def test_the_exclusion_binds_once_per_lane_on_that_lanes_identity():
    ext_s = external_session_predicate("s.mcp_session_id")
    ext_cc = external_session_predicate("cc.ref")
    relay, checkout = _lanes(H.human_acted_count_sql(_IV))
    assert relay.count(ext_s) == 1 and ext_cc not in relay
    assert checkout.count(ext_cc) == 1 and ext_s not in checkout
    for lane in _lanes(H.human_acted_count_sql(_IV, include_self_traffic=True)):
        assert ext_s not in lane and ext_cc not in lane, lane


def test_the_checkout_lane_keeps_the_v6_lesson():
    """A bare session ref only: on pk-/k-/a- refs the exclusion passes
    vacuously, so those clicks stay in human_acted_v7_links_clicked."""
    checkout = _lanes(H.human_acted_count_sql(_IV))[1]
    assert ("cc.ref_kind = '%s'"
            % H.RELAYED_CHECKOUT_DELOOPABLE_REF_KIND) in checkout
    assert H.relayed_checkout_signed() in checkout
    assert H.relayed_checkout_real_ua() in checkout


def test_a_session_that_clicked_the_relayed_checkout_did_not_abandon():
    """adoption_master_shell's `abandoned` is NOT this predicate. Exact shape,
    built from the parts: a containment check survives `and false` appended to
    the click join, which silently re-abandons every /go/c/ clicker."""
    pred = H.human_acted_session_predicate("s")
    assert pred == ("(" + H.human_acted_relay_predicate("s")
                    + " or exists (select 1 " + H._RELAYED_CHECKOUT_FROM
                    + " where " + H.relayed_checkout_session_filters()
                    + " and cc.ref = s.mcp_session_id))"), pred


@pytest.mark.parametrize("iv", ["24 hours", "7 days", "30 days"])
@pytest.mark.parametrize("incl", [False, True])
def test_no_literal_percent_and_the_window_reaches_both_lanes(iv, incl):
    sql = H.human_acted_count_sql(iv, include_self_traffic=incl)
    assert "%" not in sql and "ilike" not in sql.lower()
    for lane in _lanes(sql):
        assert ("interval '%s'" % iv) in lane, lane
    assert "%" not in H.human_acted_session_predicate("s")


def _funnel_fn():
    """The innermost function that assigns the published `steps` dict."""
    tree = ast.parse(open(_ENDPOINT, encoding="utf-8").read())
    best = None
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and _assigned(fn, "steps"):
            if best is None or (fn.end_lineno - fn.lineno
                                < best.end_lineno - best.lineno):
                best = fn
    assert best is not None, "no function assigns `steps`"
    return best


def _assigned(fn, name):
    return [n.value for n in ast.walk(fn) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name
                    for t in n.targets)]


def _is_union_call(value, include_self: bool) -> bool:
    if not (isinstance(value, ast.Call)
            and getattr(value.func, "id", None) == "one"
            and len(value.args) == 1 and isinstance(value.args[0], ast.Call)):
        return False
    inner = value.args[0]
    flag = any(k.arg == "include_self_traffic"
               and isinstance(k.value, ast.Constant) and k.value.value is True
               for k in inner.keywords)
    return (getattr(inner.func, "id", None) == "_human_acted_count_sql"
            and flag == include_self)


def _published(fn) -> dict:
    return {k.value: v for d in ast.walk(fn) if isinstance(d, ast.Dict)
            for k, v in zip(d.keys, d.values) if isinstance(k, ast.Constant)}


def test_the_endpoint_headline_is_the_canonical_union():
    fn = _funnel_fn()
    opened = _assigned(fn, "opened")
    assert len(opened) == 1 and _is_union_call(opened[0], False), \
        [ast.dump(v) for v in opened]
    incl = _assigned(fn, "opened_incl_self")
    assert len(incl) == 1 and _is_union_call(incl[0], True), \
        [ast.dump(v) for v in incl]
    steps = _assigned(fn, "steps")
    assert len(steps) == 1 and isinstance(steps[0], ast.Dict)
    bound = {k.value: v for k, v in zip(steps[0].keys, steps[0].values)}
    assert isinstance(bound["human_acted"], ast.Name) \
        and bound["human_acted"].id == "opened"
    pub = _published(fn)
    for key, name in (("human_acted_v5_before_relayed_checkout", "opened_v5"),
                      ("human_acted_including_self_traffic", "opened_incl_self")):
        assert isinstance(pub.get(key), ast.Name) and pub[key].id == name, key
        assert key in H.HUMAN_ACTED_DEFINITION_CHANGELOG[
            H.HUMAN_ACTED_DEFINITION_VERSION] or key.endswith("self_traffic")


def test_removed_is_the_same_union_with_and_without_the_exclusion():
    """v3 minus the headline goes negative the first time a /go/c/ click lands
    that v3 cannot see."""
    removed = _published(_funnel_fn())["human_acted_removed"]
    subs = [n for n in ast.walk(removed)
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub)]
    assert len(subs) == 1, [ast.dump(s) for s in subs]
    assert isinstance(subs[0].left, ast.Name) \
        and subs[0].left.id == "opened_incl_self"
    assert isinstance(subs[0].right, ast.Name) and subs[0].right.id == "opened"


def test_the_promoting_version_is_described_and_the_unpromoted_ones_say_so():
    assert H.HUMAN_ACTED_DEFINITION_VERSION >= 8
    for v in (6, 7):
        assert H.HUMAN_ACTED_DEFINITION_CHANGELOG[v].startswith(
            "DEFINED, NEVER PROMOTED."), v
