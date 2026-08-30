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


# ── lane 6: brain verdict consistency ──────────────────────────────────────

def _brain(**kw):
    base = {"active": True, "health": "active", "verdict": "healthy_backlog",
            "minutes_since_last_run": 2, "stale_minutes_since_last_log": 280,
            "actionable_findings_count": 11, "proposed_fixes_count": 0}
    base.update(kw)
    return base


def _b(d, cid, monkeypatch):
    monkeypatch.setattr(sh, "_get_json", lambda *a, **k: d)
    return next(c for c in sh._lane_brain() if c["id"] == cid)["pass"]


def test_quiet_brain_run_log_divergence_is_not_a_defect(monkeypatch):
    """THE REGRESSION THIS FILE EXISTS TO PREVENT. Live 08-18: run=2min,
    log=280min, learning_log_count=5172, verdict=healthy_backlog. The old
    rule (log <= max(run*4,120)) called this "running but not recording";
    it is the documented signature of a brain with nothing text-fixable —
    last_run_at stamps every pass, last_log_at only real learn attempts."""
    assert _b(_brain(), "b_log_gap", monkeypatch) is True


def test_healthy_working_with_a_stale_log_and_no_proposals_is_caught(monkeypatch):
    """The one genuine contradiction: healthy_working is only returned when
    pf>0 or stale<180, so this combination cannot legitimately occur."""
    assert _b(_brain(verdict="healthy_working", stale_minutes_since_last_log=400,
                     proposed_fixes_count=0), "b_log_gap", monkeypatch) is False


def test_healthy_working_with_proposals_in_flight_is_fine(monkeypatch):
    """A stale log is legitimate when proposals ARE in flight."""
    assert _b(_brain(verdict="healthy_working", stale_minutes_since_last_log=400,
                     proposed_fixes_count=3), "b_log_gap", monkeypatch) is True


def test_stalled_verdict_fails_b_active_even_though_active_is_true(monkeypatch):
    """`active` only reflects ANTHROPIC_API_KEY being set — it reads True
    straight through a stall. The verdict is the authoritative field."""
    assert _b(_brain(verdict="stalled"), "b_active", monkeypatch) is False


def test_backlog_hidden_under_healthy_quiet_is_caught(monkeypatch):
    """r36's actual bug: 'the healer's findings are clean' shipped while
    dozens of actionable findings sat open."""
    assert _b(_brain(verdict="healthy_quiet", actionable_findings_count=11),
              "b_backlog", monkeypatch) is False


def test_backlog_admitted_by_the_verdict_passes(monkeypatch):
    assert _b(_brain(verdict="healthy_backlog", actionable_findings_count=11),
              "b_backlog", monkeypatch) is True


def test_no_backlog_and_quiet_verdict_is_consistent(monkeypatch):
    assert _b(_brain(verdict="healthy_quiet", actionable_findings_count=0),
              "b_backlog", monkeypatch) is True


def test_b_backlog_is_unmeasured_when_the_count_is_absent(monkeypatch):
    """Must be `?`, never a silent True — that was the old bug."""
    assert _b(_brain(actionable_findings_count=None), "b_backlog", monkeypatch) is None


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


# ── lane 6b: the log WRITER, not the heartbeat (2026-08-18) ────────────────
#
# b_log_gap correctly stopped alarming on run>>log divergence. That leaves the
# opposite failure uncovered: if the layer-5 writer dies, `verdict` stays
# healthy_backlog and stale_minutes climbs forever with no lane objecting.
#
# Checkable because the cadence is known: last_run_at comes from evolve-cron
# (`0 * * * *`, hourly, stamped before any work) and last_log_at from
# brain-layer5 (`8 */6 * * *`). Measured six log bursts 6.0h apart, so
# stale_minutes is a 0→360 sawtooth by design.

def _lw(monkeypatch, **kw):
    monkeypatch.setattr(sh, "_get_json", lambda *a, **k: _brain(**kw))
    return next(c for c in sh._lane_brain() if c["id"] == "b_log_writing")


@pytest.mark.parametrize("mins", [0, 240, 278, 331, 359])
def test_the_sawtooth_is_never_a_stall(monkeypatch, mins):
    """Every value here was observed live on a healthy brain."""
    assert _lw(monkeypatch, stale_minutes_since_last_log=mins)["pass"] is True


def test_one_missed_writer_cycle_is_tolerated(monkeypatch):
    assert _lw(monkeypatch, stale_minutes_since_last_log=700)["pass"] is True


def test_two_silent_writer_cycles_fail(monkeypatch):
    """The failure no other lane can see: verdict still reads healthy_backlog."""
    c = _lw(monkeypatch, stale_minutes_since_last_log=721,
            verdict="healthy_backlog")
    assert c["pass"] is False
    assert "SILENT" in c["detail"]


def test_the_writer_check_ignores_the_heartbeat(monkeypatch):
    """Root-cause regression: conflating the two cadences is what produced the
    original false alarm. A wild heartbeat must not move this verdict."""
    a = _lw(monkeypatch, stale_minutes_since_last_log=240,
            minutes_since_last_run=0)["pass"]
    b = _lw(monkeypatch, stale_minutes_since_last_log=240,
            minutes_since_last_run=999)["pass"]
    assert a is b is True


def test_an_absent_log_field_is_unmeasured_not_a_pass(monkeypatch):
    assert _lw(monkeypatch, stale_minutes_since_last_log=None)["pass"] is None


# ── b_backlog: the verdict must NAME an unproposed backlog ────────────────────
# ★2026-08-30. The old rule was `verdict != "healthy_quiet"`, which the live
# failure walked straight past under a different healthy verdict: actionable=39,
# proposed=0, verdict=`healthy_working`. These three cases fence the rule that
# replaced it. Added because a mutation showed the new rule had NO test — the
# check could have been reverted to the weak form with the suite still green,
# which is the same defect class this shell exists to catch.

def test_b_backlog_rejects_healthy_working_while_a_backlog_sits_unproposed(monkeypatch):
    """The live 2026-08-30 state: 39 open, 0 proposed, verdict healthy_working."""
    assert _b(_brain(verdict="healthy_working", actionable_findings_count=39,
                     proposed_fixes_count=0), "b_backlog", monkeypatch) is False


def test_b_backlog_accepts_the_verdict_that_names_the_backlog(monkeypatch):
    assert _b(_brain(verdict="healthy_backlog", actionable_findings_count=39,
                     proposed_fixes_count=0), "b_backlog", monkeypatch) is True


def test_b_backlog_accepts_a_backlog_that_is_being_worked(monkeypatch):
    """Proposals in flight against an open backlog is healthy_working, honestly."""
    assert _b(_brain(verdict="healthy_working", actionable_findings_count=39,
                     proposed_fixes_count=3), "b_backlog", monkeypatch) is True
