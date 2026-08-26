"""Guard: the no-key/no-session cohort is measurable, and measured SEPARATELY.

2026-08-26 (r-anon-attrib). Every Smithery / listed-connector caller is
anonymous — the listing asks for no API key and the gateway mediates the
transport, so the call carries neither a durable key nor an Mcp-Session-Id.
server.mjs `_goUrl` therefore embedded no client_reference_id at all, and the
click stamped `mcp_checkout_clicks.ref = ''` and joined nothing, forever.

server.mjs now mints an ephemeral `a-<hex>` attribution id for exactly that
class. These tests pin the two things that must both stay true:

  1. the anon lane is COUNTED (it stops living in
     checkout_clicks_unattributed), and
  2. the cohort numbers do NOT absorb it. `agent_click` / `agent_paid` read
     mcp_high_intent_sessions, which an anonymous caller never enters. #3171
     deliberately did not loosen that join — loosening it is how the 5 curl QA
     probes would have become customers in a published number.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from routes.checkout_click_tracker import _ref_kind
from routes.mcp_high_intent_claim import build_step_waterfall


def _capture_run(**by_marker):
    """Scalar executor that returns per-query values and records every SQL."""
    seen = []

    def run(sql):
        seen.append(sql)
        for marker, val in by_marker.items():
            if marker in sql:
                return val
        return 0

    run.seen = seen
    return run


def _anon_click_sql(seen):
    # Matched on the opening "'a-" only, NOT on the predicate spelling: selecting by
    # "LEFT(cc.ref, 2)" -- or even by the closing quote of "'a-'" -- means a
    # rewrite to LIKE 'a-%' empties this list and every assertion over it passes
    # vacuously. Both spellings were caught doing exactly that (mutation, 2026-08-26).
    return [s for s in seen
            if "FROM mcp_checkout_clicks cc " in s
            and "JOIN mcp_checkout_clicks cc ON" not in s
            and "'a-" in s]


def _anon_paid_sql(seen):
    return [s for s in seen if "t.mcp_session_id" in s and "'a-" in s]


def test_ref_kind_classifies_the_anon_prefix():
    assert _ref_kind("a-0f1e2d3c4b5a69788796a5b4c3d2e1f0") == "anon"
    # ...and does not steal the classes that already existed.
    assert _ref_kind("pk-" + "0" * 64) == "pack_key"
    assert _ref_kind("k-" + "0" * 64) == "sub_key"
    assert _ref_kind("6f61-uuid-shaped-session") == "session"
    assert _ref_kind("") == "none"


def test_anon_lane_is_queried_and_surfaced():
    run = _capture_run()
    agent = build_step_waterfall(run)["branch_agent"]
    assert "checkout_clicks_anon" in agent
    assert "paid_anon" in agent
    assert _anon_click_sql(run.seen), "anon click lane was never queried"
    assert _anon_paid_sql(run.seen), "anon paid lane was never queried"


def test_anon_lane_carries_the_same_self_traffic_filter():
    """The whole point of #3171's UA filter was that 5 of 6 prod rows were curl
    QA. A new lane that skipped it would re-import exactly that inflation."""
    run = _capture_run()
    build_step_waterfall(run)
    for sql in _anon_click_sql(run.seen) + _anon_paid_sql(run.seen):
        assert "cc.user_agent" in sql and "!~*" in sql, (
            "anon lane does not filter script UAs: %s" % sql)
        assert "curl/" in sql, "not the canonical _SCRIPT_UA_SQL token list"
        assert "cc.sig_ok" in sql, "unsigned tokens are not believable clicks"


def test_anon_lane_uses_no_percent_literal():
    """`run` is caller-supplied and the two live callers bind params
    differently: mcp_high_intent_claim's _scalar passes an empty tuple (psycopg2
    still %-interpolates, so a literal % raises), funnel_health's _ds passes no
    params (so '%%' stays a literal '%%' and matches nothing). Both swallow the
    error and return 0, so the wrong choice is invisible either way. Any % here
    is a bug in one of the two callers."""
    run = _capture_run()
    build_step_waterfall(run)
    for sql in _anon_click_sql(run.seen) + _anon_paid_sql(run.seen):
        assert "%" not in sql, (
            "anon lane uses a %% literal — silently 0 under one of the two "
            "executors: %s" % sql)


def test_anon_clicks_do_not_enter_the_cohort_numbers():
    """A DB whose ONLY clicks are anonymous must still report cohort 0."""
    run = _capture_run(**{
        "JOIN mcp_checkout_clicks cc ON": 0,   # cohort join: matches nothing
        "LEFT(cc.ref, 2) = 'a-'": 7,           # 7 anonymous clicks
    })
    wf = build_step_waterfall(run)
    agent = wf["branch_agent"]
    assert agent["checkout_clicks_anon"] == 7
    assert agent["checkout_click"] == 0, "anon clicks leaked into the branch summary"
    assert agent["paid"] == 0, "anon payments leaked into agent_paid"
    # ...and the STEP, which is a SECOND copy of the same number. Asserting only
    # on branch_agent let a mutation fold anon clicks into agent_click undetected.
    steps = {s["step"]: s["count"] for s in wf["steps"]}
    assert steps["agent_click"] == 0, "anon clicks leaked into the agent_click step"
    assert steps["agent_paid"] == 0, "anon payments leaked into the agent_paid step"


def test_anon_diagnostics_are_not_steps():
    """Different denominator — they must never gain a drop_from_prev or feed
    the breakage alarm."""
    wf = build_step_waterfall(_capture_run(**{"LEFT(cc.ref, 2) = 'a-'": 9}))
    names = {s["step"] for s in wf["steps"]}
    assert "checkout_clicks_anon" not in names
    assert "paid_anon" not in names
    assert wf["alarm"] is False
