"""Tests for routes/brain_review_lane.py — the human-review actuation lane.

The lane's whole safety argument is that `blocked_by == [<the one blocker>]`
means every other mechanical gate passed. These tests attack that claim from
both sides: rows that MUST be admitted, and rows that MUST NOT be.

★ Nothing at module scope (see CLAUDE.md — a module-scope failure aborts
collection and silently kills the whole suite).
"""
import os

import pytest

from routes.brain_review_lane import (
    REVIEW_BRANCH_PREFIX,
    REVIEW_KLASS,
    UNCLASSIFIED_BLOCKER,
    is_unclassified_safe,
    open_review_draft_prs,
)


# ── The gate predicate ───────────────────────────────────────────────
def test_admits_only_the_missing_class_blocker():
    assert is_unclassified_safe(
        {"is_mechanical": False, "blocked_by": [UNCLASSIFIED_BLOCKER]}) is True


def test_rejects_when_a_second_blocker_is_present():
    """★ The core safety property. The missing class alone is a labelling gap;
    the missing class PLUS anything else is a real one."""
    for extra in (
        "12 changed lines > MECH_MAX_LINES=8",
        "adds control-flow keyword(s): if,return",
        "search_text occurs 3x in live file (ambiguous)",
        "confidence 0.60 < MECH_MIN_CONF=0.8",
        "forbidden path pattern(s): main.py",
        "sqlite-data guard: search is a translation-table key",
        "search_text not present in live file (stale/drifted)",
    ):
        assert is_unclassified_safe({
            "is_mechanical": False,
            "blocked_by": [UNCLASSIFIED_BLOCKER, extra],
        }) is False, f"must reject when also blocked by: {extra}"
        # order must not matter
        assert is_unclassified_safe({
            "is_mechanical": False,
            "blocked_by": [extra, UNCLASSIFIED_BLOCKER],
        }) is False


def test_rejects_a_different_single_blocker():
    assert is_unclassified_safe({
        "is_mechanical": False,
        "blocked_by": ["adds control-flow keyword(s): return"]}) is False


def test_rejects_already_mechanical_rows():
    """A mechanical row belongs to the autofix lane; double-opening it would
    create two PRs for one proposal.

    ★ The is_mechanical=True + blocked_by=[UNCLASSIFIED_BLOCKER] case is the
    one that actually exercises the guard. With blocked_by=[] the predicate
    returns False anyway via the list compare, so that input alone lets the
    guard be deleted without any test noticing (it survived mutation)."""
    assert is_unclassified_safe(
        {"is_mechanical": True, "blocked_by": []}) is False
    assert is_unclassified_safe(
        {"is_mechanical": True, "blocked_by": [UNCLASSIFIED_BLOCKER]}) is False


def test_rejects_empty_and_malformed_verdicts():
    assert is_unclassified_safe({"is_mechanical": False, "blocked_by": []}) is False
    assert is_unclassified_safe({}) is False
    assert is_unclassified_safe(None) is False
    assert is_unclassified_safe("nope") is False
    assert is_unclassified_safe(
        {"is_mechanical": False, "blocked_by": UNCLASSIFIED_BLOCKER}) is False


def test_blocker_string_matches_the_classifier_verbatim():
    """★ The predicate is an exact string compare against the classifier's
    rule-5 message. If that message is ever reworded, this lane silently stops
    admitting anything (fail-closed, but invisibly) — so pin the string to its
    source, the way the #2491 rename should have been pinned."""
    import inspect

    from routes import brain_mechanical_classifier as mc

    src = inspect.getsource(mc.classify_mechanical)
    assert UNCLASSIFIED_BLOCKER in src, (
        "brain_review_lane.UNCLASSIFIED_BLOCKER no longer appears in "
        "classify_mechanical — the rename unplugged the review lane")


# ── The automerge-ineligibility invariant ────────────────────────────
def test_review_branch_prefix_is_not_the_autofix_prefix():
    """★ brain_automerge only ever touches AUTOFIX_BRANCH_PREFIX. If these two
    prefixes ever collide (or one becomes a prefix of the other), unreviewed
    Python logic changes become auto-mergeable."""
    from routes.brain_automerge import AUTOFIX_BRANCH_PREFIX

    assert REVIEW_BRANCH_PREFIX != AUTOFIX_BRANCH_PREFIX
    assert not REVIEW_BRANCH_PREFIX.startswith(AUTOFIX_BRANCH_PREFIX)
    assert not AUTOFIX_BRANCH_PREFIX.startswith(REVIEW_BRANCH_PREFIX)


def test_review_klass_is_not_an_allowlist_class():
    """The klass recorded for dedup must never satisfy the mechanical gate.

    ★ Assert against the CLASS TABLE, not against _matching_classes(junk, junk)
    — that returns [] for any input, so the membership check could only ever
    pass. It survived mutation precisely because it was vacuous: the permanent
    -false-positive shape this codebase has been bitten by before."""
    from routes.brain_mechanical_classifier import ALLOWLIST_CLASSES

    names = {c.get("klass") for c in ALLOWLIST_CLASSES}
    assert len(names) >= 6, f"class table looks wrong: {names}"
    assert REVIEW_KLASS not in names, (
        f"REVIEW_KLASS={REVIEW_KLASS!r} is an allowlist class — review-lane "
        f"rows would satisfy the mechanical gate")


def test_lane_never_registers_a_branch_for_automerge(monkeypatch):
    """★ The mechanical lane calls log_proposal_for_automerge so the merge pass
    can re-verify. The review lane must NOT — a registered branch is one step
    from being merge-eligible."""
    import inspect

    from routes import brain_review_lane as rl

    src = inspect.getsource(rl)
    code = "\n".join(
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#") and "★" not in ln
    )
    assert "log_proposal_for_automerge(" not in code


# ── Batch driver behaviour ───────────────────────────────────────────
def _row(rid, fp="routes/example.py"):
    return {"id": rid, "file_path": fp, "confidence": 0.9,
            "loop_name": "test", "rationale": "because",
            "changes_json": None}


def test_kill_switch_opens_nothing(monkeypatch):
    monkeypatch.setenv("BRAIN_REVIEW_LANE_ENABLED", "0")
    out = open_review_draft_prs([_row(1)], apply=True)
    assert out["enabled"] is False
    assert out["opened"] == []


def test_unknown_open_pr_count_fails_closed(monkeypatch):
    """★ A queue depth we cannot read must never be treated as an empty one."""
    from routes import brain_review_lane as rl

    monkeypatch.setenv("BRAIN_REVIEW_LANE_ENABLED", "1")
    monkeypatch.setattr(rl, "count_open_review_prs", lambda: -1)
    monkeypatch.setattr(rl, "_classify",
                        lambda row: {"is_mechanical": False,
                                     "blocked_by": [UNCLASSIFIED_BLOCKER],
                                     "reasons": []})
    monkeypatch.setattr(
        "routes.brain_draft_pr_writer._already_opened", lambda row: False)
    out = open_review_draft_prs([_row(1)], apply=True)
    assert out["opened"] == []
    assert any("fail-closed" in (s.get("reason") or "")
               for s in out["skipped"]), out["skipped"]


def test_full_queue_blocks_further_opens(monkeypatch):
    from routes import brain_review_lane as rl

    monkeypatch.setenv("BRAIN_REVIEW_LANE_ENABLED", "1")
    monkeypatch.setenv("BRAIN_REVIEW_LANE_MAX_OPEN", "2")
    monkeypatch.setattr(rl, "count_open_review_prs", lambda: 2)
    monkeypatch.setattr(rl, "_classify",
                        lambda row: {"is_mechanical": False,
                                     "blocked_by": [UNCLASSIFIED_BLOCKER],
                                     "reasons": []})
    monkeypatch.setattr(
        "routes.brain_draft_pr_writer._already_opened", lambda row: False)
    out = open_review_draft_prs([_row(1)], apply=True)
    assert out["opened"] == []
    assert any("review_queue_full" in (s.get("reason") or "")
               for s in out["skipped"]), out["skipped"]


def test_ineligible_rows_are_silently_passed_over(monkeypatch):
    """Rows belonging to the mechanical lane (or to neither) must not appear as
    review-lane skips — that would double-report every proposal every tick."""
    monkeypatch.setenv("BRAIN_REVIEW_LANE_ENABLED", "1")
    from routes import brain_review_lane as rl

    monkeypatch.setattr(rl, "_classify",
                        lambda row: {"is_mechanical": True,
                                     "blocked_by": [], "reasons": []})
    out = open_review_draft_prs([_row(1), _row(2)], apply=False)
    assert out["opened"] == []
    assert out["previewed"] == []
    assert out["skipped"] == []
