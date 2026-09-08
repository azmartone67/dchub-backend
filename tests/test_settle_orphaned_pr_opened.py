"""status='pr_opened' must not be a terminal trap.

2026-09-08. 52 proposals sat at status='pr_opened' with merge_outcome NULL,
the oldest since 2026-05-31. Resolving each row's PR against GitHub gave
38 CLOSED / 13 MERGED / 1 OPEN — so 51 of the 52 were settled business that
nothing would ever look at again, because:

  * list_merged_brain_prs() walks a TIME WINDOW (default 30 days). June PRs
    aged out and can never re-enter it.
  * that walk only lists MERGED PRs, so CLOSED-unmerged ones are never
    enumerated by any code path at all.

The drafter skips any row whose pr_url is set, so each one was out of the
queue permanently while still presenting as in-flight.

The settle pass runs DB-outward (rows -> GitHub), so it has no lookback and
cannot inherit the same blind spot.

★ The load-bearing assertion here is test_merged_rows_do_not_get_an_outcome.
Calibration counts `merge_outcome IS NOT NULL` as resolved and divides
healthy/resolved (brain_v2_layer5.py:1550-1552). Stamping any non-healthy
value on a months-old merge would silently lower the trust ratio and raise
the threshold on no evidence — the "fabricate an oracle" failure the
reconciler already refuses elsewhere with no_evidence.
"""
import re
import pathlib
import pytest

import routes.brain_merge_reconciler as rec

SRC = pathlib.Path(rec.__file__).read_text()


@pytest.mark.parametrize("url,expected", [
    ("https://github.com/azmartone67/dchub-backend/pull/4203", 4203),
    ("https://github.com/azmartone67/dchub-backend/pull/1185\n", 1185),
    ("https://github.com/o/r/pull/12/files", None),   # not a bare PR url
    ("", None),
    (None, None),
    ("nonsense", None),
])
def test_pr_number_parsing(url, expected):
    assert rec._pr_number_from_url(url) == expected


def test_settle_is_wired_into_the_master_tick():
    """A pass nothing calls is the defect it was written to fix."""
    orch = pathlib.Path(rec.__file__).with_name("brain_master_orchestrator.py").read_text()
    assert "settle_orphaned_pr_opened" in orch, (
        "settle_orphaned_pr_opened exists but the orchestrator never calls it")
    assert "tier2.settle_orphaned_pr_opened" in orch


def _settle_body() -> str:
    i = SRC.index("def settle_orphaned_pr_opened")
    return SRC[i:]


def test_merged_rows_do_not_get_an_outcome():
    """The merged branch must not write merge_outcome — see the docstring."""
    body = _settle_body()
    assert "merge_outcome = %s" not in body, (
        "settle_orphaned_pr_opened writes merge_outcome. Calibration counts "
        "any non-NULL value as 'resolved' and divides healthy/resolved, so "
        "this would lower the trust ratio on no evidence.")


def test_only_claims_rows_it_still_owns():
    """Every UPDATE must re-assert the pr_opened/NULL precondition, so a
    concurrent reconciler write is never clobbered."""
    body = _settle_body()
    updates = [m for m in re.findall(r"UPDATE brain_proposed_code_fixes.*?\"\"\"",
                                     body, re.S)]
    assert updates, "no UPDATE found — the extractor lost its target"
    for u in updates:
        assert "status = 'pr_opened'" in u and "merge_outcome IS NULL" in u, (
            "an UPDATE does not re-check the precondition it selected on")


def test_open_prs_are_left_alone():
    body = _settle_body()
    assert 'if state == "open":' in body and 'still_open' in body
