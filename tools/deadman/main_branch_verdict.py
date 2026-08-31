#!/usr/bin/env python3
"""tools/deadman/main_branch_verdict.py — is main's CURRENT HEAD green?

★2026-08-31 — THE BUG THIS CLOSES. main-branch-health.yml asked
"what did the newest COMPLETED run on main conclude?" That is not the same
question as "is main broken", and on 2026-08-31 the difference produced a false
RED on the public board:

    f5b191c8b  pre-merge run completed 22:20:56 -> FAILURE   (stale arch map)
    2e00f9aee  #3473 lands, THE FIX, run starts 22:23:28
    22:33:01   main-branch-health runs. HEAD's run is still in flight, so the
               newest COMPLETED run is the superseded commit's failure.
               verdict: main_red — "Every open PR is blocked until this is fixed."
    2e00f9aee  run completed 22:46:02 -> SUCCESS

main was already fixed. The monitor reported the state of a commit that had
been superseded 10 minutes earlier, and reported it as the state of the branch.

Nothing was broken, so nothing got fixed, so the board simply carried a red that
no action would clear — which is how a board stops being read. The same feed is
where a REAL red main would appear.

THE FIX: judge HEAD, and when HEAD is not measured yet, say so instead of
answering with an older commit. A pending verdict does NOT beat the ledger: the
`main-ci` feed has a 2h cadence, which comfortably covers a ~25 min run, so a
normal in-flight window passes quietly while CI that is genuinely STUCK stops
beating and goes overdue on its own. Silence that expires is honest; a verdict
copied from the wrong commit is not.

House rules: no DB, never import main, nothing at module scope.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

# The workflows carrying main's six required status checks.
GATING = ("pre-merge.yml", "regression-lint.yml", "app-contract-gate.yml")


def verdict(head_sha, runs_by_workflow):
    """(status, note) for main at `head_sha`. Pure — no network, no env.

    `runs_by_workflow` maps a workflow file to its recent runs on main, newest
    first, each a dict with headSha / status / conclusion.

    status is one of:
      success     every gating workflow completed successfully ON HEAD
      main_red    a gating workflow FAILED on HEAD — actionable now
      pending     HEAD is not fully measured and nothing failed — caller must
                  NOT beat, so a stuck CI ages out into overdue by itself
      unmeasured  not one gating workflow could be read
    """
    red, green, pending, unreadable = [], [], [], []
    for wf in runs_by_workflow:
        runs = runs_by_workflow.get(wf) or []
        if not runs:
            unreadable.append(wf)
            continue
        mine = [r for r in runs if (r or {}).get("headSha") == head_sha]
        if not mine:
            # A push to main always triggers these, so this is "not created
            # yet", not "does not apply".
            pending.append("%s(no run for HEAD yet)" % wf)
            continue
        run = mine[0]
        if run.get("status") != "completed":
            pending.append("%s(%s)" % (wf, run.get("status") or "in flight"))
        elif run.get("conclusion") != "success":
            red.append("%s(%s)" % (wf, run.get("conclusion")))
        else:
            green.append(wf)

    if red:
        # A definite failure on HEAD is actionable even while siblings run.
        return "main_red", "red on HEAD %s: %s" % (head_sha[:9], " ".join(red))
    if unreadable and not green and not pending:
        return "unmeasured", "no gating workflow could be read (%s)" % " ".join(unreadable)
    if pending:
        return "pending", "HEAD %s not fully measured yet: %s" % (
            head_sha[:9], " ".join(pending))
    return "success", "all %d gating workflow(s) green on HEAD %s" % (
        len(green), head_sha[:9])


def _gh(args):
    return subprocess.run(["gh"] + args, capture_output=True, text=True,
                          timeout=90, check=True).stdout


def collect(repo, head_sha=None):
    """Read HEAD and the gating workflows' recent runs on main via `gh`."""
    if not head_sha:
        head_sha = _gh(["api", "repos/%s/commits/main" % repo, "--jq", ".sha"]).strip()
    runs = {}
    for wf in GATING:
        try:
            out = _gh(["run", "list", "--repo", repo, "--workflow", wf,
                       "--branch", "main", "--limit", "25",
                       "--json", "headSha,status,conclusion,createdAt"])
            runs[wf] = json.loads(out or "[]")
        except Exception as e:
            print("::warning::could not read %s: %s" % (wf, str(e)[:160]))
            runs[wf] = []
    return head_sha, runs


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("::error::GITHUB_REPOSITORY not set")
        return 1
    head_sha, runs = collect(repo, os.environ.get("MAIN_HEAD_SHA") or None)
    status, note = verdict(head_sha, runs)
    for wf in GATING:
        mine = [r for r in (runs.get(wf) or []) if r.get("headSha") == head_sha]
        state = (mine[0].get("conclusion") or mine[0].get("status")) if mine else "no run yet"
        print("  %-24s -> %s" % (wf, state))
    print("verdict: %s — %s" % (status, note))
    # Machine-readable for the workflow step.
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write("status=%s\n" % status)
            fh.write("note=%s\n" % note.replace("\n", " "))
            fh.write("head_sha=%s\n" % head_sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
