"""A post-deploy step must wait for the pushed COMMIT to be live, not a clock.

NO NETWORK beyond 127.0.0.1.

2026-09-11. #4385 merged as 9923d803d. sitemap-snapshot.yml slept 120s and
rebuilt the snapshot at 06:04:03Z; post-deploy-smoke.yml slept 90s and ran the
smoke 06:03:40-06:04:01Z. The origin first served the new code at 06:04:22Z.
Both runs were green against the build #4385 replaced.

Both steps now run scripts/wait_for_deployed_commit.py, which polls
/api/v1/admin/build-info until the origin RUNS github.sha. So do four lanes that
still waited on a clock the same day: link-check (sleep 90), dchub-qa (sleep
20), auto-rollback (sleep 240 — and it can ROLL BACK production) and
brain-pr-post-merge-guard (an uptime poll that never matched, then "probing
anyway"). Pinned here:

  * THE DECISION — an older, diverged or unnamed commit is never "arrived"; a
    descendant is; a stale read restarts the confirmation; a refused key fails
    on the first read, not at the deadline; the deadline really ends the wait.
  * THE FIXTURE IS THE REAL ROUTE. Every build-info answer below is produced by
    routes/build_info.py through Flask's test client, so a renamed field there
    fails here instead of turning every deploy wait into a ten-minute timeout.
  * THE REAL SCRIPT, real curl, against a local origin: exit codes, the headers
    that reach the server, the key kept out of argv.
  * THE WIRING, in all six workflows, down to the command line the script parses.
  * WHAT A FAILED WAIT LEAVES RUNNING, decided from each step's real `if:` the
    way the runner decides it: nothing reaches production, auto-rollback rolls
    nothing back and files a report instead, the brain guard grades nothing —
    and the cron/dispatch paths run exactly the steps they always did.
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
import tempfile
import threading
import time
from typing import Callable, NamedTuple, Optional

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


class Lane(NamedTuple):
    """A job that must not act on a deploy until that deploy is live."""
    job: str
    acts: Callable      # picks the one step that must wait for the deploy
    after_wait_s: int   # what the job may still spend after the wait, at its ceilings
    event: str = "push"
    wait_if: Optional[str] = "github.event_name == 'push'"
    expect: str = "${{ github.sha }}"


# workflow -> the job, the step that must not run until the deploy is live, and
# what the job does after the wait
ACTING = {
    # the rebuild (240) + read-back (60 + 90)
    "sitemap-snapshot.yml": Lane("rebuild", lambda s: "rebuild-snapshot" in _code(s), 390),
    # the smoke's share of its old 8-minute budget
    "post-deploy-smoke.yml": Lane("smoke", lambda s: s.get("id") == "smoke", 390),
    # the crawl: of 30 runs, 21 finished in 41-376s and 9 were killed at
    # 385-430s by the old 8-minute cap, so its tail is unmeasured past that
    "link-check.yml": Lane("link-check", lambda s: s.get("id") == "crawl", 900),
    # the checks' share of the old 5-minute budget (they took 17s)
    "dchub-qa.yml": Lane("qa-check", lambda s: s.get("id") == "qa", 280),
    # the old 25 minutes less the 240s sleep: 5 samples, a rollback polling up
    # to 420s for recovery, the revert branch, the issue
    "auto-rollback.yml": Lane("check-and-rollback", lambda s: s.get("id") == "probe", 1260),
    # the probe (~47s), a rollback (~510s), the revert branch, comment,
    # callback (30s) and beat (20s)
    "brain-pr-post-merge-guard.yml": Lane(
        "guard", lambda s: s.get("id") == "probe", 720, event="pull_request",
        wait_if=None, expect="${{ github.event.pull_request.merge_commit_sha }}"),
}

# Lanes whose later steps read the wait's outcome — a scan of them that finds
# no such reference is not looking.
NAMES_THE_WAIT = {"post-deploy-smoke.yml", "auto-rollback.yml", "brain-pr-post-merge-guard.yml"}


def _wiring(name):
    lane = ACTING[name]
    job = yaml.safe_load((WF / name).read_text(encoding="utf-8"))["jobs"][lane.job]
    steps = job["steps"]
    waits = [i for i, s in enumerate(steps) if "scripts/wait_for_deployed_commit.py" in _code(s)]
    acting = [i for i, s in enumerate(steps) if lane.acts(s)]
    assert len(waits) == 1, f"{name}: {len(waits)} deploy-wait steps, want exactly 1"
    assert len(acting) == 1, f"{name}: the step that acts on the deploy is gone or doubled"
    return job, steps, waits[0], acting[0]


@pytest.mark.parametrize("name", sorted(ACTING))
def test_the_push_path_waits_for_the_pushed_commit(name):
    """The wait runs where there is a deploy to wait for, for the commit that
    deploy carries: github.sha on a push; the merge commit when a merged PR
    closes — whose job runs for merged PRs only, so that commit is on main."""
    lane = ACTING[name]
    job, steps, i_wait, i_act = _wiring(name)
    wait = steps[i_wait]
    assert i_wait < i_act, f"{name}: the wait must come BEFORE the step that acts"
    assert wait.get("if") == lane.wait_if, (
        f"{name}: the wait runs on {wait.get('if')!r}, want {lane.wait_if!r} — only "
        f"a push (or a merge) has a deploy to wait for")
    if lane.wait_if is None:
        assert "github.event.pull_request.merged == true" in str(job.get("if")), (
            f"{name}: an unmerged PR's merge_commit_sha is a test merge that never deploys")
    env = wait.get("env") or {}
    assert env.get("EXPECT_SHA") == lane.expect
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
    after = ACTING[name].after_wait_s
    assert budget >= got["timeout"] + after, (
        f"{name}: timeout-minutes {budget // 60} cannot hold a {got['timeout']:g}s wait "
        f"plus the {after}s after it — GitHub would kill the job mid-wait, and a "
        f"killed job runs no failure() step")


@pytest.mark.parametrize("name", sorted(ACTING))
def test_nothing_before_the_action_waits_on_a_clock(name):
    """The `sleep 120` / `sleep 90` this replaced IS the bug: a clock cannot
    know when a deploy lands."""
    _, steps, _, i_act = _wiring(name)
    for s in steps[:i_act]:
        assert not re.search(r"\bsleep\b", _code(s)), (
            f"{name}: {s.get('name')!r} sleeps before acting on the deploy")


def _script(step):
    """A github-script step's JavaScript minus whole-line `//` comments."""
    js = str((step.get("with") or {}).get("script") or "")
    return "\n".join(l for l in js.splitlines() if not l.lstrip().startswith("//"))


def _reads_the_wait(step, wait_id):
    """The step knows how the wait ended: its `if:` tests the outcome, or an env
    var carries it AND the step's own code reads that var. A var nothing reads
    is decoration, and a mention in a comment is prose."""
    outcome = f"steps.{wait_id}.outcome"
    if outcome in str(step.get("if", "")):
        return True
    code = _code(step) + "\n" + _script(step)
    return any(
        str(v).replace(" ", "") == "${{" + outcome + "}}"
        and re.search(rf"\$\{{?{re.escape(k)}\b|process\.env\.{re.escape(k)}\b", code)
        for k, v in (step.get("env") or {}).items())


@pytest.mark.parametrize("name", sorted(ACTING))
def test_after_a_failed_wait_nothing_probes_the_old_build(name):
    """An `always()` or `!cancelled()` step runs even when the wait failed —
    against the build that was live before this commit, reported as this
    commit — unless it is told how the wait ended."""
    _, steps, i_wait, _ = _wiring(name)
    wait_id = steps[i_wait].get("id")
    checked = 0
    for s in steps[i_wait + 1:]:
        cond = str(s.get("if", ""))
        if re.search(r"always\(\)|!\s*cancelled\(\)", cond) and (s.get("run") or _script(s)):
            checked += 1
            assert wait_id and _reads_the_wait(s, wait_id), (
                f"{name}: {s.get('name')!r} still runs after a failed deploy wait ({cond!r})")
    if name in NAMES_THE_WAIT:
        assert checked, f"{name}: no always()/!cancelled() step seen — the scan is not looking"


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
    if name in NAMES_THE_WAIT:
        assert "deploy_wait" in refs, "the scan saw no reference to the wait — it is not looking"


def test_the_smoke_issue_says_the_wait_failed():
    """A failed wait files the same issue as a failed smoke, with no endpoint
    list. Without this line it reads as an empty report."""
    _, steps, _, _ = _wiring("post-deploy-smoke.yml")
    issue = next(s for s in steps if "gh issue create" in _code(s))
    assert (issue.get("env") or {}).get("WAIT_OUTCOME") == "${{ steps.deploy_wait.outcome }}"
    assert '"${WAIT_OUTCOME:-}" = "failure"' in _code(issue)


# ── what a failed wait leaves running ────────────────────────────────────────
# Whether a step runs is its `if:`, evaluated by rules a reader easily gets
# wrong: no status function means `success() && ...`, and a skipped step's
# outcome is 'skipped', not empty. So these tests do not read conditions, they
# DECIDE them — for the operators these workflows use, raising on anything else
# rather than guessing.

_TOKEN = re.compile(
    r"(?P<str>'(?:[^']|'')*')|(?P<op>==|!=|&&|\|\||!|\(|\))"
    r"|(?P<fn>(?:always|success|failure|cancelled)\(\))"
    r"|(?P<lit>true|false|null)\b|(?P<ref>[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)+)")
_STATUS_FN = re.compile(r"\b(?:always|success|failure|cancelled)\(\)")


def _decide(cond, ref, failed):
    """Would a step with this `if:` run, given whether an earlier step failed
    the job? `ref` resolves context names."""
    expr = str(cond).strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    py, pos, after_not = [], 0, False
    while pos < len(expr):
        if expr[pos].isspace():
            pos += 1
            continue
        m = _TOKEN.match(expr, pos)
        if not m:
            raise ValueError(f"cannot evaluate {cond!r} at {expr[pos:]!r}")
        pos = m.end()
        if after_not and not (m["fn"] or m["op"] == "("):
            raise ValueError(f"{cond!r}: `!` before a comparison — Python's `not` binds looser")
        after_not = m["op"] == "!"
        if m["str"]:
            py.append(repr(m["str"][1:-1].replace("''", "'")))
        elif m["op"]:
            py.append({"&&": " and ", "||": " or ", "!": " not "}.get(m["op"], m["op"]))
        elif m["fn"]:
            py.append(f"_{m['fn'][:-2]}()")
        elif m["lit"]:
            py.append({"true": "True", "false": "False", "null": "None"}[m["lit"]])
        else:
            py.append(f"_ref({m['ref']!r})")
    code = "".join(py)
    if not _STATUS_FN.search(expr):
        code = f"_success() and ({code})"
    return bool(eval(code, {"__builtins__": {}}, {
        "_ref": ref, "_always": lambda: True, "_cancelled": lambda: False,
        "_success": lambda: not failed, "_failure": lambda: failed}))


def _label(step):
    return step.get("id") or step.get("name") or step.get("uses")


def _simulate(steps, event, *, ends=None, outputs=None, inputs=None):
    """The labels of the steps a job runs, in order. A step that runs ends
    'success' unless `ends` says otherwise and sets the `outputs` given for it;
    a failure fails the job unless the step is continue-on-error."""
    ends, outputs, inputs = ends or {}, outputs or {}, inputs or {}
    ran, done, failed = [], {}, False

    def ref(name):
        head, *rest = name.split(".")
        if name == "github.event_name":
            return event
        if head == "inputs" and len(rest) == 1:
            return inputs.get(rest[0])
        if head == "steps" and len(rest) >= 2 and rest[0] in done:
            if rest[1:] == ["outcome"]:
                return done[rest[0]]["outcome"]
            if len(rest) == 3 and rest[1] == "outputs":
                return done[rest[0]]["outputs"].get(rest[2], "")
        raise ValueError(f"cannot resolve {name!r}: a typo, or a step that has not run yet")

    for s in steps:
        label, coe = _label(s), s.get("continue-on-error", False)
        assert isinstance(coe, bool), f"{label}: continue-on-error is an expression"
        runs = _decide(s.get("if", "success()"), ref, failed)
        if runs:
            ran.append(label)
            failed = failed or (ends.get(label, "success") == "failure" and not coe)
        if s.get("id"):
            done[s["id"]] = ({"outcome": ends.get(label, "success"), "outputs": outputs.get(label, {})}
                             if runs else {"outcome": "skipped", "outputs": {}})
    return ran


_PROD_URL = re.compile(r"https?://[^\s\"'<>]*?(?:dchub\.cloud|railway\.app)\b")


def _reaches_production(step):
    """Does the step itself address production? Its code minus comments, its
    github-script, and its `with:`/env values — wherever a URL can sit."""
    text = "\n".join([_code(step), _script(step),
                      *(str(v) for k, v in (step.get("with") or {}).items() if k != "script"),
                      *(str(v) for v in (step.get("env") or {}).values())])
    return bool(_PROD_URL.search(text))


@pytest.mark.parametrize("name", sorted(ACTING))
def test_a_failed_wait_runs_nothing_that_reaches_production(name):
    """After a failed wait the acting step is skipped, and nothing that still
    runs addresses production: it would be judging the build this commit was
    meant to replace, under this commit's name."""
    lane = ACTING[name]
    _, steps, i_wait, i_act = _wiring(name)
    wait, act = _label(steps[i_wait]), _label(steps[i_act])
    assert _reaches_production(steps[i_act]), f"{name}: the URL detector cannot see {act!r}"
    landed = _simulate(steps, lane.event)
    assert wait in landed and act in landed, f"{name}: control — a landed deploy runs {act!r}: {landed}"
    ran = _simulate(steps, lane.event, ends={wait: "failure"})
    assert act not in ran, f"{name}: {act!r} runs after a failed wait: {ran}"
    reach = [_label(s) for s in steps[i_wait + 1:] if _label(s) in ran and _reaches_production(s)]
    assert not reach, f"{name}: after a failed wait these still address production: {reach}"


# ── auto-rollback: a failed wait is loud, and rolls nothing back ─────────────

_RB_REPORT = "Report that the post-deploy check did not run"
_RB_FAIL = "Fail if remediation did not complete"
_BURN = {"probe": {"hard_burn_count": "4"}, "decide": {"rollback": "true"}}


def test_a_deploy_that_never_lands_is_reported_and_nothing_is_rolled_back():
    """★ The two things a failed wait must not do, pinned together. Go silent:
    the report runs (and the job is already red). Roll back: a timed-out wait
    puts the decision past the anti-stacking guard's 600s window, so a rollback
    here would remove the build that was live BEFORE this commit. And "Open
    issue" stays out — it would call the missing verdict ROLLBACK FAILED.
    The burn outputs are loaded on purpose: a probe or decide that DID run
    would hand the rollback step a measured hard_burn."""
    steps = _wiring("auto-rollback.yml")[1]
    ran = _simulate(steps, "push", ends={"deploy_wait": "failure"}, outputs=_BURN)
    assert ran == ["actions/checkout@v5", "deploy_wait", _RB_REPORT], ran


def test_a_burn_on_the_landed_commit_still_rolls_back_and_alerts():
    steps = _wiring("auto-rollback.yml")[1]
    ran = _simulate(steps, "push", ends={"railway": "failure", "revertpr": "failure"}, outputs={
        **_BURN, "railway": {"outcome": "failed-rc2"}, "revertpr": {"outcome": "branch-pushed-no-pr"}})
    assert ran == ["actions/checkout@v5", "deploy_wait", "probe", "decide", "railway",
                   "revertpr", "Open issue", _RB_FAIL], ran


@pytest.mark.parametrize("event,inputs,scenario,expected", [
    ("schedule", {}, "healthy", ["probe", "decide"]),
    ("schedule", {}, "burn", ["probe", "decide", "railway", "revertpr", "Open issue"]),
    ("schedule", {}, "probe-crashed", ["probe", "Open issue"]),
    ("workflow_dispatch", {"drill": True, "force_rollback": False}, "burn",
     ["probe", "decide", "railway", "Open issue"]),
], ids=["cron-healthy", "cron-burn-rolled-back", "cron-probe-crashed", "dispatch-drill"])
def test_the_cron_and_dispatch_lanes_run_the_steps_they_always_did(event, inputs, scenario, expected):
    """Off the push path the wait and its report are skipped, and every other
    step runs exactly when it did before — including the alert on a crashed
    probe, which is what its `!cancelled()` is for."""
    outputs, ends = {}, {}
    if scenario == "healthy":
        outputs = {"probe": {"hard_burn_count": "0"}, "decide": {"rollback": "false"}}
    elif scenario == "burn":
        outputs = {**_BURN, "railway": {"outcome": "rolled-back"}, "revertpr": {"outcome": "opened"}}
    else:
        ends = {"probe": "failure"}
    ran = _simulate(_wiring("auto-rollback.yml")[1], event, ends=ends, outputs=outputs, inputs=inputs)
    assert ran == ["actions/checkout@v5", *expected], ran


def _step(name, label):
    hits = [s for s in _wiring(name)[1] if _label(s) == label]
    assert len(hits) == 1, f"{name}: {len(hits)} steps labelled {label!r}"
    return hits[0]


def _report_calls(existing):
    """Run the report step's real shell against a stub `gh`. -> the log of gh
    calls, each --body-file's content inlined after its call."""
    step = _step("auto-rollback.yml", _RB_REPORT)
    assert "${{" not in step["run"], "the report's shell must take its values from env"
    with tempfile.TemporaryDirectory() as d:
        bin_dir, log = pathlib.Path(d, "bin"), pathlib.Path(d, "gh.log")
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(
            '#!/usr/bin/env bash\n'
            'printf "CALL %s\\n" "$*" >> "$GH_LOG"\n'
            'prev=""; for a in "$@"; do\n'
            '  if [ "$prev" = "--body-file" ]; then cat "$a" >> "$GH_LOG"; fi; prev="$a"\n'
            'done\n'
            'case "$1 $2" in\n'
            '  "issue list") printf "%s\\n" "$GH_EXISTING" ;;\n'
            '  "issue create") echo "https://github.com/o/r/issues/99" ;;\n'
            'esac\n')
        (bin_dir / "gh").chmod(0o755)
        env = {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}", "GH_LOG": str(log),
               "GH_EXISTING": existing, "RUNNER_TEMP": d, "GITHUB_SHA": EXPECT, "GH_TOKEN": "t",
               "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "o/r",
               "GITHUB_RUN_ID": "77"}
        proc = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=d, capture_output=True,
                              text=True, timeout=30, env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return log.read_text()


def test_the_failed_wait_report_opens_an_issue_saying_what_did_not_run():
    log = _report_calls(existing="null")
    calls = [l for l in log.splitlines() if l.startswith("CALL ")]
    assert [c.split()[1:3] for c in calls] == [
        ["issue", "list"], ["issue", "create"], ["issue", "edit"]], calls
    assert "--title slo-gate: post-deploy SLO check did not run" in calls[1]
    assert "--add-label slo-gate" in calls[2]
    assert f"`{EXPECT[:9]}`" in log
    assert "no SLO samples" in log and "rolled **nothing** back" in log


def test_the_failed_wait_report_bumps_the_open_issue_instead_of_opening_another():
    """A refused admin key fails every push's wait; one issue, bumped."""
    calls = [l for l in _report_calls(existing="42").splitlines() if l.startswith("CALL ")]
    assert [c.split()[1:4] for c in calls] == [
        ["issue", "list", "--state"], ["issue", "comment", "42"]], calls


# ── brain-pr-post-merge-guard: a merge nobody measured is not graded ─────────

_BG = "brain-pr-post-merge-guard.yml"
_BG_COMMENT = "Comment outcome on the brain PR"
_BG_MARK = "Mark proposal outcome in brain DB"
_BG_BEAT = "Beat the gate liveness ledger"
_BG_START = ["Checkout main", "Configure git", "deploy_wait"]


def test_a_merge_that_never_lands_is_not_graded():
    """No probe, rollback or revert, and no merged_reverted POSTed to the brain
    DB — only the comment that says so, and the beat."""
    ran = _simulate(_wiring(_BG)[1], "pull_request", ends={"deploy_wait": "failure"})
    assert ran == [*_BG_START, _BG_COMMENT, _BG_BEAT], ran


@pytest.mark.parametrize("verdict,expected", [
    ("healthy", ["probe", _BG_COMMENT, _BG_MARK, _BG_BEAT]),
    ("broken", ["probe", "revert", "revertpr", _BG_COMMENT, _BG_MARK, _BG_BEAT])])
def test_a_landed_merge_is_still_graded(verdict, expected):
    ran = _simulate(_wiring(_BG)[1], "pull_request", outputs={"probe": {"verdict": verdict}},
                    ends={"revert": "failure"} if verdict == "broken" else {})
    assert ran == [*_BG_START, *expected], ran


def _beat_verdict(job_status, wait_outcome):
    """Run the beat's real shell with scripts/gate_beat.sh stubbed to record
    the verdict it is handed."""
    step = _step(_BG, _BG_BEAT)
    assert "${{" not in step["run"], "the beat's shell must take its values from env"
    with tempfile.TemporaryDirectory() as d:
        stub, out = pathlib.Path(d, "scripts", "gate_beat.sh"), pathlib.Path(d, "verdict")
        stub.parent.mkdir()
        stub.write_text('printf "%s" "$2" > "$BEAT_OUT"\n')
        proc = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=d, capture_output=True,
                              text=True, timeout=30, env={
                                  "PATH": os.environ.get("PATH", ""), "BEAT_OUT": str(out),
                                  "JOB_STATUS": job_status, "WAIT_OUTCOME": wait_outcome,
                                  "DCHUB_ADMIN_KEY": "k"})
        assert proc.returncode == 0, proc.stderr
        return out.read_text()


@pytest.mark.parametrize("job_status,wait_outcome,verdict", [
    ("success", "success", "pass"),
    ("failure", "success", "fail"),
    ("failure", "failure", "unmeasured"),
    ("failure", "skipped", "unmeasured"),
    ("cancelled", "success", "unmeasured")])
def test_the_beat_never_calls_an_unmeasured_merge_a_refusal(job_status, wait_outcome, verdict):
    """`fail` on the gate ledger means this gate REFUSED something. A merge that
    never went live was never probed, so it is unmeasured."""
    assert _beat_verdict(job_status, wait_outcome) == verdict


_NODE = shutil.which("node")
_BG_OUTPUTS = ["probe.verdict", "probe.status", "probe.dcpi_code", "probe.findings_code",
               "revert.reverted", "revert.detail", "revertpr.url"]


def _brain_comment(wait_outcome, outputs=None):
    """Run the comment step's real script under node, with the step outputs
    Actions renders into it (an expression not listed here raises), and return
    the one body it posts."""
    step, given = _step(_BG, _BG_COMMENT), outputs or {}
    values = {f"steps.{o.split('.')[0]}.outputs.{o.split('.')[1]}": given.get(o, "")
              for o in _BG_OUTPUTS}
    script = re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: values[m.group(1)], step["with"]["script"])
    js = ("const posted = [];\n"
          "const github = {rest: {issues: {createComment: async (a) => { posted.push(a); }}}};\n"
          "const context = {repo: {owner: 'o', repo: 'r'}, payload: {pull_request: {number: 4242}}};\n"
          "(async () => {\n" + script + "\n})().then("
          "() => process.stdout.write(JSON.stringify(posted)),"
          " (e) => { console.error(e); process.exit(1); });\n")
    env = {"PATH": os.environ.get("PATH", ""), "GITHUB_SERVER_URL": "https://github.com",
           "GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "77", "MERGE_SHA": EXPECT,
           "WAIT_OUTCOME": wait_outcome}
    proc = subprocess.run([_NODE, "-e", js], capture_output=True, text=True, encoding="utf-8",
                          timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    posted = json.loads(proc.stdout)
    assert len(posted) == 1 and posted[0]["issue_number"] == 4242, posted
    return posted[0]["body"]


@pytest.mark.skipif(_NODE is None, reason="node not available on this host")
@pytest.mark.parametrize("wait_outcome", ["failure", "skipped"])
def test_the_brain_pr_hears_that_the_check_did_not_run(wait_outcome):
    """★ Without its own branch this comment read the empty verdict as a FAILED
    check whose rollback "did not complete" — blaming the brain's merge for a
    deploy that never landed."""
    body = _brain_comment(wait_outcome)
    assert "Post-merge health check did not run" in body.splitlines()[0], body
    assert EXPECT[:9] in body and "no outcome was recorded" in body
    for claim in ("FAILED", "PASSED", "rolled back to", "production may still be broken"):
        assert claim not in body, f"a check that never ran claims {claim!r}: {body}"


@pytest.mark.skipif(_NODE is None, reason="node not available on this host")
def test_a_measured_merge_still_gets_its_verdict_comment():
    """Control: the branch above keys on the wait, not on an empty verdict."""
    assert "PASSED" in _brain_comment("success", {"probe.verdict": "healthy", "probe.status": "ok"})
    broken = _brain_comment("success", {"probe.verdict": "broken", "revert.reverted": "true"})
    assert "FAILED" in broken and "rolled back to the previous deployment" in broken
