#!/usr/bin/env python3
"""tests/test_mcp_facts_export_workflow.py — the facts bridge must actually run,
and must not report success without writing.

NO NETWORK, NO DB.

mcp_facts_export.py has existed since 2026-07-30 and nothing ever ran it.
Measured 2026-08-13: the file it produces — canonical/mcp_facts.json in
dchub-mcp-server — was 14 days old, publishing `facilities: 15,300+` against a
live 17,600+ and `deals: 1,600+` against 1,800+.

That file is not decoration. sync-tools-manifest.mjs CHECKS every registry
surface against it and fails CI on drift, deliberately without auto-fixing. So
a stale facts file does not merely age: it pins all 30 listings at the old
number and makes the CORRECT number look like the error.

The properties fenced here are the ones whose absence would restore the exact
failure this workflow exists to end — a scheduled job that runs, exits 0, and
changes nothing.

Run standalone:   python3 tests/test_mcp_facts_export_workflow.py
Run under pytest: pytest tests/test_mcp_facts_export_workflow.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "mcp-facts-export.yml")


def _wf():
    import yaml
    with open(WF, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _steps():
    return _wf()["jobs"]["export"]["steps"]


def _run_code(name_fragment):
    """A step's shell with comment lines stripped.

    Comments here NAME the failure modes on purpose ("NO `|| true` anywhere"),
    so a substring match on prose would read the warning as the defect. Three
    guards in this codebase have already made that mistake."""
    for s in _steps():
        if name_fragment.lower() in str(s.get("name", "")).lower() and "run" in s:
            return "\n".join(l for l in s["run"].splitlines()
                             if not l.lstrip().startswith("#"))
    raise AssertionError(f"no step matching {name_fragment!r}")


def test_the_workflow_is_scheduled_at_all():
    """The whole defect was an exporter nothing invoked."""
    on = _wf()
    on = on[True] if True in on else on["on"]
    assert "schedule" in on, "an exporter with no schedule is the bug being fixed"
    assert on["schedule"], "schedule must have at least one cron"


def test_it_runs_before_the_consumer_syncs():
    """dchub-mcp-server's daily-manifest-sync runs at 13:41 UTC and reads this
    output. Exporting after it would publish yesterday's facts for a day."""
    on = _wf()
    on = on[True] if True in on else on["on"]
    cron = on["schedule"][0]["cron"]
    minute, hour = cron.split()[0], cron.split()[1]
    assert int(hour) < 13, (
        f"facts export runs at {hour}:{minute} UTC but the consumer syncs at "
        f"13:41 — export must come first or the sync reads stale facts"
    )


def test_the_sibling_repo_is_checked_out():
    """The exporter SKIPS the mcp-server target when the sibling is absent —
    a missing checkout looks like a clean run that updated nothing."""
    withs = [s.get("with", {}) for s in _steps() if "with" in s]
    paths = [w.get("path") for w in withs]
    assert "dchub-backend" in paths, (
        "the backend must be checked out to a named path too, or ../dchub-mcp-server "
        "does not resolve"
    )
    sibling = [w for w in withs if w.get("path") == "dchub-mcp-server"]
    assert sibling, "sibling checkout missing"
    # Checking the PATH alone passed when the repository was swapped to
    # dchub-backend — the directory name was right and the contents were wrong,
    # so the exporter would have written into a copy of the wrong repo.
    assert sibling[0].get("repository") == "azmartone67/dchub-mcp-server", (
        f"sibling path is right but repository is {sibling[0].get('repository')!r}"
    )


def test_the_exporter_call_is_not_swallowed():
    """★ The exporter FAILS AND WRITES NOTHING on incomplete data, by design.
    Swallowing that turns a deliberate refusal into a silent success."""
    code = _run_code("Export canonical facts")
    m = re.search(r"^\s*python3 mcp_facts_export\.py.*$", code, re.MULTILINE)
    assert m, "the exporter invocation was not found"
    line = m.group(0)
    assert "|| true" not in line and "|| echo" not in line and "continue-on-error" not in line, (
        f"the exporter must be allowed to fail the job; got: {line.strip()!r}"
    )


def test_a_write_that_never_happened_fails_the_job():
    """`generated_at` moves on every successful run, so a byte-identical file
    means nothing was written — not 'no news'. This is the difference between
    the workflow existing and the workflow working."""
    code = _run_code("Export canonical facts")
    assert "sha256sum" in code, "needs a before/after hash to detect a skipped write"
    assert re.search(r'if \[ "\$BEFORE" = "\$AFTER" \]', code), (
        "must compare the hashes"
    )
    idx = code.index('if [ "$BEFORE" = "$AFTER" ]')
    assert "exit 1" in code[idx:idx + 500], (
        "an unchanged file must FAIL — otherwise a skipped write reports success, "
        "which is precisely the 14-day silence this workflow ends"
    )


def test_unchanged_facts_are_not_treated_as_an_error_on_push():
    """Numbers are floors that round DOWN and move slowly. 'Nothing to commit'
    is a legitimate outcome and must not page anyone."""
    code = _run_code("open a PR into dchub-mcp-server")
    assert "git diff --quiet" in code, "must detect no-change before committing"
    assert "exit 0" in code, "no-change must exit cleanly, not fail"


def test_the_sibling_change_lands_via_a_PR_not_a_push_to_main():
    """★2026-09-04 — dchub-mcp-server's main is gaining required status checks,
    so the bare `git push` this step used to do would start failing with GH006
    the same way the backend copy already did for 17 days. The step opens a PR
    instead, and this asserts the executable body actually does that rather
    than a comment claiming it."""
    code = _run_code("open a PR into dchub-mcp-server")
    assert re.search(r"git\s+(?:checkout\s+-b|switch\s+-c)", code), (
        "must create a branch — a push from the checkout targets main"
    )
    assert "gh pr create" in code, "must open a PR, not push to main"
    assert "[skip ci]" not in code, (
        "no CI-skip marker: the PR's checks are what let it merge, and this "
        "repo squash-merges, so the token would ride into main's head commit"
    )


def test_has_a_kill_switch():
    code = _run_code("Export canonical facts")
    assert re.search(r'if \[ "\$\{GATE_DISABLE:-0\}" = "1" \]', code), (
        "every scheduled writer here carries a kill switch"
    )


def test_pushes_with_a_cross_repo_token():
    """The default GITHUB_TOKEN cannot write to another repository."""
    checkout = [s for s in _steps()
                if s.get("with", {}).get("path") == "dchub-mcp-server"][0]
    tok = str(checkout["with"].get("token", ""))
    assert "PR_SUBMIT_TOKEN" in tok or "GH_PAT" in tok, (
        "cross-repo push needs a PAT; GITHUB_TOKEN is scoped to this repo only"
    )


# ── 2026-08-20: the nine tests above all PASSED while the job failed 7/7 ──────
#
# Every assertion in this file reads the workflow as TEXT: is there a schedule,
# is the cron early enough, is the sibling checked out, is the exporter call
# unswallowed. All true. And the job still never once succeeded, because
# `permissions: contents: read` made every `git push` in it a guaranteed 403 —
# a property no amount of grepping the step bodies can see.
#
# Measured 2026-08-20: 7 runs since 08-14, 7 failures, 0 successes, and
# canonical/mcp_facts.json still on the 2026-07-30 copy a human committed.
#
# These two tests fence the capability rather than the prose.

def test_the_job_may_actually_push():
    """A workflow whose whole output is two `git push`es needs write.

    This is the defect the other nine could not see: read permission plus a
    push is a job that can only ever fail, and it did, every day, silently
    enough that the dead-man board never showed it."""
    perms = _wf().get("permissions") or {}
    got = perms.get("contents")
    pushes = [s for s in _steps() if "git push" in str(s.get("run", ""))]
    assert pushes, "no push step found — this test is checking the wrong file"
    assert got == "write", (
        f"permissions.contents is {got!r} but {len(pushes)} step(s) run "
        "`git push`. That combination cannot succeed: it 403s on every run "
        "(observed 7/7, 2026-08-14..08-20)."
    )


def test_the_critical_push_runs_before_the_fragile_one():
    """Order is the fix, not a preference.

    ★2026-09-04: the ORIGINAL reason has expired and the rule outlived it. It
    read "dchub-mcp-server/main is unprotected, so that push lands" — both
    steps now open PRs, and that repo's main is gaining required checks too, so
    neither is the doomed one any more. The ordering still matters for the
    surviving reason: with `set -euo pipefail`, whichever runs first decides
    whether the other runs at all, and the sibling step is the one feeding all
    30 registry surfaces. A failure in the backend's own servable copy must not
    discard the run's real work.

    (Kept rather than deleted: for a week the step that CANNOT work ran first
    and starved the one that does.)"""
    # ★ Key on WHAT THE STEP DOES, not what it is called. The first draft
    # matched `"dchub-mcp-server" in name`, which also matches the *checkout*
    # step at index 1 — so it compared 1 < backend and was true no matter how
    # the pushes were ordered. Mutation-testing caught it: swapping the two
    # push steps left the guard green. Identify each push by the directory it
    # pushes FROM.
    pushes = [
        (i, str(s.get("working-directory", "")))
        for i, s in enumerate(_steps())
        if "git push" in str(s.get("run", ""))
    ]
    assert len(pushes) == 2, (
        f"expected exactly 2 pushing steps, found {len(pushes)}: {pushes}. "
        "If a push was added or removed, this ordering rule needs rethinking, "
        "not relaxing."
    )
    sibling = next(i for i, wd in pushes if wd == "dchub-mcp-server")
    backend = next(i for i, wd in pushes if wd == "dchub-backend")
    assert sibling < backend, (
        "the dchub-mcp-server push must come FIRST. It is the only target that "
        "feeds anything; putting the backend copy ahead of it means one "
        "predictable failure discards the run's real work."
    )


# ── 2026-09-10: the heal covered ONE of dchub-mcp-server's two derived sets ──
#
# dchub-mcp-server #397 — a facts PR this workflow opened — went red on the
# sibling repo's "Per-pack listings match the catalog" step and needed a human
# commit (db838b5, integrations/packs/site.json) before it could merge. The
# regenerate had rewritten a tool description in mcp-server.json, the site pack
# quotes that description, and the pack emitter is a separate CI STEP rather
# than a test file, so nothing this workflow ran could see it. Reproduced at
# the bot's commit 5f0fece: the check exits 1, and `--fix` produces db838b5
# byte for byte.
#
# Keyed on the COMMANDS each step runs, never on step names — the ordering test
# above records what name-matching cost last time.

_SIBLING = "dchub-mcp-server"
_HEAL = r"^\s*node scripts/sync-tools-manifest\.mjs --fix\s*$"
_EMIT_FIX = r"^\s*node scripts/emit-pack-manifests\.mjs --fix\s*$"
_EMIT_CHECK = r"^\s*node scripts/emit-pack-manifests\.mjs\s*$"
_OPEN_PR = r"^\s*PR_URL=\$\(gh pr create --repo azmartone67/dchub-mcp-server\b"


def _code(step):
    """A step's shell with comment lines stripped (the same rule as _run_code),
    so a command that survives only inside a comment does not count."""
    return "\n".join(l for l in str(step.get("run", "")).splitlines()
                     if not l.lstrip().startswith("#"))


def _indices(pattern):
    rx = re.compile(pattern, re.MULTILINE)
    return [i for i, s in enumerate(_steps())
            if s.get("working-directory") == _SIBLING and rx.search(_code(s))]


def _one(pattern, what):
    hits = _indices(pattern)
    assert len(hits) == 1, (
        f"expected exactly one {_SIBLING} step that {what}, found {len(hits)} "
        f"at {hits} — an ordering assertion over the wrong steps proves nothing")
    return hits[0]


def test_the_pack_listings_are_regenerated_after_the_catalog_heal():
    heal = _one(_HEAL, "heals the catalog")
    emit = _one(_EMIT_FIX, "regenerates the pack listings")
    pr = _one(_OPEN_PR, "opens the PR")
    assert heal < emit < pr, (
        f"heal@{heal}, pack emit@{emit}, PR@{pr}. The listings quote the catalog "
        "the heal rewrites, so they must be regenerated AFTER it and BEFORE the "
        "commit — otherwise the PR ships yesterday's descriptions and goes red on "
        "dchub-mcp-server's per-pack step, exactly as #397 did.")


def test_the_pack_emitter_has_its_dependencies_installed_first():
    """emit-pack-manifests imports server.mjs, which imports its npm deps. The
    catalog heal before it needs none, so this workflow never installed any."""
    npm = _indices(r"^\s*npm ci\b")
    emit = _one(_EMIT_FIX, "regenerates the pack listings")
    assert npm and min(npm) < emit, (
        f"npm ci@{npm}, pack emit@{emit}: the emitter's import of server.mjs "
        "throws without node_modules, and set -euo pipefail turns that into a "
        "failed export")


def test_the_pre_pr_guard_runs_the_pack_check_too():
    """Both drift checks dchub-mcp-server's CI runs, on the tree about to be
    committed — a red caught here is a red PR that is never opened."""
    emit = _one(_EMIT_FIX, "regenerates the pack listings")
    check = _one(_EMIT_CHECK, "checks the pack listings")
    pr = _one(_OPEN_PR, "opens the PR")
    assert emit < check < pr, (
        f"pack emit@{emit}, pack check@{check}, PR@{pr}: the check must read the "
        "tree the emitter produced, before it is committed")
    assert re.search(r"^\s*node scripts/sync-tools-manifest\.mjs\s*$",
                     _code(_steps()[check]), re.MULTILINE), (
        "the pack check left the drift-guard step — the two checks CI runs must "
        "be proven on the same tree")


def test_a_new_pack_listing_is_staged_not_left_untracked():
    """`git add -u` stages no untracked file, and the emitter REBUILDS its
    directory. A pack declared since the last export would be checked on disk
    by the guard step and then dropped from the commit."""
    code = _code(_steps()[_one(_OPEN_PR, "opens the PR")])
    add = re.search(r"^\s*git add -A -- integrations/packs\s*$", code, re.MULTILINE)
    commit = re.search(r"^\s*git commit\b", code, re.MULTILINE)
    assert add and commit and add.start() < commit.start(), (
        "integrations/packs/ must be staged with -A before `git commit`")


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
