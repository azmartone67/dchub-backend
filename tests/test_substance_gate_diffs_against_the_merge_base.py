"""brain-pr-substance-gate.yml must judge a PR by what the PR changes: the diff
against its MERGE-BASE, not the tree difference to the base branch's tip.

BASE_SHA is `github.event.pull_request.base.sha`, the base TIP at event time.
The two-dot `git diff --name-only "$BASE_SHA" "$HEAD_SHA"` compared two trees,
so a PR behind main was charged with every file main changed after it
branched: a docs-only scaffold claiming a fix scored "Changes real code" and
passed the required check built to block it. And a PR whose change main
already carried listed nothing: be#5114 (2026-09-21, BASE_SHA=5b1fd919 after
#5113, HEAD_SHA=03a9b054) printed an empty "Changed files:" and was failed as
a scaffold claiming a fix (the refresh bot's template says "resolved").

These tests run the workflow's OWN step scripts, read from the YAML rather
than copied, in bash against a real git repository (precedent:
tests/test_baseline_refresh_never_opens_a_no_op.py). Only `gh` and
scripts/gate_beat.sh are stubbed, at the process boundary.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows" / "brain-pr-substance-gate.yml"
GATE_STEP = "Classify diff and comment"
BEAT_STEP = "Beat the gate liveness ledger"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "git")),
    reason="runs the workflow's bash against real git",
)

# Each `gh` call is logged as one JSON line: the argv it was given.
GH_STUB = f"""#!{sys.executable}
import json, os, sys
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
"""

BEAT_STUB = """printf '%s\\n' "$@" > "$BEAT_LOG"
"""

# The refresh-api-response-baseline bot's own PR wording: "resolved" matches
# the gate's claim regex, which is how be#5114 came to be read as a fix claim.
BOT_BODY = "Guards: resolution downgrades (resolved/partial -> opaque) are refused."

SCAFFOLD = {"docs/finding.md": "# finding\n", "routes/_proposed_fix.py": "fix = 1\n"}
REAL = {"routes/api.py": "api = 1\n", "docs/api.md": "# api\n"}


def _steps():
    return yaml.safe_load(WF.read_text(encoding="utf-8"))["jobs"]["substance-gate"]["steps"]


def _step(name):
    found = [s["run"] for s in _steps() if s.get("name") == name]
    assert len(found) == 1, f"step {name!r} found {len(found)} times"
    return found[0]


class Repo:
    """One repository standing in for the runner's full-history checkout."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.dir = tmp_path / "repo"
        self.env = {
            "PATH": f"{tmp_path / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "STUB_LOG": str(tmp_path / "gh.log"), "BEAT_LOG": str(tmp_path / "beat.log"),
            "GITHUB_REPOSITORY": "o/r", "GH_TOKEN": "t", "PR_NUMBER": "7",
            "DCHUB_ADMIN_KEY": "k",
        }
        (tmp_path / "bin").mkdir()
        gh = tmp_path / "bin" / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        self.dir.mkdir()
        self.git("init", "-q", "-b", "main")
        self.n = 0
        self.root = self.commit({"routes/app.py": "app = 1\n", "docs/README.md": "# docs\n"})
        # Untracked, so no diff between two commits can list it; the ledger
        # step runs it from the checkout.
        (self.dir / "scripts").mkdir()
        (self.dir / "scripts" / "gate_beat.sh").write_text(BEAT_STUB)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.dir, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, files, parent=None, orphan=False):
        """Commit `files` ({path: text, or None to delete}) on `parent`; return the sha."""
        self.n += 1   # distinct messages: same tree + parent in one second is one sha
        if orphan:
            self.git("checkout", "-q", "--orphan", f"lonely{self.n}")
        elif parent:
            self.git("checkout", "-q", "--detach", parent)
        for path, text in files.items():
            f = self.dir / path
            if text is None:
                f.unlink()
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text)
        self.git("add", "-A", "--", *files)
        self.git("commit", "-q", "-m", f"c{self.n}")
        return self.git("rev-parse", "HEAD")

    def behind_main(self, pr_files):
        """A PR branched from root, after which main moved on with REAL code."""
        head = self.commit(pr_files, parent=self.root)
        base = self.commit({"routes/app.py": "app = 2\n"}, parent=self.root)
        return base, head

    def run(self, step, base, head, **env):
        for log in ("gh.log", "beat.log"):
            (self.tmp / log).write_text("")
        env = {"BASE_SHA": base, "HEAD_SHA": head, "PR_BODY": "", "PR_TITLE": "",
               "GATE_DISABLE": "", "JOB_STATUS": "success", **env}
        # GitHub runs an unshelled `run:` as `bash -e {0}`.
        proc = subprocess.run(["bash", "-e", "-c", _step(step)], cwd=self.dir,
                              env={**self.env, **env}, capture_output=True, text=True,
                              timeout=60)
        calls = [json.loads(line) for line in (self.tmp / "gh.log").read_text().splitlines()]
        return proc, calls

    def beat(self):
        return (self.tmp / "beat.log").read_text().splitlines()


def _verdict(calls):
    """(conclusion, title) of the one check-run the step posted."""
    posts = [c for c in calls if c[0] == "api" and any(a.endswith("/check-runs") for a in c)]
    assert len(posts) == 1, calls
    fields = dict(posts[0][i + 1].split("=", 1) for i, a in enumerate(posts[0]) if a == "-f")
    return fields["conclusion"], fields["output[title]"]


def _comment(calls):
    bodies = [c[c.index("--body") + 1] for c in calls if c[:2] == ["pr", "comment"]]
    assert len(bodies) == 1, calls
    return bodies[0]


def test_a_scaffold_behind_main_that_claims_a_fix_fails(tmp_path):
    repo = Repo(tmp_path)
    base, head = repo.behind_main(SCAFFOLD)
    proc, calls = repo.run(GATE_STEP, base, head, PR_TITLE="Fixes the stale canon finding")
    assert proc.returncode == 0, proc.stderr
    assert _verdict(calls) == ("failure", "Scaffold-only PR CLAIMS a fix — changes no running code")
    assert "0 runtime file(s) changed · 1 routes/_proposed_* scaffold(s) touched" in _comment(calls)


def test_an_honest_scaffold_behind_main_is_neutral(tmp_path):
    repo = Repo(tmp_path)
    base, head = repo.behind_main(SCAFFOLD)
    proc, calls = repo.run(GATE_STEP, base, head, PR_TITLE="spec: record the finding")
    assert proc.returncode == 0, proc.stderr
    assert _verdict(calls) == ("neutral", "Scaffold-only PR — changes no running code")


def test_a_real_code_pr_behind_main_passes_on_its_own_files(tmp_path):
    repo = Repo(tmp_path)
    base, head = repo.behind_main(REAL)
    proc, calls = repo.run(GATE_STEP, base, head, PR_TITLE="Fixes the api")
    assert proc.returncode == 0, proc.stderr
    # 1, not 2: routes/app.py is main's change since the branch point, not this PR's.
    assert _verdict(calls) == ("success", "Changes real code (1 file(s))")
    assert len(calls) == 1   # no comment on a real-code PR


def test_a_pr_whose_change_main_already_carries_is_judged_by_its_change(tmp_path):
    """be#5114: same bytes as the #5113 commit main had just merged."""
    repo = Repo(tmp_path)
    change = {"contracts/api_response_surface.json": '{"keys": 2}\n'}
    head = repo.commit(change, parent=repo.root)
    base = repo.commit(change, parent=repo.root)
    proc, calls = repo.run(GATE_STEP, base, head, PR_BODY=BOT_BODY,
                           PR_TITLE="contracts: refresh the API response baseline")
    assert proc.returncode == 0, proc.stderr
    assert _verdict(calls) == ("success", "Changes real code (1 file(s))")


def _already_on_main(repo):
    head = repo.commit({"routes/api.py": "api = 1\n"}, parent=repo.root)
    return repo.commit({"routes/app.py": "app = 2\n"}, parent=head), head


def _cancels_out(repo):
    added = repo.commit({"routes/api.py": "api = 1\n"}, parent=repo.root)
    head = repo.commit({"routes/api.py": None}, parent=added)
    return repo.commit({"routes/app.py": "app = 2\n"}, parent=repo.root), head


@pytest.mark.parametrize("shape", [_already_on_main, _cancels_out])
@pytest.mark.parametrize("title, verdict", [
    ("Fixes the api", ("failure", "Empty PR CLAIMS a fix — changes no files")),
    ("chore: sync", ("neutral", "Empty PR — changes no files")),
])
def test_an_empty_pr_is_named_empty_not_scaffold_only(tmp_path, shape, title, verdict):
    """Against the merge-base an empty list means the PR changes nothing.
    It keeps the inert verdict (a fix claim fails) under its own name."""
    repo = Repo(tmp_path)
    base, head = shape(repo)
    proc, calls = repo.run(GATE_STEP, base, head, PR_TITLE=title)
    assert proc.returncode == 0, proc.stderr
    assert _verdict(calls) == verdict
    comment = _comment(calls)
    assert "changes **no files at all**" in comment
    assert "scaffold-only" not in comment.lower()


def test_no_merge_base_fails_the_step_instead_of_judging_an_empty_list(tmp_path):
    repo = Repo(tmp_path)
    lonely = repo.commit({"docs/x.md": "x\n"}, orphan=True)
    proc, calls = repo.run(GATE_STEP, repo.root, lonely, PR_TITLE="Fixes it")
    assert proc.returncode != 0
    assert "::error::" in proc.stdout
    assert calls == []


def test_the_kill_switch_posts_neutral_and_never_diffs(tmp_path):
    """Unchanged by the merge-base fix. The SHAs name no commit, so any diff fails."""
    repo = Repo(tmp_path)
    proc, calls = repo.run(GATE_STEP, "0" * 40, "f" * 40, GATE_DISABLE="1", PR_TITLE="Fixes it")
    assert proc.returncode == 0, proc.stderr
    assert _verdict(calls) == ("neutral", "gate disabled")
    assert len(calls) == 1
    proc, _ = repo.run(BEAT_STEP, "0" * 40, "f" * 40, GATE_DISABLE="1")
    assert proc.returncode == 0, proc.stderr
    assert repo.beat()[1:3] == ["unmeasured", ""]


@pytest.mark.parametrize("pr_files, n", [(SCAFFOLD, "0"), (REAL, "1")])
def test_the_ledger_counts_the_prs_own_non_inert_files(tmp_path, pr_files, n):
    repo = Repo(tmp_path)
    base, head = repo.behind_main(pr_files)
    proc, _ = repo.run(BEAT_STEP, base, head)
    assert proc.returncode == 0, proc.stderr
    assert repo.beat()[:3] == ["brain-pr-substance-gate:substance-gate", "pass", n]


def test_the_checkout_fetches_the_history_the_merge_base_needs():
    checkouts = [s for s in _steps() if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 1
    assert checkouts[0].get("with", {}).get("fetch-depth") == 0
