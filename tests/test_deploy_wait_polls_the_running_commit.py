"""A post-deploy step must wait for the pushed COMMIT to be live, not a clock.

NO NETWORK beyond 127.0.0.1.

2026-09-11. #4385 merged as 9923d803d. sitemap-snapshot.yml slept 120s and
rebuilt the snapshot at 06:04:03Z; post-deploy-smoke.yml slept 90s and ran the
smoke 06:03:40-06:04:01Z. The origin first served the new code at 06:04:22Z.
Both runs were green against the build #4385 replaced.

Both steps now run scripts/wait_for_deployed_commit.py, which polls
/api/v1/admin/build-info until the origin RUNS github.sha. Pinned here:

  * THE DECISION — an older, diverged or unnamed commit is never "arrived"; a
    descendant is; a stale read restarts the confirmation; a refused key fails
    on the first read, not at the deadline; the deadline really ends the wait.
  * THE FIXTURE IS THE REAL ROUTE. Every build-info answer below is produced by
    routes/build_info.py through Flask's test client, so a renamed field there
    fails here instead of turning every deploy wait into a ten-minute timeout.
  * THE REAL SCRIPT, real curl, against a local origin: exit codes, the headers
    that reach the server, the key kept out of argv.
  * THE WIRING, in both workflows, down to the command line the script parses.
"""
from __future__ import annotations

import contextlib
import http.server
import importlib.util
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "wait_for_deployed_commit.py"
WF = ROOT / ".github" / "workflows"

EXPECT = "9923d803d063394b8f24fa61babcf95c39c9e9a8"    # #4385, the incident commit
BEFORE = "31f1d12da" + "a" * 31                         # the build it replaced
AFTER = "c603ea5c2" + "b" * 31                          # a later push


@pytest.fixture(scope="module")
def w():
    spec = importlib.util.spec_from_file_location("wait_for_deployed_commit", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def build_info(monkeypatch):
    """(status, body) exactly as routes/build_info.py serves them."""
    from flask import Flask
    from routes import build_info as bi

    app = Flask(__name__)
    app.register_blueprint(bi.build_info_bp)
    client = app.test_client()

    def answer(sha, *, var="RAILWAY_GIT_COMMIT_SHA", key="k-good", disabled=False):
        for v in bi._SHA_VARS:
            monkeypatch.delenv(v, raising=False)
        if sha is not None:
            monkeypatch.setenv(var, sha)
        monkeypatch.setenv("DCHUB_ADMIN_KEY", "k-good")
        if disabled:
            monkeypatch.setenv("BUILD_INFO_DISABLE", "1")
        else:
            monkeypatch.delenv("BUILD_INFO_DISABLE", raising=False)
        r = client.get("/api/v1/admin/build-info", headers={"X-Admin-Key": key})
        return r.status_code, r.get_data(as_text=True)

    return answer


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def _run(w, answers, verdicts=None, *, timeout=600):
    """Drive wait_for_commit over scripted build-info answers (the last one
    repeats forever) with the production cadence and a fake clock."""
    clock, reads, asked = _Clock(), [], []

    def read():
        reads.append(answers[min(len(reads), len(answers) - 1)])
        return w.classify(*reads[-1])

    def contains(expect, running):
        asked.append(running)
        return (verdicts or {}).get(running)

    outcome, summary = w.wait_for_commit(
        EXPECT, read, contains, timeout=timeout, interval=15, confirm=3,
        confirm_interval=5, clock=clock.now, sleep=clock.sleep, log=lambda m: None)
    return outcome, summary, len(reads), clock, asked


# ── the decision ─────────────────────────────────────────────────────────────

def test_it_waits_out_the_old_build_and_goes_on_the_new_one(w, build_info):
    old, new = build_info(BEFORE), build_info(EXPECT)
    outcome, summary, reads, clock, asked = _run(w, [old, old, old, new], {BEFORE: False})
    assert outcome == "arrived", summary
    assert reads == 6                              # 3 stale, then 3 confirming
    assert clock.sleeps == [15, 15, 15, 5, 5]
    assert asked == [BEFORE], "ancestry is asked once per commit; an exact match asks nothing"


def test_a_deploy_that_never_lands_fails_at_the_deadline(w, build_info):
    outcome, summary, reads, clock, _ = _run(w, [build_info(BEFORE)], {BEFORE: False})
    assert outcome == "timeout"
    assert 585 <= clock.t <= 600, f"waited {clock.t}s against a 600s budget"
    assert reads == 41
    assert BEFORE[:9] in summary and "does not contain" in summary


@pytest.mark.parametrize("unreadable", ["refused_key", "disabled"])
def test_a_signal_this_step_cannot_read_fails_on_the_first_read(w, build_info, unreadable):
    """★ Polling to the deadline would report a dead credential as a slow
    deploy, ten minutes late — a wait branch swallowing the fault it should name."""
    answer = (build_info(EXPECT, key="k-rotated") if unreadable == "refused_key"
              else build_info(EXPECT, disabled=True))
    assert answer[0] in (403, 404)
    outcome, summary, reads, clock, _ = _run(w, [answer])
    assert outcome == "fatal", summary
    assert reads == 1 and clock.sleeps == []


@pytest.mark.parametrize("verdict,expected", [
    (True, "arrived"), (False, "timeout"), (None, "timeout")],
    ids=["descendant", "older-or-diverged", "github-did-not-answer"])
def test_only_a_commit_that_contains_the_push_counts(w, build_info, verdict, expected):
    """Railway deploys every push, so a later push can go live before this one
    ever does and build-info never names EXPECT. Code that CONTAINS it is what
    the next step needs; an older build, a rollback or an unanswered compare is
    not."""
    outcome, summary, *_ = _run(w, [build_info(AFTER)], {AFTER: verdict})
    assert outcome == expected, summary


def test_one_stale_read_restarts_the_confirmation(w, build_info):
    """Two replicas: new, then old, means the swap is still in flight."""
    new, old = build_info(EXPECT), build_info(BEFORE)
    outcome, summary, reads, clock, _ = _run(w, [new, old, new, new, new], {BEFORE: False})
    assert outcome == "arrived", summary
    assert reads == 5
    assert clock.sleeps == [5, 15, 5, 5]


@pytest.mark.parametrize("fallback", ["deployment_id", "nothing_set"])
def test_an_answer_naming_no_commit_is_never_arrived(w, build_info, fallback):
    """build_info falls back to RAILWAY_DEPLOYMENT_ID (a UUID) and then None.
    The route answers 200 either way; unanswerable is not current."""
    answer = (build_info("97a7304d-052f-48e3-8bb5-ef74574d2b4d", var="RAILWAY_DEPLOYMENT_ID")
              if fallback == "deployment_id" else build_info(None))
    assert answer[0] == 200
    assert w.classify(*answer)[0] == "wait"
    outcome, summary, *_ = _run(w, [answer], timeout=60)
    assert outcome == "timeout", summary


def test_a_swap_in_flight_is_waited_through(w, build_info):
    answers = [(None, "no response (curl exit 28: timed out)"),
               (502, "Application failed to respond"), build_info(EXPECT)]
    outcome, summary, reads, clock, _ = _run(w, answers)
    assert outcome == "arrived", summary
    assert clock.sleeps == [15, 15, 5, 5]


def test_the_admin_key_goes_to_curl_on_stdin_not_argv(w, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["argv"], seen["stdin"] = cmd, kw.get("input") or ""
        return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": true}\n200', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert w.curl_get("http://127.0.0.1:9/x", {"X-Admin-Key": "sekret-value"}, 10) == (
        200, '{"ok": true}')
    assert "sekret-value" not in " ".join(seen["argv"])
    assert 'header = "X-Admin-Key: sekret-value"' in seen["stdin"]
    assert seen["argv"][:2] == ["curl", "-q"], "-q must come first or ~/.curlrc still applies"


# ── the real script, real curl, a local origin ──────────────────────────────

@contextlib.contextmanager
def _origin(build_info_answers, compare_status="behind"):
    """Stands in for the origin AND api.github.com. The last answer repeats."""
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append({"path": self.path,
                         "key": self.headers.get("X-Admin-Key"),
                         "auth": self.headers.get("Authorization")})
            if self.path.startswith("/api/v1/admin/build-info"):
                n = sum(1 for h in hits if h["path"].startswith("/api/v1/admin/build-info"))
                status, body = build_info_answers[min(n, len(build_info_answers)) - 1]
            elif "/compare/" in self.path:
                status, body = 200, json.dumps({"status": compare_status})
            else:
                status, body = 404, "{}"
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", hits
    finally:
        srv.shutdown()
        srv.server_close()


def _cli(url, *, key="k-good", timeout="20"):
    assert shutil.which("curl"), "curl is required: the script's HTTP goes through it"
    env = {"PATH": os.environ.get("PATH", ""), "DCHUB_ADMIN_KEY": key, "GH_TOKEN": "t-test"}
    cmd = [sys.executable, str(SCRIPT), "--expect", EXPECT, "--base", url,
           "--github-api", url, "--repo", "o/r", "--timeout", timeout,
           "--interval", "0.05", "--confirm", "2", "--confirm-interval", "0.05"]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    return proc, time.monotonic() - t0


def test_cli_goes_once_the_origin_runs_the_commit(build_info):
    with _origin([build_info(BEFORE), build_info(EXPECT)]) as (url, hits):
        proc, _ = _cli(url)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::notice title=deploy landed::" in proc.stdout
    info = [h for h in hits if h["path"].startswith("/api/v1/admin/build-info")]
    assert len(info) == 3, "1 stale read + 2 confirming"
    assert {h["key"] for h in info} == {"k-good"}
    compares = [h for h in hits if "/compare/" in h["path"]]
    assert [h["path"].split("?")[0] for h in compares] == [
        f"/repos/o/r/compare/{EXPECT}...{BEFORE}"]
    assert compares[0]["auth"] == "Bearer t-test"


def test_cli_fails_loudly_when_the_commit_never_lands(build_info):
    with _origin([build_info(BEFORE)]) as (url, _):
        proc, _ = _cli(url, timeout="0.6")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "::error title=deploy never landed::" in proc.stdout
    assert BEFORE[:9] in proc.stdout


def test_cli_fails_on_the_first_read_when_the_key_is_refused(build_info):
    with _origin([build_info(EXPECT, key="k-rotated")]) as (url, hits):
        proc, took = _cli(url, timeout="30")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "refused DCHUB_ADMIN_KEY" in proc.stdout
    assert len(hits) == 1 and took < 15, f"{len(hits)} reads over {took:.1f}s"


def test_cli_without_a_key_reads_nothing():
    with _origin([(200, "{}")]) as (url, hits):
        proc, _ = _cli(url, key="")
    assert proc.returncode == 1 and "DCHUB_ADMIN_KEY is empty" in proc.stdout
    assert hits == []


# ── the wiring ───────────────────────────────────────────────────────────────

def _code(step):
    """A step's shell minus whole-line comments: several guards in this repo
    have passed by matching the prose that explained a bug."""
    return "\n".join(l for l in (step.get("run") or "").splitlines()
                     if not l.lstrip().startswith("#"))


# workflow -> (job, the step that must not run until the deploy is live)
ACTING = {
    "sitemap-snapshot.yml": ("rebuild", lambda s: "rebuild-snapshot" in _code(s)),
    "post-deploy-smoke.yml": ("smoke", lambda s: s.get("id") == "smoke"),
}

# What each job does after the wait, at its curl ceilings: sitemap's rebuild
# (240) + read-back (60 + 90); the smoke's share of its old 8-minute budget.
AFTER_WAIT_S = 390


def _wiring(name):
    job_name, acts = ACTING[name]
    job = yaml.safe_load((WF / name).read_text(encoding="utf-8"))["jobs"][job_name]
    steps = job["steps"]
    waits = [i for i, s in enumerate(steps) if "scripts/wait_for_deployed_commit.py" in _code(s)]
    acting = [i for i, s in enumerate(steps) if acts(s)]
    assert len(waits) == 1, f"{name}: {len(waits)} deploy-wait steps, want exactly 1"
    assert len(acting) == 1, f"{name}: the step that acts on the deploy is gone or doubled"
    return job, steps, waits[0], acting[0]


@pytest.mark.parametrize("name", sorted(ACTING))
def test_the_push_path_waits_for_the_pushed_commit(name):
    _, steps, i_wait, i_act = _wiring(name)
    wait = steps[i_wait]
    assert i_wait < i_act, f"{name}: the wait must come BEFORE the step that acts"
    assert wait.get("if") == "github.event_name == 'push'", (
        f"{name}: only a push has a deploy to wait for")
    env = wait.get("env") or {}
    assert env.get("EXPECT_SHA") == "${{ github.sha }}"
    assert env.get("DCHUB_ADMIN_KEY") == "${{ secrets.DCHUB_ADMIN_KEY }}"
    assert env.get("GH_TOKEN") == "${{ secrets.GITHUB_TOKEN }}"
    assert "${{" not in _code(wait), f"{name}: expressions go through env, never inline in run:"
    assert not wait.get("continue-on-error"), f"{name}: a wait allowed to fail guards nothing"
    checkouts = [s for s in steps[:i_wait]
                 if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert checkouts and checkouts[-1].get("if") in (None, "github.event_name == 'push'"), (
        f"{name}: the script comes from the checkout, and none runs before the wait")


@pytest.mark.parametrize("name", sorted(ACTING))
def test_the_command_line_is_one_the_script_accepts(name, w, monkeypatch):
    """Parse the workflow's own command with the script's real argparse — a
    typo'd flag would otherwise first fail in production, after a merge."""
    job, steps, i_wait, _ = _wiring(name)
    lines = [l for l in _code(steps[i_wait]).splitlines() if "wait_for_deployed_commit.py" in l]
    assert len(lines) == 1
    argv = shlex.split(lines[0])
    assert argv[:2] == ["python3", "scripts/wait_for_deployed_commit.py"]
    got = {}

    def fake_wait(expect, read, contains, **kw):
        got.update(kw, expect=expect)
        return "arrived", "ok"

    monkeypatch.setattr(w, "wait_for_commit", fake_wait)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "k")
    assert w.main([a.replace("$EXPECT_SHA", EXPECT) for a in argv[2:]]) == 0
    assert got["expect"] == EXPECT
    budget = int(job["timeout-minutes"]) * 60
    assert budget >= got["timeout"] + AFTER_WAIT_S, (
        f"{name}: timeout-minutes {budget // 60} cannot hold a {got['timeout']:g}s wait "
        f"plus the {AFTER_WAIT_S}s after it — GitHub would kill the job mid-wait, and a "
        f"killed job runs no failure() step")


@pytest.mark.parametrize("name", sorted(ACTING))
def test_nothing_before_the_action_waits_on_a_clock(name):
    """The `sleep 120` / `sleep 90` this replaced IS the bug: a clock cannot
    know when a deploy lands."""
    _, steps, _, i_act = _wiring(name)
    for s in steps[:i_act]:
        assert not re.search(r"\bsleep\b", _code(s)), (
            f"{name}: {s.get('name')!r} sleeps before acting on the deploy")


@pytest.mark.parametrize("name", sorted(ACTING))
def test_after_a_failed_wait_nothing_probes_the_old_build(name):
    """An `always()` step runs even when the wait failed — against the build
    that was live before this commit, reported as this commit."""
    _, steps, i_wait, _ = _wiring(name)
    wait_id = steps[i_wait].get("id")
    for s in steps[i_wait + 1:]:
        cond = str(s.get("if", ""))
        if "always()" in cond and s.get("run"):
            assert wait_id and f"steps.{wait_id}.outcome" in cond, (
                f"{name}: {s.get('name')!r} still runs after a failed deploy wait ({cond!r})")


@pytest.mark.parametrize("name", sorted(ACTING))
def test_every_step_id_the_workflow_names_exists(name):
    """★ A misspelled `steps.<id>` is not an error in Actions: it evaluates to
    empty, so `steps.deploy_wait.outcome != 'failure'` reads TRUE and the guard
    above silently stops guarding."""
    job, steps, _, _ = _wiring(name)
    ids = {s["id"] for s in steps if s.get("id")}
    text = "\n".join(l for l in (WF / name).read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))
    refs = set(re.findall(r"\bsteps\.([A-Za-z_][\w-]*)\.", text))
    assert refs <= ids, f"{name}: names step ids that do not exist: {sorted(refs - ids)}"
    if name == "post-deploy-smoke.yml":
        assert "deploy_wait" in refs, "the scan saw no reference to the wait — it is not looking"


def test_the_smoke_issue_says_the_wait_failed():
    """A failed wait files the same issue as a failed smoke, with no endpoint
    list. Without this line it reads as an empty report."""
    _, steps, _, _ = _wiring("post-deploy-smoke.yml")
    issue = next(s for s in steps if "gh issue create" in _code(s))
    assert (issue.get("env") or {}).get("WAIT_OUTCOME") == "${{ steps.deploy_wait.outcome }}"
    assert '"${WAIT_OUTCOME:-}" = "failure"' in _code(issue)
