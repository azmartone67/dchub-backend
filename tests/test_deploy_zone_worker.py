"""scripts/deploy_zone_worker.py + .github/workflows/deploy-zone-worker.yml.

NO NETWORK. Cloudflare and the two probed hosts are one fake (FakeNet) that
answers in the framing measured from the real API on 2026-09-23:

    GET .../scripts/dchubapiproxy/content/v2
      content-type: multipart/form-data; boundary=<hex>
      cf-entrypoint: worker.js
      --<hex>\\r\\n
      Content-Disposition: form-data; name="worker.js"; filename="worker.js"\\r\\n
      Content-Type: application/javascript+module\\r\\n\\r\\n
      <file bytes>\\r\\n--<hex>--\\r\\n

and decodes uploads with the stdlib `email` parser, not the script's own
multipart code — so an upload the script builds wrong is caught by a parser
that did not come from the same author.

What these pin (the three the change was asked for, first):
  * live AHEAD of the repo  -> refused, and NO write request is ever made
  * never wrangler          -> not in any workflow step, and at runtime the
                               only process the deploy spawns is `git`
  * header after deploy     -> success needs X-DC-Worker-Version == repo on
                               BOTH hosts, read AFTER the write; a stuck host
                               fails the run
"""
from __future__ import annotations

import email.parser
import email.policy
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.parse

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import deploy_zone_worker as D  # noqa: E402

WF_PATH = ROOT / ".github" / "workflows" / "deploy-zone-worker.yml"
BASE = "https://api.cloudflare.com/client/v4/accounts/ACCT/workers/scripts/dchubapiproxy"
LIVE_BINDINGS = [{"type": "secret_text", "name": n} for n in (
    "ADMIN_SECRET", "CANARY_SECRET", "DCHUB_EDGE_KEY", "HUBSPOT_API_KEY",
    "STRIPE_WEBHOOK_SECRET")]


def worker(version: str, body: str = "export default {};\n") -> bytes:
    return f"\n/** zone worker */\nconst WORKER_VERSION = '{version}';\n{body}".encode()


REPO = worker("4.9.74-infra-projects", "export default { fetch() {} };\n")
LIVE = worker("4.9.73-kv-writes-only-credential-free")


class FakeNet:
    """Cloudflare's script API plus the two probed hosts, in one transport."""

    def __init__(self, live: bytes = LIVE, *, stuck_hosts=(), put_status=200,
                 read_status=200, drop_binding_on_put=None, entrypoint="worker.js"):
        self.live = live
        self.settings = {"compatibility_date": "2024-11-01", "compatibility_flags": [],
                         "bindings": [dict(b) for b in LIVE_BINDINGS],
                         "logpush": False, "tail_consumers": []}
        self.schedules = [{"cron": "0 */6 * * *"}]
        self.stuck_hosts = set(stuck_hosts)
        self.put_status, self.read_status = put_status, read_status
        self.drop_binding_on_put = drop_binding_on_put
        self.entrypoint = entrypoint
        self.calls: list[tuple[str, str]] = []
        self.uploads: list[dict] = []
        self.served_version = D.extract_version(live)

    # the Http interface
    def request(self, method, url, headers=None, body=None, timeout=30):
        self.calls.append((method, url))
        if url.startswith(BASE):
            return self._cf(method, url[len(BASE):], headers or {}, body)
        for host in D.PROBE_URLS:
            if url.startswith(host + "?_="):
                v = self._stuck_version if host in self.stuck_hosts else self.served_version
                return 200, {"x-dc-worker-version": v}, b"{}"
        raise AssertionError(f"unexpected request {method} {url}")

    _stuck_version = "4.9.73-kv-writes-only-credential-free"

    def _cf(self, method, path, headers, body):
        ok = lambda result: (200, {"content-type": "application/json"},
                             json.dumps({"success": True, "result": result}).encode())
        if method == "GET" and path == "/content/v2":
            if self.read_status != 200:
                return self.read_status, {}, json.dumps({"success": False, "errors": [
                    {"code": 10000, "message": "Authentication error"}]}).encode()
            b = "b216fe855e045840582946"
            framed = (f"--{b}\r\nContent-Disposition: form-data; name=\"worker.js\"; "
                      f"filename=\"worker.js\"\r\nContent-Type: application/javascript"
                      f"+module\r\n\r\n").encode() + self.live + f"\r\n--{b}--\r\n".encode()
            return 200, {"content-type": f"multipart/form-data; boundary={b}",
                         "cf-entrypoint": self.entrypoint}, framed
        if method == "GET" and path == "/settings":
            return ok(self.settings)
        if method == "GET" and path == "/schedules":
            return ok({"schedules": self.schedules})
        if method == "GET" and path == "/deployments":
            return ok({"deployments": [{"id": "dep1", "source": "quick_editor",
                                        "author_email": "someone@example.com",
                                        "versions": [{"version_id": "331cf038", "percentage": 100}]}]})
        if method == "PUT" and path in ("/content", ""):
            if self.put_status != 200:
                return self.put_status, {}, json.dumps({"success": False, "errors": [
                    {"code": 10000, "message": "Authentication error"}]}).encode()
            msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                b"Content-Type: " + headers["Content-Type"].encode() + b"\r\n\r\n" + body)
            parts = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
            module = parts["worker.js"]
            self.uploads.append({
                "path": path,
                "metadata": json.loads(parts["metadata"].get_content()),
                "filename": module.get_filename(),
                "content_type": module.get_content_type(),
                "bytes": module.get_payload(decode=True),
            })
            self.live = self.uploads[-1]["bytes"]
            self.served_version = D.extract_version(self.live)
            if self.drop_binding_on_put:
                self.settings["bindings"] = [b for b in self.settings["bindings"]
                                             if b["name"] != self.drop_binding_on_put]
            return ok({"id": "dchubapiproxy"})
        raise AssertionError(f"unexpected CF call {method} {path}")

    def writes(self):
        return [(m, u) for m, u in self.calls if m not in ("GET", "HEAD")]


class Clock:
    def __init__(self):
        self.t = 1_790_000_000.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(s, 1)


def _repo(tmp_path, content: bytes = REPO) -> str:
    (tmp_path / "worker.js").write_bytes(content)
    return str(tmp_path)


def _run(tmp_path, net, mode="deploy", *, history=None, repo=REPO, timeout=300, **kw):
    clock = Clock()
    logs: list[str] = []
    history = {D.git_blob_id(LIVE), D.git_blob_id(REPO)} if history is None else history
    rc = D.run(mode, repo_dir=_repo(tmp_path, repo), out=str(tmp_path / "out"),
               http=net, token="t", account="ACCT", verify_timeout=timeout,
               verify_interval=10, history_blobs=history, sleep=clock.sleep,
               clock=clock.time, log=logs.append, **kw)
    return rc, "\n".join(logs)


# ── ★ 1. refuses when live is ahead ──────────────────────────────────

def test_refuses_when_live_is_ahead_and_makes_no_write(tmp_path):
    ahead = worker("4.9.75-pasted-hotfix")
    net = FakeNet(live=ahead)
    rc, log = _run(tmp_path, net, history={D.git_blob_id(ahead), D.git_blob_id(REPO)})
    assert rc == D.EXIT_REFUSED
    assert net.writes() == [], "a refused deploy must not send a single write"
    assert not any(u.startswith(D.PROBE_URLS) for _, u in net.calls)
    assert "live_ahead_of_repo" in log and "revert" in log.lower()
    # the rollback artifact is written even on a refusal
    assert (tmp_path / "out" / "predeploy_worker.js").read_bytes() == ahead


def test_refuses_ahead_even_when_the_repo_went_backwards_on_purpose(tmp_path):
    """A git revert of worker.js puts an OLDER version on main while live holds
    a committed newer one. Still refused: a rollback ships as a new version."""
    newer = worker("4.9.75-committed")
    net = FakeNet(live=newer)
    rc, _ = _run(tmp_path, net, history={D.git_blob_id(newer), D.git_blob_id(REPO)})
    assert rc == D.EXIT_REFUSED and net.writes() == []


def test_refuses_live_bytes_that_no_commit_contains(tmp_path):
    """Dashboard hot-fix without a bump: live says 4.9.73 (behind), but its
    bytes are in no commit. Deploying would erase the hot-fix."""
    pasted = LIVE + b"// hot-fix typed into the dashboard\n"
    net = FakeNet(live=pasted)
    rc, log = _run(tmp_path, net)
    assert rc == D.EXIT_REFUSED and net.writes() == []
    assert "live_content_not_in_any_commit" in log


@pytest.mark.parametrize("live_v,repo_v,live_committed,code", [
    ("4.9.75-x", "4.9.74-y", True, "live_ahead_of_repo"),
    ("4.10.0-x", "4.9.99-y", True, "live_ahead_of_repo"),
    ("4.9.74-other", "4.9.74-y", True, "suffix_mismatch"),
    ("4.9.74-y", "4.9.74-y", True, "same_version_different_content"),
    ("dev", "4.9.74-y", True, "version_unparseable"),
    ("4.9.73-x", "4.9.74-y", False, "live_content_not_in_any_commit"),
    ("4.9.73-x", "4.9.74-y", True, "repo_ahead"),
])
def test_decide_table(live_v, repo_v, live_committed, code):
    live_blobs = {"L"}
    d = D.decide(repo_version=repo_v, repo_blob="R", live_version=live_v,
                 live_blobs=live_blobs, history_blobs={"L"} if live_committed else set(),
                 live_module_count=1)
    assert d.code == code
    assert (d.action == "deploy") == (code == "repo_ahead")


def test_already_live_is_a_noop_not_a_write(tmp_path):
    net = FakeNet(live=REPO)
    rc, log = _run(tmp_path, net)
    assert rc == D.EXIT_OK and net.writes() == [] and "already_live" in log


def test_multi_module_or_foreign_entrypoint_is_refused(tmp_path):
    for net in (FakeNet(entrypoint="index.js"),):
        rc, log = _run(tmp_path, net)
        assert rc == D.EXIT_REFUSED and net.writes() == []
    d = D.decide(repo_version="4.9.74-y", repo_blob="R", live_version="4.9.73-x",
                 live_blobs={"L"}, history_blobs={"L"}, live_module_count=2454)
    assert d.code == "live_not_single_module"


def test_plan_mode_never_writes_even_when_it_would_deploy(tmp_path):
    net = FakeNet()
    rc, log = _run(tmp_path, net, mode="plan")
    assert rc == D.EXIT_OK and net.writes() == [] and "repo_ahead" in log


# ── ★ 3. verifies the header after deploy ────────────────────────────

def test_deploy_writes_content_only_then_verifies_both_hosts(tmp_path):
    net = FakeNet()
    rc, log = _run(tmp_path, net)
    assert rc == D.EXIT_OK, log
    assert net.writes() == [("PUT", BASE + "/content")], "exactly one write, to /content"
    up = net.uploads[0]
    assert up["metadata"] == {"main_module": "worker.js"}, (
        "content-only: no bindings/compat keys that could replace live config")
    assert up["filename"] == "worker.js", "CF resolves main_module by FILENAME (10021)"
    assert up["content_type"] == "application/javascript+module"
    assert up["bytes"] == REPO, "uploaded bytes must be the repo file, unmodified"
    put_at = net.calls.index(("PUT", BASE + "/content"))
    for host in D.PROBE_URLS:
        reads = [i for i, (m, u) in enumerate(net.calls) if u.startswith(host + "?_=")]
        assert len(reads) >= 2, f"{host} must be read twice in a row at the new version"
        assert min(reads) > put_at, f"{host} was probed BEFORE the write — proves nothing"
    assert "VERIFIED" in log


def test_header_that_never_updates_fails_loudly(tmp_path):
    net = FakeNet(stuck_hosts=set(D.PROBE_URLS))
    rc, log = _run(tmp_path, net, timeout=300)
    assert rc == D.EXIT_VERIFY
    assert "4.9.74-infra-projects" in log and "within 300s" in log
    assert "ROLLBACK" in log.upper()


@pytest.mark.parametrize("stuck", D.PROBE_URLS)
def test_one_host_stuck_is_not_verified(tmp_path, stuck):
    net = FakeNet(stuck_hosts={stuck})
    rc, log = _run(tmp_path, net)
    assert rc == D.EXIT_VERIFY, f"{stuck} still old must fail the run"


def test_a_binding_lost_in_the_upload_fails_verification(tmp_path):
    net = FakeNet(drop_binding_on_put="HUBSPOT_API_KEY")
    rc, log = _run(tmp_path, net)
    assert rc == D.EXIT_VERIFY and "bindings CHANGED" in log and "HUBSPOT_API_KEY" in log


def test_wait_for_header_needs_two_consecutive_matches():
    class Flappy:
        n = 0

        def request(self, method, url, headers=None, body=None, timeout=30):
            Flappy.n += 1
            # new, old, new, new ... per host
            return 200, {"x-dc-worker-version": "old" if Flappy.n in (3, 4) else "new"}, b""
    c = Clock()
    ok, _ = D.wait_for_header(Flappy(), "new", 300, 10, sleep=c.sleep, clock=c.time, log=lambda *_: None)
    assert ok and Flappy.n == 8, "round 2 read old, so it takes rounds 3+4 to verify"


def test_probes_are_cache_busted_and_self_tagged(tmp_path):
    seen = []

    class Rec:
        def request(self, method, url, headers=None, body=None, timeout=30):
            seen.append((url, headers))
            return 200, {"x-dc-worker-version": "v"}, b""
    D.probe_versions(Rec(), 1234.5)
    assert [u.split("?")[0] for u, _ in seen] == list(D.PROBE_URLS)
    for url, headers in seen:
        assert re.search(r"\?_=\d+$", url)
        assert re.search(r"dchub|verify|probe", headers["User-Agent"])
        assert "Python-urllib" not in headers["User-Agent"]
    pages = [u for u, _ in seen if urllib.parse.urlsplit(u).hostname == "dchub.cloud"
             and urllib.parse.urlsplit(u).path.startswith("/api/")]
    assert not pages, "dchub.cloud/api/* is the Pages worker, with its own 5.x version"


# ── token permissions ────────────────────────────────────────────────

def test_token_that_cannot_read_stops_at_the_first_request(tmp_path):
    net = FakeNet(read_status=403)
    rc, log = _run(tmp_path, net)
    assert rc == D.EXIT_TOKEN
    assert net.calls == [("GET", BASE + "/content/v2")]
    assert "Workers Scripts" in log and "Read" in log


def test_token_that_cannot_write_says_edit_and_changes_nothing(tmp_path):
    net = FakeNet(put_status=403)
    rc, log = _run(tmp_path, net)
    assert rc == D.EXIT_TOKEN and "Edit" in log
    assert net.live == LIVE
    assert not any(u.startswith(D.PROBE_URLS) for _, u in net.calls)


# ── the --method full fallback ───────────────────────────────────────

def test_full_fallback_inherits_every_live_binding_not_a_written_list():
    meta = D.full_put_metadata({"compatibility_date": "2024-11-01",
                                "compatibility_flags": [], "bindings": LIVE_BINDINGS})
    assert meta["bindings"] == [{"type": "inherit", "name": b["name"]} for b in LIVE_BINDINGS]
    assert {"type": "inherit", "name": "HUBSPOT_API_KEY"} in meta["bindings"], (
        "the 4-name list in the original brief would have dropped this one")
    assert meta["compatibility_date"] == "2024-11-01" and meta["main_module"] == "worker.js"


def test_full_fallback_runs_through_the_same_verification(tmp_path):
    net = FakeNet()
    rc, _ = _run(tmp_path, net, method="full")
    assert rc == D.EXIT_OK and net.writes() == [("PUT", BASE)]
    assert {"type": "inherit", "name": "HUBSPOT_API_KEY"} in net.uploads[0]["metadata"]["bindings"]


# ── who may write ────────────────────────────────────────────────────

@pytest.mark.parametrize("event,ref,dry,armed,want", [
    ("pull_request", "refs/pull/1/merge", "false", "1", False),
    ("pull_request", "refs/heads/main", "false", "1", False),
    ("workflow_dispatch", "refs/heads/main", "true", "", False),
    ("workflow_dispatch", "refs/heads/main", "", "", False),
    ("workflow_dispatch", "refs/heads/main", "false", "", True),
    ("push", "refs/heads/main", "", "", False),
    ("push", "refs/heads/main", "", "true", False),
    ("push", "refs/heads/main", "", "1", True),
    ("push", "refs/heads/feature", "", "1", False),
    ("schedule", "refs/heads/main", "false", "1", False),
    ("", "", "false", "1", False),
])
def test_will_write(event, ref, dry, armed, want):
    assert D.will_write(event, ref, dry, armed)[0] is want


def test_real_dispatch_from_a_branch_is_refused_not_dried():
    with pytest.raises(D.Refused):
        D.will_write("workflow_dispatch", "refs/heads/some-branch", "false", "1")


def test_deploy_mode_rederives_permission_before_any_request(tmp_path, monkeypatch):
    net = FakeNet()
    monkeypatch.setattr(D, "Http", lambda: net)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "ACCT")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    rc = D.main(["deploy", "--repo-dir", _repo(tmp_path), "--out", str(tmp_path / "o"),
                 "--dry-run-input", "false", "--armed", "1"])
    assert rc == D.EXIT_REFUSED and net.calls == []


# ── ★ 2. never wrangler ──────────────────────────────────────────────

def _steps():
    wf = yaml.safe_load(WF_PATH.read_text())
    for job in wf["jobs"].values():
        yield from job["steps"]


def _executable(run: str) -> str:
    return "\n".join(ln for ln in run.splitlines() if not ln.lstrip().startswith("#"))


def test_no_workflow_step_uses_or_runs_wrangler():
    steps = list(_steps())
    assert steps, "parsed no steps — this test would pass on anything"
    for s in steps:
        assert "wrangler" not in s.get("uses", "").lower(), s
        run = _executable(s.get("run", ""))
        assert not re.search(r"\bwrangler\b", run), s
        assert "deploy-v4.7.sh" not in run, "stale script: adds nodejs_compat + dead bindings"
    runs = [s for s in steps if "run" in s]
    assert any("deploy_zone_worker.py deploy" in s["run"] for s in runs), (
        "the deploy step must go through the script these tests exercise")


def test_the_deploy_spawns_nothing_but_git(tmp_path, monkeypatch):
    """End to end through main(), with REAL git for the history check. Every
    process start is recorded; only `git` may appear, and never wrangler."""
    repo = tmp_path / "repo"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                  capture_output=True, env={**os.environ,
                                  "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                  "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    g("init", "-q")
    for content in (LIVE, REPO):
        (repo / "worker.js").write_bytes(content)
        g("add", "worker.js")
        g("commit", "-qm", "w")

    spawned: list[list[str]] = []
    real_popen = subprocess.Popen

    class RecordingPopen(real_popen):
        def __init__(self, args, *a, **kw):
            spawned.append([str(x) for x in (args if isinstance(args, (list, tuple)) else [args])])
            super().__init__(args, *a, **kw)

    def forbidden(*a, **kw):
        raise AssertionError(f"deploy started a process outside subprocess: {a}")

    net = FakeNet()
    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)
    for name in ("system", "popen", "execv", "execvp", "execvpe", "spawnv", "spawnvp", "posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbidden)
    monkeypatch.setattr(D, "Http", lambda: net)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "ACCT")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rc = D.main(["deploy", "--repo-dir", str(repo), "--out", str(tmp_path / "o"),
                 "--armed", "1", "--verify-interval", "0"])
    assert rc == D.EXIT_OK
    assert net.writes() == [("PUT", BASE + "/content")]
    assert spawned, "recorded no process at all — the history check did not run through git"
    for argv in spawned:
        assert pathlib.Path(argv[0]).name == "git", argv
        assert not any("wrangler" in a for a in argv), argv


# ── helpers against real git / real framing ──────────────────────────

def test_git_blob_id_matches_git_hash_object(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(REPO)
    want = subprocess.run(["git", "hash-object", str(p)], capture_output=True,
                          text=True, check=True).stdout.strip()
    assert D.git_blob_id(REPO) == want


def test_history_blobs_lists_every_version_the_file_had(tmp_path):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    g = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True,
                                  capture_output=True, env=env)
    g("init", "-q")
    versions = [worker(f"4.9.{n}-v") for n in (1, 2, 3)]
    for v in versions:
        (tmp_path / "worker.js").write_bytes(v)
        g("add", "worker.js")
        g("commit", "-qm", "w")
    (tmp_path / "other.txt").write_text("x")
    g("add", "other.txt")
    g("commit", "-qm", "unrelated")
    assert D.git_history_blobs(str(tmp_path)) == {D.git_blob_id(v) for v in versions}


def test_parse_multipart_strips_only_the_delimiter_crlf():
    b = "abc123"
    file = b"line\r\n"  # a file that itself ends in CRLF keeps it
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"worker.js\"; "
            f"filename=\"worker.js\"\r\nContent-Type: application/javascript+module"
            f"\r\n\r\n").encode() + file + f"\r\n--{b}--\r\n".encode()
    parts = D.parse_multipart(f"multipart/form-data; boundary={b}", body)
    assert parts == [{"name": "worker.js", "filename": "worker.js", "data": file}]


def test_trailing_newline_drift_counts_as_committed_but_code_does_not():
    committed = {D.git_blob_id(LIVE)}
    assert D.live_blob_candidates(LIVE.rstrip(b"\n")) & committed
    assert D.live_blob_candidates(LIVE + b"\n") & committed
    assert not D.live_blob_candidates(LIVE.replace(b"{}", b"{ }")) & committed


def test_version_core_agrees_with_the_detector():
    """'Ahead' must mean the same thing to the deploy and to the detector that
    files zone_worker_deployed_ahead_of_repo."""
    from routes import brain_consistency_radar as R
    for v in ("4.9.73-kv", "4.9.74", "4.10.0-x", "5.0.0-listing", "dev", "4.9", "4.9.x7", ""):
        assert D.version_core(v) == R._version_core(v), v


def test_version_regex_is_the_bump_gate_shape():
    gate = (ROOT / "scripts" / "check_worker_version_bump.sh").read_text()
    assert "s/^const WORKER_VERSION = '\\([^']*\\)'.*/\\1/p" in gate, (
        "bump gate changed its extraction shape; update _VERSION_RE to match")
    assert D.extract_version(REPO) == "4.9.74-infra-projects"
    assert D.extract_version(b'const WORKER_VERSION = "4.9.74";\n') is None
    assert D.extract_version((ROOT / "worker.js").read_bytes())


# ── workflow wiring ──────────────────────────────────────────────────

def _wf():
    wf = yaml.safe_load(WF_PATH.read_text())
    return wf, wf.get("on") or wf.get(True)  # PyYAML reads the key `on` as True


def test_push_trigger_is_worker_js_only():
    """Merging this workflow, or a change to the deploy script, must never ship
    production by itself — only a worker.js change may."""
    _, on = _wf()
    assert on["push"]["branches"] == ["main"]
    assert on["push"]["paths"] == ["worker.js"]


def test_deploy_job_is_gated_serialized_and_deploys_main():
    wf, on = _wf()
    assert wf["permissions"] == {"contents": "read"}
    dep = wf["jobs"]["deploy"]
    assert dep["needs"] == "plan"
    assert "needs.plan.outputs.write == 'true'" in dep["if"]
    assert "needs.plan.outputs.action == 'deploy'" in dep["if"]
    assert dep["concurrency"]["group"] == "zone-worker-deploy"
    assert dep["concurrency"]["cancel-in-progress"] is False
    checkout = next(s for s in dep["steps"] if s.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["ref"] == "main"
    assert checkout["with"]["fetch-depth"] == 0, "history check needs every worker.js blob id"
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True
    uploads = [s for s in dep["steps"] if s.get("uses", "").startswith("actions/upload-artifact")]
    assert uploads and uploads[0]["if"] == "always()", "rollback artifact must survive a failed run"


def test_plan_job_has_no_write_step():
    wf, _ = _wf()
    for s in wf["jobs"]["plan"]["steps"]:
        assert "deploy_zone_worker.py deploy" not in s.get("run", "")


def test_detector_detail_names_this_workflow_and_its_arm_variable():
    from routes import brain_consistency_radar as R
    assert (ROOT / R._ZONE_WORKER_DEPLOY_WORKFLOW) == WF_PATH and WF_PATH.exists()
    text = WF_PATH.read_text()
    assert f"vars.{R._ZONE_WORKER_ARM_VAR}" in text
