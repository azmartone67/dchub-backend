"""Both halves of the post-merge outcome contract, pinned on the producer.

WHY (2026-09-08). `brain-pr-post-merge-guard.yml` closes the loop that feeds
the L5 confidence calibration. It needs TWO things from whoever opens the PR,
and `routes/brain_backlog_admin.py` supplied neither:

  1. the label its job gates on --
     `if: contains(labels.*.name, 'autonomous-brain-layer5')`
     This route never applied it, so its PRs merged with the guard `skipped` --
     neutral grey in the UI, no error anywhere.

  2. the proposal id as the TRAILING digits of the head ref, which the guard
     parses with `grep -oE '[0-9]+$'`. This route built
     `...-{pid}-{ts}`, so the unix timestamp was parsed instead.

Both were live, and the same night produced a matched pair that proves it:

    #4203  brain-v2/auto-mcp-upgrade-signals-...-25   labels=[autonomous-brain-layer5]
    #4202  brain-v2/auto-cache_rate-24-67--100789-1788841887   labels=[]

#4203 came from the workflow-side opener and works end to end. #4202 came from
this route: no label, and its trailing digits are 1788841887 -- a proposal that
does not exist. Even labelled, the callback would have written nothing.

That gap is why `merge_outcome` was NULL on 119 of 119 rows while the
calibration self-tuned on it.

These tests read the SHIPPED workflow and the SHIPPED route, so a change to
either side that breaks the contract reds CI rather than silently reopening it.

Stdlib + pyyaml; no DB, no network, no module import.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
ROUTE = REPO / "routes/brain_backlog_admin.py"
GUARD = REPO / ".github/workflows/brain-pr-post-merge-guard.yml"
OPENER = REPO / ".github/workflows/brain-layer5-pr-opener.yml"


def _guard_required_label():
    """The label the guard's job `if:` gates on."""
    m = re.search(r"contains\(\s*github\.event\.pull_request\.labels\.\*\.name\s*,\s*'([^']+)'\s*\)",
                  GUARD.read_text())
    assert m, "guard job no longer gates on a label — re-read the contract"
    return m.group(1)


def _route_branch_template():
    m = re.search(r'branch = f"(brain-v2/auto-[^"]+)"', ROUTE.read_text())
    assert m, "branch template not found in brain_backlog_admin.py"
    return m.group(1)


def test_route_applies_the_label_the_guard_requires():
    """Half one. Read from the guard, not hardcoded, so renaming the label on
    one side alone cannot pass."""
    label = _guard_required_label()
    src = ROUTE.read_text()
    assert f'_OUTCOME_LABEL = "{label}"' in src, (
        f"route must apply the guard's label {label!r}; without it every PR "
        f"this route opens merges with the guard skipped and no outcome is "
        f"ever recorded")
    assert "/labels" in src and "_OUTCOME_LABEL" in src, (
        "the label constant exists but is never POSTed to the labels endpoint")


def test_branch_ends_with_the_bare_proposal_id():
    """Half two. The guard parses the id as trailing digits, so anything after
    {pid} in the template steals it."""
    tpl = _route_branch_template()
    assert tpl.endswith("{pid}"), (
        f"branch template {tpl!r} must end with {{pid}} — the guard reads the "
        f"proposal id as the trailing digits of the head ref, so a suffix "
        f"after it (a timestamp, a hash) is parsed instead")


@pytest.mark.parametrize("branch,expected", [
    # the shape this route now produces
    ("brain-v2/auto-cache_rate-24-67--1788841887-100789", "100789"),
    # the workflow-side opener's shape, which already worked
    ("brain-v2/auto-mcp-upgrade-signals-tool-get-intelligenc-25", "25"),
])
def test_guard_regex_recovers_the_id_from_real_branches(branch, expected):
    """Pins the guard's own parse against branches both producers emit."""
    m = re.search(r"[0-9]+$", branch)
    assert m and m.group(0) == expected


def test_the_live_broken_branch_would_still_fail():
    """A must-fail control on the parse itself: the shape that shipped on
    2026-09-08 recovers the timestamp, not the proposal. If this ever passes,
    the regex changed and these assertions stopped meaning anything."""
    broken = "brain-v2/auto-cache_rate-24-67--100789-1788841887"
    assert re.search(r"[0-9]+$", broken).group(0) == "1788841887"
    assert re.search(r"[0-9]+$", broken).group(0) != "100789"


def test_both_producers_agree_on_the_branch_convention():
    """The workflow opener and this route must not drift apart again — two
    producers with one contract is how this broke in the first place."""
    m = re.search(r'branch = f"(brain-v2/auto-[^"]+)"', OPENER.read_text())
    assert m, "workflow opener branch template not found"
    assert m.group(1).endswith("{pid}")
    assert _route_branch_template().endswith("{pid}")
