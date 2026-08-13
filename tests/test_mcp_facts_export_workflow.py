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
    code = _run_code("Commit + push into dchub-mcp-server")
    assert "git diff --quiet" in code, "must detect no-change before committing"
    assert "exit 0" in code, "no-change must exit cleanly, not fail"


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
