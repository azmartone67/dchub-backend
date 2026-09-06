"""A push made with GITHUB_TOKEN creates no workflow run. Post-merge lanes must
not depend on the push event alone.

★ THE DEFECT (2026-09-06). auto-enable-automerge.yml armed GitHub's native
auto-merge using `secrets.GITHUB_TOKEN`. GitHub then performed the merge as
app/github-actions — and it does not create workflow runs for pushes made with
GITHUB_TOKEN. So the merge landed on main and triggered NOTHING.

Measured over the 24 commits in the 6 hours after that workflow shipped:

    merged by azmartone67          7 commits    14-15 push workflows each
    merged by app/github-actions  17 commits     ZERO push workflows each

Not one exception. Five lanes had no schedule and so ran on none of the 17:
API Response Contract, route-tables coherence, dataset-inventory, Deploy Guard,
refresh-architecture-map. Three more were one bot merge from the same fate:
syntax-check (which had no pull_request trigger either), app-contract-gate, and
pre-merge — whose push-to-main run is the stale-main backstop that
tests/test_vault_map_generator.py::test_the_push_side_backstop_still_runs_the_suite
exists to protect.

It stayed invisible because the REST of the lanes — auto-rollback (15 min),
Post-Deploy Smoke, QA Health, Link Check, sitemap-snapshot-rebuild (4-hourly) —
have crons. main still healed, just late. A gate that degrades quietly is
exactly the kind this repo keeps finding months later.

TWO RULES, because either alone leaves the hole open:
  1. the arming token is not GITHUB_TOKEN   (the cause)
  2. every push-to-main workflow has a non-push trigger   (the belt — it holds
     for ANY future bot push, whatever token is in fashion)
"""
import pathlib

import pytest
import yaml

WF = pathlib.Path(__file__).resolve().parent.parent / ".github" / "workflows"
ARMER = WF / "auto-enable-automerge.yml"


def _on(path):
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(d, dict):
        return None
    # PyYAML parses a bare `on:` key as the boolean True.
    on = d.get("on", d.get(True))
    return on if isinstance(on, dict) else None


def _push_to_main(path):
    on = _on(path)
    if not on:
        return False
    push = on.get("push")
    return isinstance(push, dict) and "main" in (push.get("branches") or [])


def _workflows():
    out = [f for f in sorted(WF.glob("*.yml"))]
    assert len(out) > 20, (
        f"only {len(out)} workflow files found — this guard is scanning the wrong "
        f"directory and would pass over an empty set")
    return out


# ── RULE 1: the cause ────────────────────────────────────────────────────────
def test_auto_merge_is_not_armed_with_github_token():
    assert ARMER.is_file(), f"{ARMER.name} is gone — if auto-merge is no longer armed \
by a workflow, delete this test with it"
    src = ARMER.read_text(encoding="utf-8")
    # Only the GH_TOKEN the arming step runs with matters.
    tok = [ln for ln in src.splitlines() if "GH_TOKEN:" in ln]
    assert tok, "no GH_TOKEN line found — the arming step changed shape"
    joined = " ".join(tok)
    assert "secrets.GITHUB_TOKEN" not in joined, (
        "auto-merge is armed with GITHUB_TOKEN again. GitHub creates NO workflow run "
        "for a push made with it, so every auto-merged commit lands on main having "
        f"triggered nothing. Use secrets.PR_SUBMIT_TOKEN (proven: it arms "
        f"refresh-architecture-map's merge, which fires all 14 push lanes). "
        f"Line(s): {joined.strip()}")
    assert "PR_SUBMIT_TOKEN" in joined or "GH_PAT" in joined, (
        f"the arming token is neither PAT: {joined.strip()}")


def test_the_armer_does_not_fall_back_to_github_token():
    """A `PAT || GITHUB_TOKEN` fallback restores the bug the moment the PAT
    lapses, and does it silently. Failing to arm is the safe direction: the PR
    is then merged by a person, which triggers everything."""
    src = ARMER.read_text(encoding="utf-8")
    for ln in src.splitlines():
        if "GH_TOKEN:" in ln and "||" in ln:
            assert "GITHUB_TOKEN" not in ln, (
                f"GITHUB_TOKEN used as a fallback — it is the broken state, so falling "
                f"back to it re-breaks this silently: {ln.strip()}")


# ── RULE 2: the belt ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("wf", [f for f in sorted(WF.glob("*.yml"))], ids=lambda f: f.name)
def test_push_to_main_lanes_have_a_non_push_trigger(wf):
    if not _push_to_main(wf):
        pytest.skip("not a push-to-main lane")
    on = _on(wf)
    assert {"schedule", "workflow_dispatch"} & set(on), (
        f"{wf.name} runs on push to main and nothing else can start it. A push made "
        f"with GITHUB_TOKEN creates no workflow run, so one bot merge silences this "
        f"lane entirely. Add a schedule (and/or workflow_dispatch).")


def test_the_scan_actually_sees_the_push_to_main_lanes():
    """★ NON-VACUITY. If _push_to_main stops matching — a key renamed, PyYAML
    parsing `on` differently — every parametrised case above SKIPS and the suite
    is green over nothing."""
    n = sum(1 for f in _workflows() if _push_to_main(f))
    assert n >= 10, (
        f"only {n} push-to-main workflows detected; there were 16 when this was "
        f"written. The detector is broken and the rule above is skipping silently.")
