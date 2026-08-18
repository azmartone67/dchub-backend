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
the very status they needed. Hence the >180s assertion in the fence below.
"""
import ast
import glob
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, ".github/workflows")
REASONING = os.path.join(WF_DIR, "brain-reasoning-layers.yml")
LAYER5 = os.path.join(WF_DIR, "brain-layer5.yml")

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


# Stub curl: writes $RUN_BODY to the -o target and echoes $RUN_CODE, exactly
# as real curl does with -o/-w. Everything else in the block is real bash.
_MOCK = r'''
curl() {
  local out="" prev=""
  for a in "$@"; do
    if [ "$prev" = "-o" ]; then out="$a"; fi
    prev="$a"
  done
  [ -n "$out" ] && printf '%s' "$RUN_BODY" > "$out"
  printf '%s' "$RUN_CODE"
  return 0
}
sleep() { :; }
'''


def _exec(block, run_body, run_code):
    """Run one de-indented block against the stub; return (rc, stdout+stderr)."""
    script = block.replace("${{ secrets.DCHUB_ADMIN_KEY }}", "k")
    with tempfile.TemporaryDirectory() as td:
        sh = os.path.join(td, "s.sh")
        with open(sh, "w") as fh:
            fh.write(_MOCK + "\n" + script)
        env = dict(os.environ, ADMIN_KEY="k", RUN_BODY=run_body,
                   RUN_CODE=run_code,
                   RAILWAY_BASE="https://example.invalid")
        p = subprocess.run(["bash", sh], env=env, capture_output=True,
                           text=True)
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


# --------------------------------------------------------------------------
# Non-vacuity: if extraction broke, every scenario below would run an empty
# script and pass. Assert we really pulled the blocks we think we did.
# --------------------------------------------------------------------------

def test_extraction_is_not_vacuous():
    assert len(_run_blocks(REASONING)) == 4, "expected 4 steps in reasoning-layers"
    assert len(_run_blocks(LAYER5)) == 2, "expected 2 steps in layer5"
    for b in (_l16_block(), _backend_issues_block()):
        assert len(b) > 400, "run block looks truncated"
        assert "curl" in b


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
# Repo-wide fence: any workflow curl to a _WORKER_PROXY_SYNC_PATHS endpoint
# can receive the 202 envelope, so it must read the status code — and must
# allow more than the 180s relay budget, or the 202 can never arrive.
#
# This is what stops the next endpoint added to the allowlist from silently
# reintroducing the bug in a cron nobody re-reads.
#
# ★ Known coverage limit, stated rather than implied: the scan only sees a
# literal endpoint on the curl line. dcpi-daily.yml (BASE=".../dcpi/recompute"
# then curl "$BASE?...") and dchub-jobs.yml (URLs built from a JOBS list) call
# sync paths through variables and are NOT checked here.
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
}

def _sync_paths():
    tree = ast.parse(open(os.path.join(ROOT, "main.py"), encoding="utf-8").read())
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Name)
                        and t.id == "_WORKER_PROXY_SYNC_PATHS"):
                    return {e.value for e in n.value.args[0].elts}
    raise AssertionError("_WORKER_PROXY_SYNC_PATHS not found in main.py")


def _curl_commands(text):
    """Each curl invocation with backslash-continuations joined into one line."""
    joined = re.sub(r"\\\n\s*", " ", text)
    return [l for l in joined.split("\n") if "curl " in l]


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


def test_sync_path_workflow_calls_read_the_status_code():
    paths = _sync_paths()
    assert len(paths) > 10, "sync-path allowlist looks empty — fence is vacuous"
    checked, offenders, seen_bad = 0, [], set()
    for wf in sorted(glob.glob(os.path.join(WF_DIR, "*.yml"))
                     + glob.glob(os.path.join(WF_DIR, "*.yaml"))):
        text = open(wf, encoding="utf-8").read()
        for cmd in _curl_commands(text):
            path = _hits_sync_path(cmd, paths)
            if not path:
                continue
            checked += 1
            name = os.path.basename(wf)
            why = None
            if "%{http_code}" not in cmd:
                why = (f"never reads the HTTP status, so a relayed 202 is "
                       f"indistinguishable from a completed 200")
            else:
                m = re.search(r"--max-time\s+(\d+)", cmd)
                if m and int(m.group(1)) <= 180:
                    why = (f"uses --max-time {m.group(1)}, <= the 180s relay "
                           f"budget — the 202 arrives after curl has already "
                           f"given up, so it can never be observed")
            if why is None:
                continue
            seen_bad.add((name, path))
            if (name, path) not in _KNOWN_GAPS:
                offenders.append(f"{name}: curl to {path} {why}")

    assert checked >= 7, (
        f"fence only matched {checked} sync-path curls — extraction likely "
        f"broke and this test would pass vacuously")
    assert not offenders, (
        "workflow curl to a worker-relayed endpoint cannot observe a 202:\n"
        + "\n".join(offenders))

    # Anti-rot: a gap that got fixed must be deleted from _KNOWN_GAPS, or the
    # list quietly becomes a permanent exemption for bugs that no longer exist.
    stale = sorted(set(_KNOWN_GAPS) - seen_bad)
    assert not stale, (
        "these _KNOWN_GAPS entries are no longer offenders — remove them:\n"
        + "\n".join(f"  {n}: {p}" for n, p in stale))
