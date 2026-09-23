"""infrastructure-sync: schedule retired, manual door judges the REAL verdict.

MEASURED 2026-09-22. daily-infra-sync.yml (04:08 UTC) had failed every day
since its last success on 2026-09-02, and dchub-jobs.yml fired the same
endpoint at 01:00. /api/jobs/infrastructure-sync has one live leg — fiber
discovery — and it has no source (PeeringDB /api/ix carries no coordinates;
the owner ruled out synthetic PeeringDB pairing on 2026-09-07). The owner
decided to stop running it rather than keep a red no PR can clear.

The red was ALSO the wrong red. The endpoint is relayed to dchub-worker and
web answers 202 when the job outlives the 180s relay budget (01:16 -> 01:19,
193s, measured). The workflow's comment said a 202 carries no verdict, and the
next lines judged the empty 202 body anyway: loaders_total=0, "NO loader
succeeded (0/0)". A comment promising behaviour the code did not have.

What this pins:
  1. Neither scheduler fires the endpoint any more, and dchub-jobs.yml does not
     offer it by hand either (it judges any 2xx, including a relayed 202, as
     success — a manual fire there would be a false green).
  2. daily-infra-sync.yml stays on the default branch, dispatch-only — deleting
     it would be a GHOST to ingestion_integrity_master_shell.workflow_present —
     and no longer beats the dead-man board for a retired feed.
  3. Its script, EXECUTED against a stub curl, polls /api/jobs/last-run on a
     202 and returns the handler's own verdict: green only for last_status=ok,
     red for http_500 or a watermark that never moves.
  4. The retired cron does not page: check_cron_freshness skips it, and the
     unscheduled-endpoint detector is told it is manual on purpose.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys

import pytest

yaml = pytest.importorskip("yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, ".github", "workflows")
INFRA_WF = os.path.join(WF_DIR, "daily-infra-sync.yml")
JOBS_WF = os.path.join(WF_DIR, "dchub-jobs.yml")
JOB = "infrastructure-sync"


def _read(p: str) -> str:
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def _triggers(doc: dict) -> dict:
    # PyYAML reads the bare key `on:` as boolean True.
    t = doc.get("on", doc.get(True))
    return t if isinstance(t, dict) else {t: None}


# ── 1. no scheduler fires it ────────────────────────────────────────────────

def test_daily_infra_sync_has_no_schedule_but_stays_dispatchable():
    doc = yaml.safe_load(_read(INFRA_WF))
    trig = _triggers(doc)
    assert "schedule" not in trig, (
        "daily-infra-sync.yml is scheduled again. Its only live leg (fiber "
        "discovery) has no source, so a cron here is a daily red nobody can "
        "clear. Re-schedule only with a real source AND a restored dead-man beat.")
    assert "workflow_dispatch" in trig, (
        "daily-infra-sync.yml lost workflow_dispatch — it is the manual door, "
        "and a file with no trigger at all reads as abandoned")


def test_dchub_jobs_neither_schedules_nor_offers_infrastructure_sync():
    src = _read(JOBS_WF)
    m = re.search(r'case "\$\{TRIGGER_CRON\}" in(.*?)\n\s+esac', src, re.S)
    assert m, "TRIGGER_CRON case block not found — dispatch was rewritten?"
    arms = re.findall(r'^\s*"([^"]+)"\)\s*JOBS="([^"]+)"', m.group(1), re.M)
    assert arms, "no dispatch arms parsed — the regex no longer matches the file"
    firing = [cron for cron, jobs in arms
              if JOB in [j.strip() for j in jobs.split(",")]]
    assert not firing, f"dchub-jobs.yml dispatches {JOB} again on {firing}"
    assert "'0 1 * * *'" not in src, "the retired 01:00 cron is declared again"

    dd = re.search(r"job:\n(.*?)\n\nenv:", src, re.S)
    assert dd, "workflow_dispatch job input not found"
    choices = set(re.findall(r"^\s+- (\S+)\s*$", dd.group(1), re.M))
    assert "keep-alive" in choices, "dropdown parse is broken (keep-alive missing)"
    assert JOB not in choices, (
        "dchub-jobs.yml offers infrastructure-sync by hand again. Its runner "
        "prints SUCCESS for any 2xx, and this endpoint answers a relayed 202 "
        "— a manual fire there is a false green. Use daily-infra-sync.yml.")


# ── 2. the file stays; the retired feed is not beaten ──────────────────────

def test_the_manual_door_does_not_beat_the_retired_deadman_feed():
    src = _read(INFRA_WF)
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "ingest-runs/beat" not in body, (
        "daily-infra-sync.yml beats the dead-man board again. The feed is "
        "retired (purged from ingest_runs); a hand-fired beat re-creates it "
        "with a 30h cadence and it reads STALE two days later.")


# ── 3. the script, executed ─────────────────────────────────────────────────

def _step_script() -> str:
    doc = yaml.safe_load(_read(INFRA_WF))
    steps = doc["jobs"]["sync"]["steps"]
    runs = [s["run"] for s in steps if isinstance(s, dict) and "run" in s]
    assert len(runs) == 1, f"expected one run step, found {len(runs)}"
    return runs[0]


_FAKE_CURL = r'''
import json, os, sys
args = sys.argv[1:]
out, method, i = None, "GET", 0
while i < len(args):
    if args[i] == "-o":
        out = args[i + 1]; i += 2; continue
    if args[i] == "-X":
        method = args[i + 1]; i += 2; continue
    i += 1
url = args[-1]
scen = json.load(open(os.environ["FAKE_SCENARIO"]))
sp = os.environ["FAKE_STATE"]
st = json.load(open(sp)) if os.path.exists(sp) else {"wm": 0, "post": 0, "urls": []}
st["urls"].append(method + " " + url)
if "/api/jobs/last-run" in url:
    seq = scen["watermarks"]
    code, body = seq[min(st["wm"], len(seq) - 1)]
    st["wm"] += 1
else:
    st["post"] += 1
    code, body = scen["post"]
json.dump(st, open(sp, "w"))
if out:
    with open(out, "w") as fh:
        fh.write(body if isinstance(body, str) else json.dumps(body))
sys.stdout.write(str(code))
'''


def _wm(at, status="ok"):
    return [200, {"success": True, "jobs": {JOB: {
        "last_completed_at": at, "last_status": status}}}]


def _run(tmp_path, scenario: dict):
    """Execute the workflow step exactly as GitHub's default bash shell does,
    with curl and sleep stubbed. Returns (rc, output, state)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text("#!" + sys.executable + "\n" + _FAKE_CURL)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    for p in (curl, sleep):
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (tmp_path / "scenario.json").write_text(json.dumps(scenario))
    script = tmp_path / "step.sh"
    script.write_text(_step_script())
    env = dict(os.environ)
    env.update({
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
        "ADMIN_KEY": "test-admin-key",
        "BACKEND_URL": "https://origin.invalid",
        "RUNNER_TEMP": str(tmp_path),
        "FAKE_SCENARIO": str(tmp_path / "scenario.json"),
        "FAKE_STATE": str(tmp_path / "state.json"),
    })
    p = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail",
                        str(script)], env=env, capture_output=True, text=True,
                       timeout=60)
    state = json.loads((tmp_path / "state.json").read_text())
    return p.returncode, p.stdout + p.stderr, state


_ACCEPTED = [202, {"success": True, "delegated_to": "worker", "completed": False,
                   "note": "job still running on dchub-worker; check worker logs"}]
_PREV = "2026-09-23T01:19:25.210129+00:00"
_NEXT = "2099-01-01T00:00:00+00:00"


def test_202_then_watermark_moves_with_ok_is_green(tmp_path):
    rc, out, st = _run(tmp_path, {
        "post": _ACCEPTED,
        "watermarks": [_wm(_PREV, "http_500"), _wm(_PREV, "http_500"),
                       _wm(_NEXT, "ok")],
    })
    assert rc == 0, out
    assert st["post"] == 1
    assert st["wm"] == 3, f"expected 1 pre-read + 2 polls, got {st['wm']}: {out}"
    assert f"POST https://origin.invalid/api/jobs/{JOB}" in st["urls"]
    assert "last_status=ok" in out


def test_202_then_worker_reports_500_is_red_and_names_it(tmp_path):
    """THE CURRENT PRODUCTION STATE. The worker finishes and the handler says
    500 (fiber no_source). That is the verdict, and it is red."""
    rc, out, _ = _run(tmp_path, {
        "post": _ACCEPTED,
        "watermarks": [_wm(_PREV, "http_500"), _wm(_NEXT, "http_500")],
    })
    assert rc == 1, out
    assert "last_status=http_500" in out, out


def test_202_with_a_watermark_that_never_moves_is_red(tmp_path):
    """The old failure shape, inverted: an empty 202 must never be read as a
    verdict — neither 'NO loader succeeded' nor success."""
    rc, out, st = _run(tmp_path, {
        "post": _ACCEPTED,
        "watermarks": [_wm(_PREV, "ok")],
    })
    assert rc == 1, out
    assert "never advanced" in out, out
    assert st["wm"] == 17, f"expected 1 pre-read + 16 polls, got {st['wm']}"


def test_unreadable_pre_read_does_not_let_an_old_stamp_pass(tmp_path):
    """With no BEFORE watermark, only a stamp AFTER the POST counts. A
    previous run's 'ok' must not be taken as this run's completion."""
    rc, out, _ = _run(tmp_path, {
        "post": _ACCEPTED,
        "watermarks": [[503, "upstream down"],
                       _wm("2020-01-01T00:00:00+00:00", "ok")],
    })
    assert rc == 1, out
    assert "never advanced" in out, out


def test_in_band_200_success_is_green(tmp_path):
    rc, out, st = _run(tmp_path, {
        "post": [200, {"success": True, "job": JOB,
                       "results": {"fiber": {"status": "ok", "error": None}}}],
        "watermarks": [_wm(_PREV, "http_500")],
    })
    assert rc == 0, out
    assert st["wm"] == 1, "an in-band verdict must not poll"


def test_in_band_500_no_source_is_red_and_names_it(tmp_path):
    rc, out, _ = _run(tmp_path, {
        "post": [500, {"success": False, "job": JOB, "results": {"fiber": {
            "status": "no_source",
            "error": "PeeringDB /api/ix returned no coordinates"}}}],
        "watermarks": [_wm(_PREV, "http_500")],
    })
    assert rc == 1, out
    assert "no_source" in out, out


# ── 4. the retired cron does not page ───────────────────────────────────────

class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return ("cron_last_run",)          # to_regclass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cur(self._rows)

    def close(self):
        pass


def test_check_cron_freshness_skips_the_retired_job_only(monkeypatch):
    import routes.brain_consistency_radar as bcr
    rows = [  # (job_name, last_started_at, expected_interval_s, run_count, seconds_since)
        (JOB, None, None, 438, 10 * 86400),
        ("news-refresh", None, None, 400, 10 * 86400),   # control: same silence
    ]
    monkeypatch.setattr(bcr, "_db", lambda: _Conn(rows))
    found = {f["url"] for f in bcr.check_cron_freshness()
             if f["issue"] == "cron_silently_dead"}
    assert "/api/jobs/news-refresh" in found, (
        "control failed: an undeclared job 10 days silent must be flagged, or "
        "this test proves nothing about the retired one")
    assert f"/api/jobs/{JOB}" not in found


def test_retired_job_declares_no_interval_and_is_manual_on_purpose():
    import routes.jobs_routes as jr
    import routes.brain_consistency_radar as bcr
    assert JOB not in jr._JOB_INTERVALS, (
        "a declared cadence on a job nothing fires pages forever")
    assert f"/api/jobs/{JOB}" in bcr._CRON_INTENTIONAL_MANUAL
    sched = _read(os.path.join(ROOT, "dchub-scheduler.py"))
    endpoints = re.findall(r"['\"]endpoint['\"]\s*:\s*['\"]([^'\"]+)['\"]", sched)
    assert endpoints, "no endpoints parsed from dchub-scheduler.py"
    assert f"/api/jobs/{JOB}" not in endpoints, (
        "dchub-scheduler.py lists the retired endpoint again — "
        "check_cron_endpoint_unscheduled reads that roster as 'scheduled'")
