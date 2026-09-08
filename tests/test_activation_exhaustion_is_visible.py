"""An armed sender that can never reach anyone must say so.

2026-09-08. The white-glove tick reported, every day:

    acted: {"activation_armed": true, "stranded_routed_to_activation": 0}
    health: {"systemic_activation_failure": true, "stranded_ratio": 0.476}

Armed, and routing nobody, while ~48% of paying customers were stranded.

The cause is not a broken sender. stranded_candidates() excludes anyone with
`nudged` set, permanently, and all 9 stranded payers had been nudged exactly
once — seven of them ~50 days earlier — and were still at zero calls, having
joined 22 to 214 days ago.

That exclusion is DELIBERATE and correct. _classify already decides "nudged,
still zero calls -> the automated nudge FAILED; human touch, not another email
into the void", and re-firing the same email at nine people who ignored it is
a decision for a person, not a cron.

The defect was that the exhaustion was INVISIBLE: nothing distinguished
"0 sent because everyone is healthy" from "0 sent because every candidate is
permanently disqualified". Those two states rendered identically.
"""
import pathlib
import re

from routes import customer_white_glove as cwg


def _health(roster):
    return cwg._self_health(roster)


def _payer(stage, nudged, email="a@b.com", total_calls=0):
    return {"stage": stage, "nudged": nudged, "email": email,
            "total_calls": total_calls, "escalate": False,
            "needs_human": False, "welcome_attempted": False,
            "welcomed": True, "welcome_errored": False}


def test_all_nudged_reads_as_exhausted():
    """The live shape: every stranded payer already nudged."""
    roster = [_payer("stranded", True, f"s{i}@x.com") for i in range(9)] \
        + [_payer("power", False, "p@x.com", total_calls=9)]
    h = _health(roster)
    assert h["nudge_exhausted"] == 9
    assert h["stranded_reachable_by_automation"] == 0
    assert h["automation_exhausted"] is True


def test_a_nudged_payer_who_activated_is_not_exhausted():
    """Scoping test. A customer who was nudged and then STARTED USING the
    product is a success, not a dead end — counting every `nudged` row instead
    of only the stranded ones inflates the human queue with people who need
    nothing. Caught by mutation: without this case the two forms are
    indistinguishable."""
    roster = [_payer("stranded", True, "s@x.com"),
              _payer("power", True, "worked@x.com", total_calls=310)]
    h = _health(roster)
    assert h["nudge_exhausted"] == 1, (
        "nudge_exhausted must count only STRANDED nudged payers — the power "
        "user was nudged and it worked")


def test_a_fresh_stranded_payer_is_reachable():
    """One un-nudged payer means the sender still has work — not exhausted."""
    roster = [_payer("stranded", True, "old@x.com"),
              _payer("stranded", False, "new@x.com")]
    h = _health(roster)
    assert h["nudge_exhausted"] == 1
    assert h["stranded_reachable_by_automation"] == 1
    assert h["automation_exhausted"] is False


def test_no_stranded_is_not_exhausted():
    """The load-bearing negative: a healthy board must NOT claim exhaustion.
    Both states send zero, and conflating them is the whole defect."""
    roster = [_payer("power", False, "p@x.com", total_calls=9),
              _payer("healthy", False, "h@x.com", total_calls=4)]
    h = _health(roster)
    assert h["nudge_exhausted"] == 0
    assert h["stranded_reachable_by_automation"] == 0
    assert h["automation_exhausted"] is False


def test_workflow_notice_is_derived_not_asserted():
    """It used to print 'no customer email sent' as a fixed string, identically
    whether the tick routed nobody or routed 25 people into the armed sender."""
    wf = (pathlib.Path(cwg.__file__).parents[1]
          / ".github/workflows/white-glove-tick-daily.yml").read_text()
    assert "measure/classify only, no customer email sent." not in wf, (
        "the notice still asserts an outcome it never read")
    assert "stranded_routed_to_activation" in wf, (
        "the notice must read the actual routed count from the response")
    assert re.search(r'if \[ "\$\{ROUTED\}" = "0" \]', wf), (
        "the notice must branch on what happened")
