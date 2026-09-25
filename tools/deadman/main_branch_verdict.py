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

# main's required status checks. They live in branch protection, not in this
# repo, so this is the one copy here. tests/test_main_red_is_watched.py maps
# each one to the workflow job that emits it and fails if GATING misses that
# workflow — but nothing offline can see a context ADDED in GitHub's settings,
# which is how `contract` joined unnoticed. So every run also compares this
# tuple with what branch protection requires NOW (collect_required ->
# required_drift), and a mismatch either way keeps the verdict off green.
# Measured 2026-09-21: seven. By hand, with the owner's token:
#   gh api repos/azmartone67/dchub-backend/branches/main/protection \
#     --jq .required_status_checks.contexts
REQUIRED_CONTEXTS = ("substance-gate", "syntax-check", "unit-tests",
                     "regression-lint", "db-parity", "app-contract-gate",
                     "contract", "web-image-build")
# Required, but only brain-pr-substance-gate.yml emits it and that runs on
# pull_request alone, so main never has a run of it to judge.
PR_ONLY_CONTEXTS = ("substance-gate",)

# The workflows whose runs on main carry the other seven (a check's context is its
# job's `name:`, else the job id):
#   pre-merge.yml              syntax-check, unit-tests, regression-lint, db-parity
#   app-contract-gate.yml      app-contract-gate
#   api-response-contract.yml  contract
#   web-image-build.yml        web-image-build   (required 2026-09-25: the
#                              image railway.toml deploys for web + worker)
# ★2026-09-21 api-response-contract.yml was missing: `contract` went red on main
# at 84f4a441d (stats.mw_coverage removed) and four runs of this verdict said
# "all 3 gating workflow(s) green" until #5039 fixed main.
# regression-lint.yml is NOT here: it carries none of the eight. Its job is
# `lint`; the `regression-lint` context is pre-merge.yml's job of that name, and
# the workflow's `name:` matching it is what kept it here until 2026-09-21. A red
# lint blocks no PR, so it must not read as main_red (ci-triage still triages it).
GATING = ("pre-merge.yml", "app-contract-gate.yml", "api-response-contract.yml",
          "web-image-build.yml")


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


def required_drift(live, declared):
    """(status, note): the contexts branch protection requires (`live`) against
    the ones this module declares (`declared`). Pure — no network, no env.

    `live` is whatever collect_required() returned: a list of context names, or
    the exception that stopped the read.

    status is one of:
      match       the same set on both sides
      drift       they differ, and the note names each side. Required in GitHub
                  but not declared: GATING may not watch it — the 2026-09-21
                  `contract` blind spot. Declared but no longer required: its
                  red would be reported as blocking PRs it does not block.
      unmeasured  the read failed, or came back empty, missing or malformed.
                  NEVER `match`: main requires checks, so an empty answer is a
                  failed probe, not proof that nothing is required.
    """
    if isinstance(live, BaseException):
        return "unmeasured", "could not read main's required contexts: %s" % (
            str(live).replace("\n", " ")[:200] or type(live).__name__)
    if not isinstance(live, (list, tuple)) or not live:
        return "unmeasured", ("branch protection answered %s for main's required "
                              "contexts — a failed read here, not proof that "
                              "nothing is required" % repr(live)[:80])
    if not all(isinstance(c, str) and c for c in live):
        return "unmeasured", "malformed required contexts: %s" % repr(live)[:200]
    added = sorted(set(live) - set(declared))
    dropped = sorted(set(declared) - set(live))
    if not added and not dropped:
        return "match", "all %d match REQUIRED_CONTEXTS" % len(set(live))
    parts = []
    if added:
        parts.append("required in GitHub but missing from REQUIRED_CONTEXTS: %s"
                     % " ".join(added))
    if dropped:
        parts.append("in REQUIRED_CONTEXTS but no longer required: %s"
                     % " ".join(dropped))
    return "drift", "; ".join(parts)


def combined(verdict_result, contexts_result):
    """(status, note) for the board: verdict() joined with required_drift().
    Pure.

    Any verdict other than `success` stands — main_red is still the actionable
    news, and pending still beats nothing. `success` stands only on a contexts
    `match`: all-green on GATING is only as complete as REQUIRED_CONTEXTS, so
    otherwise it becomes `contexts_drift` or `contexts_unmeasured`. Neither is
    in the ledger's _OK_STATUS, so the main-ci feed turns red; and the contexts
    result rides in every note, so no status hides it."""
    status, note = verdict_result
    c_status, c_note = contexts_result
    note = "%s | required contexts %s: %s" % (note, c_status, c_note)
    if status == "success" and c_status != "match":
        status = "contexts_%s" % c_status
    return status, note


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


def collect_required(repo):
    """main's required status-check contexts as branch protection reports them
    to this job's token: a list, or the exception that stopped the read. Never
    raises — required_drift() makes anything but a non-empty list `unmeasured`.

    ★ GET /branches/main, NOT /branches/main/protection. Measured 2026-09-21 in
    Actions run 35576294578, with main-branch-health's own permissions (actions
    + contents read) and again with read-all:
      /branches/main/protection[/required_status_checks[/contexts]]
                          403 "Resource not accessible by integration",
                          x-accepted-github-permissions: administration=read —
                          which GITHUB_TOKEN cannot be granted at all
      GraphQL ref.branchProtectionRule          FORBIDDEN, same message
      /branches?protected=true                  200, but protection: null
      /rules/branches/main, /rulesets           200 [] — branch protection here,
                                                not rulesets
      /branches/main                            200 on contents=read, all seven
    """
    try:
        out = _gh(["api", "repos/%s/branches/main" % repo,
                   "--jq", ".protection.required_status_checks"])
        rsc = json.loads(out or "null") or {}
        # `contexts` is the legacy list, `checks` its app-pinned successor;
        # read both, so a context listed in only one still counts.
        live = list(rsc.get("contexts") or [])
        live += [c["context"] for c in rsc.get("checks") or []]
    except subprocess.CalledProcessError as e:
        return RuntimeError("gh api repos/%s/branches/main: %s" % (
            repo, (e.stderr or "").strip()[:160] or "exit %s" % e.returncode))
    except Exception as e:
        # Any other surprise in the answer is unreadable, not "nothing required".
        return e
    return live


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        print("::error::GITHUB_REPOSITORY not set")
        return 1
    head_sha, runs = collect(repo, os.environ.get("MAIN_HEAD_SHA") or None)
    c_status, c_note = required_drift(collect_required(repo), REQUIRED_CONTEXTS)
    status, note = combined(verdict(head_sha, runs), (c_status, c_note))
    for wf in GATING:
        mine = [r for r in (runs.get(wf) or []) if r.get("headSha") == head_sha]
        state = (mine[0].get("conclusion") or mine[0].get("status")) if mine else "no run yet"
        print("  %-24s -> %s" % (wf, state))
    print("  %-24s -> %s: %s" % ("required contexts", c_status, c_note))
    print("verdict: %s — %s" % (status, note))
    # Machine-readable for the workflow step.
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write("status=%s\n" % status)
            fh.write("note=%s\n" % note.replace("\n", " "))
            fh.write("head_sha=%s\n" % head_sha)
            fh.write("contexts_status=%s\n" % c_status)
            fh.write("contexts_note=%s\n" % c_note.replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
