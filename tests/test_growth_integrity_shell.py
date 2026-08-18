"""tests/test_growth_integrity_shell.py — shell #52 must not flatter us.

This shell exists because five surfaces lied on 2026-08-17, so the failure mode
it must not have is its OWN: a lane that verified nothing rendering green.

Every lane here is driven through injected payloads — no network, no DB — so the
tests pin the JUDGEMENT, which is the part that was wrong in every defect this
shell watches for.

Run:  python3 -m pytest tests/test_growth_integrity_shell.py -v
"""
from __future__ import annotations

import pytest

from routes import growth_integrity_master_shell as sh


# ── the honest-state ladder ────────────────────────────────────────────────

def test_all_pass_is_pass():
    """Control: without this, every assertion below could pass vacuously."""
    assert sh._lane_verdict([sh._check("a", "a", True, "")]) == "PASS"


def test_any_false_fails():
    assert sh._lane_verdict([sh._check("a", "a", True, ""),
                             sh._check("b", "b", False, "")]) == "FAIL"


def test_a_lane_that_verified_nothing_is_not_a_pass():
    """THE PIN. `?` and PASS must be different states — this shell was built
    because 'I could not measure it' kept rendering as 'it is fine'."""
    assert sh._lane_verdict([sh._check("a", "a", None, "")]) == "?"


def test_unknown_critical_check_is_not_a_pass():
    assert sh._lane_verdict([sh._check("a", "a", True, ""),
                             sh._check("b", "b", None, "", critical=True)]) == "?"


# ── lane 1: the #2834 invariant ────────────────────────────────────────────

def _trend(weeks, **extra):
    d = {"weeks": weeks, "latest_complete": {"week_start": "2026-08-10"}}
    d.update(extra)
    return d


def test_reach_invariant_catches_new_exceeding_distinct(monkeypatch):
    """THE #2834 SHAPE: 43 agents, 245 'new'. Held in EVERY week until 08-18."""
    monkeypatch.setattr(sh, "_get_json", lambda *a, **k: _trend([
        {"week_start": "2026-07-06", "distinct_external_ips": 43,
         "new_external_ips": 245, "computed_at": "2026-08-18T04:21", "partial": False},
    ]))
    lane = sh._lane_reach()
    inv = next(c for c in lane if c["id"] == "r_invariant")
    assert inv["pass"] is False
    assert "VIOLATED" in inv["detail"]


def test_reach_invariant_passes_when_bounded(monkeypatch):
    """Inverse control — the corrected series must not read as broken."""
    monkeypatch.setattr(sh, "_get_json", lambda *a, **k: _trend([
        {"week_start": "2026-08-10", "distinct_external_ips": 72,
         "new_external_ips": 64, "computed_at": "2026-08-18T04:21", "partial": False},
    ]))
    assert next(c for c in sh._lane_reach() if c["id"] == "r_invariant")["pass"] is True


def test_reach_ignores_weeks_outside_the_backfill_window(monkeypatch):
    """Weeks older than BACKFILL_WEEKS keep their legacy value forever and are
    never recomputed. Failing on them would make this lane permanently red and
    therefore ignored — the way a guard dies."""
    monkeypatch.setattr(sh, "_get_json", lambda *a, **k: _trend([
        {"week_start": "2026-05-04", "distinct_external_ips": 0,
         "new_external_ips": 13, "computed_at": "2026-07-26T13:02", "partial": False},
        {"week_start": "2026-08-10", "distinct_external_ips": 72,
         "new_external_ips": 64, "computed_at": "2026-08-18T04:21", "partial": False},
    ]))
    assert next(c for c in sh._lane_reach() if c["id"] == "r_invariant")["pass"] is True


def test_reach_requires_the_partial_flag(monkeypatch):
    """Without it a Monday read charts as a 100% collapse."""
    monkeypatch.setattr(sh, "_get_json", lambda *a, **k: _trend([
        {"week_start": "2026-08-17", "distinct_external_ips": 8,
         "new_external_ips": 4, "computed_at": "2026-08-18T04:21"},
    ]))
    assert next(c for c in sh._lane_reach() if c["id"] == "r_partial")["pass"] is False


def test_reach_unreadable_is_unmeasured_not_a_pass(monkeypatch):
    monkeypatch.setattr(sh, "_get_json", lambda *a, **k: None)
    assert sh._lane_verdict(sh._lane_reach()) == "?"


def test_missing_weeks_array_is_a_failure_not_an_unknown(monkeypatch):
    """A field ABSENT from a payload means the emitter changed shape — that is
    a regression to act on, not a read we could not make."""
    monkeypatch.setattr(sh, "_get_json", lambda *a, **k: {"note": "hi"})
    assert sh._lane_reach()[0]["pass"] is False


# ── lane 5: the reachability class ─────────────────────────────────────────

def test_envelope_probe_disabled_is_unmeasured(monkeypatch):
    """A disabled probe verified NOTHING. It must never render green — this is
    the exact inversion that let three reachability bugs live for weeks."""
    monkeypatch.setenv("GROWTH_INTEGRITY_SHELL_PROBE", "0")
    lane = sh._lane_envelope()
    assert lane[0]["pass"] is None
    assert sh._lane_verdict(lane) == "?"


def test_envelope_must_assert_the_anon_path():
    """A probe that lands on the KEYED path proves nothing about real traffic —
    that is how the 07-27 pre-wall verification fooled itself. The lane has to
    check which cascade it reached, so the check must exist by name."""
    src = open(sh.__file__.replace(".pyc", ".py")).read()
    assert "e_anon_path" in src
    assert "next_session" in sh._ENVELOPE_MUST_CARRY


# ── lane 7: attribution ────────────────────────────────────────────────────

def test_attribution_flags_one_opaque_bucket(monkeypatch):
    """Measured 08-17: mcp-generic-client = 86% of real calls."""
    monkeypatch.setattr(sh, "_get_json", lambda *a, **k: {"platforms_7d": [
        {"platform": "mcp-generic-client", "calls": 1994, "agents": 42},
        {"platform": "claude", "calls": 315, "agents": 7}]})
    assert next(c for c in sh._lane_attribution()
                if c["id"] == "a_concentration")["pass"] is False


def test_attribution_passes_when_spread(monkeypatch):
    monkeypatch.setattr(sh, "_get_json", lambda *a, **k: {"platforms_7d": [
        {"platform": "claude", "calls": 500}, {"platform": "chatgpt", "calls": 500},
        {"platform": "cursor", "calls": 400}]})
    assert next(c for c in sh._lane_attribution()
                if c["id"] == "a_concentration")["pass"] is True


# ── lane 6: running-but-not-logging ────────────────────────────────────────

def test_brain_log_divergence_is_caught(monkeypatch):
    """Measured twice on 08-17/18: last_run 9min, last_log 240min. Invisible if
    you only watch `active`, which reads healthy throughout."""
    monkeypatch.setattr(sh, "_get_json", lambda *a, **k: {
        "active": True, "health": "active", "minutes_since_last_run": 9,
        "stale_minutes_since_last_log": 240, "actionable_findings_count": 11,
        "proposed_fixes_count": 0})
    assert next(c for c in sh._lane_brain() if c["id"] == "b_log_gap")["pass"] is False


def test_brain_consistent_logging_passes(monkeypatch):
    monkeypatch.setattr(sh, "_get_json", lambda *a, **k: {
        "active": True, "health": "active", "minutes_since_last_run": 30,
        "stale_minutes_since_last_log": 35, "actionable_findings_count": 2,
        "proposed_fixes_count": 0})
    assert next(c for c in sh._lane_brain() if c["id"] == "b_log_gap")["pass"] is True


# ── whole-shell shape ──────────────────────────────────────────────────────

def test_a_crashing_lane_never_takes_down_the_shell(monkeypatch):
    def boom():
        raise RuntimeError("lane exploded")
    monkeypatch.setattr(sh, "_LANES", [("x", "X", boom)])
    d = sh._run()
    assert d["lanes"][0]["verdict"] == "?"
    assert "lane exploded" in d["lanes"][0]["checks"][0]["detail"]


def test_every_lane_is_represented():
    ids = {lid for lid, _n, _f in sh._LANES}
    assert ids == {"reach", "retention", "registries", "conversion",
                   "envelope", "brain", "attribution"}
