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
