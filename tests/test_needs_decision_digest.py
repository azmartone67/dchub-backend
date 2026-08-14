#!/usr/bin/env python3
"""tests/test_needs_decision_digest.py — the queue for decisions nobody owns.

NO NETWORK.

Every queue in this repo is "a machine found a defect": brain-l15-auto,
brain-l22-auto-code, qa-superuser, workflow-failure, deadman, watchdog,
registry-outreach, spec-debt. There is no queue for "a human must DECIDE
something", so an issue filed as a decision belongs to nobody — the brain works
its own labelled queues and closes what it authored.

Measured 2026-08-13 across the 22 open issues: every one carried a machine label
EXCEPT two. #2526 (opened 08-10, three days, zero comments, untouched) and
#2643. Both invisible to every loop.

★ THE ORPHAN HALF IS THE POINT. A digest of only `needs-decision` would not have
caught #2526, because nobody thought to label it — and not labelling is exactly
how an issue becomes invisible. Selecting on "has no label at all" is what makes
this catch the case that motivated it.

★ DELIVERY IS A FAILING JOB. Today's session found the welcome path healthy
while every nudge channel had sent 2 emails in the product's lifetime, and an
alarm watching an abandoned table. Email here would be a fourth thing that can
silently not-send. A red workflow reaches a human through GitHub's own
notifications, needs no secret, and cannot quietly succeed.

Run standalone:   python3 tests/test_needs_decision_digest.py
Run under pytest: pytest tests/test_needs_decision_digest.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "needs-decision-digest.yml")


def _wf():
    import yaml
    with open(WF, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _run():
    """Shell minus comments — the comments name the failure modes on purpose."""
    step = _wf()["jobs"]["sweep"]["steps"][0]["run"]
    return "\n".join(l for l in step.splitlines() if not l.lstrip().startswith("#"))


def test_it_selects_orphans_not_just_the_label():
    """★ The motivating case. #2526 had NO label; a label-only filter misses
    exactly the issues that became invisible by not being labelled."""
    run = _run()
    assert "(.labels | length) == 0" in run, (
        "must select issues with NO label — those belong to no queue at all"
    )
    assert 'index("needs-decision")' in run, (
        "must also select explicitly-flagged decisions"
    )


def test_an_empty_api_result_fails_rather_than_reporting_all_clear():
    """This repo always has open issues. An empty list means the query broke,
    and 'no decisions pending' would be green over no evidence."""
    run = _run()
    lines = run.splitlines()
    # Bound the search to THIS if-block. A DOTALL `.*?exit 1` reached the
    # stale-check's exit further down the file, so the guard passed with the
    # empty-result branch gutted — it was asserting that SOME exit 1 existed
    # anywhere below, which is always true.
    starts = [i for i, l in enumerate(lines) if 'if [ "$TOTAL" -eq 0 ]' in l]
    assert starts, "the empty-result check is gone entirely"
    i = starts[0]
    block = []
    for l in lines[i + 1:]:
        if l.strip() == "fi":
            break
        block.append(l)
    assert any(x.strip() == "exit 1" for x in block), (
        "the empty-result branch must exit 1 — otherwise a failed query reads "
        "as a clear queue, which is green over no evidence"
    )


def test_the_issue_query_is_not_swallowed():
    run = _run()
    m = re.search(r"^\s*gh issue list\b.*?(?=\n\s*\n|\Z)", run, re.DOTALL | re.MULTILINE)
    assert m, "the issue query was not found"
    assert "|| true" not in m.group(0) and "|| echo" not in m.group(0), (
        "the query must be allowed to fail the job"
    )


def test_stale_items_fail_and_fresh_ones_do_not():
    """A pending decision is fine. A forgotten one is the defect, so only AGE
    trips the job — otherwise it cries wolf the moment anything is filed."""
    run = _run()
    assert "STALE_DAYS" in run, "needs an age threshold"
    m = re.search(r'if \[ "\$\{STALE:-0\}" -gt 0 \]; then.*?exit 1', run, re.DOTALL)
    assert m, "stale items must fail the job"
    # And the pass path must exist — "nothing pending" cannot be an error.
    assert "none older than" in run, (
        "a queue with only fresh items must exit cleanly and quietly"
    )


def test_delivery_does_not_depend_on_email():
    """Three separate send paths were found broken or near-silent today. A
    digest that emails is a fourth thing that can fail without saying so."""
    run = _run()
    for sender in ("RESEND", "SENDGRID", "smtp", "mailto:"):
        assert sender.lower() not in run.lower(), (
            f"delivery must not depend on {sender} — use the failing job, which "
            f"GitHub notifies on and which cannot silently succeed"
        )
    assert "GITHUB_STEP_SUMMARY" in run, "the digest must be written where a human can read it"


def test_has_a_kill_switch_and_an_external_trigger():
    run = _run()
    assert re.search(r'if \[ "\$\{GATE_DISABLE:-0\}" = "1" \]', run), "needs a kill switch"
    on = _wf()
    on = on[True] if True in on else on["on"]
    assert "repository_dispatch" in on, (
        "must be triggerable from outside, like the other sweeps"
    )
    assert "schedule" in on, "an unscheduled sweep is the bug it exists to fix"


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✓ {_name}")
            except AssertionError as _e:
                _failed += 1
                print(f"✗ {_name}: {_e}")
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
