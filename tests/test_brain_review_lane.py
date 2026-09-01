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
    is_review_eligible,
    is_patch_unsafe,
    open_review_draft_prs,
)


# ── The gate predicate ───────────────────────────────────────────────
#
# ★ CONTRACT CHANGED 2026-08-31, deliberately. The old predicate
# (is_unclassified_safe) required blocked_by == [UNCLASSIFIED_BLOCKER] EXACTLY.
# Measured across all 85 open proposals, that admitted ZERO — not one is blocked
# solely by the missing class — so the lane was unreachable by construction and
# had opened nothing, ever.
#
# The replacement separates two questions the old one conflated:
#   1. is the PATCH applicable and safe to show a human?
#   2. is it MECHANICAL enough to merge unattended?
# The mechanical lane answers (2). This lane exists for proposals that fail (2)
# and pass (1). On the live queue that is 81 reviewable / 3 excluded.

def test_admits_the_missing_class_blocker():
    assert is_review_eligible(
        {"is_mechanical": False, "blocked_by": [UNCLASSIFIED_BLOCKER]}) is True


@pytest.mark.parametrize("extra", [
    "12 changed lines > MECH_MAX_LINES=8",
    "adds control-flow keyword(s): if,return",
    "confidence 0.60 < MECH_MIN_CONF=0.8",
    "adds an import",
    "adds call name(s) not in search: to_regclass",
])
def test_admits_not_mechanical_but_reviewable(extra):
    """These mean "unproven", not "wrong". A human reading the diff is exactly
    what they call for — and they are 81 of the 84 blocked proposals, so
    rejecting them is what emptied the lane."""
    assert is_review_eligible({
        "is_mechanical": False,
        "blocked_by": [UNCLASSIFIED_BLOCKER, extra]}) is True, extra
    assert is_review_eligible({
        "is_mechanical": False,
        "blocked_by": [extra, UNCLASSIFIED_BLOCKER]}) is True, "order must not matter"


@pytest.mark.parametrize("extra", [
    "search_text occurs 3x in live file (ambiguous)",
    "search_text not present in live file (stale/drifted)",
    "search_text < 10 chars (6) — too ambiguous",
    "forbidden path pattern(s): main.py",
    "sqlite-data guard: search is a translation-table key",
    "ambiguous: matched multiple allowlist classes",
    "replacement_breaks_syntax: SyntaxError",
])
def test_still_rejects_an_unsound_patch(extra):
    """★ The safety property that survives. These mean the PATCH is broken —
    it would not apply, would apply in the wrong place, or is off-limits. A
    draft PR built on one is garbage or dangerous, however well a human reads.

    All three real exclusions on the live queue are in this set: a search_text
    occurring 3x, one absent from the file, and one 6 characters long."""
    assert is_review_eligible({
        "is_mechanical": False,
        "blocked_by": [UNCLASSIFIED_BLOCKER, extra]}) is False, extra
    assert is_review_eligible({
        "is_mechanical": False,
        "blocked_by": [extra, UNCLASSIFIED_BLOCKER]}) is False
    assert is_patch_unsafe(extra) is True


def test_admits_a_different_single_blocker_when_the_patch_is_sound():
    """Under the old exact-match rule this was a rejection. It is the change."""
    assert is_review_eligible({
        "is_mechanical": False,
        "blocked_by": ["adds control-flow keyword(s): return"]}) is True


def test_rejects_already_mechanical_rows():
    """A mechanical row belongs to the autofix lane; double-opening it would
    create two PRs for one proposal.

    ★ The is_mechanical=True + blocked_by=[UNCLASSIFIED_BLOCKER] case is the
    one that actually exercises the guard. With blocked_by=[] the predicate
    returns False anyway via the list compare, so that input alone lets the
    guard be deleted without any test noticing (it survived mutation)."""
    assert is_review_eligible(
        {"is_mechanical": True, "blocked_by": []}) is False
    assert is_review_eligible(
        {"is_mechanical": True, "blocked_by": [UNCLASSIFIED_BLOCKER]}) is False


def test_rejects_empty_and_malformed_verdicts():
    assert is_review_eligible({"is_mechanical": False, "blocked_by": []}) is False
    assert is_review_eligible({}) is False
    assert is_review_eligible(None) is False
    assert is_review_eligible("nope") is False
    assert is_review_eligible(
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


# ── the silent-refusal bug ───────────────────────────────────────────

def test_a_row_neither_lane_will_take_is_RECORDED(monkeypatch):
    """★ THE BUG. `if not eligible: continue` had no skipped entry, so 85 real
    proposals produced 0 previewed AND 0 skipped — byte-identical to an empty
    queue. "Nothing to do" and "everything rejected" were indistinguishable on
    the board, and the lane looked healthy while opening nothing for weeks.

    A mechanical row stays silent (the other lane owns it). A row NEITHER lane
    will take is reported by nobody, so it must surface here."""
    monkeypatch.setenv("BRAIN_REVIEW_LANE_ENABLED", "1")
    from routes import brain_review_lane as rl

    monkeypatch.setattr(rl, "_classify", lambda row: {
        "is_mechanical": False,
        "blocked_by": ["search_text occurs 3x in live file (ambiguous)"],
        "reasons": []})
    out = open_review_draft_prs([_row(1), _row(2)], apply=False)
    assert out["previewed"] == []
    assert len(out["skipped"]) == 2, (
        "an unsound patch must be recorded, not silently dropped — silence "
        "here is what made a total refusal look like an empty queue")
    assert out["skipped"][0]["reason"] == "not_review_eligible"
    assert out["skipped"][0].get("blocked_by"), "say WHY it was refused"


def test_mechanical_rows_stay_silent_so_they_are_not_double_reported(monkeypatch):
    """The half of the original silence that was CORRECT, kept deliberately.
    The mechanical lane already reports these; reporting them here too would
    double-count every proposal on every tick."""
    monkeypatch.setenv("BRAIN_REVIEW_LANE_ENABLED", "1")
    from routes import brain_review_lane as rl

    monkeypatch.setattr(rl, "_classify",
                        lambda row: {"is_mechanical": True,
                                     "blocked_by": [], "reasons": []})
    out = open_review_draft_prs([_row(1), _row(2)], apply=False)
    assert out["skipped"] == [] and out["previewed"] == []


def test_every_row_is_accounted_for(monkeypatch):
    """The invariant that would have caught this immediately: rows in must equal
    previewed + skipped + opened + (mechanical, which another lane owns)."""
    monkeypatch.setenv("BRAIN_REVIEW_LANE_ENABLED", "1")
    from routes import brain_review_lane as rl

    verdicts = {
        1: {"is_mechanical": True, "blocked_by": []},
        2: {"is_mechanical": False,
            "blocked_by": ["forbidden path pattern(s): main.py"]},
        3: {"is_mechanical": False, "blocked_by": [UNCLASSIFIED_BLOCKER]},
    }
    monkeypatch.setattr(rl, "_classify", lambda row: verdicts[row["id"]])
    monkeypatch.setattr(rl, "open_review_draft_pr",
                        lambda row, dry_run: {"ok": True, "branch": "brain/review-x",
                                              "file_path": "x.py"})
    rows = [_row(1), _row(2), _row(3)]
    out = open_review_draft_prs(rows, apply=False)
    mechanical = 1
    accounted = (len(out["previewed"]) + len(out["skipped"])
                 + len(out["opened"]) + mechanical)
    assert accounted == len(rows), (
        f"{len(rows)} rows in, {accounted} accounted for — the difference is "
        f"proposals that vanished")
