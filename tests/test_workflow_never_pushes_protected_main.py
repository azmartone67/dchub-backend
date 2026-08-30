"""A bot `git push` to this repo's main can NEVER succeed — so it is dead code.

★ THE 17-DAY RED. mcp-facts-export.yml ran daily from 2026-08-14 and failed
every single time — 0 successes in 60 runs. The failing step did:

    git commit -m "chore(facts): refresh servable mcp_facts.json [skip ci]"
    git push

against dchub-backend's main, which requires 6 status checks. GitHub answers
GH006 "Protected branch update failed ... 6 of 6 required status checks are
expected", and a `[skip ci]` commit reports NONE of them, so the push is
rejected on principle rather than on permissions: `contents: write` is
necessary but not sufficient. The step could not have worked on any day.

★ WHY IT WENT UNNOTICED FOR 17 DAYS. The step's own remediation text said the
file "is not served by any route anyway", so every red run read as cosmetic.
That had been true when written on 2026-08-13 — and became false on 2026-08-20,
when a surface sweep added /.well-known/mcp_facts.json to the served allowlist
in main.py. The result was a live AI-discovery surface serving numbers 30 days
old (facilities 15,300+ against a live 19,500+; nine figures in all), which no
honest-numbers fence caught because they all ban OVER-claims and these were
UNDER-claims.

★ WHAT THIS FENCE ASSERTS, and why it is on the executable body. It reads the
parsed `run:` script of every workflow step — never a comment, never the file
text — so it cannot be satisfied by prose that merely explains the rule. A step
that pushes from the BACKEND working directory must first create a branch; the
PR is what runs the required checks. Pushes from a sibling checkout (the
dchub-mcp-server repo, whose main is unprotected) are explicitly out of scope.
"""
import os
import re

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, ".github", "workflows")

# `git push` with no refspec pushes the CURRENT branch to its upstream. In a
# plain actions/checkout that branch is main.
_BARE_PUSH = re.compile(r"^\s*git push\s*(?:(?:-[^\s]+|--[^\s]+)\s*)*$", re.M)
_MAKES_BRANCH = re.compile(r"git\s+(?:checkout\s+-b|switch\s+-c)\b")
# an explicit refspec that is not main, e.g. `git push origin HEAD:my/branch`
_EXPLICIT_NON_MAIN = re.compile(r"git\s+push\b[^\n]*\s(?!main\b)[^\s]+:[^\s]+")


def _is_backend_dir(wd: str) -> bool:
    """The backend is the repo root, or an explicit dchub-backend checkout dir.

    A step running in a SIBLING repo's directory is pushing to that repo, whose
    branch protection is its own business.
    """
    wd = (wd or "").strip().rstrip("/")
    return wd in ("", ".") or wd.endswith("dchub-backend")


def _steps():
    for fn in sorted(os.listdir(WF_DIR)):
        if not fn.endswith((".yml", ".yaml")):
            continue
        with open(os.path.join(WF_DIR, fn), encoding="utf-8") as fh:
            try:
                doc = yaml.safe_load(fh)
            except yaml.YAMLError:
                continue           # a malformed workflow is another fence's job
        if not isinstance(doc, dict):
            continue
        for job_name, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            job_wd = ((job.get("defaults") or {}).get("run") or {}).get("working-directory")
            for step in (job.get("steps") or []):
                if not isinstance(step, dict):
                    continue
                run = step.get("run")
                if not isinstance(run, str):
                    continue
                yield fn, job_name, step.get("name", "<unnamed>"), \
                    step.get("working-directory") or job_wd, run


def test_no_workflow_pushes_the_backends_protected_main():
    offenders = []
    for fn, job, name, wd, run in _steps():
        if not _BARE_PUSH.search(run):
            continue
        if not _is_backend_dir(wd):
            continue               # sibling repo — out of scope, see docstring
        if _MAKES_BRANCH.search(run) or _EXPLICIT_NON_MAIN.search(run):
            continue               # lands on a branch, so a PR can run the checks
        offenders.append(f"  {fn} :: job {job} :: step {name!r} "
                         f"(working-directory={wd or 'repo root'})")
    assert not offenders, (
        "A bare `git push` from the backend working directory targets a "
        "protected main and is rejected with GH006 on every run — the step is "
        "dead code that reports red forever. Create a branch and open a PR so "
        "the 6 required checks can actually run:\n" + "\n".join(offenders))


def test_the_served_wellknown_allowlist_is_refreshed_by_the_exporter():
    """Served implies refreshed. /.well-known/mcp_facts.json went stale for 30
    days precisely because it was served by one change and refreshed by another
    that could never succeed. If main.py serves the file, the exporter must
    still name it as a write target."""
    main_py = os.path.join(ROOT, "main.py")
    with open(main_py, encoding="utf-8") as fh:
        main_src = fh.read()
    if "/.well-known/mcp_facts.json" not in main_src:
        pytest.skip("main.py no longer serves mcp_facts.json")
    exporter = os.path.join(ROOT, "mcp_facts_export.py")
    with open(exporter, encoding="utf-8") as fh:
        exp_src = fh.read()
    assert '".well-known", "mcp_facts.json"' in exp_src, (
        "main.py SERVES /.well-known/mcp_facts.json but mcp_facts_export.py no "
        "longer writes static/.well-known/mcp_facts.json — the surface would be "
        "frozen at whatever was last committed, which is how it came to publish "
        "a 27%-under facility count for 30 days.")
