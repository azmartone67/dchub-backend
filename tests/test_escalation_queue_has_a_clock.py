"""A catcher with no clock catches nothing.

★ WHAT HAPPENED. Nine external customers paid, hold a working key, and have
never made a single call on any surface. The automated nudge fired to all nine
— email_drip_log carries an `activation_nudge` row each, six of them ~40 days
old — and all nine stayed at zero. customer_white_glove._classify drew the right
conclusion ("nudged 39d ago, still zero calls — automated nudge FAILED. Human
touch, not another email") and escalated.

brain_escalation_queue (PR #3310, merged 2026-08-29) is the catcher for that
escalation: it persists the board's verdict so a human can work it, and
auto-resolves a row the moment the customer makes their first call. It merged
and NOTHING EVER CALLED IT. Measured 2026-08-30: the only references to
/api/v1/brain/escalations/sync in the entire repo were the route definition and
its own docstring. The queue had been empty since the day it shipped.

★ THE SHAPE, which is the reusable part: a persistence endpoint that exists to
be driven on a schedule is DEAD CODE until something drives it, and dead code
that returns 200 to a manual probe looks alive. The endpoint answered; the
queue was empty; nobody noticed for a day short of a week.

★ WHAT THIS ASSERTS, on the executable body. It reads each workflow step's
parsed `run:` script — never a comment — and requires at least one to call the
sync route. Deliberately narrow: it fences THIS catcher, whose failure mode is
now documented, rather than guessing which other endpoints need clocks.
"""
import os

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, ".github", "workflows")
ROUTE_FILE = os.path.join(ROOT, "routes", "brain_escalation_queue.py")
SYNC_PATH = "/api/v1/brain/escalations/sync"


def _workflow_run_scripts():
    for fn in sorted(os.listdir(WF_DIR)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(WF_DIR, fn), encoding="utf-8") as fh:
            try:
                doc = yaml.safe_load(fh)
            except yaml.YAMLError:
                continue
        if not isinstance(doc, dict):
            continue
        for job in (doc.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            for step in (job.get("steps") or []):
                if isinstance(step, dict) and isinstance(step.get("run"), str):
                    yield fn, step["run"]


def test_the_escalation_sync_is_driven_by_something():
    if not os.path.exists(ROUTE_FILE):
        pytest.skip("brain_escalation_queue has been removed")
    with open(ROUTE_FILE, encoding="utf-8") as fh:
        if SYNC_PATH not in fh.read():
            pytest.skip("the sync route no longer exists")
    callers = sorted({fn for fn, run in _workflow_run_scripts() if SYNC_PATH in run})
    assert callers, (
        f"{SYNC_PATH} exists but no workflow calls it. The queue it fills stays "
        "empty, the white-glove escalation for every stranded payer lands "
        "nowhere, and the endpoint still answers 200 to a manual probe — so it "
        "looks alive while catching nothing. Give it a clock.")


def test_the_clock_is_itself_watched():
    """The sync job must be on the deadman board. An unwatched catcher-driver
    can die as quietly as the catcher did — mcp-facts-export failed 60
    consecutive runs before anyone looked."""
    callers = {fn for fn, run in _workflow_run_scripts() if SYNC_PATH in run}
    if not callers:
        pytest.skip("covered by the assertion above")
    watch_py = os.path.join(ROOT, "tools", "deadman", "watch.py")
    with open(watch_py, encoding="utf-8") as fh:
        watched = fh.read()
    unwatched = sorted(fn for fn in callers if f'"{fn}"' not in watched)
    assert not unwatched, (
        "these workflows drive the escalation queue but are not on the deadman "
        "board, so their silent death would empty the queue without any "
        "signal:\n" + "".join(f"  {fn}\n" for fn in unwatched))
