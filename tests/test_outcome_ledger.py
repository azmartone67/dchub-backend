"""Tests for routes/brain_outcome_ledger.py — the outcome ledger (Phase 0).

The ledger exists so the brain finally learns whether what it filed LIVED or
DIED. The one lie that would poison that signal is recording a dead PR as
alive — classify_pr's mapping is pinned here and mutation-tested on the real
path (see the PR body for the red/green traces).
"""
from datetime import datetime, timedelta, timezone

from routes.brain_outcome_ledger import (DIED, IN_FLIGHT, LIVED, ROTTED,
                                         classify_pr, is_brain_pr, klass_for,
                                         spec_debt_doc_id)

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)


def _pr(**kw):
    base = {"number": 2633, "state": "open", "merged_at": None,
            "closed_at": None,
            "created_at": (NOW - timedelta(days=1)).isoformat(),
            "head": {"ref": "brain-spec/agenda-100194"}}
    base.update(kw)
    return base


# ── terminal-state mapping: died is died, lived is lived ────────────

def test_merged_pr_records_LIVED_resolved_true():
    v = classify_pr(_pr(state="closed", merged_at="2026-08-14T10:00:00Z"),
                    now=NOW, rot_after_days=14)
    assert v["state"] == LIVED
    assert v["resolved"] is True
    assert v["terminal"] is True


def test_closed_unmerged_pr_records_DIED_resolved_false():
    # Kills: recording died as lived — the mutation that would poison the
    # only feedback signal this phase adds.
    v = classify_pr(_pr(state="closed", closed_at="2026-08-13T10:00:00Z"),
                    now=NOW, rot_after_days=14)
    assert v["state"] == DIED
    assert v["resolved"] is False
    assert v["terminal"] is True


def test_open_pr_older_than_rot_window_records_ROTTED_false():
    v = classify_pr(_pr(created_at=(NOW - timedelta(days=20)).isoformat()),
                    now=NOW, rot_after_days=14)
    assert v["state"] == ROTTED
    assert v["resolved"] is False
    assert v["terminal"] is True
    assert "rot" in v["reason"]


def test_young_open_pr_is_IN_FLIGHT_no_verdict_not_recorded():
    v = classify_pr(_pr(), now=NOW, rot_after_days=14)
    assert v["state"] == IN_FLIGHT
    assert v["resolved"] is None      # tri-state: no verdict is NOT a verdict
    assert v["terminal"] is False     # the recorder must skip it


def test_merge_beats_closed_state():
    # GitHub reports merged PRs as state=closed; merged_at is the tiebreak.
    v = classify_pr(_pr(state="closed", merged_at="2026-08-14T10:00:00Z",
                        closed_at="2026-08-14T10:00:00Z"), now=NOW)
    assert v["state"] == LIVED and v["resolved"] is True


# ── brain-authorship + lane class ───────────────────────────────────

def test_brain_head_branches_are_recognised():
    assert is_brain_pr(_pr())
    assert is_brain_pr(_pr(head={"ref": "brain/autofix-now-text-cast-1"}))
    assert not is_brain_pr(_pr(head={"ref": "feature/some-human-work"}))


def test_klass_is_lane_scoped_and_disjoint_from_mechanical_classes():
    assert klass_for(_pr()) == "brain_spec_pr"
    assert klass_for(_pr(head={"ref": "brain/autofix-x"})) == "brain_autofix_pr"
    assert klass_for(_pr(head={"ref": "brain-v2/x"})) == "brain_code_pr"
    # The mechanical allowlist classes must never collide with these names.
    for mech in ("interval_literal", "tz_naive_utcnow", "bool_is_active",
                 "sqlite_datetime_on_pg", "now_text_cast", "immutable_index"):
        assert klass_for(_pr()) != mech


# ── spec-debt doc ids: stable, kind-scoped ──────────────────────────

def test_spec_debt_doc_id_prefers_agenda_number():
    assert spec_debt_doc_id("agenda-100094-x.md") == 100094


def test_spec_debt_doc_id_falls_back_to_stable_hash():
    a = spec_debt_doc_id("README-ish-no-number.md")
    b = spec_debt_doc_id("README-ish-no-number.md")
    assert a == b and a > 0
