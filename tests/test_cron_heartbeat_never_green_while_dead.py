"""The 5-min heartbeat must not report success while it is being refused.

House rule: tests NEVER import main. This one reads files. Nothing at module
scope.

WHY THIS EXISTS
===============
`/api/v1/cron/heartbeat` is not one job. `routes/cron_heartbeat.py` dispatches
38 master-ticks from it — RAG daily at 06:00, plus the grid warmer on every
call and brain warming hourly.

★★★ 2026-08-26: this file used to continue "GitHub Actions hitting that one URL
every five minutes is the only thing driving any of it." That stopped being
true on 2026-07-03, when main.py::_cron_self_heartbeat_loop began driving the
same dispatcher in-process on the leader worker every 300s. The premise died
and every assertion below kept passing, because none of them tested it — see
test_the_workflow_is_no_longer_the_only_driver at the bottom, which now pins
the redundancy instead of asserting its absence.

★ Until 2026-08-04, a SUSTAINED 4xx exited 0. Three attempts, all refused, then
`::warning::… treating as transient` and a green check. A 4xx is exactly the
failure that never self-heals — 404 means the route is gone (a blueprint that
failed to register, a path that moved), 403 means auth changed. Neither
recovers on its own. So the single driver behind 38 scheduled ticks could have
been dead indefinitely while this workflow reported success every five minutes.

That is the same shape as weekly-shadow-audit, which reported success for two
weeks while its push was rejected and swallowed by `|| true` — the failure
CLAUDE.md warns about under "Verifying". "Transient" was always a claim about
5xx; it was never true of 4xx.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(".github", "workflows", "cron-heartbeat.yml")


def _src(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _tail(src):
    """The block after the retry loop — where the verdict is reached."""
    return src[src.index("# All 3 attempts failed"):]


def test_a_sustained_4xx_fails_the_workflow():
    """★ The whole point. A 404 or 403 on all three attempts must go RED."""
    tail = _tail(_src(WF))
    assert "treating as transient" not in tail, (
        "a sustained 4xx is not transient — 404 = the route is gone, "
        "403 = auth changed. Neither self-heals, and this endpoint is the only "
        "driver for 38 master-ticks.")
    # every terminal path out of the block exits non-zero
    exits = [l.strip() for l in tail.splitlines() if l.strip().startswith("exit")]
    assert exits, "the block must reach an explicit exit"
    assert all(e != "exit 0" for e in exits), f"green-while-dead path: {exits}"


def test_a_sustained_5xx_still_fails_too():
    tail = _tail(_src(WF))
    assert '-ge 500' in tail and '"000"' in tail
    assert "::error::" in tail


def test_a_first_attempt_success_still_exits_early():
    """The retry tolerance is the point of the loop and must survive — a
    momentary 503 on one replica is not an outage, and a workflow that goes red
    on noise gets muted, which is its own way of going blind."""
    src = _src(WF)
    loop = src[src.index("for attempt in"):src.index("# All 3 attempts failed")]
    assert "exit 0" in loop, "a successful attempt must still short-circuit"
    assert "for attempt in 1 2 3" in loop


def test_the_error_message_says_what_stops_working():
    """A red check nobody understands gets clicked past. Name the blast
    radius in the message, not in a comment someone has to go find."""
    tail = _tail(_src(WF))
    low = tail.lower()
    assert "master-tick" in low
    assert "404" in tail and "403" in tail


def test_the_workflow_is_no_longer_the_only_driver():
    """The premise, inverted — and now actually tested.

    Until 2026-07-03 this workflow WAS the single driver, and the error text it
    prints on failure still said so on 2026-08-26: "backend outage; 38
    master-ticks + warmers are not running". By then the claim was eight weeks
    stale. main.py::_cron_self_heartbeat_loop drives the same dispatcher
    in-process on the leader worker every 300s, and a third caller
    (cron-job.org) hits it too. Measured that morning from
    /api/v1/cron/last-fired: DCHub-SelfHeartbeat 280 fires/24h, this workflow
    79, cron-job.org 33.

    That mattered. A comment inside a backslash continuation had broken the
    curl (be#3203), and the workflow reported its own breakage as a two-hour
    backend outage with 38 dead master-ticks. Nothing was down.

    So this pins the REDUNDANCY. If the in-process driver is ever removed, this
    workflow silently becomes load-bearing again and the urgency in the header
    becomes real — CI should say so at that moment, not eight weeks later.
    """
    src = _src("routes", "cron_heartbeat.py")
    assert src.count("master-tick") >= 20, (
        "cron_heartbeat no longer fans out to a large number of master-ticks; "
        "re-read whether this workflow is still the single point of failure")
    wf = _src(WF)
    assert "/api/v1/cron/heartbeat" in wf
    assert "1-59/5 * * * *" in wf, "still the 5-minute driver"

    main = _src("main.py")
    assert "_cron_self_heartbeat_loop" in main, (
        "the in-process 5-min heartbeat driver is GONE. This GitHub Actions "
        "workflow is now the single point of failure for 38 master-ticks "
        "again — restore the loop, or accept that and say so here.")
    assert "/api/v1/cron/heartbeat" in main, (
        "the self-heartbeat loop no longer targets the dispatcher URL")

    # And the text the operator actually SEES must not resurrect the false
    # claim. Judge the echo lines only: the surrounding commentary quotes the
    # old wording on purpose, and an assertion that matched our own postmortem
    # would fail for the one reason that is never the bug.
    tail = _tail(wf)
    said = "\n".join(l for l in tail.splitlines()
                     if l.lstrip().startswith("echo"))
    assert said, "the failure block no longer echoes anything"
    assert "backend outage" not in said, (
        "a 000/5xx here means THIS CALLER failed. 000 in particular means curl "
        "never issued a request. Do not assert the backend's state from the "
        "caller's exit code — point the reader at /api/v1/cron/last-fired.")
    assert "last-fired" in said, (
        "the error text must name the endpoint that actually answers "
        "'are the master-ticks running?' — GET /api/v1/cron/last-fired")
