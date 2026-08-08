"""Tests for routes/brain_audit_intake.py — pure selection + guarded refresh.

The three rules in the module docstring are the whole safety story, so each
gets a test whose failure means the rule is gone:
  1. OPEN-RED only (never seed unverified registry rows)
  2. severity-capped (never starve the other detectors)
  3. never a live tick on the hot heal path (snapshot reads only)
"""

from routes import brain_audit_intake as ai


def _row(fid, sev="H", status="OPEN-RED", domain="brain", title="t"):
    return {"id": fid, "domain": domain, "sev": sev, "effort": "S",
            "status": status, "title": title}


# ── rule 1: OPEN-RED only ────────────────────────────────────────────────

def test_only_open_red_is_seedable():
    # Kills: seeding OPEN rows (138 unverifiable items into a 10/cycle loop).
    rows = [_row("A", status="OPEN"), _row("B", status="OPEN-RED"),
            _row("C", status="CLOSED"), _row("D", status="ACKED"),
            _row("E", status="?")]
    got = [r["id"] for r in ai.select_seedable(rows, limit=10)]
    assert got == ["B"]


def test_unknown_status_is_not_seedable():
    assert ai.select_seedable([_row("X", status="MYSTERY")], limit=5) == []


# ── rule 2: severity order + cap ─────────────────────────────────────────

def test_critical_outranks_high_outranks_medium():
    rows = [_row("M1", sev="M"), _row("C1", sev="C"), _row("H1", sev="H")]
    got = [r["id"] for r in ai.select_seedable(rows, limit=3)]
    assert got == ["C1", "H1", "M1"]


def test_cap_limits_rows():
    rows = [_row("R%02d" % i) for i in range(20)]
    assert len(ai.select_seedable(rows, limit=8)) == 8


def test_cap_keeps_the_worst_when_truncating():
    # Kills: capping BEFORE sorting (would drop criticals for arbitrary ids).
    rows = [_row("Z9", sev="M"), _row("Z8", sev="M"), _row("A1", sev="C")]
    got = [r["id"] for r in ai.select_seedable(rows, limit=1)]
    assert got == ["A1"]


def test_env_cap_is_honoured(monkeypatch):
    monkeypatch.setenv("AUDIT_INTAKE_MAX", "2")
    rows = [_row("R%d" % i) for i in range(6)]
    assert len(ai.select_seedable(rows)) == 2


# ── finding shape ────────────────────────────────────────────────────────

def test_finding_shape_matches_actionable_backend_issues():
    f = ai.to_findings([_row("SH52-001", sev="H", title="beat has no cron")])
    assert len(f) == 1
    assert set(f[0]) == {"url", "issue", "count", "detail"}
    assert f[0]["url"] == "dchub://audit/SH52-001"
    assert f[0]["count"] == 1
    assert "SH52-001" in f[0]["detail"]


def test_issue_label_is_prefixed_so_no_fix_map_key_matches():
    # Kills: dropping the prefix — the master-heal string-replacer would try
    # to body-substitute an audit finding.
    f = ai.to_findings([_row("SH52-002", title="x")])
    assert f[0]["issue"].startswith("audit_")


def test_rows_without_an_id_are_skipped():
    assert ai.to_findings([{"sev": "H", "title": "orphan"}]) == []


def test_long_titles_are_bounded():
    f = ai.to_findings([_row("SH52-003", title="y" * 500)])
    assert len(f[0]["issue"]) <= 260


# ── rule 3: the heal path reads a snapshot, never a tick ────────────────

def test_audit_findings_reads_snapshot_only(monkeypatch):
    called = {"tick": 0}

    def _boom():
        called["tick"] += 1
        raise AssertionError("the hot heal path must not run a live tick")

    monkeypatch.setattr(ai, "_state_get",
                        lambda k: {"rows": [_row("SH52-010")]})
    monkeypatch.setitem(__import__("sys").modules, "_never", None)
    out = ai.audit_findings()
    assert len(out) == 1 and called["tick"] == 0


def test_audit_findings_empty_without_snapshot(monkeypatch):
    monkeypatch.setattr(ai, "_state_get", lambda k: None)
    assert ai.audit_findings() == []


def test_kill_switch_yields_no_findings(monkeypatch):
    monkeypatch.setenv("AUDIT_INTAKE_DISABLE", "1")
    monkeypatch.setattr(ai, "_state_get",
                        lambda k: {"rows": [_row("SH52-011")]})
    assert ai.audit_findings() == []


def test_state_read_failure_is_soft(monkeypatch):
    def _raise(_k):
        raise RuntimeError("db down")
    monkeypatch.setattr(ai, "_state_get", _raise)
    assert ai.audit_findings() == []


# ── refresh cadence ─────────────────────────────────────────────────────

def _tick_with(rows):
    return lambda: {"registry": {"findings": rows, "closure_pct": 42.0,
                                 "total": 138}}


def test_refresh_persists_selected_rows(monkeypatch):
    saved = {}
    monkeypatch.setattr(ai, "_state_get", lambda k: None)
    monkeypatch.setattr(ai, "_state_set",
                        lambda k, v: saved.update({k: v}) or True)
    out = ai.refresh_snapshot(force=True, tick_fn=_tick_with(
        [_row("A", status="OPEN"), _row("B", status="OPEN-RED")]))
    assert out["ok"] and out["rows"] == 1
    assert [r["id"] for r in saved[ai._STATE_KEY]["rows"]] == ["B"]
    assert saved[ai._STATE_KEY]["closure_pct"] == 42.0


def test_refresh_skips_while_snapshot_is_fresh(monkeypatch):
    import time as _t
    monkeypatch.setattr(ai, "_state_get",
                        lambda k: {"ts": _t.time(), "rows": []})
    ran = {"n": 0}

    def _tick():
        ran["n"] += 1
        return {"registry": {"findings": []}}

    out = ai.refresh_snapshot(force=False, tick_fn=_tick)
    assert out.get("skipped") == "fresh" and ran["n"] == 0


def test_refresh_runs_when_snapshot_is_stale(monkeypatch):
    monkeypatch.setattr(ai, "_state_get", lambda k: {"ts": 0, "rows": []})
    monkeypatch.setattr(ai, "_state_set", lambda k, v: True)
    out = ai.refresh_snapshot(force=False,
                              tick_fn=_tick_with([_row("C")]))
    assert out.get("refreshed") is True and out["rows"] == 1


def test_refresh_survives_a_crashing_tick(monkeypatch):
    monkeypatch.setattr(ai, "_state_get", lambda k: None)

    def _boom():
        raise RuntimeError("lane exploded")

    out = ai.refresh_snapshot(force=True, tick_fn=_boom)
    assert out["ok"] is False and "lane exploded" in out["error"]


def test_refresh_respects_kill_switch(monkeypatch):
    monkeypatch.setenv("AUDIT_INTAKE_DISABLE", "1")
    out = ai.refresh_snapshot(force=True, tick_fn=_tick_with([_row("D")]))
    assert out.get("skipped") == "AUDIT_INTAKE_DISABLE=1"


# ── registered ≠ scheduled: the class this repo keeps re-shipping ───────
# An intake whose snapshot never refreshes serves [] forever — built-but-dark,
# the 4th-firing class the audit itself named. These assert a real driver.

def _dispatch_entry(label):
    from routes.cron_heartbeat import _DISPATCH
    return next((e for e in _DISPATCH if e[0] == label), None)


def test_refresh_has_a_scheduler_entry():
    assert _dispatch_entry("audit_intake_refresh") is not None, \
        "audit_intake_refresh must be driven by cron_heartbeat._DISPATCH"


def test_scheduler_entry_targets_the_real_route(monkeypatch):
    entry = _dispatch_entry("audit_intake_refresh")
    assert entry[1].endswith("/api/v1/brain/audit-intake/refresh")
    assert entry[2] == "POST"


def test_scheduler_predicate_can_actually_be_true(monkeypatch):
    # Kills: an arm that never fires (a predicate no clock satisfies).
    import datetime as _dt
    monkeypatch.delenv("AUDIT_INTAKE_DISABLE", raising=False)
    pred = _dispatch_entry("audit_intake_refresh")[3]
    fires = [h for h in range(24)
             if pred(_dt.datetime(2026, 8, 8, h, 5))]
    assert fires, "no hour of the day satisfies the predicate"
    assert pred(_dt.datetime(2026, 8, 8, 13, 5)) is True


def test_scheduler_predicate_honours_the_kill_switch(monkeypatch):
    import datetime as _dt
    monkeypatch.setenv("AUDIT_INTAKE_DISABLE", "1")
    pred = _dispatch_entry("audit_intake_refresh")[3]
    assert pred(_dt.datetime(2026, 8, 8, 13, 5)) is False


def test_refresh_is_refire_guarded_and_pool_throttled():
    # The tick is heavy (live probes); without both guards a sporadic
    # heartbeat stacks it within the <55-minute window.
    from routes.cron_heartbeat import _MIN_REFIRE_S, _HEAVY_LABELS
    assert _MIN_REFIRE_S.get("audit_intake_refresh", 0) >= 3600
    assert "audit_intake_refresh" in _HEAVY_LABELS
