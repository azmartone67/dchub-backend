"""Tests for routes/brain_site_qa_intake.py — the website-master intake.

This board differs from the qa-superuser board in two ways that drive the
whole design, so both get their own section:
  * it has NO must-fail control, so rule 0 is blast-radius + staleness, and
    those gates must actually refuse;
  * it records OUR harness crashing as status='fail', so the intake — not the
    board — has to tell an instrument fault from a platform defect.

Plus the usual: capped + rotated, and never a live board read on the hot path.
"""

import os
import re
from datetime import datetime, timedelta, timezone

from routes import brain_site_qa_intake as si


def _a(name, severity="p0", fails=3, msg="500 from /x", meta=None,
       first_failed_h=5.0):
    at = datetime.now(timezone.utc) - timedelta(hours=first_failed_h)
    return {"test_name": name, "severity": severity, "message": msg,
            "first_failed_at": at.isoformat(), "consecutive_failures": fails,
            "proposed_fix": None, "metadata": meta or {"url": "/x",
                                                       "http_code": 500}}


def _board(alerts=None, configured=28, age_h=0.2):
    at = datetime.now(timezone.utc) - timedelta(hours=age_h)
    return {"alerts": alerts if alerts is not None else [_a("t1")],
            "tests_configured": configured, "last_run_at": at.isoformat()}


# ── rule 0: the two gates that stand in for a canary ────────────────────

def test_blast_radius_refuses_a_suite_wide_failure():
    # Kills: seeding 20 p0s from one broken prober run. 20/28 red at once is
    # far likelier an instrument/network event than 20 simultaneous outages.
    board = _board([_a("t%d" % i) for i in range(20)], configured=28)
    why = si.run_refusal(board, max_ratio=0.5)
    assert why and "failing at once" in why


def test_blast_radius_says_it_is_not_a_canary():
    # The refusal must not overclaim: it cannot tell a real site-wide outage
    # from a broken prober, and it has to say so.
    board = _board([_a("t%d" % i) for i in range(20)], configured=28)
    why = si.run_refusal(board, max_ratio=0.5)
    assert "must-fail control" in why


def test_a_normal_number_of_failures_is_accepted():
    board = _board([_a("t1"), _a("t2")], configured=28)
    assert si.run_refusal(board, max_ratio=0.5) is None


def test_unknown_suite_size_is_refused():
    # No suite size => no blast radius => the only canary-substitute is gone.
    for bad in (None, 0, "28"):
        board = _board([_a("t1")], configured=bad)
        assert si.run_refusal(board) is not None, bad


def test_stale_board_is_refused():
    why = si.run_refusal(_board(age_h=48.0), max_age_h=2.0)
    assert why and "last ran" in why


def test_unreadable_last_run_is_refused_not_waved_through():
    # The "cannot tell" branch in the DANGEROUS direction.
    for bad in (None, "", "not-a-date"):
        b = _board(); b["last_run_at"] = bad
        assert si.run_refusal(b) is not None, bad


def test_empty_board_is_refused():
    assert si.run_refusal(None) is not None
    assert si.run_refusal({}) is not None


def test_refresh_persists_the_refusal_and_seeds_nothing(monkeypatch):
    saved = {}
    monkeypatch.setattr(si, "state_get", lambda k: None)
    monkeypatch.setattr(si, "state_set",
                        lambda k, v: saved.update({"v": v}) or True)
    board = _board([_a("t%d" % i) for i in range(25)], configured=28)
    out = si.refresh_snapshot(force=True, load_fn=lambda: board)
    assert out["ok"] and out["rows"] == 0 and out["refused"]
    assert saved["v"]["rows"] == [] and saved["v"]["refused"]


# ── our crash vs their outage — the board does not distinguish these ────

def test_the_runner_exception_marker_is_pinned_to_the_producer():
    # Kills: site_qa.py rewording its runner-exception string, after which
    # every harness crash would silently re-classify as a platform defect.
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(si.__file__))), "routes", "site_qa.py"),
        encoding="utf-8").read()
    assert si.RUNNER_EXCEPTION_MARKER in src, (
        "site_qa.py no longer emits %r — the instrument-fault split is broken"
        % si.RUNNER_EXCEPTION_MARKER)
    # and it must be the error_detail assignment, not an incidental mention
    assert re.search(r'error_detail["\']\s*:\s*f?["\']runner exception:', src)


def test_a_runner_exception_is_labelled_ours_not_the_platforms():
    out = si.to_findings([_a("t1", msg="runner exception: KeyError('x')")])
    assert "INSTRUMENT FAULT" in out[0]["detail"]
    assert out[0]["issue"].startswith("siteqa_fault")


def test_a_platform_failure_is_not_labelled_an_instrument_fault():
    out = si.to_findings([_a("t1", msg="500 from /markets")])
    assert "INSTRUMENT FAULT" not in out[0]["detail"]
    assert out[0]["issue"].startswith("siteqa_p0")


def test_instrument_faults_rank_below_confirmed_platform_failures():
    rows = [_a("z_fault", severity="p0",
               msg="runner exception: boom"),
            _a("y_p1", severity="p1"), _a("x_p0", severity="p0")]
    got = [a["test_name"] for a in si.select_seedable(rows, limit=3, cycle=0)[0]]
    assert got == ["x_p0", "y_p1", "z_fault"]


def test_a_runner_exception_is_seedable_even_at_low_severity():
    # A broken probe is real work no matter what severity the test declared —
    # it is invisible anywhere except this board.
    rows = [_a("t1", severity="p3", msg="runner exception: boom")]
    assert len(si.select_seedable(rows, limit=5, cycle=0)[0]) == 1


# ── rule 1: severity floor + anti-flap ──────────────────────────────────

def test_only_p0_and_p1_are_seedable():
    rows = [_a("a", severity="p0"), _a("b", severity="p1"),
            _a("c", severity="p2"), _a("d", severity="p3"),
            _a("e", severity=None)]
    got = {a["test_name"] for a in si.select_seedable(rows, limit=9, cycle=0)[0]}
    assert got == {"a", "b"}


def test_a_single_failure_is_a_flap_not_a_finding():
    # Kills: seeding a 15-minute-old blip into a backlog that dedupes and
    # persists. Two consecutive failures = ~30 min of sustained failure.
    rows = [_a("once", fails=1), _a("twice", fails=2)]
    got = {a["test_name"] for a in si.select_seedable(rows, limit=9, cycle=0)[0]}
    assert got == {"twice"}


def test_anti_flap_floor_is_env_tunable(monkeypatch):
    monkeypatch.setenv("SITE_QA_INTAKE_MIN_FAILS", "4")
    rows = [_a("a", fails=3), _a("b", fails=4)]
    got = {a["test_name"] for a in si.select_seedable(rows, limit=9, cycle=0)[0]}
    assert got == {"b"}


def test_a_missing_failure_count_is_not_eligible():
    rows = [dict(_a("a"), consecutive_failures=None)]
    assert si.select_seedable(rows, limit=9, cycle=0) == ([], 0)


# ── rule 2: cap + rotation ──────────────────────────────────────────────

def test_cap_limits_rows_and_reports_the_true_total():
    rows = [_a("t%02d" % i) for i in range(9)]
    got, total = si.select_seedable(rows, limit=3, cycle=0)
    assert len(got) == 3 and total == 9


def test_cap_keeps_the_worst_when_truncating():
    rows = [_a("z1", severity="p1"), _a("z2", severity="p1"),
            _a("a1", severity="p0")]
    got = [a["test_name"] for a in si.select_seedable(rows, limit=1, cycle=0)[0]]
    assert got == ["a1"]


def test_rotation_gives_every_eligible_alert_budget():
    rows = [_a("t%02d" % i) for i in range(9)]
    seen = set()
    for cyc in range(4):
        seen |= {a["test_name"]
                 for a in si.select_seedable(rows, limit=3, cycle=cyc)[0]}
    assert seen == {"t%02d" % i for i in range(9)}


def test_env_cap_is_honoured(monkeypatch):
    monkeypatch.setenv("SITE_QA_INTAKE_MAX", "2")
    rows = [_a("t%d" % i) for i in range(6)]
    assert len(si.select_seedable(rows, cycle=0)[0]) == 2


# ── rule 3: the heal path reads the snapshot, never the board ───────────

def test_findings_read_snapshot_only(monkeypatch):
    def _boom():
        raise AssertionError("live board read on the hot path")
    monkeypatch.setattr(si, "_load_board", _boom)
    monkeypatch.setattr(si, "state_get",
                        lambda k: {"rows": [_a("t1")], "board_as_of": "x"})
    assert len(si.site_qa_findings()) == 1


def test_kill_switch_silences_the_lane(monkeypatch):
    monkeypatch.setenv("SITE_QA_INTAKE_DISABLE", "1")
    monkeypatch.setattr(si, "state_get", lambda k: {"rows": [_a("t1")]})
    assert si.site_qa_findings() == []


def test_findings_are_fail_soft_on_state_error(monkeypatch):
    def _boom(_k):
        raise RuntimeError("brain_state unreachable")
    monkeypatch.setattr(si, "state_get", _boom)
    assert si.site_qa_findings() == []


# ── shape ───────────────────────────────────────────────────────────────

def test_issue_label_uses_the_siteqa_prefix():
    assert si.to_findings([_a("t1")])[0]["issue"].startswith("siteqa_")


def test_identity_is_stable_across_runs():
    a = si.to_findings([_a("t1")])[0]
    b = si.to_findings([dict(_a("t1"), consecutive_failures=99)])[0]
    assert (a["url"], a["issue"]) == (b["url"], b["issue"])
    assert a["url"] == "dchub://site-qa/t1"


def test_rows_without_a_test_name_are_dropped():
    assert si.to_findings([{"severity": "p0"}]) == []


def test_non_dict_metadata_does_not_explode():
    assert len(si.to_findings([dict(_a("t1"), metadata="oops")])) == 1
