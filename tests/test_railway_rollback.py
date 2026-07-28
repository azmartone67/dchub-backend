"""Tests for scripts/railway_rollback.py — the SLO gate's rollback path.

The target-selection logic is the part that decides what production gets
rolled back to, so it is tested against deployment lists shaped exactly like
the ones the live Railway API returns (captured 2026-07-28 from the
dchub-backend production service).
"""

import importlib.util
import pathlib
import sys

import pytest
import yaml

WORKFLOWS = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"

_SPEC = importlib.util.spec_from_file_location(
    "railway_rollback",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "railway_rollback.py",
)
rb = importlib.util.module_from_spec(_SPEC)
sys.modules["railway_rollback"] = rb
_SPEC.loader.exec_module(rb)


def dep(dep_id, status, sha, can_rollback=True, created="2026-07-28T08:00:00Z"):
    return {
        "id": dep_id,
        "status": status,
        "createdAt": created,
        "canRollback": can_rollback,
        "meta": {"commitHash": sha},
    }


# Shaped like the real response: newest first, one live deployment, the rest
# REMOVED (Railway tears down superseded deployments).
REAL_SHAPE = [
    dep("d58dd37b-dffc-4244-abad-978b3bce676a", "BUILDING", "98f704d629fbef8645514e4978a7a43262a7e9c9"),
    dep("27fae8c2-8fd6-45a8-871d-424ed03d0e7b", "SUCCESS", "cefef4c7f35d21938d5f144bdff214ffdc492c2d"),
    dep("8fcd1231-d0df-4c7d-a3f6-5b470556998b", "REMOVED", "9d094891cdd109dec158735d3cffa0147ee25e06"),
    dep("81357971-8ff9-4016-b4dc-92d52c2d76ee", "REMOVED", "2e3525a3381efefeb5d7783d14086711fc4df808"),
]


class TestPickRollbackTarget:
    def test_picks_newest_older_success(self):
        deps = [
            dep("cur", "SUCCESS", "bad0000"),
            dep("prev", "SUCCESS", "good111"),
            dep("older", "SUCCESS", "good222"),
        ]
        current, target, reason = rb.pick_rollback_target(deps)
        assert current["id"] == "cur"
        assert target["id"] == "prev", "must pick the most recent good build, not the oldest"
        assert reason == "ok"

    def test_rolls_back_from_a_still_building_bad_deploy(self):
        """The live deployment can be BUILDING/DEPLOYING during an outage."""
        current, target, reason = rb.pick_rollback_target(REAL_SHAPE)
        assert current["id"].startswith("d58dd37b")
        assert target["id"].startswith("27fae8c2")
        assert rb.commit_sha(target).startswith("cefef4c7")

    def test_skips_deployments_past_image_retention(self):
        """canRollback=False means Railway no longer has the image."""
        deps = [
            dep("cur", "SUCCESS", "bad0000"),
            dep("expired", "SUCCESS", "good111", can_rollback=False),
            dep("kept", "SUCCESS", "good222", can_rollback=True),
        ]
        _, target, _ = rb.pick_rollback_target(deps)
        assert target["id"] == "kept"

    def test_skips_non_success_history(self):
        deps = [
            dep("cur", "SUCCESS", "bad0000"),
            dep("crashed", "CRASHED", "bad1111"),
            dep("failed", "FAILED", "bad2222"),
            dep("good", "SUCCESS", "good333"),
        ]
        _, target, _ = rb.pick_rollback_target(deps)
        assert target["id"] == "good"

    def test_refuses_target_with_same_commit_as_current(self):
        """Redeploying the same SHA is a no-op — it would 'succeed' and fix nothing."""
        deps = [
            dep("cur", "SUCCESS", "bad0000"),
            dep("redeploy_of_bad", "SUCCESS", "bad0000"),
            dep("actually_good", "SUCCESS", "good111"),
        ]
        _, target, _ = rb.pick_rollback_target(deps)
        assert target["id"] == "actually_good"

    def test_never_rolls_forward_to_a_newer_deployment(self):
        """Entries newer than the live one must not be selected."""
        deps = [
            dep("newer_failed", "FAILED", "newer00"),
            dep("newer_success", "SUCCESS", "newer11"),
            dep("cur", "DEPLOYING", "bad0000"),
            dep("prev", "SUCCESS", "good111"),
        ]
        current, target, _ = rb.pick_rollback_target(deps)
        # 'newer_success' is above the live deployment in the list, so it is a
        # roll-forward, not a rollback.
        assert current["id"] == "newer_success"
        assert target["id"] == "prev"

    def test_no_target_when_only_one_deployment(self):
        _, target, reason = rb.pick_rollback_target([dep("only", "SUCCESS", "abc")])
        assert target is None
        assert "no older SUCCESS deployment" in reason

    def test_no_target_when_history_empty(self):
        current, target, reason = rb.pick_rollback_target([])
        assert (current, target) == (None, None)
        assert "no deployments" in reason

    def test_no_live_deployment_is_reported_not_crashed(self):
        deps = [dep("a", "REMOVED", "x"), dep("b", "REMOVED", "y")]
        _, target, reason = rb.pick_rollback_target(deps)
        assert target is None
        assert "no live deployment" in reason

    def test_all_retained_builds_are_the_same_commit(self):
        deps = [dep("cur", "SUCCESS", "same"), dep("p1", "SUCCESS", "same")]
        _, target, reason = rb.pick_rollback_target(deps)
        assert target is None
        assert "same commit" in reason


class TestCommitSha:
    @pytest.mark.parametrize("meta,expected", [
        ({"commitHash": "abc123"}, "abc123"),
        ({"commitSHA": "def456"}, "def456"),
        ('{"commitHash": "json789"}', "json789"),   # meta sometimes arrives as a JSON string
        ({}, ""),
        (None, ""),
        ("not json at all", ""),
    ])
    def test_extracts_or_degrades(self, meta, expected):
        assert rb.commit_sha({"meta": meta}) == expected

    def test_missing_sha_does_not_block_selection(self):
        """Deployments from image sources have no commit; rollback must still work."""
        deps = [
            {"id": "cur", "status": "SUCCESS", "canRollback": True, "meta": {}},
            {"id": "prev", "status": "SUCCESS", "canRollback": True, "meta": {}},
        ]
        _, target, reason = rb.pick_rollback_target(deps)
        assert target["id"] == "prev"
        assert reason == "ok"


class TestMutationContract:
    """Guard the contract the published Railway docs get wrong.

    The docs show `deploymentRollback(id: $id) { id status }`. The live schema
    returns Boolean!, so a sub-selection fails GraphQL validation — during an
    outage, on a code path nothing had ever exercised.
    """

    def test_rollback_mutation_has_no_sub_selection(self):
        mutation = rb._ROLLBACK_M
        assert "deploymentRollback(id: $id)" in mutation
        body = mutation.split("deploymentRollback(id: $id)", 1)[1]
        assert "{" not in body, (
            "deploymentRollback returns Boolean! — a sub-selection makes the "
            "mutation fail GraphQL validation"
        )

    def test_deployments_query_requests_can_rollback(self):
        assert "canRollback" in rb._DEPLOYMENTS_Q, (
            "target selection depends on canRollback; it must be requested"
        )


# ── Guards on the workflows themselves ────────────────────────────────────────
# These encode the two properties that were actually broken in production. They
# are asserted on the parsed YAML, not by grepping source text, so a comment
# mentioning `git push origin main` cannot satisfy them.

GIT_PUSH_WORKFLOWS = [
    "auto-rollback.yml",
    "weekly-shadow-audit.yml",
    "brain-pr-post-merge-guard.yml",
]


def _run_scripts(path: pathlib.Path):
    """Every `run:` script in a workflow, as a list of strings."""
    doc = yaml.safe_load(path.read_text())
    assert doc, f"{path.name} parsed to nothing"
    assert "jobs" in doc, f"{path.name} has no jobs"
    out = []
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if isinstance(step.get("run"), str):
                out.append(step["run"])
    assert out, f"{path.name} has no run: steps — did the parse silently degrade?"
    return out


@pytest.mark.parametrize("name", GIT_PUSH_WORKFLOWS)
def test_workflow_parses(name):
    """A workflow that does not parse does not run at all."""
    doc = yaml.safe_load((WORKFLOWS / name).read_text())
    assert doc and doc.get("jobs"), f"{name} is not a valid workflow"


@pytest.mark.parametrize("name", GIT_PUSH_WORKFLOWS)
def test_no_bot_push_to_main(name):
    """None of these may push to main — the bot is not an admin, so GH006.

    Checks the executable script bodies, with comments stripped, so the
    explanatory comments about the old `git push origin main` do not count.
    """
    for script in _run_scripts(WORKFLOWS / name):
        code = "\n".join(
            line for line in script.splitlines() if not line.lstrip().startswith("#")
        )
        assert "push origin main" not in code, (
            f"{name} still pushes to main; branch protection rejects that (GH006)"
        )
        assert "push origin HEAD:main" not in code, f"{name} still pushes to main"


def test_auto_rollback_alert_cannot_be_skipped():
    """The alert must survive a failing remediation step.

    Before the fix the issue step was `if: steps.decide.outputs.rollback ==
    'true'`, so when the push to main failed the job died and the alert was
    SKIPPED — no rollback and no alert. Verified against real Actions runs:
    with the old condition the alert reports `skipped`; with `!cancelled()` it
    runs and can still read the failed step's outputs.
    """
    doc = yaml.safe_load((WORKFLOWS / "auto-rollback.yml").read_text())
    steps = doc["jobs"]["check-and-rollback"]["steps"]
    alert = next((s for s in steps if s.get("name") == "Open issue"), None)
    assert alert is not None, "auto-rollback has no 'Open issue' alert step"
    cond = str(alert.get("if", ""))
    assert "cancelled()" in cond or "always()" in cond, (
        "the alert step must run even when a remediation step fails, or a real "
        "burn produces no rollback AND no alert"
    )


def test_auto_rollback_remediation_steps_do_not_abort_the_job():
    """Remediation must not kill the job before the alert runs."""
    doc = yaml.safe_load((WORKFLOWS / "auto-rollback.yml").read_text())
    steps = doc["jobs"]["check-and-rollback"]["steps"]
    for name in ("Roll back Railway to last good deployment", "Open revert PR"):
        step = next((s for s in steps if s.get("name") == name), None)
        assert step is not None, f"auto-rollback lost its '{name}' step"
        assert step.get("continue-on-error") is True, (
            f"'{name}' must be continue-on-error so the alert step still runs"
        )


@pytest.mark.parametrize("name", GIT_PUSH_WORKFLOWS)
def test_no_push_failure_is_swallowed(name):
    """`git push ... || true` is what hid two weeks of no-op runs.

    Matched per-line against `git push` specifically, so unrelated uses of
    `|| true` — and the phrase appearing in PR-body prose — do not trip it.
    """
    for script in _run_scripts(WORKFLOWS / name):
        for line in script.splitlines():
            code = line.split("#", 1)[0]
            if "git push" in code:
                assert "|| true" not in code and "|| :" not in code, (
                    f"{name} swallows a push failure: {line.strip()!r} — that is how "
                    "SHADOWED-ROUTES.md silently stopped updating after 2026-07-13"
                )
