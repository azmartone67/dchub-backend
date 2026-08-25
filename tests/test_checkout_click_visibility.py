"""Guard: an unattributable checkout click must not read as "nobody clicked".

2026-08-25. `mcp_checkout_clicks` has exactly one reader — the cohort join in
build_step_waterfall — and that join can only see clicks whose ref binds to a
claim-flow session or durable key. Prod held 1 real human click (Windows
browser, 08-13, $10 pack) with ref='' because server.mjs `_goUrl` embeds a
client_reference_id only for keyed (pk-/k-) or session-bearing callers. The
funnel reported checkout_click=0 while a human demonstrably clicked, and no
other surface could contradict it.

These tests pin the PAIR: the cohort count stays cohort-scoped, and the raw
diagnostics see the click it cannot.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from routes.mcp_high_intent_claim import build_step_waterfall


def _make_run(cohort_clicks, all_clicks, unattributed):
    """Fake scalar executor: a DB where the only click is unattributable."""
    seen = []

    def run(sql):
        seen.append(sql)
        if "JOIN mcp_checkout_clicks cc ON" in sql:
            return cohort_clicks
        if "FROM mcp_checkout_clicks cc " in sql:
            return unattributed if "COALESCE(cc.ref,'') = ''" in sql else all_clicks
        return 0

    run.seen = seen
    return run


def test_unattributed_click_is_visible_even_though_cohort_join_misses_it():
    run = _make_run(cohort_clicks=0, all_clicks=1, unattributed=1)
    agent = build_step_waterfall(run)["branch_agent"]

    # The cohort number is honest about its own scope — it stays 0.
    assert agent["checkout_click"] == 0
    # ...but the click is no longer invisible. This is the whole fix: reading
    # checkout_click alone said "no human clicked"; the pair says "one did,
    # and we could not attribute them".
    assert agent["checkout_clicks_all"] == 1
    assert agent["checkout_clicks_unattributed"] == 1


def test_raw_click_query_excludes_script_user_agents():
    """5 of the 6 prod rows were curl QA probes. They must not inflate this."""
    run = _make_run(0, 0, 0)
    build_step_waterfall(run)
    raw = [s for s in run.seen
           if "FROM mcp_checkout_clicks cc " in s
           and "JOIN mcp_checkout_clicks cc ON" not in s]
    assert raw, "raw checkout-click diagnostics were never queried"
    for sql in raw:
        assert "cc.user_agent" in sql and "!~*" in sql, (
            "raw click count does not filter script UAs — curl probes will "
            "inflate it: %s" % sql)
        assert "curl/" in sql, (
            "UA filter is not the canonical _SCRIPT_UA_SQL token list")
        # Signature-invalid tokens are not clicks we can believe.
        assert "cc.sig_ok" in sql


def test_diagnostics_are_not_steps():
    """They have a different denominator, so they must never gain a
    drop_from_prev or feed the breakage alarm."""
    wf = build_step_waterfall(_make_run(0, 3, 3))
    names = {s["step"] for s in wf["steps"]}
    assert "agent_click" in names, "step shape changed — this test is stale"
    assert "checkout_clicks_all" not in names
    assert "checkout_clicks_unattributed" not in names
    # A pile of unattributed clicks is not a breakage.
    assert wf["alarm"] is False
