"""The brain crons must tell a relayed 202 apart from a completed 200.

These RUN each workflow's bash against a stubbed curl rather than asserting on
its source text — this bug class is invisible to a grep, which is why #2886
shipped the same defect in news-ner-discovery.yml.

Measured 2026-08-18 against main.py's _WORKER_PROXY_SYNC_PATHS. Two of these
calls are relayed to dchub-worker with a 180s read budget:

    /api/v1/brain/self-critique/run       (brain-reasoning-layers.yml, L16)
    /api/v1/brain/learn-backend-issues    (brain-layer5.yml, 2nd step)

When the job outlives that budget _delegate_to_worker() answers honestly:

    202 {"success": true, "delegated_to": "worker", "completed": false,
         "note": "job still running on dchub-worker; check worker logs"}

That body parses and carries none of the job's counters. Neither cron beats the
deadman board, so unlike news-ner a 202 cannot raise a false red — the failure
is the inverse and quieter: layer5 rendered it as
`examined=0 proposed=0 refused=0` (a healthy zero run) and L16 simply printed
the envelope. A green cron that observed nothing.

Absence of a counter is UNKNOWN, never zero.

Both calls also used --max-time 180, exactly equal to the relay budget, so the
202 arrived only AFTER curl had already given up — the steps could not observe
the very status they needed. Hence the >budget assertion in the fence below.

★ 2026-08-19 — the FOURTH instance, and the one that inverts the symptom:

    /api/v1/brain/autonomy/tick           (brain-autonomy.yml, 1st step)

It is in _WORKER_PROXY_POST_PATHS but NOT _WORKER_PROXY_SYNC_PATHS, so its
relay budget is 15s, not 180 — main.py:
`_read_budget = 180 if request.path in _WORKER_PROXY_SYNC_PATHS else 15`.
Run 32210235319 POSTed at 02:54:39.9 and got the 202 at 02:54:55.2: 15.3s.
--max-time 180 was never that cron's defect; it read the status correctly and
then called a 202 "the loop did NOT run" and exit 1'd. brain-autonomy DOES beat
the deadman board (tools/deadman/watch.py, off the run CONCLUSION), so unlike
its three siblings its 202 raised a false RED — the board read
status="latest-run-failed" while the tick ran normally on the worker.

False green and false red, one root cause: absence of a counter is UNKNOWN,
never zero and never failure.

Two consequences for this fence:

  · The repo-wide scan below covers ALL delegated paths, not just the 21 sync
    ones. _delegate_to_worker()'s ReadTimeout branch answers 202 for anything
    in the allowlist; only the deadline differs. Scoped to SYNC_PATHS it never
    looked at brain-autonomy.yml at all.
  · The static scan cannot catch what brain-autonomy actually did wrong — it
    read %{http_code} and allowed 300s, so it passes both rules. Only the
    executable tests below see the exit 1. That is the point of this file.

★ 2026-08-19, later — the scan was blind in two ways, both proven by mutation
rather than by reading, and both now closed. The bullet above is superseded:

  1. IT READ THE CURL LINE, so a path held in a shell variable was invisible.
     brain-master-tick.yml and sentinel-master-tick.yml both build
     `URL="$BASE/api/v1/admin/.../master-tick"` and curl `"$URL"` — the most
     ordinary way to write a step in this repo — and neither had ever been
     looked at. Planting the identical defect inline failed the scan; planting
     it through a variable passed. dcpi-daily.yml hid there too, and it was
     violating the ORIGINAL --max-time rule the whole time.
  2. IT ACCEPTED "READS THE STATUS" AS "HANDLES THE STATUS". Capturing
     %{http_code} and never separating the delegated case is not a milder
     version of this bug, it IS brain-autonomy's bug, which is why the bullet
     above had to concede the scan could not see it. It can now: the step must
     branch on 202 or discriminate on delegated_to.

Both rules read the step with comments STRIPPED. Every fix in this class ships
40+ lines of comment quoting the 202 envelope verbatim, so a substring rule is
otherwise satisfied by the prose explaining the bug — the #2938 defect.

Scanning is per STEP, not per file: #2931 fixed iso-queue-ingest's aggregate
step and left the sibling below it, and facility-snapshot-daily.yml has seven
steps of which one was fixed. File-level, a fixed step vouches for its blind
neighbours.
"""
import ast
import glob
import os
import re
import shutil
import subprocess
import tempfile

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, ".github/workflows")
REASONING = os.path.join(WF_DIR, "brain-reasoning-layers.yml")
LAYER5 = os.path.join(WF_DIR, "brain-layer5.yml")
AUTONOMY = os.path.join(WF_DIR, "brain-autonomy.yml")

pytestmark = pytest.mark.skipif(
    not shutil.which("bash"), reason="workflow scripts need bash")

# The 202 envelope _delegate_to_worker() actually returns, verbatim.
DELEGATED_202 = ('{"success": true, "delegated_to": "worker", '
                 '"completed": false, "note": "job still running on '
                 'dchub-worker; check worker logs"}')


def _run_blocks(path):
    """Every `run: |` body in a workflow, de-indented by its 10-space stanza."""
    y = open(path, encoding="utf-8").read()
    marker = "        run: |\n"
    out, idx = [], 0
    while True:
        i = y.find(marker, idx)
        if i < 0:
            break
        start = i + len(marker)
        nxt = y.find("\n      - name:", start)
        body = y[start:nxt if nxt > 0 else len(y)]
        lines = [l[10:] if l.startswith(" " * 10) else l
                 for l in body.split("\n")]
        out.append("\n".join(lines))
        idx = start
    return out


# Stub curl. With -o it writes the body to that file and echoes the status
# code, exactly as real curl does with -o/-w; without -o it writes the body to
# stdout.
#
# A URL containing /status is a watermark read, answered from its OWN pair of
# env vars: the FIRST such call returns $STATUS_BEFORE and every later one
# $STATUS_AFTER, at $STATUS_CODE. A test therefore picks whether the watermark
# advances, and can make the watermark read itself fail or 202 independently
# of the job endpoint. Everything else in the block — the branching, the loop,
# the exits — is real bash.
_MOCK = r'''
curl() {
  local out="" prev="" url="" body="" code="" n=0
  for a in "$@"; do
    if [ "$prev" = "-o" ]; then out="$a"; fi
    case "$a" in http*://*) url="$a";; esac
    prev="$a"
  done
  case "$url" in
    */status*)
      n=$(cat "$CURL_STATE" 2>/dev/null || echo 0)
      n=$((n + 1))
      printf '%s' "$n" > "$CURL_STATE"
      if [ "$n" = "1" ]; then body="$STATUS_BEFORE"; else body="$STATUS_AFTER"; fi
      code="$STATUS_CODE"
      ;;
    *) body="$RUN_BODY"; code="$RUN_CODE" ;;
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


def _exec(block, run_body, run_code, status_before="", status_after="",
          status_code="200"):
    """Run one de-indented block against the stub; return (rc, stdout+stderr).

    status_before/status_after are the bodies the stubbed /status endpoint
    returns on the first call and on every subsequent one, and status_code the
    HTTP status it answers with — separate from run_code, because /status is a
    delegated path too and can 202 independently of the job endpoint. Blocks
    that never call a /status URL ignore all three.
    """
    script = block.replace("${{ secrets.DCHUB_ADMIN_KEY }}", "k")
    with tempfile.TemporaryDirectory() as td:
        sh = os.path.join(td, "s.sh")
        with open(sh, "w") as fh:
            fh.write(_MOCK + "\n" + script)
        env = dict(os.environ, ADMIN_KEY="k", RUN_BODY=run_body,
                   RUN_CODE=run_code,
                   STATUS_BEFORE=status_before, STATUS_AFTER=status_after,
                   STATUS_CODE=status_code, CURL_STATE=os.path.join(td, "n"),
                   RAILWAY_BASE="https://example.invalid")
        p = subprocess.run(["bash", sh], env=env, capture_output=True,
                           text=True, cwd=td)
        return p.returncode, p.stdout + p.stderr


def _l16_block():
    for b in _run_blocks(REASONING):
        if "self-critique/run" in b:
            return b
    raise AssertionError("L16 self-critique block not found")


def _backend_issues_block():
    for b in _run_blocks(LAYER5):
        if "learn-backend-issues" in b:
            return b
    raise AssertionError("layer5 backend-issues block not found")


def _autonomy_tick_block():
    for b in _run_blocks(AUTONOMY):
        if "autonomy/tick" in b:
            return b
    raise AssertionError("brain-autonomy tick block not found")


# A /autonomy/status body carrying a given last_tick watermark.
def _status(last_tick):
    return ('{"ok": true, "autonomy_enabled": false, "last_tick": %s, '
            '"counts": {}}' % (f'"{last_tick}"' if last_tick else "null"))


# --------------------------------------------------------------------------
# Non-vacuity: if extraction broke, every scenario below would run an empty
# script and pass. Assert we really pulled the blocks we think we did.
# --------------------------------------------------------------------------

def test_extraction_is_not_vacuous():
    assert len(_run_blocks(REASONING)) == 4, "expected 4 steps in reasoning-layers"
    assert len(_run_blocks(LAYER5)) == 2, "expected 2 steps in layer5"
    assert len(_run_blocks(AUTONOMY)) == 4, "expected 4 steps in brain-autonomy"
    for b in (_l16_block(), _backend_issues_block(), _autonomy_tick_block()):
        assert len(b) > 400, "run block looks truncated"
        assert "curl" in b


def test_the_status_stub_really_serves_the_watermark():
    """If the /status branch of the stub never fired, every 202 test below
    would poll a body of "" and agree with whatever the block decided for the
    wrong reason. Prove the two bodies are distinguishable through it."""
    rc, out = _exec(_autonomy_tick_block(), DELEGATED_202, "202",
                    _status("2026-08-19T02:00:00+00:00"),
                    _status("2026-08-19T03:00:00+00:00"))
    assert rc == 0, out
    assert "pre-run last_tick=2026-08-19T02:00:00+00:00" in out, out
    assert "poll 1/10 last_tick=2026-08-19T03:00:00+00:00" in out, out


# --------------------------------------------------------------------------
# L16 — self-critique
# --------------------------------------------------------------------------

def test_l16_warns_on_delegated_202():
    rc, out = _exec(_l16_block(), DELEGATED_202, "202")
    assert rc == 0, out
    assert "::warning::" in out, f"202 produced no warning:\n{out}"
    assert "202" in out
    assert "UNKNOWN" in out, "must say the counters are unknown, not zero"


def test_l16_is_quiet_on_a_real_200():
    body = '{"ok": true, "verified": 7, "predictions_scored": 3}'
    rc, out = _exec(_l16_block(), body, "200")
    assert rc == 0, out
    assert "::warning::" not in out, f"clean 200 must not warn:\n{out}"


def test_l16_warns_when_curl_produced_no_status():
    rc, out = _exec(_l16_block(), "", "000")
    assert rc == 0, out
    assert "::warning::" in out
    assert "UNKNOWN" in out


def test_l16_warns_on_a_non_200_status():
    rc, out = _exec(_l16_block(), '{"error":"nope"}', "503")
    assert rc == 0, out
    assert "::warning::" in out and "503" in out


# --------------------------------------------------------------------------
# layer5 — learn-backend-issues. The regression: a 202 rendered as
# `examined=0 proposed=0 refused=0`, indistinguishable from a healthy run
# that found nothing to do.
# --------------------------------------------------------------------------

def test_layer5_202_does_not_report_zero_counters():
    rc, out = _exec(_backend_issues_block(), DELEGATED_202, "202")
    assert rc == 0, out
    assert "::warning::" in out, f"202 produced no warning:\n{out}"
    assert "proposed=0" not in out, (
        "a still-running delegated job was reported as 0 proposals:\n" + out)
    assert "examined=0" not in out, (
        "a still-running delegated job was reported as 0 examined:\n" + out)
    assert "UNKNOWN" in out


def test_layer5_still_reports_counters_on_200():
    body = ('{"issues_examined": 4, "results": ['
            '{"outcome": "proposed"}, {"outcome": "proposed"}, '
            '{"outcome": "refused"}]}')
    rc, out = _exec(_backend_issues_block(), body, "200")
    assert rc == 0, out
    assert "examined=4" in out, out
    assert "proposed=2" in out, out
    assert "refused=1" in out, out
    assert "::warning::" not in out


def test_layer5_still_reports_a_genuine_skip_on_200():
    rc, out = _exec(_backend_issues_block(), '{"skipped": true}', "200")
    assert rc == 0, out
    assert "no actionable_backend_issues" in out, out


def test_layer5_a_real_zero_run_is_still_reported_as_zero():
    """The fix must not swallow a genuine 200 that examined nothing —
    that is real information and stays visible."""
    rc, out = _exec(_backend_issues_block(),
                    '{"issues_examined": 0, "results": []}', "200")
    assert rc == 0, out
    assert "examined=0" in out and "proposed=0" in out, out


def test_layer5_warns_when_curl_produced_no_status():
    rc, out = _exec(_backend_issues_block(), "", "000")
    assert rc == 0, out
    assert "::warning::" in out
    assert "proposed=0" not in out, "curl failure must not read as 0 proposals"


# --------------------------------------------------------------------------
# brain-autonomy — the inverted case. This cron BEATS the deadman board
# (tools/deadman/watch.py reads its run CONCLUSION), so a 202 treated as
# failure raised a false RED on https://dchub.cloud/api/v1/ops/deadman.
#
# The 202 gate here was added deliberately to replace a silent green, so
# these assert BOTH directions: a delegated tick that demonstrably ran must
# not fail the job, and one that never ran must still fail it.
# --------------------------------------------------------------------------

_T0 = "2026-08-19T02:54:00+00:00"
_T1 = "2026-08-19T02:56:12+00:00"


def test_autonomy_202_with_an_advancing_watermark_is_not_a_failure():
    """Run 32210235319's exact response. The tick was executing on the worker;
    exiting 1 on it flipped the public board to latest-run-failed."""
    rc, out = _exec(_autonomy_tick_block(), DELEGATED_202, "202",
                    _status(_T0), _status(_T1))
    assert rc == 0, f"a delegated tick that demonstrably ran still failed:\n{out}"
    assert "::error::" not in out, out
    assert "the loop did NOT run" not in out, (
        "202 means delegated-and-running, not did-not-run:\n" + out)
    assert "UNKNOWN" in out, "must say the counters are unknown, not zero"


def test_autonomy_202_that_never_advances_still_fails():
    """The gate must not become a blanket 202-is-fine. A tick that was
    accepted and then died leaves the watermark where it was."""
    rc, out = _exec(_autonomy_tick_block(), DELEGATED_202, "202",
                    _status(_T0), _status(_T0))
    assert rc != 0, f"a tick that never completed passed silently:\n{out}"
    assert "::error::" in out and "never advanced" in out, out


def test_autonomy_202_with_a_watermark_that_never_appears_still_fails():
    """last_tick null on every poll — a worker that never recorded a tick, or
    a /status still being answered by the process that did not run it."""
    rc, out = _exec(_autonomy_tick_block(), DELEGATED_202, "202",
                    _status(None), _status(None))
    assert rc != 0, f"a null watermark was accepted as completion:\n{out}"
    assert "::error::" in out, out


def test_autonomy_202_does_not_report_zero_counters():
    """The layer5 defect, checked here too: the 202 body carries no counters
    and `r.get('findings_filed', 0)` would render every one of them as 0."""
    rc, out = _exec(_autonomy_tick_block(), DELEGATED_202, "202",
                    _status(_T0), _status(_T1))
    assert rc == 0, out
    for k in ("findings_filed=0", "proposals_created=0", "draft_prs_opened=0",
              "reconciled=0"):
        assert k not in out, (
            f"a still-running delegated tick was reported as {k}:\n{out}")


def test_autonomy_still_reports_counters_on_a_real_200():
    body = ('{"ok": true, "result": {"findings_filed": 3, '
            '"proposals_created": 2, "draft_prs_opened": 1, "reconciled": 4}}')
    rc, out = _exec(_autonomy_tick_block(), body, "200")
    assert rc == 0, out
    assert "findings_filed=3" in out and "proposals_created=2" in out, out
    assert "draft_prs_opened=1" in out and "reconciled=4" in out, out
    assert "::error::" not in out, out


def test_autonomy_a_real_zero_tick_is_still_reported_as_zero():
    """A genuine 200 that did nothing is real information and stays visible —
    the fix must not swallow it along with the unknown case."""
    body = ('{"ok": true, "result": {"findings_filed": 0, '
            '"proposals_created": 0, "draft_prs_opened": 0, "reconciled": 0}}')
    rc, out = _exec(_autonomy_tick_block(), body, "200")
    assert rc == 0, out
    assert "findings_filed=0" in out and "proposals_created=0" in out, out


def test_autonomy_dormant_200_is_still_recognised():
    rc, out = _exec(_autonomy_tick_block(),
                    '{"ok": true, "result": {"disabled": true}}', "200")
    assert rc == 0, out
    assert "DORMANT" in out, out


def test_autonomy_still_fails_loudly_on_a_non_200():
    """The gate this replaces existed for a reason — keep it working."""
    for code in ("401", "403", "500", "503"):
        rc, out = _exec(_autonomy_tick_block(), '{"error":"nope"}', code)
        assert rc != 0, f"HTTP {code} did not fail the step:\n{out}"
        assert "::error::" in out and code in out, out


def test_autonomy_still_fails_loudly_when_curl_produced_no_status():
    rc, out = _exec(_autonomy_tick_block(), "", "000")
    assert rc != 0, f"a dead origin did not fail the step:\n{out}"
    assert "::error::" in out, out


def test_autonomy_no_pre_run_watermark_rejects_a_stale_timestamp():
    """If the pre-run read failed there is no BEFORE to compare against, and
    the PREVIOUS tick's timestamp — 30 min old, sitting in the worker's
    memory — would otherwise pass as this tick's completion. That is a false
    green built out of a failed read."""
    rc, out = _exec(_autonomy_tick_block(), DELEGATED_202, "202",
                    _status(None), _status("2020-01-01T00:00:00+00:00"))
    assert rc != 0, f"a pre-existing watermark was accepted as completion:\n{out}"
    assert "stale, not this tick" in out, out


def test_autonomy_no_pre_run_watermark_accepts_a_tick_stamped_after_the_post():
    """The other half — with no BEFORE, a watermark stamped after the POST is
    genuine completion and must still pass."""
    rc, out = _exec(_autonomy_tick_block(), DELEGATED_202, "202",
                    _status(None), _status("2099-01-01T00:00:00+00:00"))
    assert rc == 0, f"a genuinely fresh watermark was rejected:\n{out}"
    assert "completed on the worker" in out, out


def test_autonomy_says_so_when_the_watermark_read_itself_is_relayed():
    """/autonomy/status is a delegated path too, so it can answer with the
    same 202 envelope — which carries no last_tick. Reading that as "no
    progress" is the identical false-zero one level down. It must still fail
    (we did not observe completion) but say the watermark was UNKNOWN."""
    rc, out = _exec(_autonomy_tick_block(), DELEGATED_202, "202",
                    DELEGATED_202, DELEGATED_202, status_code="202")
    assert rc != 0, out
    assert "watermark UNKNOWN" in out, (
        "a relayed watermark read was silently treated as no-progress:\n" + out)


def test_the_autonomy_watermark_is_read_from_the_process_that_moves_it():
    """The poll above is only meaningful if /autonomy/status is answered by
    the process that ran the tick.

    brain_autonomy_loop._LAST_TICK is a module-level global — one copy per
    service, since start_web.sh runs `gunicorn --workers 1`. /autonomy/tick is
    delegated, so ticks execute on dchub-worker and advance the WORKER's copy.
    Leave the status GET undelegated and it serves WEB's copy, which nothing
    ever writes: last_tick stays null forever, the poll never sees an advance,
    and brain-autonomy goes permanently red — the same false failure this
    change removed, one level down.

    Both halves are asserted, so neither can be undone quietly. If _LAST_TICK
    is ever replaced by a DB-backed watermark the first assertion fails on
    purpose: the delegation requirement goes away with it, and this test
    should be rewritten rather than the entry kept out of habit.
    """
    src = open(os.path.join(ROOT, "routes/brain_autonomy_loop.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    globals_ = {t.id for n in tree.body if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assert "_LAST_TICK" in globals_, (
        "_LAST_TICK is no longer a module global — if the watermark is now "
        "DB-backed, /autonomy/status need not be delegated; rewrite this test")
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "last_tick")
    assert any(isinstance(d, ast.Name) and d.id == "_LAST_TICK"
               for d in ast.walk(fn)), "last_tick() no longer reads _LAST_TICK"

    paths, _sync = _proxy_path_sets()
    assert "/api/v1/brain/autonomy/tick" in paths, (
        "the tick is no longer delegated — re-read brain-autonomy.yml, the "
        "202 handling there assumes it is")
    assert "/api/v1/brain/autonomy/status" in paths, (
        "/api/v1/brain/autonomy/status is not in main.py's worker-proxy "
        "allowlist, so it answers from web's never-written _LAST_TICK while "
        "the tick runs on the worker — brain-autonomy.yml's poll can never "
        "observe an advance and the cron fails on every delegated tick")


def test_autonomy_polls_a_bounded_number_of_times():
    """Unbounded polling would hang until the job timeout, which the deadman
    board reads as a failure just the same."""
    rc, out = _exec(_autonomy_tick_block(), DELEGATED_202, "202",
                    _status(_T0), _status(_T0))
    assert rc != 0
    assert "poll 10/10" in out, out
    assert "poll 11/10" not in out, out


# --------------------------------------------------------------------------
# Repo-wide fence: any workflow curl to a worker-delegated endpoint
# can receive the 202 envelope, so it must read the status code — and must
# allow more than that path's relay budget, or the 202 can never arrive.
#
# This is what stops the next endpoint added to the allowlist from silently
# reintroducing the bug in a cron nobody re-reads.
#
# ★ 2026-08-19: scope widened from _WORKER_PROXY_SYNC_PATHS (21 paths) to
# every delegated path (POST ∪ GET ∪ SYNC, 70). _delegate_to_worker()'s
# ReadTimeout branch returns the 202 envelope for ANYTHING in the allowlist;
# SYNC membership only picks the deadline. Scoped to the sync set this fence
# matched 7 curls and never looked at brain-autonomy.yml, whose endpoint sits
# in POST_PATHS on the 15s budget. It now matches 13.
#
# The budget is therefore per-path, mirroring main.py exactly:
#   _read_budget = 180 if request.path in _WORKER_PROXY_SYNC_PATHS else 15
#
# ★ Known coverage limits, stated rather than implied:
#   · The scan only sees a literal endpoint on the curl line. dcpi-daily.yml
#     (BASE=".../dcpi/recompute" then curl "$BASE?...") and dchub-jobs.yml
#     (URLs built from a JOBS list) call delegated paths through variables and
#     are NOT checked here.
#   · It cannot see what a cron DOES with the status it read. brain-autonomy's
#     defect — reading 202 correctly and calling it "did NOT run" — passes both
#     rules below. Only the executable tests above catch that class, which is
#     why this file runs the bash instead of grepping it.
# --------------------------------------------------------------------------

# Offenders that predate this fence. Each is the SAME defect, but in a cron
# outside the scope of the change that added this test — and data-sync.yml
# beats the deadman board, so its 202 needs news-ner's poll-the-watermark
# treatment (#2886), not the one-line warning used above.
#
# This list cannot rot: an entry that stops being an offender FAILS the test
# below, which forces its removal instead of letting it linger as a stale
# exemption.
_KNOWN_GAPS = {
    ("brain-inspector.yml", "/api/v1/brain/brief/generate"):
        "no status capture; does not beat the deadman board (quiet class)",
    ("data-sync.yml", "/api/jobs/news-refresh"):
        "--max-time 180 == relay budget, so the 202 can never be observed",
    ("data-sync.yml", "/api/jobs/evolution"):
        "no status capture, --max-time 60; BEATS the deadman board",
    # Surfaced by the 2026-08-19 widening to all delegated paths. Each is the
    # same defect on the 15s budget, and each is a cron outside the scope of
    # the brain-autonomy change — listed so the widening lands without
    # pretending they are fixed.
    # FIXED 2026-08-19: brain-self-direct.yml / /api/v1/brain/self-direct/tick
    # now spawns-and-polls /api/v1/brain/self-direct/status last_tick, a
    # brain_state row _record_tick() stamps at the END of every tick including
    # the skips. Behaviour covered by tests/test_self_direct_delegated_202.py.
    ("data-sync.yml", "/api/kmz-discovery/run"):
        "no status capture; the handler runs ~17 min so the 202 is the NORMAL "
        "answer here, not the exception",
    # ★ 2026-08-19 — surfaced by teaching this scan to resolve a path held in a
    # shell variable. dcpi-daily.yml builds
    #   BASE="https://...up.railway.app/api/v1/dcpi/recompute"
    # and curls "${BASE}?offset=..", so the path was never on the curl line and
    # the fence had never looked at this cron at all. It violates the ORIGINAL
    # --max-time rule, not the new one: --max-time 150 against this path's 180s
    # sync budget, so the 202 arrives after curl has already given up.
    #
    # Left listed rather than fixed here on purpose. The failure is a false RED,
    # not a false green — a delegated chunk yields markets_scored=0, misses the
    # EXACT-coverage check (r-dcpi-exactcoverage), burns all 3 retries and reds
    # the run — so nothing untrue is published while it stands. Fixing it means
    # giving the chunk loop a watermark to poll, which reopens the exact-coverage
    # invariant that 41 markets sat frozen behind for 14 days. That deserves its
    # own change with its own executable test, not a rider on a fence widening.
    ("dcpi-daily.yml", "/api/v1/dcpi/recompute"):
        "--max-time 150 <= the 180s sync budget; false RED, needs a watermark "
        "poll that reopens the exact-coverage invariant — own change",
    # FIXED 2026-08-19: facility-snapshot-daily.yml /
    # /api/v1/markets/deep-dive/cron now POSTs origin-direct (it went through
    # the CF edge, where ROUTE_TIMEOUTS 15s answered it first) and
    # spawns-and-polls /api/v1/markets/deep-dive/status last_cron_run, stamped
    # at the END of every rotation. Covered by
    # tests/test_deep_dive_delegated_202.py.
    # FIXED 2026-08-19: iso-queue-ingest.yml / /api/v1/iso-queue/ingest now
    # spawns-and-polls MAX(last_run) over /api/v1/iso-queue/ingest/status, and
    # its beat reports the OBSERVED outcome with rows_inserted omitted when
    # unknown. Behaviour covered by tests/test_iso_queue_delegated_202.py.
}


def _proxy_path_sets():
    """(all delegated paths, the sync subset) straight out of main.py."""
    tree = ast.parse(open(os.path.join(ROOT, "main.py"), encoding="utf-8").read())
    found = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Name)
                        and t.id.startswith("_WORKER_PROXY_")
                        and t.id.endswith("_PATHS")):
                    found[t.id] = {e.value for e in n.value.args[0].elts}
    for name in ("_WORKER_PROXY_POST_PATHS", "_WORKER_PROXY_GET_PATHS",
                 "_WORKER_PROXY_SYNC_PATHS"):
        assert name in found, f"{name} not found in main.py"
    return set().union(*found.values()), found["_WORKER_PROXY_SYNC_PATHS"]


def _read_budget(path, sync_paths):
    """main.py: 180 if request.path in _WORKER_PROXY_SYNC_PATHS else 15."""
    return 180 if path in sync_paths else 15


def _strip_bash_comments(body):
    """Drop `#` comments so a RULE can never be satisfied by prose.

    Not cosmetic. Every fix in this class ships 40+ lines of comment that
    quote the 202 envelope verbatim, so a bare `"202" in body` test is
    satisfied by the explanation of the bug rather than by any code that
    handles it — the same way #2938 found the self-direct watermark check
    could be talked into passing by a comment. A `#` only opens a comment at
    the start of a word, so `?cb=1#x` and `${VAR#pfx}` survive.
    """
    out = []
    for line in body.split("\n"):
        cut, quote = None, None
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                cut = i
                break
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def _resolve_shell_vars(body):
    """Expand $VAR / ${VAR} from assignments made earlier in the SAME step.

    ★ 2026-08-19 — the blind spot that let this fence pass two live crons.
    brain-master-tick.yml and sentinel-master-tick.yml both do

        BASE="${DCHUB_BACKEND_BASE:-https://...up.railway.app}"
        URL="$BASE/api/v1/admin/brain/master-tick?cb=${GITHUB_RUN_ID}"
        body=$(curl -sS --max-time 60 -X POST "$URL" ...)

    so the delegated path never appears on the curl LINE the scan reads.
    Proven by mutation: the identical defect written inline is caught, and
    written through a variable it passes. That is the most common way to
    write a cron step in this repo, so the fence was blind by construction
    rather than by accident.

    Deliberately per-step: a variable set in one step does not exist in the
    next (each `run:` is its own shell), and resolving across step
    boundaries would attribute one step's URL to another's curl.

    `${VAR:-default}` is left alone on purpose — it is not a plain
    reference, and leaving the literal in place keeps the path visible,
    which is all the match below needs.
    """
    assigns, out = {}, []
    for line in body.split("\n"):
        line = re.sub(r"\$\{(\w+)\}|\$(\w+)",
                      lambda m: assigns.get(m.group(1) or m.group(2), m.group(0)),
                      line)
        out.append(line)
        m = re.match(r"\s*(?:export\s+)?(\w+)=(.*)$", line)
        if m:
            val = m.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            # Only plain string assignments. `body=$(curl ... "$URL")` captures
            # OUTPUT, not a URL; substituting it would paste a whole curl
            # invocation into every later `echo "$body"` and invent curl
            # commands — some without %{http_code} — that nobody wrote.
            if "$(" not in val and "`" not in val:
                assigns[m.group(1)] = val
    return "\n".join(out)


def _curl_commands(text):
    """Each curl invocation with backslash-continuations joined into one line."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [l for l in joined.split("\n") if "curl " in l]


def _delegating_steps():
    """(workflow name, step name, resolved code) for every step that runs bash.

    Per STEP, not per file. iso-queue-ingest.yml is why: #2931 fixed its
    aggregate step and left the sibling step below it POSTing on the same
    schedule, and facility-snapshot-daily.yml has seven steps of which one
    was fixed. A file-level check lets the one fixed step vouch for its
    blind neighbours.
    """
    for wf in sorted(glob.glob(os.path.join(WF_DIR, "*.yml"))
                     + glob.glob(os.path.join(WF_DIR, "*.yaml"))):
        doc = yaml.safe_load(open(wf, encoding="utf-8").read())
        if not isinstance(doc, dict):
            continue
        for job in (doc.get("jobs") or {}).values():
            if not isinstance(job, dict):
                continue
            for i, step in enumerate(job.get("steps") or []):
                if not isinstance(step, dict):
                    continue
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                code = _resolve_shell_vars(_strip_bash_comments(run))
                yield (os.path.basename(wf),
                       str(step.get("name") or f"step {i}"), code)


def _hits_sync_path(cmd, paths):
    """Exact endpoint match. `/api/v1/dcpi/recompute` is a PREFIX of
    `/api/v1/dcpi/recompute-missing`, which is NOT delegated (the gate does
    `request.path in <frozenset>`), so a substring test mislabels it."""
    for p in sorted(paths, key=len, reverse=True):
        for m in re.finditer(re.escape(p), cmd):
            nxt = cmd[m.end():m.end() + 1]
            if nxt not in ("", "-") and not (nxt.isalnum() or nxt in "_/"):
                return p
    return None


def test_delegated_path_workflow_calls_read_the_status_code():
    paths, sync_paths = _proxy_path_sets()
    assert len(paths) > 40, "delegation allowlist looks empty — fence is vacuous"
    assert len(sync_paths) > 10, "sync allowlist looks empty — fence is vacuous"
    assert sync_paths < paths, "sync paths must be a subset of the delegated set"
    checked, offenders, seen_bad = 0, [], set()
    for name, step, code in _delegating_steps():
        for cmd in _curl_commands(code):
            path = _hits_sync_path(cmd, paths)
            if not path:
                continue
            checked += 1
            why = None
            budget = _read_budget(path, sync_paths)
            if "%{http_code}" not in cmd:
                why = (f"never reads the HTTP status, so a relayed 202 is "
                       f"indistinguishable from a completed 200")
            elif not re.search(r"\b202\b|delegated_to", code):
                # ★ 2026-08-19. Reading the status and never separating the
                # delegated case is not a lesser version of this bug — it IS
                # the bug, and exactly what brain-autonomy.yml did before
                # #2929: it captured %{http_code}, allowed 300s, satisfied both
                # older rules, and still called a delegated run "the loop did
                # NOT run" and beat the public board red. This file's own
                # docstring conceded the static scan could not catch it.
                #
                # `delegated_to` counts as well as a literal 202, because on
                # the fire-and-forget paths a 202 is AMBIGUOUS: the handler's
                # own ack and _delegate_to_worker()'s give-up envelope share
                # the status and differ only in the body. brain-master-tick and
                # sentinel-master-tick match `2*` and then discriminate on
                # delegated_to/completed, which is strictly more precise than a
                # `202)` case would be. Requiring the literal would push authors
                # toward the weaker check.
                why = ("reads the HTTP status but never separates the delegated "
                       "response (no 202 branch, no delegated_to check), so a "
                       "relayed run lands in whichever branch the step uses for "
                       "a completed 200")
            else:
                m = re.search(r"--max-time\s+(\d+)", cmd)
                if m and int(m.group(1)) <= budget:
                    why = (f"uses --max-time {m.group(1)}, <= the {budget}s relay "
                           f"budget for this path — the 202 arrives after curl "
                           f"has already given up, so it can never be observed")
            if why is None:
                continue
            seen_bad.add((name, path))
            if (name, path) not in _KNOWN_GAPS:
                offenders.append(f"{name} :: {step}: curl to {path} {why}")

    # 16, raised from 13 on 2026-08-19: resolving $URL took the scan from 14
    # delegated-path curls to 18. At 13 the resolver could stop working
    # entirely, drop all four back out of view, and this floor would still
    # pass — which is precisely how the blind spot survived in the first
    # place. 16 leaves room for two legitimate removals and still catches a
    # wholesale resolver failure. The mechanism itself is pinned by
    # test_a_path_built_in_a_shell_variable_is_still_seen below, which does
    # not depend on what the repo happens to contain today.
    assert checked >= 16, (
        f"fence only matched {checked} delegated-path curls — extraction "
        f"likely broke and this test would pass vacuously")
    assert not offenders, (
        "workflow curl to a worker-relayed endpoint cannot observe a 202:\n"
        + "\n".join(offenders))

    # Anti-rot: a gap that got fixed must be deleted from _KNOWN_GAPS, or the
    # list quietly becomes a permanent exemption for bugs that no longer exist.
    stale = sorted(set(_KNOWN_GAPS) - seen_bad)
    assert not stale, (
        "these _KNOWN_GAPS entries are no longer offenders — remove them:\n"
        + "\n".join(f"  {n}: {p}" for n, p in stale))


# ── The two mechanisms the 2026-08-19 widening added ────────────────────────
# Pinned directly, on synthetic input, so they keep holding no matter what the
# repo's workflows happen to look like later. The repo-wide scan above can only
# tell you SOMETHING is still being checked; these say what.

def test_a_path_built_in_a_shell_variable_is_still_seen():
    """The blind spot that let brain-master-tick and sentinel-master-tick pass.

    Both write BASE=... then URL="$BASE/<delegated path>" then curl "$URL", so
    the path never appears on the curl line. Proven by mutation on 2026-08-19:
    the identical defect written inline was caught and written through a
    variable it was not.
    """
    paths, _ = _proxy_path_sets()
    target = "/api/v1/admin/brain/master-tick"
    assert target in paths, "fixture path is no longer delegated — pick another"

    inline = f'curl -sS -X POST "https://h{target}"'
    assert _hits_sync_path(inline, paths) == target, "control: inline must match"

    step = (f'BASE="${{DCHUB_BACKEND_BASE:-https://h}}"\n'
            f'URL="$BASE{target}?cb=${{GITHUB_RUN_ID}}"\n'
            f'body=$(curl -sS --max-time 60 -X POST "$URL")\n')
    # The control has to mirror how the scan actually uses these: per CURL
    # LINE, not per step. The path is of course present somewhere in the step
    # text — in the URL= assignment — which is exactly why a whole-step
    # substring check would prove nothing about resolution.
    assert not [c for c in _curl_commands(step) if _hits_sync_path(c, paths)], (
        "unresolved, no curl LINE may carry the path — otherwise this test "
        "passes without the resolver doing anything")
    resolved = _resolve_shell_vars(step)
    cmds = [c for c in _curl_commands(resolved) if _hits_sync_path(c, paths)]
    assert cmds, f"variable-built path went unseen after resolution:\n{resolved}"


def test_a_comment_cannot_satisfy_the_202_rule():
    """#2938's class: every fix here ships 40 lines of comment quoting the 202
    envelope verbatim, so a bare substring test is satisfied by the prose that
    explains the bug rather than by any code that handles it."""
    prose = ('# a relayed 202 means delegated_to worker, completed false\n'
             'CODE=$(curl -sS -o b -w \'%{http_code}\' -X POST "$URL")\n')
    stripped = _strip_bash_comments(prose)
    assert not re.search(r"\b202\b|delegated_to", stripped), (
        "comment survived stripping — the rule can be talked into passing")
    assert "%{http_code}" in stripped, "stripper ate real code"

    real = 'case "$http" in\n  202) echo delegated ;;\nesac\n'
    assert re.search(r"\b202\b", _strip_bash_comments(real)), (
        "stripper ate a real 202 branch")


def test_the_resolver_does_not_invent_curl_commands():
    """`body=$(curl ...)` captures OUTPUT. Substituting it would paste a whole
    curl invocation into every later `echo "$body"` and manufacture offenders —
    without %{http_code} — that nobody wrote."""
    body = ('URL="https://h/api/v1/dcpi/recompute"\n'
            'body=$(curl -sS -X POST "$URL")\n'
            'echo "$body"\n'
            'echo "$body" | head -c 400\n')
    assert len(_curl_commands(_resolve_shell_vars(body))) == 1
