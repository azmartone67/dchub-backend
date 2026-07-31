"""Guard: the land-power sync runs ONE SOURCE, synchronously, and reports its real result.

WHY — A SPAWNED THREAD ON THIS SERVICE CANNOT SURVIVE
─────────────────────────────────────────────────────
Measured 2026-07-31 from the Railway deployment list: the web service redeployed
TWELVE times in 63 minutes —

    02:06 02:18 02:21 02:26 02:29 02:33 02:35 02:46 02:52 02:59 03:03 03:09

roughly one every five minutes, because many sessions merge PRs into this repo
all day. A sync fired at 03:01:54 was killed by the 03:03 deploy about 70 seconds
in and wrote nothing: 16 minutes of polling produced ZERO new rows in
land_power_sync_log for any of the four sources.

That is the real reason this feed produced nothing for four months, and it is
why the earlier fixes in this chain (#1990 route, #1994 reporter binding, #1996
self-healing endpoints) were all individually correct and still invisible at
runtime. The only rows this log has EVER held are fast-fail errors — ~13s, well
inside a deploy gap. Anything taking minutes has never once completed.

    ★ Every fix in this chain was verified against the live API in-process and
      then failed to appear in production. When repeated correct fixes produce no
      observable change, stop fixing the code and measure the ENVIRONMENT.

THE CHANGE
──────────
The unit of work is now ONE SOURCE, run INSIDE the request. Measured page counts
at 2000 rows/page with a 1.0s inter-request delay:

    eia-860-plants        15 pages   ~18s + fetch
    hifld-substations     38 pages   ~46s + fetch
    hifld-transmission    45 pages   ~54s + fetch

Each fits the dispatcher's 300s curl budget and a typical deploy gap; the four
chained together do not. A source killed mid-flight retries next cycle, and
/api/land-power/status reports it stale meanwhile — so a partial run degrades
visibly instead of silently.

THE CONTRACT
────────────
  P1. ?source=<key> runs that crawler and returns only when it is done — no
      thread, no 202.
  P2. The response carries the crawl's REAL outcome, read from the sync log
      (the crawlers return None; the log row is the durable record).
  P3. A failed source returns a non-2xx, so the dispatcher can see it. A
      spawn-and-200 is what let this feed die.
  P4. An unknown source is a 400 naming the valid keys, never a silent no-op.
  P5. Every source the status endpoint expects is runnable — no source can be
      monitored but unreachable.
  P6. The workflow calls each source in its OWN request, and the generic
      dispatcher does not also POST the job (which would run the whole chain and
      be killed).
  P7. The no-source path still exists for manual use and says plainly that it is
      likely to be killed.

EXPECTED PASS/FAIL — MEASURED, not predicted.
─────────────────────────────────────────────
UNPATCHED (origin/main @ 603c956c):   7 failed, 0 passed, 1 xfailed
PATCHED (this branch):                0 failed, 7 passed, 1 xfailed

`1 xfailed` in both runs — strict-xfail must-fail control.

No network, no DB, no main.py import; nothing runs at module scope.

Run:  python3 -m pytest tests/test_land_power_per_source_sync.py -v
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "land_power_crawler.py")
WF = os.path.join(ROOT, ".github", "workflows", "dchub-jobs.yml")

# Measured 2026-07-31.
DEPLOYS_PER_HOUR = 12
KILLED_AFTER_S = 70


def _src():
    src = open(MOD).read()
    t = ast.parse(src)
    assert isinstance(t, ast.Module), "parse did not produce a Module"
    assert t.body, "parsed module body is EMPTY — extraction read nothing"
    return src


def _job_body():
    """The /api/jobs/land-power-sync handler, anchored on its decorator.

    ★ Anchored on the decorator, not the first mention of the path — the module
    docstring names it too, and indexing on that reads the wrong region (a
    mistake this session's guards made twice).
    """
    src = _src()
    i = src.index("@app.route('/api/jobs/land-power-sync'")
    j = src.index("@app.route", i + 10)
    return src[i:j]


# ── P1 ────────────────────────────────────────────────────────────────────────
def test_a_named_source_runs_synchronously_not_in_a_thread():
    body = _job_body()
    assert "source = (request.args.get('source')" in body, \
        "the job endpoint accepts no ?source= — it can only run the whole chain"
    si = body.index("if source:")
    ti = body.find("threading.Thread")
    assert ti == -1 or ti > si, (
        "the ?source= branch is not ahead of the thread spawn — a named source "
        "must run in-request, because a background thread on this service is "
        f"killed by the next deploy ({DEPLOYS_PER_HOUR} in 63 minutes measured; "
        f"one run died after ~{KILLED_AFTER_S}s)")
    # Slice the per-source branch and check for the SPAWN CALL, not the word
    # "threading" — the `import threading` line sits immediately above the spawn
    # and is inside the slice, so the bare substring is a false positive.
    per_source = body[si:ti if ti > si else len(body)]
    assert "threading.Thread(" not in per_source, \
        "the per-source branch still spawns a thread"
    assert "return jsonify" in per_source, \
        "the per-source branch does not return in-request"


# ── P2 ────────────────────────────────────────────────────────────────────────
def test_the_response_carries_the_real_outcome_from_the_log():
    body = _job_body()
    assert "FROM land_power_sync_log" in body, (
        "the per-source response does not read the sync log. The crawlers return "
        "None, so the log row is the only durable record of what happened")
    for key in ("'fetched'", "'upserted'", "'errors'"):
        assert key in body, f"the response omits {key} from the crawl result"
    assert "'elapsed_s'" in body, "no elapsed time reported"


# ── P3 ────────────────────────────────────────────────────────────────────────
def test_a_failed_source_is_not_reported_as_success():
    body = _job_body()
    assert "status_code = 200 if" in body, \
        "the per-source branch has no conditional status code"
    assert "else 500" in body, (
        "a failed crawl still returns 2xx — a spawn-and-200 is exactly what let "
        "this feed die unnoticed for four months")
    assert "row.get('errors')" in body, \
        "the status code does not consider the crawl's own error count"


# ── P4 ────────────────────────────────────────────────────────────────────────
def test_an_unknown_source_is_a_400_naming_the_valid_keys():
    body = _job_body()
    assert "unknown_source" in body, "an unknown ?source= is not rejected"
    assert "400" in body, "an unknown source does not return 400"
    assert "known=sorted(_RUNNERS)" in body, \
        "the rejection does not tell the caller which sources exist"


# ── P5 ────────────────────────────────────────────────────────────────────────
def test_every_monitored_source_is_runnable():
    """No source may be watched by /status but unreachable by the job."""
    src = _src()
    body = _job_body()
    runners = set()
    i = body.index("_RUNNERS = {")
    for line in body[i:body.index("}", i)].split("\n"):
        if "':" in line:
            runners.add(line.split("'")[1])
    # the sources /status declares it expects
    j = src.index("_EXPECTED = (")
    expected = set()
    for part in src[j:src.index(")", j)].split("'"):
        if part.strip().startswith(("eia-", "hifld-")):
            expected.add(part.strip())
    assert expected, "could not read _EXPECTED from the status endpoint"
    missing = expected - runners
    assert not missing, (
        f"{sorted(missing)} appear in the status endpoint's expected list but "
        f"cannot be run by the job — monitored yet unreachable")


# ── P6 ────────────────────────────────────────────────────────────────────────
def test_the_workflow_calls_each_source_in_its_own_request():
    wf = open(WF).read()
    assert "source=${SRC}" in wf, (
        "the workflow does not call the job per source — one request for the "
        "whole chain exceeds both the 300s budget and the deploy gap")
    for s in ("eia-860-plants", "hifld-substations", "hifld-transmission",
              "eia-ng-pipelines"):
        assert s in wf, f"{s} is not dispatched by the workflow"
    assert "steps.schedule.outputs.jobs != 'land-power-sync'" in wf, (
        "the generic dispatcher still also POSTs land-power-sync, which would "
        "run the whole chain in one request and be killed mid-flight")
    assert "land-power/status" in wf, \
        "the workflow never reads the verdict, so a red run looks like a green job"


# ── P7 ────────────────────────────────────────────────────────────────────────
def test_the_chain_path_still_exists_and_admits_it_is_fragile():
    body = _job_body()
    assert "threading.Thread" in body, \
        "the whole-chain path was removed; it is still useful manually"
    assert "202" in body, "the spawn path should not claim 200"
    low = body.lower()
    assert "killed" in low and "deploy" in low, (
        "the spawn response does not warn that a background crawl is usually "
        "killed by the next deploy on this service")


# ── must-fail control — never delete ─────────────────────────────────────────
@pytest.mark.xfail(strict=True, reason="must-fail control: proves this file runs")
def test_zzz_must_fail_control():
    assert False, "control"
