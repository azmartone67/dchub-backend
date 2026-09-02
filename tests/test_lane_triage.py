"""The lane triage registry must stay bonded to the shells and to DEFERRED.

★ WHAT THIS PROTECTS. routes/lane_triage.py answers one question per lane:
can an ENGINEER clear this red, or not? Measured 2026-09-02, of the 10 red
`*-shell-daily` feeds, lanes like loop_control/agent_identity ("no one caller
is >40pct" — chain-hire is 66.8%) and agent_pay/demand ("a REAL agent has ever
asked to pay") are CORRECT and unclearable by code. A board that is mostly
unclearable red trains everyone to scroll past all of it, which is how
failover-canary sat red while the DR mirror drifted 112 commits behind.

★ THE TWO WAYS THIS REGISTRY ROTS, both guarded here:
  1. A lane is renamed or deleted in its shell and its entry lingers,
     describing a lane that no longer exists.
  2. Someone types a class that is not in the vocabulary; with a plain dict
     literal that silently invents a sixth class.

★ AND THE BOND. The vocabulary was PROMOTED from
audit_closure_master_shell.DEFERRED (79 findings classified since 2026-08),
not invented. test_deferred_classes_are_a_subset keeps the two from drifting
into two vocabularies for one concept — which is the drift mechanism this
whole session was chasing.

Behavioural except where noted: the existence check reads the shell sources,
because that IS the coupling being asserted.
"""
import ast
import io
import os
import re

import pytest

from routes.lane_triage import (
    CODE_ACTIONABLE,
    LANE_CLASSES,
    LANE_TRIAGE,
    UNCLASSIFIED_SHELLS,
    classify,
    is_code_actionable,
    split_lanes,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTES = os.path.join(ROOT, "routes")


def _shell_source(shell: str) -> str:
    p = os.path.join(ROUTES, f"{shell}_master_shell.py")
    assert os.path.isfile(p), f"no shell file for {shell!r}: {p}"
    return io.open(p, encoding="utf-8").read()


# ── the vocabulary ────────────────────────────────────────────────────
def test_every_class_used_is_in_the_vocabulary():
    """Catches the typo that would silently invent a sixth class."""
    bad = {k: v[0] for k, v in LANE_TRIAGE.items() if v[0] not in LANE_CLASSES}
    assert not bad, f"unknown class(es): {bad}"


def test_code_actionable_is_a_subset_of_the_vocabulary():
    assert CODE_ACTIONABLE <= set(LANE_CLASSES)


def test_commercial_is_not_code_actionable():
    """★ The whole point. If 'commercial' ever becomes code-actionable the
    registry stops distinguishing anything."""
    assert "commercial" not in CODE_ACTIONABLE
    assert "owner-flag" not in CODE_ACTIONABLE


def test_every_entry_carries_evidence_not_just_a_class():
    """A class with no `why` is an assertion nobody can check."""
    thin = [k for k, v in LANE_TRIAGE.items() if len(v[1].strip()) < 40]
    assert not thin, f"entries with no real rationale: {thin}"


# ── the bond to audit_closure's existing vocabulary ───────────────────
def test_deferred_classes_are_a_subset_of_the_vocabulary():
    """★ THE ANTI-DRIFT BOND. audit_closure.DEFERRED has classified 79
    findings with these words since 2026-08. If it grows a class this module
    does not know, we have two vocabularies for one concept — exactly the
    drift this registry exists to end."""
    src = _shell_source("audit_closure")
    used = set(re.findall(r'"SH52-\d+":\s*\("([a-z-]+)"', src))
    assert used, "found no DEFERRED classes — the regex has rotted"
    unknown = used - set(LANE_CLASSES)
    assert not unknown, (
        f"audit_closure.DEFERRED uses class(es) {sorted(unknown)} that "
        f"lane_triage.LANE_CLASSES does not define")


# ── the bond to the shells themselves ─────────────────────────────────
def test_every_classified_lane_still_exists_in_its_shell():
    """★ ANTI-ROT. A renamed or deleted lane must FAIL here, not linger as an
    entry describing something that is gone. Accepts either convention the
    shells use: a lane id string, or a `_lane_<id>` function."""
    missing = []
    for shell, lane in LANE_TRIAGE:
        src = _shell_source(shell)
        by_id = f'"{lane}"' in src
        by_fn = re.search(rf"def _lane_{re.escape(lane)}\b", src) is not None
        if not (by_id or by_fn):
            missing.append(f"{shell}/{lane}")
    assert not missing, f"classified lanes absent from their shell: {missing}"


def test_the_shell_scan_actually_found_lanes():
    """Guards the guard: if _shell_source started returning '' every
    assertion above would pass vacuously."""
    assert len(LANE_TRIAGE) >= 30
    assert len(_shell_source("loop_control")) > 10_000


def test_unclassified_shells_are_named_with_a_reason():
    """Silence about a shell must be deliberate and explained."""
    assert "audit_closure" in UNCLASSIFIED_SHELLS
    assert len(UNCLASSIFIED_SHELLS["audit_closure"]) > 30
    assert not any(s == "audit_closure" for s, _ in LANE_TRIAGE), \
        "audit_closure is declared unclassified but has entries"


# ── behaviour ─────────────────────────────────────────────────────────
def test_a_known_signal_lane_is_not_code_actionable():
    klass, why = classify("loop_control", "agent_identity")
    assert klass == "commercial"
    assert is_code_actionable("loop_control", "agent_identity") is False
    assert "66.8" in why


def test_a_known_defect_lane_is_code_actionable():
    """★ THE GREEN DIRECTION. Without this, classifying EVERYTHING as
    not-actionable would satisfy the rest of this file."""
    assert classify("agent_pay", "pricing")[0] == "build"
    assert is_code_actionable("agent_pay", "pricing") is True


def test_a_miscalibrated_check_is_code_actionable_but_not_a_system_defect():
    """`instrument` red is cleared by fixing the CHECK — still engineering."""
    assert classify("loop_control", "counter_canon")[0] == "instrument"
    assert is_code_actionable("loop_control", "counter_canon") is True


def test_an_unclassified_lane_is_none_never_false():
    """★ None is 'unknown'. Returning False would quietly assert that an
    unclassified lane is somebody else's problem."""
    assert classify("loop_control", "no_such_lane") is None
    assert is_code_actionable("loop_control", "no_such_lane") is None


def test_split_lanes_partitions_all_three_ways():
    got = split_lanes([("agent_pay", "pricing"),
                       ("loop_control", "agent_identity"),
                       ("loop_control", "no_such_lane")])
    assert [x[1] for x in got["code_actionable"]] == ["pricing"]
    assert [x[1] for x in got["not_code"]] == ["agent_identity"]
    assert [x[1] for x in got["unclassified"]] == ["no_such_lane"]


def test_split_lanes_handles_empty_input():
    got = split_lanes([])
    assert got == {"code_actionable": [], "not_code": [], "unclassified": []}


def test_the_two_circular_or_miscalibrated_lanes_are_named():
    """These are the two reds no system fix can clear because the CHECK is
    wrong; if either is reclassified, that should be a deliberate edit."""
    assert classify("loop_flywheel", "cron")[0] == "instrument"
    assert "itself" in classify("loop_flywheel", "cron")[1]
