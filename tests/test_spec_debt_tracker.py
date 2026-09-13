#!/usr/bin/env python3
"""tests/test_spec_debt_tracker.py — a merged spec must leave an obligation behind.

NO NETWORK. Parses the workflow and exercises its shell logic against the real
body of PR #2448.

MEASURED 2026-08-13: 60 of 60 merged [brain-spec] PRs carry an unchecked human
checklist. Not one was ever completed.

The substance gate is not the hole. Those PRs declare **SPEC-ONLY** and say in
their own words that they change no running code and are not a fix; the gate
returns `neutral`, which is correct. Blocking them would re-break what
r-spec-honesty (2026-07-18) fixed, when honest spec PRs were classified as
fix-claims and had to be bypassed by hand.

The hole is MERGE: a spec PR is a proposal with a to-do list, and merging
closes it, so the list stops being anywhere a human looks. This tracker
re-files that list as an issue. It invents no judgement — it relocates a
human-authored checklist somewhere it survives.

Run standalone:   python3 tests/test_spec_debt_tracker.py
Run under pytest: pytest tests/test_spec_debt_tracker.py
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "brain-spec-debt-tracker.yml")

# The real checklist shipped by every brain-spec PR (from #2448).
REAL_BODY = """<!-- fingerprint:fc2e334e4284273ee058ba41d5e29ff1 -->
**SPEC-ONLY** — this PR changes no running code and is not a fix.

## Human checklist

- [ ] Confirm this is still worth doing
- [ ] Scope it to a concrete change (file(s) + approach)
- [ ] Implement + verify
- [ ] Or discard this PR if superseded / not worth it
"""


def _wf():
    import yaml
    with open(WF, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _tracker_step():
    """The step that carries the tracker's shell.

    Found by CONTENT, not by index. This was `steps[0]` until 2026-09-01, when
    the job gained an `actions/checkout` first step (the ledger beat needs
    scripts/gate_beat.sh on disk) and five tests here died with KeyError: 'run'
    on a `uses:` step. An index is a claim about step ORDER that these tests
    never meant to make.
    """
    hits = [
        st for st in _wf()["jobs"]["file-spec-debt"]["steps"]
        if "spec-debt-body.md" in (st.get("run") or "")
    ]
    # EXACTLY one, and it fails loudly on ambiguity rather than taking the
    # first. A looser predicate ("mentions BRAIN_SPEC_DEBT_DISABLE") also
    # matches the ledger beat step, which names the switch in its own note —
    # so it would have returned the right step only because that step happens
    # to come first, which is the order-dependence this helper exists to remove.
    assert len(hits) == 1, (
        "expected exactly one step carrying the tracker shell, found %d: %r"
        % (len(hits), [h.get("name") for h in hits])
    )
    return hits[0]


def _run_block():
    return _tracker_step()["run"]


def _run_code():
    """The run block with comment lines removed.

    Three separate guards in this session matched PROSE describing a bug
    instead of the bug: a comment naming a removed call, a docstring naming a
    removed behaviour, and — here — the phrase "gh issue create --label fails"
    inside an explanatory comment. Scanning source for a command means
    scanning the COMMANDS.
    """
    return "\n".join(l for l in _run_block().splitlines()
                      if not l.lstrip().startswith("#"))


def test_workflow_is_valid_yaml_and_shell():
    run = _run_block()
    assert run, "the job must have a run block"
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(run)
        path = fh.name
    try:
        r = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        assert r.returncode == 0, f"shell syntax error: {r.stderr}"
    finally:
        os.unlink(path)


_TITLE_GATE_RE = re.compile(
    r"startsWith\(\s*github\.event\.pull_request\.title\s*,\s*'(\[brain-[^']*)'\s*\)")


def _title_gate_prefix() -> str:
    """The namespace prefix the job gate keys on, or fail loudly."""
    cond = " ".join(str(_wf()["jobs"]["file-spec-debt"]["if"]).split())
    m = _TITLE_GATE_RE.search(cond)
    assert m, f"must match on the PR title prefix; got: {cond}"
    return m.group(1)


def test_fires_only_on_merged_brain_prs():
    cond = " ".join(str(_wf()["jobs"]["file-spec-debt"]["if"]).split())
    assert "merged == true" in cond, "must not fire on a closed-unmerged PR"
    # Assert the TITLE is what is matched. A weaker check ("[brain-spec]" is
    # somewhere in the condition) passed when the field was swapped to .body,
    # which would fire on any PR merely mentioning a spec. That protection is
    # unchanged — _TITLE_GATE_RE still pins the FIELD.
    #
    # ★ 2026-08-31 — the PREFIX widened from '[brain-spec]' to '[brain-'. It
    # watched one of the three paths that open brain scaffold PRs, so 9 merged
    # [brain-l6 strategic-draft] PRs went untracked, 0 of 9. The gate is now the
    # namespace ROOT so the next producer is covered by default. This test
    # therefore pins the SHAPE (title field + a [brain- prefix), not one literal
    # namespace — pinning the literal is what made the miss invisible.
    assert _title_gate_prefix().startswith("[brain-")


def test_the_gate_does_not_sweep_in_ordinary_prs():
    """The widened prefix must still exclude human PRs, which often carry
    checklists of their own — otherwise every one of them files spec debt."""
    prefix = _title_gate_prefix()
    for title in ("fix(seo): the GSC blueprint never registered",
                  "chore: weekly shadow route inventory refresh",
                  "Add DC Hub — data-center intelligence MCP server",
                  "[spec-debt] inv #100399: story links"):
        assert not title.startswith(prefix), (
            f"{title!r} would be swept in by the gate prefix {prefix!r}")


def test_the_gate_covers_every_brain_pr_opener():
    """Each PR-opening path's title prefix, asserted against the live gate.

    The 2026-08-31 miss was invisible because nothing enumerated the producers
    in one place. This is that place: add a producer, add it here.
    """
    prefix = _title_gate_prefix()
    for opener, title_prefix in (
            ("routes/brain_pr_opener.py",         "[brain-spec]"),
            ("routes/brain_strategic_planner.py", "[brain-l6 strategic-draft]"),
            ("routes/brain_backlog_admin.py",     "[brain-l5 draft]")):
        assert title_prefix.startswith(prefix), (
            f"{opener} opens PRs titled {title_prefix!r}, which the spec-debt "
            f"gate prefix {prefix!r} does not match — its merged obligations "
            f"would vanish untracked")


def test_an_unchecked_checklist_is_detected():
    """Positive case, against the real shipped body."""
    assert re.search(r"^- \[ \]", REAL_BODY, re.MULTILINE), (
        "the fixture no longer resembles a real spec PR — re-check the format"
    )
    items = re.findall(r"^- \[ \].*$", REAL_BODY, re.MULTILINE)
    assert len(items) == 4, f"expected the 4-item human checklist, got {items}"


def test_a_completed_checklist_files_nothing():
    """An implemented spec has no unchecked boxes and must not create debt.

    The fixture assertion alone was vacuous — it tested this file's own string,
    not the workflow. It kept passing when the workflow's skip was disabled,
    which would have filed debt against specs that were actually done."""
    done = REAL_BODY.replace("- [ ]", "- [x]")
    assert not re.search(r"^- \[ \]", done, re.MULTILINE)

    code = _run_code()
    m = re.search(r"if ! printf '%s' \"\$PR_BODY\" \| grep -q .*?exit 0",
                  code, re.DOTALL)
    assert m, (
        "the workflow must exit early when no unchecked checklist item remains"
    )
    assert code.index(m.group(0)) < code.index("gh issue create"), (
        "the completed-checklist skip must run BEFORE the create"
    )


def test_is_idempotent_on_pr_number():
    """The lookup must be USED, not merely present.

    A weaker version of this test passed when the search result was assigned to
    a throwaway and EXISTING forced to "" — the strings were all still there,
    and every re-merge would have opened another issue."""
    code = _run_code()
    assert "spec-debt-for-pr-" in code, "needs a stable per-PR marker"
    m = re.search(r"EXISTING=\$\(\s*gh issue list", code)
    assert m, "the existing-issue lookup must populate EXISTING"
    guard = re.search(r'if \[ -n "\$EXISTING" \]; then.*?exit 0', code, re.DOTALL)
    assert guard, "a populated EXISTING must short-circuit before creating"
    assert code.index(guard.group(0)) < code.index("gh issue create"), (
        "the idempotency guard must run BEFORE the create"
    )


def test_issue_creation_failure_is_not_swallowed():
    """★ This repo lost 147 hours to a step that swallowed a 401 and stayed
    green (data-sync.yml). A tracker that silently fails to track is worse than
    no tracker: it manufactures the belief that debt is being recorded."""
    code = _run_code()
    m = re.search(r"^\s*gh issue create\b.*?(?=\n\s*\n|\Z)", code,
                  re.DOTALL | re.MULTILINE)
    assert m, "issue creation command not found"
    create = m.group(0)
    assert "--label spec-debt" in create, (
        f"matched something other than the create command: {create[:120]!r}"
    )
    assert "|| true" not in create and "|| echo" not in create, (
        "gh issue create must be allowed to fail the job — see data-sync.yml"
    )


def test_has_a_kill_switch():
    """The switch must be BRANCHED ON, not just declared.

    A weaker version passed while the condition had been hard-wired to
    `[ "0" = "1" ]` — the variable was still named in `env:`, so the string was
    present and the switch was dead."""
    env = _tracker_step()["env"]
    assert any("BRAIN_SPEC_DEBT_DISABLE" in str(v) for v in env.values()), (
        "the kill switch must be wired into the step env"
    )
    code = _run_code()
    m = re.search(r'if \[ "\$\{GATE_DISABLE:-0\}" = "1" \]; then.*?exit 0',
                  code, re.DOTALL)
    assert m, "GATE_DISABLE must be tested and exit early when set"


def test_does_not_backfill_retroactively():
    """60 issues in one burst would be triaged as noise and close the loop the
    wrong way. The comment must say so, so nobody 'improves' it later."""
    with open(WF, encoding="utf-8") as fh:
        header = fh.read()
    assert "NOT retroactive" in header


# ── ★ 2026-09-13: one issue per PROBLEM ─────────────────────────────────────
# Filing once per PR turned 71 problems into 171 issues, and nothing closed any
# of them. These tests RUN the tracker's shell with `gh` and `python3` stubbed on
# PATH, so they check what the step does rather than which strings it holds.

_STUB_GH = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_LOG"
case "$1 $2" in
  "issue list")    printf '%s' "${FAKE_EXISTING:-}" ;;
  "label create")  exit 1 ;;
  "issue comment") exit "${FAKE_COMMENT_RC:-0}" ;;
  "issue create")  exit "${FAKE_CREATE_RC:-0}" ;;
esac
"""

_STUB_PYTHON = """#!/usr/bin/env bash
printf 'python3 %s\\n' "$*" >> "$STUB_LOG"
printf '%s' "${FAKE_CLASS_ISSUE:-}"
exit "${FAKE_CLASS_RC:-0}"
"""

_PR_TITLE = ("[brain-spec] inv #100625: facility_duplicates_unmarked (observed at: "
             "/api/v1/admin/facility-dedu")


def _run_tracker(workdir, **fake):
    """Run the tracker step's real shell in `workdir`; return (process, calls)."""
    bindir = os.path.join(workdir, "bin")
    os.makedirs(bindir)
    for name, text in (("gh", _STUB_GH), ("python3", _STUB_PYTHON)):
        path = os.path.join(bindir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(path, 0o755)
    log = os.path.join(workdir, "calls.log")
    open(log, "w", encoding="utf-8").close()
    env = {"PATH": bindir + ":/usr/bin:/bin", "STUB_LOG": log,
           "PR_NUMBER": "4242", "PR_TITLE": _PR_TITLE, "PR_BODY": REAL_BODY,
           "PR_URL": "https://github.com/o/r/pull/4242", "GATE_DISABLE": "",
           "GITHUB_REPOSITORY": "o/r"}
    env.update({k: str(v) for k, v in fake.items()})
    proc = subprocess.run(["bash", "-c", _run_block().replace("/tmp/", workdir + "/")],
                          capture_output=True, text=True, env=env, timeout=60)
    with open(log, encoding="utf-8") as fh:
        return proc, fh.read().splitlines()


def _writes(calls):
    return [c for c in calls if c.startswith(("issue comment", "issue create"))]


def test_a_repeat_problem_is_appended_to_its_issue_not_filed_again():
    with tempfile.TemporaryDirectory() as d:
        proc, calls = _run_tracker(d, FAKE_CLASS_ISSUE="2702")
        assert proc.returncode == 0, proc.stderr
        assert [c for c in _writes(calls) if c.startswith("issue comment 2702 ")], calls
        assert not [c for c in calls if c.startswith("issue create")], calls
        with open(os.path.join(d, "spec-debt-comment.md"), encoding="utf-8") as fh:
            assert "spec-debt-for-pr-4242" in fh.read(), (
                "the appended comment must carry the marker the idempotency search finds")


def test_a_new_problem_still_gets_its_own_issue():
    with tempfile.TemporaryDirectory() as d:
        proc, calls = _run_tracker(d, FAKE_CLASS_ISSUE="")
    assert proc.returncode == 0, proc.stderr
    assert [c.split(" ")[:2] for c in _writes(calls)] == [["issue", "create"]], calls


def test_a_failed_problem_lookup_files_instead_of_dropping_the_obligation():
    with tempfile.TemporaryDirectory() as d:
        proc, calls = _run_tracker(d, FAKE_CLASS_ISSUE="2702", FAKE_CLASS_RC="2")
    assert proc.returncode == 0, proc.stderr
    assert [c.split(" ")[:2] for c in _writes(calls)] == [["issue", "create"]], calls
    assert "::warning::" in proc.stdout


def test_an_already_tracked_pr_writes_nothing_and_looks_nothing_up():
    with tempfile.TemporaryDirectory() as d:
        proc, calls = _run_tracker(d, FAKE_EXISTING="77", FAKE_CLASS_ISSUE="2702")
    assert proc.returncode == 0, proc.stderr
    assert not _writes(calls) and not [c for c in calls if c.startswith("python3")], calls


def test_a_failed_append_fails_the_job():
    with tempfile.TemporaryDirectory() as d:
        proc, calls = _run_tracker(d, FAKE_CLASS_ISSUE="2702", FAKE_COMMENT_RC="1")
    assert proc.returncode != 0, "a swallowed append would record debt nowhere"
    assert not [c for c in calls if c.startswith("issue create")], (
        "a failed append must fail loudly, not quietly file a duplicate instead")


def test_the_marker_search_reads_comments_too():
    with tempfile.TemporaryDirectory() as d:
        _, calls = _run_tracker(d, FAKE_CLASS_ISSUE="2702")
    [search] = [c for c in calls if c.startswith("issue list")]
    assert "spec-debt-for-pr-4242" in search and "in:body,comments" in search, search


def test_the_problem_lookup_is_the_script_the_reconciler_uses():
    with tempfile.TemporaryDirectory() as d:
        _, calls = _run_tracker(d, FAKE_CLASS_ISSUE="")
    assert [c for c in calls if c.startswith("python3 ")] == [
        f"python3 scripts/spec_debt_issues.py find-class-issue --repo o/r --title {_PR_TITLE}"]
    assert os.path.exists(os.path.join(ROOT, "scripts", "spec_debt_issues.py"))


def test_the_checkout_is_the_base_branch():
    """A spec PR branched before scripts/spec_debt_issues.py landed has no copy
    at its own ref, so the lookup would fail on every merge and quietly fall
    back to one issue per PR."""
    steps = _wf()["jobs"]["file-spec-debt"]["steps"]
    [co] = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]
    assert (co.get("with") or {}).get("ref") == "${{ github.event.pull_request.base.ref }}"


if __name__ == "__main__":
    _failed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"✓ {_name}")
            except AssertionError as _e:
                _failed += 1
                print(f"✗ {_name}: {_e}")
    print(f"\n{'FAILED' if _failed else 'PASSED'} — {_failed} failure(s)")
    sys.exit(1 if _failed else 0)
