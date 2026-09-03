"""tests/test_grid_parked_datasets_are_filed.py — the promise that was never wired.

`_ingest_gridstatus_dataset`'s own docstring says the non-allowlisted datasets
are "skipped with an honest marker so the brain can repoint them (finding filed
by the caller)". The caller filed `_GAP_FINDINGS` — a static list of nine — and
the repoint gap was not one of them. So the marker was returned into a dict
nobody read, no finding was ever filed, the brain never saw it, and nobody
repointed anything.

MEASURED 2026-09-03:

    TARGET_DATASETS rows                 21
    GRIDSTATUS_DATASET_ALLOWLIST ids      4   (pjm_load is allowlisted but is
                                               NOT declared in the registry)
    reachable datasets                    3
    parked                               18   since 2026-07-26

The 2026-07-26 cut was correct — the gridstatus free tier is 250 req/month and
July burned 375. What was missing is that parking 18 datasets left no trace any
loop could act on, so a shell whose stated job is "constantly widening +
freshening coverage" cycled three datasets for five weeks while every tick
reported success. `/api/v1/sources` shows the same shape from the other side:
grid feeds read `fresh` because the extractor RAN, not because coverage grew.

What is proved here:
  · the finding is DERIVED from TARGET_DATASETS and the live allowlist, never
    from a second hand-written list — which would rot exactly the way the nine
    did;
  · it names the real parked ids and the real counts;
  · it REPORTS NOTHING once the allowlist covers the registry, so it cannot
    become permanent furniture;
  · it is appended to the gaps the tick actually files, not computed and
    dropped — which is the precise bug it fixes.

House rules: no DB, never import main, nothing runs at module scope.

Run:  python3 -m pytest tests/test_grid_parked_datasets_are_filed.py -v
"""
from __future__ import annotations

import inspect

import pytest

from routes import grid_data_master_shell as g


def test_the_parked_set_is_the_registry_minus_the_allowlist():
    ids = {t["id"] for t in g.TARGET_DATASETS}
    expected = sorted(ids - set(g._GS_ALLOWLIST))
    assert sorted(t["id"] for t in g.parked_datasets()) == expected


def test_it_is_derived_not_a_second_hand_list():
    """★ ANTI-ROT. A hardcoded parked list would go stale the moment someone
    widens the allowlist — which is exactly how the nine standing gaps missed
    this one."""
    src = inspect.getsource(g.parked_datasets)
    assert "TARGET_DATASETS" in src and "_GS_ALLOWLIST" in src
    for t in g.TARGET_DATASETS[:5]:
        assert '"%s"' % t["id"] not in src, (
            "%s is hardcoded into the parked helper" % t["id"])


def test_the_finding_names_the_counts_and_the_allowlist():
    issue, detail = g._parked_finding()
    assert issue == "grid_datasets_parked_pending_repoint"
    n = len(g.parked_datasets())
    assert "%d of %d" % (n, len(g.TARGET_DATASETS)) in detail
    for a in sorted(g._GS_ALLOWLIST):
        assert a in detail, "the allowlist must be named so it can be widened"


def test_it_says_what_the_work_actually_is():
    """A finding nobody can act on is the thing being fixed. The detail must
    name the remedy — repoint each adapter at the free direct source — not just
    report a number."""
    _, detail = g._parked_finding()
    assert "repoint" in detail.lower()
    assert "free direct source" in detail.lower()
    assert "250 req/month" in detail.lower()


def test_it_reports_NOTHING_when_the_allowlist_covers_the_registry(monkeypatch):
    """★ It must be able to go away. A finding that can never clear is
    furniture, and the board already has too much of that."""
    monkeypatch.setattr(g, "_GS_ALLOWLIST",
                        {t["id"] for t in g.TARGET_DATASETS})
    assert g.parked_datasets() == []
    assert g._parked_finding() is None


def test_a_widened_allowlist_shrinks_the_finding(monkeypatch):
    before = len(g.parked_datasets())
    extra = next(t["id"] for t in g.TARGET_DATASETS
                 if t["id"] not in g._GS_ALLOWLIST)
    monkeypatch.setattr(g, "_GS_ALLOWLIST", set(g._GS_ALLOWLIST) | {extra})
    assert len(g.parked_datasets()) == before - 1
    assert extra not in {t["id"] for t in g.parked_datasets()}
    # (it DOES still appear in the detail — the finding names the allowlist so
    # a reader can see what to widen next, and `extra` is now part of it.)
    assert "%d of %d" % (before - 1, len(g.TARGET_DATASETS)) in g._parked_finding()[1]


def test_the_tick_actually_FILES_it_not_just_computes_it():
    """★ THE WHOLE BUG IN ONE ASSERTION. The marker existed and was returned;
    nothing appended it to the gaps the tick files, so it reached nobody."""
    src = inspect.getsource(g._file_gap_findings)
    assert "_parked_finding()" in src, (
        "the derived gap must be appended to the filed set — computing it and "
        "dropping it is the defect this file is about")
    assert "_gaps.append" in src and "for issue, detail in _gaps:" in src


def test_the_standing_nine_are_still_filed():
    """CONTROL: the derived gap is ADDITIVE. Losing the nine would trade one
    blind spot for another."""
    src = inspect.getsource(g._file_gap_findings)
    assert "list(_GAP_FINDINGS)" in src
    assert len(g._GAP_FINDINGS) == 9


def test_an_allowlisted_id_absent_from_the_registry_is_visible():
    """`pjm_load` is allowlisted and never declared, so the shell reaches three
    datasets, not four. Pinned because it silently costs a quarter of the
    reachable surface."""
    ids = {t["id"] for t in g.TARGET_DATASETS}
    ghosts = sorted(set(g._GS_ALLOWLIST) - ids)
    reachable = sorted(ids & set(g._GS_ALLOWLIST))
    assert ghosts == ["pjm_load"], (
        "the allowlist/registry mismatch changed: ghosts=%s" % ghosts)
    assert len(reachable) == 3


@pytest.mark.parametrize("env", ["", "pjm_fuel_mix"])
def test_a_narrow_or_empty_allowlist_never_raises(monkeypatch, env):
    monkeypatch.setattr(g, "_GS_ALLOWLIST", set(x for x in env.split(",") if x))
    out = g._parked_finding()
    assert out is not None and isinstance(out[1], str) and out[1]
