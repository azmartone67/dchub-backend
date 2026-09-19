"""data-sync.yml "Fire async loaders": fire only a loader that can load, and
fail on the outcomes the step can actually see (2026-09-13).

Measured on the 2026-09-12 12:19Z and 2026-09-13 00:46Z runs (Railway deploy
logs and each run's own output), three of the four loaders this step fired
could not load anything:

  load-substations-live   its ArcGIS service no longer exists; RuntimeError
                          on every run
  load-power-plants-live  28 minutes of EIA paging, then every insert chunk
                          failed on a column the live table does not have
  load-facilities-live    fetches four sources and inserts none of them

The step itself could not fail: `curl | json.tool || echo`, a 90s sleep, one
loader-status read. That state is in-memory per replica, and railway.json runs
two, so the single read printed {"loaders": {}} on two of four runs and printed
the substations RuntimeError under a green step on a third.

These tests RUN the step's bash under `bash -e` (what Actions uses for a step
with no `shell:`), against a curl stub that answers by URL. Every /tmp path in
the step is redirected into the test's own directory.
"""
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "data-sync.yml")
JOB = "sync-infrastructure"
STEP = "Fire async loaders (phase 12g — non-blocking)"
RETIRED = ("load-substations-live", "load-power-plants-live", "load-facilities-live")

pytestmark = pytest.mark.skipif(not shutil.which("bash"), reason="workflow scripts need bash")

FIRED = json.dumps({"started": True, "status_key": "pipelines",
                    "check_at": "/api/admin/loader-status"})

# The status stub serves the i-th scripted read (and the last one again once the
# script runs out), so a test can put different replicas' answers in sequence.
_MOCK = r'''
curl() {
  local out="" url="" method="GET" prev="" n i code body
  for a in "$@"; do
    if [ "$prev" = "-o" ]; then out="$a"; fi
    if [ "$prev" = "-X" ]; then method="$a"; fi
    case "$a" in http*://*) url="$a" ;; esac
    prev="$a"
  done
  printf '%s %s\n' "$method" "$url" >> "$URL_LOG"
  case "$url" in
    */api/admin/loader-status*)
      n=$(cat "$READ_N" 2>/dev/null || echo 0)
      n=$((n + 1))
      printf '%s' "$n" > "$READ_N"
      i=$n
      if [ "$i" -gt "$READ_COUNT" ]; then i=$READ_COUNT; fi
      code=$(cat "$READS_DIR/$i.code")
      body=$(cat "$READS_DIR/$i.body")
      ;;
    */api/admin/load-*) code="$FIRE_CODE"; body="$FIRE_BODY" ;;
    *) code="599"; body="unscripted URL" ;;
  esac
  if [ -n "$out" ]; then
    printf '%s' "$body" > "$out"
    printf '%s' "$code"
  else
    printf '%s' "$body"
  fi
  return 0
}
sleep() { :; }
'''


def _steps():
    doc = yaml.safe_load(open(WORKFLOW, encoding="utf-8"))
    return doc["jobs"][JOB]["steps"]


def _step():
    hits = [s for s in _steps() if s.get("name") == STEP]
    assert len(hits) == 1, f"expected one {STEP!r} step in {JOB}, found {len(hits)}"
    return hits[0]


def _stamp(minutes_from_now=0):
    t = datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _rec(started_at=None, **fields):
    rec = {"entry_point": "main", "started_at": started_at or _stamp(), "running": False}
    rec.update(fields)
    return rec


def _status(**loaders):
    return ["200", json.dumps({"success": True, "loaders": loaders})]


def _run(reads, fire_code="200", fire_body=FIRED):
    # A step that interpolates the secret into its script (instead of env:)
    # must still run here the way it would in Actions.
    run = _step()["run"].replace("${{ secrets.DCHUB_INTERNAL_KEY }}", "k")
    with tempfile.TemporaryDirectory() as td:
        reads_dir = os.path.join(td, "reads")
        os.mkdir(reads_dir)
        for i, (code, body) in enumerate(reads, 1):
            with open(os.path.join(reads_dir, f"{i}.code"), "w") as fh:
                fh.write(code)
            with open(os.path.join(reads_dir, f"{i}.body"), "w") as fh:
                fh.write(body)
        sh = os.path.join(td, "step.sh")
        with open(sh, "w") as fh:
            fh.write(_MOCK + "\n" + run.replace("/tmp/", td + "/"))
        log = os.path.join(td, "urls")
        open(log, "w").close()
        env = dict(os.environ, INTERNAL_KEY="k", URL_LOG=log,
                   READ_N=os.path.join(td, "n"), READS_DIR=reads_dir,
                   READ_COUNT=str(len(reads)), FIRE_CODE=fire_code, FIRE_BODY=fire_body)
        p = subprocess.run(["bash", "-e", sh], env=env, capture_output=True,
                           text=True, cwd=td, timeout=60)
        urls = [line for line in open(log).read().splitlines() if line]
    return p.returncode, p.stdout + p.stderr, urls


def test_only_the_loader_that_writes_is_fired():
    rc, out, urls = _run([_status(pipelines=_rec(ok=True, result="ran"))])
    posts = [u for u in urls if u.startswith("POST ")]
    assert any(u.endswith("/api/admin/load-pipelines-live") for u in posts), urls
    for retired in RETIRED:
        assert not any(retired in u for u in urls), (
            f"{retired} is fired again — it cannot load anything (see this "
            f"module's docstring): {urls}")
    assert rc == 0, out


def test_a_loader_this_run_started_that_failed_fails_the_step():
    rc, out, _ = _run([_status(pipelines=_rec(error="RuntimeError: loader aborted"))])
    assert rc != 0, f"a loader that finished with an error left the step green:\n{out}"
    assert "::error::" in out and "pipelines" in out and "RuntimeError" in out, out


def test_a_loader_that_finished_ok_passes_and_says_so():
    rc, out, _ = _run([_status(pipelines=_rec(ok=True, result="ran"))])
    assert rc == 0, out
    assert "the pipelines loader finished: ran" in out, out
    assert "::error::" not in out, out


def test_state_on_a_replica_no_read_reached_is_unknown_not_a_failure():
    rc, out, _ = _run([_status()])
    assert rc == 0, f"an unobservable outcome was failed:\n{out}"
    assert "outcome UNKNOWN" in out, out
    assert "::error::" not in out, out


def test_an_earlier_runs_error_is_not_this_runs_outcome():
    """A replica that did not run this fire can still hold the record of a
    fire six hours ago. Judging that record would fail (or pass) this run on
    another run's result."""
    stale = _rec(started_at=_stamp(-360), error="RuntimeError: from six hours ago")
    rc, out, _ = _run([_status(pipelines=stale)])
    assert rc == 0, out
    assert "six hours ago" not in out and "outcome UNKNOWN" in out, out


def test_reads_are_merged_across_replicas():
    """Each read lands on either replica. A finished record seen on any read
    decides; a running record never outranks it."""
    running = _rec(running=True)
    failed = _rec(error="RuntimeError: loader aborted")
    rc, out, _ = _run([_status(), _status(pipelines=running), _status(),
                       _status(pipelines=failed), _status(), _status()])
    assert rc != 0, f"the failure on read 4 was lost behind reads 1-3:\n{out}"
    assert "RuntimeError" in out, out

    rc, out, _ = _run([_status(), _status(pipelines=_rec(ok=True, result="ran")),
                       _status(), _status(), _status(), _status()])
    assert rc == 0 and "finished: ran" in out, out


def test_still_running_after_the_poll_is_unknown_not_a_failure():
    rc, out, _ = _run([_status(pipelines=_rec(running=True))])
    assert rc == 0, out
    assert "still running after the poll" in out and "UNKNOWN" in out, out


def test_the_status_is_read_six_times():
    rc, out, urls = _run([_status()])
    assert sum("/api/admin/loader-status" in u for u in urls) == 6, urls


def test_a_refused_fire_fails_the_step_and_reads_nothing():
    rc, out, urls = _run([_status()], fire_code="403", fire_body='{"error": "forbidden"}')
    assert rc != 0, f"a refused POST left the step green:\n{out}"
    # "HTTP 403", not a bare 3-digit run: the step echoes "$endpoint answered
    # HTTP $CODE", and a loose match is satisfied by any digits that line up.
    assert "::error::" in out and "HTTP 403" in out, out
    assert not any("loader-status" in u for u in urls), urls


def test_a_200_that_did_not_start_the_loader_fails():
    rc, out, _ = _run([_status()], fire_body='{"started": false, "reason": "no callable entry"}')
    assert rc != 0, out
    assert "without starting" in out, out


def test_already_running_is_reported_not_failed():
    body = '{"started": false, "reason": "already running", "status_key": "pipelines"}'
    rc, out, _ = _run([_status()], fire_body=body)
    assert rc == 0, out
    assert "still running from an earlier fire" in out, out


def test_an_unreadable_status_endpoint_fails_the_step():
    """The 2026-09-02 shape: every loader-status read 401'd and the step
    exited 0 over it. A watcher that can read nothing verifies nothing."""
    rc, out, _ = _run([["401", '{"error": "unauthorized"}']])
    assert rc != 0, out
    assert "no /api/admin/loader-status read succeeded" in out, out


def test_a_red_loader_step_cannot_skip_the_steps_after_it():
    steps = _steps()
    idx = next(i for i, s in enumerate(steps) if s.get("name") == STEP)
    after = steps[idx + 1:]
    assert len(after) >= 8, [s.get("name") for s in after]
    unguarded = [s.get("name") for s in after if s.get("if") not in ("always()", "failure()")]
    assert not unguarded, (
        f"these steps run only if every step before them succeeded, so a red "
        f"loader step now skips them: {unguarded}")
