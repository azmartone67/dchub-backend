"""Tests for routes/brain_qa_superuser_intake.py — the four rules.

The module docstring's four rules are the whole safety story, so each gets a
test whose failure means the rule is gone:
  0. canary gate  (never seed from a run whose must-fail control didn't fire)
  1. RED-critical/major or instrument fault only (BLIND/GAUGE are not defects)
  2. capped + rotated (never starve the loop, never starve its own tail)
  3. never a live board read on the hot heal path (snapshot reads only)
"""

from datetime import datetime, timedelta, timezone

from routes import brain_qa_superuser_intake as qi


def _f(key, verdict="RED", severity="critical", fault=False, title=None,
       surface="mcp"):
    return {"key": key, "verdict": verdict, "severity": severity,
            "instrument_fault": fault, "title": title or ("t-" + key),
            "surface": surface, "failing_since": None}


def _run(findings=None, canary=True, age_h=0.5):
    at = datetime.now(timezone.utc) - timedelta(hours=age_h)
    return {"canary_fired": canary, "generated_at": at.isoformat(),
            "findings": findings if findings is not None else [_f("A")]}


# ── rule 0: the canary gate ──────────────────────────────────────────────

def test_canary_did_not_fire_refuses_the_whole_run():
    # Kills: laundering an unproven harness's reds into the brain's backlog.
    why = qi.run_refusal(_run(canary=False))
    assert why and "must-fail control" in why


def test_canary_failure_refuses_even_when_reds_are_present():
    # The reds look like great evidence. They are not evidence at all.
    run = _run([_f("A"), _f("B", severity="major")], canary=False)
    assert qi.run_refusal(run) is not None


def test_missing_run_is_refused():
    assert qi.run_refusal(None) is not None
    assert qi.run_refusal({}) is not None


def test_stale_board_is_refused():
    why = qi.run_refusal(_run(age_h=50.0), max_age_h=9.0)
    assert why and "old" in why


def test_fresh_canary_verified_run_is_accepted():
    assert qi.run_refusal(_run(age_h=1.0), max_age_h=9.0) is None


def test_refresh_persists_the_refusal_and_seeds_nothing(monkeypatch):
    # Kills: leaving the PREVIOUS trusted snapshot serving findings from a run
    # we have since decided not to trust — the stalest failure, and invisible.
    saved = {}
    monkeypatch.setattr(qi, "_state_get", lambda k: None)
    monkeypatch.setattr(qi, "_state_set",
                        lambda k, v: saved.update({"k": k, "v": v}) or True)
    out = qi.refresh_snapshot(force=True, load_fn=lambda: _run(canary=False))
    assert out["ok"] and out["rows"] == 0 and out["refused"]
    assert saved["v"]["rows"] == []
    assert saved["v"]["refused"]


# ── rule 1: eligibility ──────────────────────────────────────────────────

def test_only_red_critical_major_and_faults_are_seedable():
    # Kills: seeding BLIND (probe could not look) or GAUGE (makes no claim).
    rows = [
        _f("pass", verdict="PASS"),
        _f("gauge", verdict="GAUGE", severity=None),
        _f("blind", verdict="BLIND", severity="critical"),
        _f("minor", verdict="RED", severity="minor"),
        _f("info", verdict="RED", severity="info"),
        _f("red_c", verdict="RED", severity="critical"),
        _f("red_m", verdict="RED", severity="major"),
        _f("fault", verdict="BLIND", severity="minor", fault=True),
    ]
    got = {f["key"] for f in qi.select_seedable(rows, limit=99, cycle=0)[0]}
    assert got == {"red_c", "red_m", "fault"}


def test_gauge_is_never_seedable_even_at_critical():
    # A GAUGE makes no pass/fail claim BY CONSTRUCTION; severity can't override.
    rows = [_f("g", verdict="GAUGE", severity="critical")]
    assert qi.select_seedable(rows, limit=9, cycle=0) == ([], 0)


def test_blind_without_fault_is_not_a_defect():
    rows = [_f("b", verdict="BLIND", severity="critical", fault=False)]
    assert qi.select_seedable(rows, limit=9, cycle=0) == ([], 0)


# ── rule 2: impact order, cap, rotation ─────────────────────────────────

def test_critical_outranks_major_outranks_fault():
    rows = [_f("z_fault", verdict="BLIND", severity="minor", fault=True),
            _f("y_major", severity="major"),
            _f("x_crit", severity="critical")]
    got = [f["key"] for f in qi.select_seedable(rows, limit=3, cycle=0)[0]]
    assert got == ["x_crit", "y_major", "z_fault"]


def test_cap_limits_rows_and_reports_the_true_total():
    rows = [_f("R%02d" % i) for i in range(12)]
    got, total = qi.select_seedable(rows, limit=4, cycle=0)
    assert len(got) == 4 and total == 12


def test_cap_keeps_the_worst_when_truncating():
    # Kills: capping BEFORE ordering (would drop criticals for arbitrary keys).
    rows = [_f("z1", severity="major"), _f("z2", severity="major"),
            _f("a1", severity="critical")]
    got = [f["key"] for f in qi.select_seedable(rows, limit=1, cycle=0)[0]]
    assert got == ["a1"]


def test_rotation_gives_every_eligible_finding_budget():
    # Kills: head-of-list starvation — a fixed sort under a cap means the tail
    # NEVER reaches the worklist (the r78 class, retrofitted into audit intake
    # after it shipped; built in here from the start).
    rows = [_f("R%02d" % i) for i in range(10)]
    seen = set()
    for cyc in range(5):  # ceil(10/4) = 3 cycles is enough; 5 for slack
        seen |= {f["key"] for f in qi.select_seedable(rows, limit=4, cycle=cyc)[0]}
    assert seen == {"R%02d" % i for i in range(10)}


def test_env_cap_is_honoured(monkeypatch):
    monkeypatch.setenv("QA_INTAKE_MAX", "2")
    rows = [_f("R%d" % i) for i in range(6)]
    assert len(qi.select_seedable(rows, cycle=0)[0]) == 2


def test_zero_cap_seeds_nothing_but_still_counts():
    rows = [_f("R%d" % i) for i in range(3)]
    got, total = qi.select_seedable(rows, limit=0, cycle=0)
    assert got == [] and total == 3


# ── rule 3: the heal path reads the snapshot, never the board ───────────

def test_findings_read_snapshot_only(monkeypatch):
    # Kills: a live DB/board read landing on the hot public heal path.
    called = {"n": 0}

    def _boom():
        called["n"] += 1
        raise AssertionError("live board read on the hot path")

    monkeypatch.setattr(qi, "_load_latest", _boom)
    monkeypatch.setattr(qi, "_state_get",
                        lambda k: {"rows": [_f("A")], "board_as_of": "x"})
    out = qi.qa_superuser_findings()
    assert called["n"] == 0 and len(out) == 1


def test_kill_switch_silences_the_lane(monkeypatch):
    monkeypatch.setenv("QA_INTAKE_DISABLE", "1")
    monkeypatch.setattr(qi, "_state_get", lambda k: {"rows": [_f("A")]})
    assert qi.qa_superuser_findings() == []


def test_findings_are_fail_soft_on_state_error(monkeypatch):
    def _boom(_k):
        raise RuntimeError("brain_state unreachable")
    monkeypatch.setattr(qi, "_state_get", _boom)
    assert qi.qa_superuser_findings() == []


# ── shape: stable identity + the no-body-substitution prefix ────────────

def test_issue_label_uses_the_qa_prefix():
    # Kills: dropping the prefix, which would let master-heal's FIX_MAP
    # string-replacer try to body-substitute a QA finding.
    out = qi.to_findings([_f("k1")])
    assert out[0]["issue"].startswith("qa_")


def test_identity_is_stable_across_runs():
    # The Layer-5 learn loop dedupes on (issue[:200], url); if either moved
    # per run, every cycle would re-propose the same finding forever.
    a = qi.to_findings([_f("k1")])[0]
    b = qi.to_findings([_f("k1")])[0]
    assert (a["url"], a["issue"]) == (b["url"], b["issue"])
    assert a["url"] == "dchub://qa-superuser/k1"


def test_instrument_fault_is_labelled_as_ours_not_the_platforms():
    out = qi.to_findings([_f("k", verdict="BLIND", severity="minor",
                             fault=True)])
    assert "INSTRUMENT FAULT" in out[0]["detail"]
    assert out[0]["issue"].startswith("qa_fault")


def test_rows_without_a_key_are_dropped():
    assert qi.to_findings([{"verdict": "RED", "severity": "critical"}]) == []


def test_unreadable_board_age_is_refused_not_waved_through():
    # The "cannot tell" branch, in the DANGEROUS direction: an unknown age
    # must NOT be treated as fresh, or a year-old board could seed the loop.
    assert qi.run_refusal({"canary_fired": True, "generated_at": None})
    assert qi.run_refusal({"canary_fired": True, "generated_at": "not-a-date"})
