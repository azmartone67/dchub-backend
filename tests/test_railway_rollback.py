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
        # Wording changed 2026-07-29: the reason now itemises WHY each candidate
        # was rejected. "no rollback target" alone gave an operator nothing to
        # act on mid-outage.
        assert "no eligible rollback target" in reason
        assert "among 1 deployments" in reason

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


class TestAntiStackingGuard:
    """Refuse to roll back onto a deployment that only just became live.

    More than one actor reacts to the same incident — the worker sentinel runs
    on two processes, and auto-rollback.yml is armed as a backstop. Whoever
    acts second sees the freshly restored good image as "same commit as
    current", skips it, and rolls back to something OLDER. One correct
    rollback becomes an over-rollback.
    """

    T0 = 1_800_000_000.0  # fixed clock; the fixtures below are relative to it

    def at(self, offset_s):
        import datetime as _dt
        return _dt.datetime.fromtimestamp(
            self.T0 + offset_s, _dt.timezone.utc
        ).isoformat().replace("+00:00", "Z")

    def test_refuses_when_the_live_deployment_just_changed(self):
        deps = [
            dep("cur", "SUCCESS", "restored", created=self.at(-120)),   # 2 min old
            dep("older", "SUCCESS", "good111", created=self.at(-9000)),
        ]
        current, target, reason = rb.pick_rollback_target(deps, now=self.T0)
        assert target is None, "must not stack a rollback on a 2-minute-old deployment"
        assert current["id"] == "cur"
        assert "already rolled back" in reason or "just deployed" in reason

    def test_allows_once_the_live_deployment_has_settled(self):
        deps = [
            dep("cur", "SUCCESS", "bad0000", created=self.at(-1200)),   # 20 min old
            dep("older", "SUCCESS", "good111", created=self.at(-9000)),
        ]
        _, target, reason = rb.pick_rollback_target(deps, now=self.T0)
        assert target["id"] == "older"
        assert reason == "ok"

    def test_force_overrides_the_guard(self):
        deps = [
            dep("cur", "SUCCESS", "restored", created=self.at(-10)),
            dep("older", "SUCCESS", "good111", created=self.at(-9000)),
        ]
        _, blocked, _ = rb.pick_rollback_target(deps, now=self.T0)
        assert blocked is None
        _, forced, reason = rb.pick_rollback_target(deps, now=self.T0, min_current_age_s=0)
        assert forced["id"] == "older" and reason == "ok"

    def test_unparseable_timestamp_does_not_block(self):
        """Fail open on a timestamp we cannot read — never wedge a real rollback."""
        deps = [
            dep("cur", "SUCCESS", "bad0000", created="not-a-timestamp"),
            dep("older", "SUCCESS", "good111", created="also-bad"),
        ]
        _, target, reason = rb.pick_rollback_target(deps, now=self.T0)
        assert target["id"] == "older" and reason == "ok"

    @pytest.mark.parametrize("stamp,expected_ok", [
        ("2026-07-28T08:20:04.893Z", True),      # GraphQL ISO-8601 with Z
        ("2026-07-28 08:20:04.893", True),       # space-separated, as the CLI prints
        ("2026-07-28T08:20:04+00:00", True),     # explicit offset
        ("", False),
        (None, False),
        (12345, False),                          # non-string
    ])
    def test_timestamp_parsing(self, stamp, expected_ok):
        assert (rb._parse_ts(stamp) > 0) is expected_ok


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
def test_run_scripts_are_valid_bash(name):
    """Every `run:` script must actually parse as bash.

    A heredoc nested inside an if/else keeps the extra indentation after YAML
    strips the block indent, so its terminator never matches column 0 and the
    step dies with "unexpected end of file". That shipped in
    weekly-shadow-audit and only surfaced by dispatching the workflow: the
    YAML parses fine, and the failure is in the *generated* shell script.
    """
    import re
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")

    for i, script in enumerate(_run_scripts(WORKFLOWS / name)):
        # ${{ ... }} is interpolated by Actions before bash sees it; substitute
        # a harmless literal so it does not confuse the parser.
        cleaned = re.sub(r"\$\{\{[^}]*\}\}", "X", script)
        proc = subprocess.run(
            [bash, "-n"], input=cleaned, text=True, capture_output=True
        )
        assert proc.returncode == 0, (
            f"{name} run-script #{i} is not valid bash:\n{proc.stderr.strip()}"
        )


def _logical_commands(script: str):
    """Comment-stripped logical shell commands (backslash continuations joined).

    Only WHOLE-LINE comments are dropped. Splitting on the first `#` would
    truncate at the `#` inside a quoted PR title (`"revert: brain PR #123"`)
    and silently hide the rest of the command from these guards — which it did.
    """
    lines = [
        "" if ln.lstrip().startswith("#") else ln.rstrip()
        for ln in script.splitlines()
    ]
    out, buf = [], ""
    for ln in lines:
        if ln.endswith("\\"):
            buf += ln[:-1] + " "
            continue
        out.append(buf + ln)
        buf = ""
    if buf:
        out.append(buf)
    return out


def _pr_create_commands(path: pathlib.Path):
    return [
        cmd
        for script in _run_scripts(path)
        for cmd in _logical_commands(script)
        if "gh pr create" in cmd
    ]


@pytest.mark.parametrize("name", GIT_PUSH_WORKFLOWS)
def test_pr_create_exit_code_is_not_masked(name):
    """`gh pr create ... | tail` throws away the exit code.

    The rollback path recorded `outcome=opened` off a piped create, so a
    failure — e.g. `could not add label: 'auto-rollback' not found`, which is
    exactly what happened — would have been reported to the incident issue as
    a revert PR that did not exist.
    """
    for cmd in _pr_create_commands(WORKFLOWS / name):
        after = cmd.split("gh pr create", 1)[1]
        assert "|" not in after, (
            f"{name} pipes gh pr create, masking its exit code: {cmd.strip()!r}"
        )


@pytest.mark.parametrize("name", GIT_PUSH_WORKFLOWS)
def test_pr_create_does_not_require_a_label(name):
    """`--label` on create is fatal when the label is missing; label after."""
    for cmd in _pr_create_commands(WORKFLOWS / name):
        assert "--label" not in cmd, (
            f"{name} passes --label to gh pr create; if the label does not "
            "exist the whole command fails and no PR is opened. Use "
            f"`gh pr edit --add-label` afterwards instead: {cmd.strip()!r}"
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


# ── auth-header fallback ────────────────────────────────────────────────────
# Railway's token types do not share a header: account/team tokens use
# `Authorization: Bearer`, project tokens use `Project-Access-Token`. A project
# token sent as a Bearer returns a bare "Not Authorized" that says nothing about
# the header — 2026-07-28 that cost three trips to the Railway dashboard
# re-minting a token that had been correct all along.


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


def _install_fake_post(monkeypatch, accepts):
    """Fake requests.post that only accepts one auth style. Records attempts."""
    seen = []

    def fake_post(url, json=None, headers=None, timeout=None):
        if "Authorization" in headers:
            style = "bearer"
        elif "Project-Access-Token" in headers:
            style = "project"
        else:
            style = "none"
        seen.append(style)
        if style != accepts:
            return _FakeResp({"errors": [{"message": "Not Authorized"}]})
        return _FakeResp({"data": {"ok": True}})

    monkeypatch.setattr(rb.requests, "post", fake_post)
    monkeypatch.setattr(rb, "_auth_style", None, raising=False)
    return seen


@pytest.mark.parametrize("accepts", ["bearer", "project"])
def test_gql_succeeds_with_either_token_type(monkeypatch, accepts):
    seen = _install_fake_post(monkeypatch, accepts)
    assert rb.gql("query{x}", {}, "tok") == {"ok": True}
    assert seen[-1] == accepts, f"did not end on the accepted style: {seen}"


def test_project_token_falls_back_after_bearer_is_refused(monkeypatch):
    """★ The regression this exists for: Bearer first, project second."""
    seen = _install_fake_post(monkeypatch, "project")
    rb.gql("query{x}", {}, "tok")
    assert seen == ["bearer", "project"], f"expected both styles tried in order, got {seen}"


def test_working_style_is_cached_so_later_calls_cost_one_request(monkeypatch):
    seen = _install_fake_post(monkeypatch, "project")
    rb.gql("query{x}", {}, "tok")
    before = len(seen)
    rb.gql("query{x}", {}, "tok")
    assert len(seen) - before == 1, f"cached style not reused: {seen}"


def test_a_non_auth_error_is_raised_immediately_not_retried(monkeypatch):
    """A bad query must not be reported as an auth-header problem."""
    seen = []

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.append("call")
        return _FakeResp({"errors": [{"message": "Cannot query field 'nope'"}]})

    monkeypatch.setattr(rb.requests, "post", fake_post)
    monkeypatch.setattr(rb, "_auth_style", None, raising=False)
    with pytest.raises(rb.RailwayError, match="Cannot query field"):
        rb.gql("query{nope}", {}, "tok")
    assert len(seen) == 1, "a non-auth error was retried against the other header"


def test_both_styles_refused_names_both_headers(monkeypatch):
    """MUST-FAIL control: when nothing works, the error has to say so."""
    _install_fake_post(monkeypatch, "nothing-accepts-this")
    with pytest.raises(rb.RailwayError) as exc:
        rb.gql("query{x}", {}, "tok")
    msg = str(exc.value)
    assert "Not Authorized" in msg
    assert "bearer" in msg and "project" in msg, f"error hides what was tried: {msg}"


# ── drill mode must rehearse, and must not authorise a real rollback ────────

def _rollback_step_script():
    wf = yaml.safe_load((WORKFLOWS / "auto-rollback.yml").read_text())
    job = wf["jobs"]["check-and-rollback"]
    step = next(s for s in job["steps"] if "railway_rollback.py" in (s.get("run") or ""))
    return step["run"]


def test_drill_bypasses_the_anti_stacking_guard():
    """A dry run rolls nothing back, so the age guard only blocks the rehearsal.

    Without --force a drill returns "live deployment is only Ns old — refusing
    to stack" and never exercises target selection. Waiting it out is not
    available: main merges several times an hour and each merge resets the clock.
    """
    run = _rollback_step_script()
    assert "--dry-run --force" in run, \
        "drill mode must pass --force so it can resolve a target on a fresh deployment"


def test_force_is_never_passed_without_dry_run():
    """★ MUST-FAIL control. --force alone authorises a real rollback that
    ignores the anti-stacking guard — the exact double-rollback the guard was
    added to prevent. It is only ever safe coupled to --dry-run."""
    run = _rollback_step_script()
    for line in run.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "--force" not in stripped:
            continue
        assert "--dry-run" in stripped, \
            f"--force appears without --dry-run, authorising a real forced rollback: {stripped!r}"


# ── the REMOVED-status bug ──────────────────────────────────────────────────
# Railway marks a deployment REMOVED the moment a newer one supersedes it, so
# exactly one deployment is ever SUCCESS: the live one. Selecting targets on
# status == "SUCCESS" therefore matched nothing, ever. Measured 2026-07-29 over
# 20 real deployments spanning 18h: one SUCCESS (live), the rest REMOVED/FAILED.

def test_rolls_back_to_a_removed_deployment():
    """★ The regression. Real-world shape: live SUCCESS, older ones REMOVED."""
    deps = [
        dep("live", "SUCCESS", "aaa1111"),
        dep("prev", "REMOVED", "bbb2222"),
    ]
    current, target, reason = rb.pick_rollback_target(deps)
    assert target is not None, f"no target chosen from a realistic list: {reason}"
    assert target["id"] == "prev"
    assert reason == "ok"


def test_failed_builds_are_never_a_rollback_target():
    """REMOVED means 'served then superseded'. FAILED never served."""
    deps = [
        dep("live", "SUCCESS", "aaa1111"),
        dep("bad", "FAILED", "bbb2222"),
    ]
    _, target, reason = rb.pick_rollback_target(deps)
    assert target is None
    assert "never-good" in reason


def test_unretained_images_are_skipped_and_counted():
    deps = [
        dep("live", "SUCCESS", "aaa1111"),
        dep("gone", "REMOVED", "bbb2222", can_rollback=False),
    ]
    _, target, reason = rb.pick_rollback_target(deps)
    assert target is None
    assert "1 image no longer retained" in reason, reason


def test_rejection_reason_is_actionable_not_just_no_target():
    """During an outage 'no rollback target' alone gives nobody anything to do."""
    deps = [
        dep("live", "SUCCESS", "aaa1111"),
        dep("bad", "FAILED", "bbb2222"),
        dep("gone", "REMOVED", "ccc3333", can_rollback=False),
        dep("same", "REMOVED", "aaa1111"),
    ]
    _, target, reason = rb.pick_rollback_target(deps)
    assert target is None
    for fragment in ("never-good", "no longer retained", "same commit"):
        assert fragment in reason, f"reason hides the {fragment!r} rejections: {reason}"
