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
the GATING line out of main-branch-health.yml — the job that decides whether
main is green — and asserts every workflow it checks is also triaged on
failure. Add a gating workflow to one and the fence makes you add it to the
other.
"""
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")
HEALTH = os.path.join(WF, "main-branch-health.yml")
TRIAGE = os.path.join(WF, "ci-triage.yml")


def _executable_body(path):
    """The workflow's run-script lines with COMMENTS STRIPPED.

    ★ Why this exists. The first cut of test_the_health_job_actually_scopes_to_main
    asserted `"--branch main" in body` against the raw file — and SURVIVED a
    mutation that deleted the flag from the code, because the comment directly
    above it explains why --branch main matters and still contained the string.
    The assertion matched its own explanation. That trap has now appeared five
    times in this audit, and this is the one I wrote myself.
    """
    with open(path, encoding="utf-8") as fh:
        return "\n".join(l for l in fh.read().splitlines()
                          if not l.lstrip().startswith("#"))


def _gating_workflow_files():
    """The workflow FILES main-branch-health treats as gating."""
    with open(HEALTH, encoding="utf-8") as fh:
        m = re.search(r'^\s*GATING="([^"]+)"', fh.read(), re.M)
    assert m, ("main-branch-health.yml no longer declares a GATING list — this "
               "fence binds to it and cannot check anything without it.")
    return m.group(1).split()


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
    a failing feature branch would redden the board while main was fine."""
    body = _executable_body(HEALTH)
    assert "--branch main" in body, (
        "main-branch-health.yml must scope its run query to main. An unscoped "
        "`gh run list` counts PR runs, which is why pre-merge.yml is not simply "
        "added to tools/deadman/watch.py instead.")


def test_an_unreadable_result_is_not_reported_as_green():
    """`could not check` has never been `clean` in this repo."""
    body = _executable_body(HEALTH)
    assert 'STATUS="unmeasured"' in body, (
        "main-branch-health must distinguish 'every gating workflow is green' "
        "from 'no gating workflow could be read'. Collapsing the second into "
        "the first is the failure this whole audit has been about.")
