"""The squasher's non-PR exits — awaiting_ops / awaiting_decision.

Guards the fix for the 2026-08-20 finding that the queue never drained: all 25
rows sat in 'refused', and every one of them had a real conclusion attached
("Approve running POST /api/v1/admin/facility-dedup/apply?country=NL",
"Choose between (a) ... or (b) ..."). The lane's only exit that counted as
progress was a PR, so anything needing an operator action or a human judgement
was closed terminal with its analysis discarded — and re-surfaced by the
detector on the next 6h tick, forever.

These are BEHAVIOR tests: they call the real classifier with the real shape of
the investigator's result and assert on what comes back. They deliberately do
not assert that any particular string appears in the source — that style of
test is what let this bug live, because a lane can refuse 100% of the time
while every string assertion about it still passes.
"""
from __future__ import annotations

import ast
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_ROOT, "routes", "squasher_queue.py")


def _load():
    """Pull the classifier out of the module without importing Flask/DB.

    Asserts the extraction actually found the target — an empty extraction
    makes every assertion below pass vacuously, which is the failure mode
    tests/_scan_floors.py exists to end elsewhere in this suite.
    """
    src = open(_SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    ns: dict = {}
    import re as _re
    ns["re"] = _re

    wanted = {"_ACTION_RE", "_NONMECHANICAL_STATUSES", "STATUSES",
              "_REFUSED_REASON"}
    fns = {"_nonmechanical_exit", "_settle_for"}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in wanted:
                    exec(compile(ast.Module([node], []), _SRC, "exec"), ns)
        if isinstance(node, ast.FunctionDef) and node.name in fns:
            exec(compile(ast.Module([node], []), _SRC, "exec"), ns)

    missing = (wanted | fns) - set(ns)
    assert not missing, (
        f"EXTRACTION EMPTY: {sorted(missing)} not found in {_SRC}. Every "
        f"assertion below would pass vacuously. Repoint the extraction; do not "
        f"delete the check."
    )
    return ns


NS = _load()
_exit = NS["_nonmechanical_exit"]
_settle = NS["_settle_for"]


# ── the DECISION, not just the classifier ────────────────────────────────
#
# These cover the drain loop's control flow. Testing _nonmechanical_exit alone
# was not enough: a mutation that replaced the loop's routing with a constant
# left all seven classifier tests green, because none of them could see the
# call site. That is the same shape as the bug being fixed.

def test_a_mechanical_remedy_still_opens_a_pr():
    """None means 'caller opens the PR' — the pre-existing path must survive."""
    assert _settle({"decision_for_human": "run POST /api/v1/admin/x"},
                   {"file": "a.py", "find": "x", "replace": "y"}) is None


def test_no_remedy_but_an_action_settles_as_awaiting_ops():
    got = _settle({"decision_for_human":
                   "Approve running POST /api/v1/admin/facility-dedup/apply"
                   "?country=DE"}, None)
    assert got is not None and got[0] == "awaiting_ops"


def test_no_remedy_and_no_analysis_still_settles_as_refused():
    got = _settle({}, None)
    assert got is not None and got[0] == "refused"
    assert got[1] == NS["_REFUSED_REASON"]


# ── the two real shapes observed in the live queue on 2026-08-20 ──────────

def test_an_operator_action_routes_to_awaiting_ops():
    """The 7-of-25 facility-dedup shape: a concrete endpoint to run."""
    got = _exit({
        "decision_for_human":
            "Approve running POST /api/v1/admin/facility-dedup/apply"
            "?country=NL&confirm=1 after reviewing the analyze output.",
        "recommendation": "This is an operational/data issue, not a code defect.",
    })
    assert got is not None, "a named endpoint must not fall through to refused"
    status, reason = got
    assert status == "awaiting_ops"
    assert "/api/v1/admin/facility-dedup/apply" in reason, (
        "the operator has to be told WHICH action, or the hand-off is useless"
    )


def test_a_judgement_call_routes_to_awaiting_decision():
    """The 'choose between (a) and (b)' shape."""
    got = _exit({
        "decision_for_human":
            "Choose between: (a) close this finding as likely-covered by the "
            "three prior PRs, or (b) re-run the detector scan first.",
        "recommendation": "No single mechanical fix exists.",
    })
    assert got is not None
    status, reason = got
    assert status == "awaiting_decision"
    assert "Choose between" in reason


def test_an_action_beats_a_bare_decision():
    """A concrete endpoint is a more useful hand-off than 'decide something'."""
    status, _ = _exit({
        "decision_for_human": "Decide whether to run POST /api/v1/admin/heal",
        "recommendation": "",
    })
    assert status == "awaiting_ops"


# ── the fail-open direction: refusing must still be possible ──────────────

def test_an_empty_analysis_still_refuses():
    """If the analysis really said nothing, 'refused' remains honest.

    The dangerous direction for this change is the opposite of the bug it
    fixes: routing EVERYTHING to a waiting state would empty the refused
    bucket while quietly filling a human inbox with nothing to act on.
    """
    assert _exit({}) is None
    assert _exit({"decision_for_human": "", "recommendation": ""}) is None
    assert _exit(None) is None
    assert _exit("not a dict") is None


def test_prose_mentioning_an_endpoint_is_not_an_action():
    """Only a VERB + /api/ path counts, so passing mentions do not become
    instructions to mutate production."""
    got = _exit({
        "decision_for_human":
            "The alert's endpoint is confirmed to live at the /api/v1/ops "
            "surface, but no change is needed.",
        "recommendation": "",
    })
    assert got is not None
    assert got[0] == "awaiting_decision", (
        "a bare path with no HTTP verb must NOT be promoted to an ops action — "
        "that is how a model's aside becomes a production mutation"
    )


# ── wiring the statuses are declared and counted ──────────────────────────

def test_new_statuses_are_declared():
    for s in ("awaiting_ops", "awaiting_decision", "resolved"):
        assert s in NS["STATUSES"], (
            f"{s} is written by the lane but missing from STATUSES, so every "
            f"consumer that validates against it would reject the row"
        )


def test_convergence_reports_not_measured_as_null_not_zero():
    """A rate with a zero denominator must be null, never 0.0.

    'Nothing recurred' and 'nothing was measured' are different claims, and
    collapsing them is how a dashboard reports a success it never observed —
    the exact class of measurement dishonesty this codebase has been bitten by
    repeatedly (a pipeline that runs correctly and finds nothing is otherwise
    indistinguishable from a dead one).
    """
    src = open(_SRC, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = [n for n in tree.body
          if isinstance(n, ast.FunctionDef) and n.name == "convergence"]
    assert fn, "EXTRACTION EMPTY: convergence() not found"
    inner = [n for n in ast.walk(fn[0])
             if isinstance(n, ast.FunctionDef) and n.name == "_rate"]
    assert inner, "EXTRACTION EMPTY: convergence._rate not found"
    ns: dict = {}
    exec(compile(ast.Module(inner, []), _SRC, "exec"), ns)
    rate = ns["_rate"]
    assert rate(0, 0) is None, "no denominator must report null, not 0.0"
    assert rate(0, 10) == 0.0, "a real zero must still report 0.0"
    assert rate(3, 10) == 0.3


def test_waiting_states_count_against_the_model_budget():
    """Each waiting row cost a ~80s investigation, exactly like a refusal.

    This file already records the same accounting bug twice: on 2026-08-09 a
    broken extractor produced 13 false refusals that consumed the PR budget
    even though a refusal opens no PR. Leaving the new states out of the work
    filter would repeat it in the other direction — the lane would bill nothing
    for real model spend and blow through its daily pacing.
    """
    src = open(_SRC, encoding="utf-8").read()
    i = src.index("COUNT(*) FILTER (WHERE status IN")
    window = src[i:i + 400]
    for s in ("awaiting_ops", "awaiting_decision"):
        assert s in window, (
            f"{s} costs a model call but is not counted in the 24h work "
            f"budget — the lane would under-report its own spend"
        )
