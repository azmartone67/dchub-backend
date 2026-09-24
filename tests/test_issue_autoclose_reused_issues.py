#!/usr/bin/env python3
"""tests/test_issue_autoclose_reused_issues.py — the auto-closer sees REUSED failure issues.

NO NETWORK. Runs issue-autoclose.yml's real github-script under node against a
stubbed `github`, so these tests check what the script does, not what it says.

MEASURED 2026-09-12: issue-autoclose logged `owned=0` every hour while two
workflow-failure issues stayed open after their workflows went green —
#3378 "[failover-canary] the failover drill is failing" (15 passes in a row)
and #4441 "Post-Deploy Smoke Test failing on main". `owned` required a
[workflow-failed]/[STATUS] title as well as the label; those two filers reuse
one issue with a title of their own and link the run that failed.
"""
import json
import os
import shutil
import subprocess

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "issue-autoclose.yml")
_NODE = shutil.which("node")
BASE = "https://github.com/azmartone67/dchub-backend/actions/runs/"

# The body failover-canary.yml writes (issue #3378, verbatim apart from line breaks).
CANARY_BODY = (
    f"The failover drill is failing. Latest run: {BASE}33306988924\n\n"
    "This issue is REUSED by later failures rather than duplicated, so it staying "
    "open means the cause is still unfixed. Close it when the cause is fixed, not "
    "when one run passes.")
# A comment post-deploy-smoke.yml bumps onto its reused issue (#4441).
SMOKE_COMMENT = (
    f"Smoke failed on `c97aeb5` — [run log]({BASE}34722558078).\n\n"
    "**Failing endpoint checks**\n\n_Bumped on each failing run. Close by hand once smoke is green._")

HARNESS = r"""
const DATA = JSON.parse(process.env.FIXTURE);
const calls = [];
const github = {
  paginate: async (fn, params) => (await fn(params)).data,
  rest: {
    actions: {
      listWorkflowRunsForRepo: async () => ({data: {workflow_runs: DATA.repoRuns || []}}),
      getWorkflowRun: async ({run_id}) => {
        calls.push({op: 'getWorkflowRun', run_id});
        const r = (DATA.runs || {})[String(run_id)];
        if (!r) throw new Error('no such run ' + run_id);
        return {data: r};
      },
      listWorkflowRuns: async (a) => {
        calls.push({op: 'listWorkflowRuns', workflow_id: a.workflow_id, branch: a.branch,
                    status: a.status, per_page: a.per_page});
        const runs = ((DATA.byWorkflow || {})[String(a.workflow_id)] || []).slice(0, a.per_page);
        return {data: {workflow_runs: runs}};
      },
    },
    issues: {
      listForRepo: async () => ({data: DATA.issues}),
      listComments: async (a) => ({data: (DATA.comments || {})[String(a.issue_number)] || []}),
      createComment: async (a) => { calls.push({op: 'comment', issue_number: a.issue_number, body: a.body}); },
      update: async (a) => { calls.push({op: 'update', issue_number: a.issue_number,
                                         state: a.state, state_reason: a.state_reason}); },
    },
  },
};
const core = {
  notice: () => {},
  warning: (m) => calls.push({op: 'warning', m: String(m)}),
  summary: {addHeading() { return this; }, addRaw() { return this; }, async write() {}},
};
const context = {repo: {owner: 'azmartone67', repo: 'dchub-backend'},
                 payload: {inputs: DATA.inputs || {}, repository: {default_branch: 'main'}}};
(async () => {
__SCRIPT__
})().then(() => process.stdout.write(JSON.stringify(calls)),
          (e) => { console.error(e); process.exit(1); });
"""


def _script():
    with open(WF, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    steps = [s for s in wf["jobs"]["autoclose"]["steps"] if "github-script" in str(s.get("uses"))]
    assert len(steps) == 1, steps
    return steps[0]["with"]["script"]


def _run(fixture):
    js = HARNESS.replace("__SCRIPT__", _script())
    proc = subprocess.run([_NODE, "-e", js], capture_output=True, text=True, encoding="utf-8",
                          timeout=60, env={"PATH": os.environ.get("PATH", ""),
                                           "FIXTURE": json.dumps(fixture)})
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _run_obj(run_id, conclusion, created, wf=91, name="failover-canary"):
    return {"id": run_id, "name": name, "workflow_id": wf, "status": "completed",
            "conclusion": conclusion, "created_at": created, "html_url": f"{BASE}{run_id}"}


FAILED_AT = "2026-09-09T00:54:51Z"
GREEN_AFTER = [_run_obj(3, "success", "2026-09-12T18:21:00Z"),
               _run_obj(2, "success", "2026-09-12T12:26:00Z"),
               _run_obj(1, "success", "2026-09-12T06:31:00Z")]


def _canary(since, *, labels=("workflow-failure",), body=CANARY_BODY, inputs=None):
    return {
        "issues": [{"number": 3378, "title": "[failover-canary] the failover drill is failing",
                    "body": body, "labels": [{"name": l} for l in labels],
                    "created_at": "2026-08-30T18:20:00Z"}],
        "comments": {},
        "runs": {"33306988924": _run_obj(33306988924, "failure", FAILED_AT)},
        "byWorkflow": {"91": since},
        "inputs": inputs or {},
    }


def _writes(calls, number=None):
    return [c for c in calls if c["op"] in ("update", "comment")
            and (number is None or c["issue_number"] == number)]


pytestmark = pytest.mark.skipif(_NODE is None, reason="node not available on this host")


def test_a_reused_issue_closes_after_three_passes_newer_than_its_failure():
    calls = _run(_canary(GREEN_AFTER))
    assert [c for c in calls if c["op"] == "update"] == [
        {"op": "update", "issue_number": 3378, "state": "closed", "state_reason": "completed"}]
    [comment] = [c for c in calls if c["op"] == "comment"]
    assert "failover-canary" in comment["body"] and "33306988924" in comment["body"]
    assert all(f"{BASE}{i}" in comment["body"] for i in (1, 2, 3))
    [lookup] = [c for c in calls if c["op"] == "listWorkflowRuns"]
    assert lookup == {"op": "listWorkflowRuns", "workflow_id": 91, "branch": "main",
                      "status": "completed", "per_page": 3}


@pytest.mark.parametrize("since", [
    [_run_obj(3, "success", "2026-09-12T18:21:00Z"), _run_obj(2, "success", "2026-09-12T12:26:00Z"),
     _run_obj(1, "failure", "2026-09-12T06:31:00Z")],
    [_run_obj(3, "success", "2026-09-12T18:21:00Z"), _run_obj(2, "success", "2026-09-12T12:26:00Z")],
    [_run_obj(3, "success", "2026-09-12T18:21:00Z"), _run_obj(2, "cancelled", "2026-09-12T12:26:00Z"),
     _run_obj(1, "success", "2026-09-12T06:31:00Z")],
], ids=["a-failure-in-the-last-three", "only-two-runs", "a-cancelled-run"])
def test_fewer_than_three_straight_passes_is_not_a_fix(since):
    """#3378's own text: close it when the cause is fixed, not when one run passes."""
    assert _writes(_run(_canary(since))) == []


def test_passes_older_than_the_failure_do_not_count():
    old = [_run_obj(3, "success", "2026-09-08T18:21:00Z"), _run_obj(2, "success", "2026-09-08T12:26:00Z"),
           _run_obj(1, "success", "2026-09-08T06:31:00Z")]
    assert _writes(_run(_canary(old))) == []


def test_the_failure_link_is_found_in_a_comment():
    fixture = {
        "issues": [{"number": 4441, "title": "Post-Deploy Smoke Test failing on main",
                    "body": "Post-deploy smoke failed on main.", "labels": [{"name": "workflow-failure"}],
                    "created_at": "2026-09-11T20:00:00Z"}],
        "comments": {"4441": [{"body": SMOKE_COMMENT}]},
        "runs": {"34722558078": _run_obj(34722558078, "failure", "2026-09-12T22:22:11Z", wf=92,
                                         name="Post-Deploy Smoke Test")},
        "byWorkflow": {"92": [_run_obj(9, "success", "2026-09-12T23:48:00Z", wf=92),
                              _run_obj(8, "success", "2026-09-12T23:24:00Z", wf=92),
                              _run_obj(7, "success", "2026-09-12T23:15:00Z", wf=92)]},
    }
    calls = _run(fixture)
    assert [c["issue_number"] for c in calls if c["op"] == "update"] == [4441]


def test_an_issue_with_no_run_link_is_left_alone():
    calls = _run(_canary(GREEN_AFTER, body="The failover drill is failing."))
    assert _writes(calls) == [] and not [c for c in calls if c["op"] == "getWorkflowRun"]


def test_a_never_label_is_left_alone():
    assert _writes(_run(_canary(GREEN_AFTER, labels=("workflow-failure", "keep")))) == []


def test_a_dry_run_writes_nothing():
    assert _writes(_run(_canary(GREEN_AFTER, inputs={"dry_run": "true"}))) == []


def test_a_titled_failure_issue_is_still_handled_once_by_the_original_arm():
    """Control: the titled arm is unchanged and a titled issue never reaches the
    reused arm, so it is not closed twice."""
    fixture = {
        "issues": [{"number": 500, "title": "[workflow-failed] CI on main",
                    "body": f"see {BASE}777", "labels": [{"name": "workflow-failure"}],
                    "created_at": "2026-09-10T00:00:00Z"}],
        "repoRuns": [{"id": 800, "name": "CI", "status": "completed", "conclusion": "success",
                      "created_at": "2026-09-11T00:00:00Z", "html_url": f"{BASE}800"}],
    }
    calls = _run(fixture)
    assert [c["issue_number"] for c in calls if c["op"] == "update"] == [500]
    assert not [c for c in calls if c["op"] == "getWorkflowRun"]


# The body kill-switch-probe.yml writes (issue #5270, verbatim). That filer
# labels `kill-switch-probe`, not `workflow-failure`, so the reused arm skipped
# it and a single transient edge 502 on the beat sat open with nothing to close it.
PROBE_BODY = (
    "kill-switch-probe FAILED: a switch is SET but NOT IN EFFECT, the beat was refused, "
    "or the probe could observe nothing. The per-switch table is in the run log: "
    f"{BASE}35793512081")


def _probe(since, *, labels=("kill-switch-probe",)):
    return {
        "issues": [{"number": 5270, "title": "[kill-switch-probe] set ≠ in effect at 2026-09-22T22:39Z",
                    "body": PROBE_BODY, "labels": [{"name": l} for l in labels],
                    "created_at": "2026-09-22T22:39:30Z"}],
        "comments": {},
        "runs": {"35793512081": _run_obj(35793512081, "failure", "2026-09-22T22:39:13Z", wf=93,
                                         name="kill-switch-probe")},
        "byWorkflow": {"93": [_run_obj(6, "success", "2026-09-24T02:47:21Z", wf=93),
                              _run_obj(5, "success", "2026-09-24T00:47:00Z", wf=93),
                              _run_obj(4, "success", "2026-09-23T22:47:00Z", wf=93)]},
    }


def test_a_kill_switch_probe_issue_closes_after_three_passes():
    calls = _run(_probe(None))
    assert [c for c in calls if c["op"] == "update"] == [
        {"op": "update", "issue_number": 5270, "state": "closed", "state_reason": "completed"}]


def test_a_kill_switch_probe_issue_stays_open_while_the_probe_is_red():
    fixture = _probe(None)
    fixture["byWorkflow"]["93"][1]["conclusion"] = "failure"
    assert _writes(_run(fixture)) == []


def test_a_kill_switch_probe_issue_with_a_never_label_is_left_alone():
    assert _writes(_run(_probe(None, labels=("kill-switch-probe", "keep")))) == []
