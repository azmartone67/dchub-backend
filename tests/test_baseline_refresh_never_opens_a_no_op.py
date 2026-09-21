"""refresh-api-response-baseline.yml must neither open nor keep open a refresh
PR whose baseline main already carries.

be#5114 (2026-09-21) opened ~30 s after be#5113 merged the same regeneration:
its tree was identical to main's. `verify-against` passes such a PR (it IS
main's surface), `substance-gate` blocks it forever (its diff is empty), and
the one-refresh-at-a-time rule then opened no other refresh for ~7 h.

These tests run the workflow's OWN step scripts, read from the YAML rather
than copied, in bash against real git repositories. Only `gh` and the contract
script are stubbed, at the process boundary. The stub `gh` answers `pr list`
by running the step's own `--jq` filter. The only edit to the step text moves
its hardcoded /tmp scratch files into the test's directory.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows" / "refresh-api-response-baseline.yml"
CLOSE_STEP = "Close refresh PRs whose baseline is no longer main's surface"
OPEN_STEP = "Regenerate, and open an auto-merge PR if the baseline drifted"
BASELINE = "contracts/api_response_surface.json"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "git", "jq")),
    reason="runs the workflow's bash against real git, with gh answering through jq",
)

GH_STUB = r"""#!/usr/bin/env bash
printf 'gh %s\n' "$*" >> "$STUB_LOG"
if [ "$1 $2" = "pr list" ]; then
  q=""
  while [ $# -gt 0 ]; do
    if [ "$1" = "--jq" ]; then q="$2"; shift; fi
    shift
  done
  printf '%s' "$STUB_PRS_JSON" | jq -r "$q"
  exit
fi
if [ "$1 $2" = "pr comment" ]; then
  while [ $# -gt 0 ]; do
    if [ "$1" = "--body-file" ]; then cat "$2" >> "$STUB_LOG"; fi
    shift
  done
fi
exit 0
"""

CONTRACT_STUB = r"""import os, shutil, sys
with open(os.environ["STUB_LOG"], "a") as log:
    log.write("contract " + " ".join(sys.argv[1:]) + "\n")
if sys.argv[1] == "verify-against":
    sys.exit(int(os.environ.get("STUB_VERIFY_RC", "0")))
if sys.argv[1] == "baseline":
    shutil.copy(os.environ["STUB_REGEN"], "contracts/api_response_surface.json")
    sys.exit(0)
sys.exit(2)
"""


def _step(name, scratch):
    steps = yaml.safe_load(WF.read_text(encoding="utf-8"))["jobs"]["refresh"]["steps"]
    found = [s["run"] for s in steps if s.get("name") == name]
    assert len(found) == 1, f"step {name!r} found {len(found)} times"
    script = found[0]
    assert "/tmp/" in script, "the step no longer writes to /tmp — update the relocation"
    return script.replace("/tmp/", f"{scratch}/")


def _surface(*keys):
    return json.dumps({
        "stats": {"keys_total": len(keys), "keys_strictly_protected": len(keys)},
        "endpoints": {"GET /api/v1/x": {"resolution": "resolved", "keys": sorted(keys)}},
    }, indent=2, sort_keys=True) + "\n"


class Repo:
    """A bare `origin` plus a clone that plays the runner's checkout."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.env = {
            "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "STUB_LOG": str(tmp_path / "calls.log"),
            "STUB_PRS_JSON": "[]",
            "STUB_VERIFY_RC": "0",
            "LIVE": "success", "SELFTEST": "success", "GH_TOKEN": "t",
        }
        (tmp_path / "bin").mkdir()
        gh = tmp_path / "bin" / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        (tmp_path / "calls.log").write_text("")
        self.n = 0
        self.origin = tmp_path / "origin.git"
        self.seed = tmp_path / "seed"
        self.work = tmp_path / "work"
        self.git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.git(tmp_path, "clone", "-q", str(self.origin), str(self.seed))

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, surface, parent=None):
        """Commit `surface` as the baseline on top of `parent`; return its sha."""
        if parent:
            self.git(self.seed, "checkout", "-q", "--detach", parent)
        path = self.seed / BASELINE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(surface)
        self.git(self.seed, "add", BASELINE)
        self.n += 1   # distinct messages: same tree + parent in one second is one sha
        self.git(self.seed, "commit", "-q", "--allow-empty", "-m", f"c{self.n}")
        return self.git(self.seed, "rev-parse", "HEAD")

    def set_main(self, sha):
        self.git(self.seed, "push", "-q", "-f", "origin", f"{sha}:refs/heads/main")

    def open_pr(self, number, sha):
        self.git(self.seed, "push", "-q", "-f", "origin", f"{sha}:refs/pull/{number}/head")

    def checkout_runner(self):
        """The runner's checkout: live main, detached (step `Move to live main`)."""
        self.git(self.tmp, "clone", "-q", str(self.origin), str(self.work))
        self.git(self.work, "checkout", "-q", "--detach", "origin/main")
        (self.work / "scripts").mkdir(exist_ok=True)
        (self.work / "scripts" / "api_response_contract.py").write_text(CONTRACT_STUB)

    def run(self, step, **env):
        scratch = self.tmp / "scratch"
        scratch.mkdir(exist_ok=True)
        proc = subprocess.run(["bash", "-e", "-c", _step(step, scratch)], cwd=self.work,
                              env={**self.env, **env}, capture_output=True, text=True,
                              timeout=120)
        return proc, (self.tmp / "calls.log").read_text()


def _refresh_prs(*numbers):
    prs = [{"number": n, "headRefName": f"chore/api-baseline-{n}"} for n in numbers]
    prs.append({"number": 1, "headRefName": "feat/not-a-refresh"})
    return json.dumps(prs)


OLD, NEW, OTHER = _surface("a"), _surface("a", "b"), _surface("a", "c")


# ── (2) the close loop ─────────────────────────────────────────────────────

def test_an_open_refresh_whose_baseline_main_already_has_is_closed(tmp_path):
    """#5114: verify-against says 0 (it IS main's surface) and the PR merges nothing."""
    r = Repo(tmp_path)
    base = r.commit(OLD)
    landed = r.commit(NEW, parent=base)        # #5113 merged the regeneration
    r.set_main(landed)
    r.open_pr(9001, r.commit(NEW, parent=base))  # #5114, same bytes, older base
    r.checkout_runner()

    proc, log = r.run(CLOSE_STEP, STUB_PRS_JSON=_refresh_prs(9001), STUB_VERIFY_RC="0")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "gh pr close 9001 --delete-branch" in log, log
    assert "main already carries this baseline" in log
    assert "gh pr close 1 " not in log            # only refresh PRs are touched


def test_an_open_refresh_that_is_still_main_s_surface_and_changes_it_stays_open(tmp_path):
    """Control: a refresh that would still land real bytes is left alone."""
    r = Repo(tmp_path)
    base = r.commit(OLD)
    r.set_main(base)
    r.open_pr(9002, r.commit(NEW, parent=base))
    r.checkout_runner()

    proc, log = r.run(CLOSE_STEP, STUB_PRS_JSON=_refresh_prs(9002), STUB_VERIFY_RC="0")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "contract verify-against --main origin/main --candidate refs/remotes/pr/9002" in log
    assert "pr close" not in log, log
    assert "#9002 is still main's surface — leaving it open" in proc.stdout


def test_a_stale_refresh_is_still_closed_as_stale(tmp_path):
    """The existing rule survives: verify-against exit 1 closes it, and says why."""
    r = Repo(tmp_path)
    base = r.commit(OLD)
    r.set_main(r.commit(OTHER, parent=base))
    r.open_pr(9003, r.commit(NEW, parent=base))
    r.checkout_runner()

    proc, log = r.run(CLOSE_STEP, STUB_PRS_JSON=_refresh_prs(9003), STUB_VERIFY_RC="1")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "gh pr close 9003 --delete-branch" in log, log
    assert "exit 1)" in log                      # the verdict is named, in either wording
    assert "would not be main's surface" in log


# ── (3) the pre-open re-verify ─────────────────────────────────────────────

def test_no_refresh_is_opened_when_main_already_has_its_bytes(tmp_path):
    """The run built on an older main, and another refresh landed the same bytes meanwhile."""
    r = Repo(tmp_path)
    base = r.commit(OLD)
    r.set_main(base)
    r.checkout_runner()                       # the run starts on `base`
    r.set_main(r.commit(NEW, parent=base))    # an earlier refresh merges meanwhile
    regen = tmp_path / "regen.json"
    regen.write_text(NEW)

    proc, log = r.run(OPEN_STEP, STUB_REGEN=str(regen), STUB_VERIFY_RC="0")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "already carries this exact baseline" in proc.stdout
    assert "pr create" not in log, log
    heads = r.git(r.work, "ls-remote", "--heads", "origin").splitlines()
    assert [h for h in heads if "chore/api-baseline-" in h] == [], heads


def test_a_refresh_is_opened_when_main_does_not_have_its_bytes(tmp_path):
    """Control: the same run with main unmoved opens the PR, so the harness can see an open."""
    r = Repo(tmp_path)
    base = r.commit(OLD)
    r.set_main(base)
    r.checkout_runner()
    regen = tmp_path / "regen.json"
    regen.write_text(NEW)

    proc, log = r.run(OPEN_STEP, STUB_REGEN=str(regen), STUB_VERIFY_RC="0")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "gh pr create --fill" in log, log
    assert "gh pr merge --auto --squash" in log
    heads = r.git(r.work, "ls-remote", "--heads", "origin").splitlines()
    assert len([h for h in heads if "chore/api-baseline-" in h]) == 1, heads
