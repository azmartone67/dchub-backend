"""A red main blocks everyone, so something must say so without being asked.

★ 2026-08-30. main went RED TWICE in one day — a fence pair hard-coding
opposite tables, then a unit test asserting against glama.ai's live web page —
and 11 of 12 pre-merge runs on main failed. BOTH were found cold, by hand,
while watching unrelated PRs. A red main blocks every open PR in the repo,
including other people's, and nothing announced it anywhere.

Nothing was broken. ci-triage.yml already existed for exactly this, and its own
header says it runs so failures arrive pre-diagnosed "instead of the founder
finding a red run cold". Its watch list simply did not include the workflow
whose failure actually stops the repo: pre-merge-gauntlet, which carries
unit-tests, substance-gate and db-parity.

★ THE INVARIANT, and why it is derived rather than hard-coded. Listing the
gating workflows in two places would rot the moment one changed. So this reads
the GATING declaration out of the job that decides whether main is green, and
asserts every workflow it checks is also triaged on failure. Add a gating
workflow to one and the fence makes you add it to the other.

★ 2026-08-31 — THE DECLARATION MOVED, SO THIS FENCE MOVED WITH IT. The verdict
logic left inline YAML for tools/deadman/main_branch_verdict.py so it could be
tested (main-branch-health was answering with a commit HEAD had already
replaced, and beat a false `main_red` to the public board). This fence now binds
to GATING in that module. Following the declaration is the point; copying it
here would recreate exactly the two-places rot the invariant exists to stop.

★ Two of these assertions are now BEHAVIOURAL — they call the code instead of
grepping its text. The helper they used to share existed because an assertion
matched its own explanatory comment and survived a mutation that deleted the
flag it was checking. Text assertions against a module with real docstrings
would walk straight back into that: the word "unmeasured" appears in prose that
describes the behaviour. Calling the function cannot match prose.
"""
import os

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")
HEALTH = os.path.join(WF, "main-branch-health.yml")
TRIAGE = os.path.join(WF, "ci-triage.yml")


def _verdict_module():
    """tools/deadman/main_branch_verdict.py — where the verdict now lives."""
    import importlib.util

    path = os.path.join(ROOT, "tools", "deadman", "main_branch_verdict.py")
    assert os.path.exists(path), (
        "the verdict module is gone — main-branch-health cannot decide anything "
        "and this fence has nothing to bind to")
    spec = importlib.util.spec_from_file_location("main_branch_verdict", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _gating_workflow_files():
    """The workflow FILES main-branch-health treats as gating."""
    gating = list(getattr(_verdict_module(), "GATING", ()))
    assert gating, ("the verdict module no longer declares GATING — this fence "
                    "binds to it and cannot check anything without it.")
    return gating


def _workflow_name(fn):
    with open(os.path.join(WF, fn), encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("name")


def _triaged_names():
    with open(TRIAGE, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    on = doc.get(True) or doc.get("on") or {}
    return set((on.get("workflow_run") or {}).get("workflows") or [])


def test_every_gating_workflow_is_triaged_when_it_fails():
    triaged = _triaged_names()
    missing = []
    for fn in _gating_workflow_files():
        path = os.path.join(WF, fn)
        if not os.path.exists(path):
            missing.append(f"{fn} (named in GATING but the file is gone)")
            continue
        name = _workflow_name(fn)
        if name not in triaged:
            missing.append(f"{fn} -> name {name!r}")
    assert not missing, (
        "these workflows decide whether main is green, but ci-triage.yml does "
        "not watch them — so their failure arrives cold, which is exactly how "
        "main stayed red for most of 2026-08-30:\n"
        + "".join(f"  {m}\n" for m in missing)
        + "\nci-triage matches on a workflow's `name:`, not its filename.")


def test_the_health_job_actually_scopes_to_main():
    """Without --branch main this reports on PR runs and is worse than nothing:
    a failing feature branch would redden the board while main was fine.

    Asserted by CALLING collect() with a recording stub, not by grepping for the
    flag — the string appears in the module's own prose, so a text assertion
    would pass with the flag deleted."""
    m = _verdict_module()
    calls = []

    def fake_gh(args):
        calls.append(list(args))
        return "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n" if args[:1] == ["api"] else "[]"

    m._gh = fake_gh
    m.collect("azmartone67/dchub-backend")

    queries = [a for a in calls if a[:2] == ["run", "list"]]
    assert queries, "no run query was issued at all — the monitor reads nothing"
    for a in queries:
        assert "--branch" in a and a[a.index("--branch") + 1] == "main", (
            "main-branch-health must scope its run query to main. An unscoped "
            "`gh run list` counts PR runs, which is why pre-merge.yml is not "
            "simply added to tools/deadman/watch.py instead. Got: %r" % (a,))


def test_an_unreadable_result_is_not_reported_as_green():
    """`could not check` has never been `clean` in this repo.

    Behavioural for the same reason as above: "unmeasured" appears in the
    module's docstrings, so `in body` would hold even if the branch were gone."""
    m = _verdict_module()
    status, _note = m.verdict("deadbeef", {wf: [] for wf in m.GATING})
    assert status == "unmeasured", (
        "main-branch-health must distinguish 'every gating workflow is green' "
        "from 'no gating workflow could be read'. Collapsing the second into "
        "the first is the failure this whole audit has been about. Got %r"
        % (status,))
