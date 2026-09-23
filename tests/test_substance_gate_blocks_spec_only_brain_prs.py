"""A scaffold-only PR on a `brain-spec/*` branch FAILS the substance gate
unless the owner labels it `spec-land-approved`.

Why: the gate was advisory on the premise that a human reads its comment
before merging. Measured 2026-09-21/22 that premise was false — 106 of 111
brain-spec merges in September changed only docs/, the human rejection rate
was 0.0%, and every docs-only merge counted as "shipped" while its finding
re-fired. substance-gate is a required check on main, so `failure` blocks.

Runs the workflow's own step script against real git, reusing the harness in
test_substance_gate_diffs_against_the_merge_base.py.
"""
import pytest

from tests.test_substance_gate_diffs_against_the_merge_base import (
    GATE_STEP, REAL, SCAFFOLD, Repo, _comment, _steps, _verdict, WF,
)

import yaml

# The spec opener's own body: carries the SPEC-ONLY marker and the
# "or close this PR" boilerplate the claim regex would otherwise match.
SPEC_BODY = "SPEC-ONLY — this PR records a spec, it is not a fix. Implement it, or close this PR."
SPEC_REF = "brain-spec/inv-100695-qa-critical-1-public-pa"
BLOCKED = ("failure",
           "brain-spec PR changes no running code — implement, close, or label spec-land-approved")


def _run(tmp_path, files, **env):
    repo = Repo(tmp_path)
    base, head = repo.behind_main(files)
    env = {"PR_BODY": SPEC_BODY, "PR_TITLE": "[brain-spec] inv #100695: qa_critical",
           "HEAD_REF": SPEC_REF, "PR_LABELS": "", **env}
    proc, calls = repo.run(GATE_STEP, base, head, **env)
    assert proc.returncode == 0, proc.stderr
    return calls


def test_a_spec_only_brain_spec_pr_is_blocked(tmp_path):
    calls = _run(tmp_path, SCAFFOLD)
    assert _verdict(calls) == BLOCKED
    comment = _comment(calls)
    assert comment.startswith("🛑 **Substance gate: brain-spec PR changes no running code**")
    assert "`spec-land-approved`" in comment


@pytest.mark.parametrize("labels", ["spec-land-approved", "brain,spec-land-approved,docs"])
def test_the_owner_label_lands_it_as_neutral(tmp_path, labels):
    calls = _run(tmp_path, SCAFFOLD, PR_LABELS=labels)
    assert _verdict(calls) == ("neutral", "Scaffold-only PR — changes no running code")
    assert "Landing approved by the `spec-land-approved` label." in _comment(calls)


@pytest.mark.parametrize("labels", ["spec-land-approved-later", "not-spec-land-approved", "spec-land"])
def test_a_near_miss_label_does_not_approve(tmp_path, labels):
    assert _verdict(_run(tmp_path, SCAFFOLD, PR_LABELS=labels)) == BLOCKED


@pytest.mark.parametrize("ref", ["brain/spec-loop-actuation", "docs/brain-spec-notes",
                                 "feature/brain-spec/x", ""])
def test_other_branches_keep_the_advisory_neutral(tmp_path, ref):
    calls = _run(tmp_path, SCAFFOLD, HEAD_REF=ref)
    assert _verdict(calls) == ("neutral", "Scaffold-only PR — changes no running code")


def test_a_brain_spec_pr_that_changes_real_code_passes(tmp_path):
    calls = _run(tmp_path, REAL)
    assert _verdict(calls) == ("success", "Changes real code (1 file(s))")


def test_a_fix_claim_keeps_its_own_more_specific_failure(tmp_path):
    calls = _run(tmp_path, SCAFFOLD, PR_BODY="Fixes the qa_critical finding.")
    assert _verdict(calls) == ("failure", "Scaffold-only PR CLAIMS a fix — changes no running code")


def test_adding_or_removing_the_label_reruns_the_gate():
    wf = yaml.safe_load(WF.read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as boolean True.
    types = (wf.get("on") or wf.get(True))["pull_request"]["types"]
    assert {"labeled", "unlabeled"} <= set(types)


def test_the_step_reads_ref_and_labels_from_the_event():
    step = [s for s in _steps() if s.get("name") == GATE_STEP][0]
    assert step["env"]["HEAD_REF"] == "${{ github.event.pull_request.head.ref }}"
    assert step["env"]["PR_LABELS"] == "${{ join(github.event.pull_request.labels.*.name, ',') }}"
