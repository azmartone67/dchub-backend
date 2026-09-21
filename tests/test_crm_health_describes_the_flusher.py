#!/usr/bin/env python3
"""/crm/health must describe the process that FLUSHES, not the one answering.

★ THE DEFECT (measured 2026-09-21, after #5041). GET /api/v1/admin/crm/health
is served by dchub-backend (DCHUB_ROLE=web). It said provider 'hubspot',
destination_configured false, "HUBSPOT_API_KEY is set but does not start with
'pat-'". The scheduled flush runs only on dchub-worker (scheduler threads start
only where main._ROLE_RUNS_BG), and its 07:01:54Z log said "NOT delivering
(destination_not_configured) — CRM_PROVIDER='stub' and no credential is set".
Two services, two CRM envs, and the verdict described the one that never runs
the slot: fixing the key on web alone would have turned health green while the
worker kept skipping. And _run_crm_outbound_flush never raised, so its dead-man
beat said 'success' for a flush that delivered nothing.

Driven through the REAL flush_outbound_queue, admin_health,
_run_crm_outbound_flush and _run_with_guard, over the FakeDB of
test_crm_config_failure_never_burns_a_lead — which executes the SQL it is given
and learns crm_flush_last's columns and primary key from the schema script.

MUST-FAIL CONTROLS included.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re

import pytest

from tests.test_crm_config_failure_never_burns_a_lead import (  # noqa: F401 — m is a fixture
    ADMIN, HS_201, HS_401, HS_503, NOW, FakeDB, FakeHubSpot, _client,
    _incident_rows, _row, _wire, m)


def _be(m, monkeypatch, role, service, **env):
    """Become one service: its DCHUB_ROLE, Railway service name and CRM env."""
    monkeypatch.setenv("DCHUB_ROLE", role)
    monkeypatch.setenv("RAILWAY_SERVICE_NAME", service)
    for k, v in env.items():
        monkeypatch.setattr(m, k, v)


def _worker(m, monkeypatch, **env):
    """dchub-worker as measured 2026-09-21: CRM_PROVIDER unset, no credential."""
    _be(m, monkeypatch, "worker", "dchub-worker",
        **{"CRM_PROVIDER": "stub", "HUBSPOT_API_KEY": "", **env})


def _web(m, monkeypatch, **env):
    """dchub-backend as measured 2026-09-21: hubspot, a key without 'pat-'."""
    _be(m, monkeypatch, "web", "dchub-backend",
        **{"CRM_PROVIDER": "hubspot", "HUBSPOT_API_KEY": "eu1-legacy-key", **env})


def _health(m):
    return _client(m).get("/api/v1/admin/crm/health", headers=ADMIN).get_json()


# ── 1. health reads the flusher's row ────────────────────────────────

def test_THE_INCIDENT_health_on_web_reports_the_workers_flush(m, monkeypatch):
    db, hs = _wire(m, monkeypatch, _incident_rows())
    _worker(m, monkeypatch)
    out = m.flush_outbound_queue(limit=100, trigger="scheduler")
    assert out["skipped"] == "destination_not_configured", out
    assert (out["recorded"], out["unsent"]) == (True, 31), out
    db.flush_last["worker"]["ran_at"] = NOW - _dt.timedelta(hours=20)

    _web(m, monkeypatch)
    j = _health(m)
    lf = j["last_flush"]
    assert lf["host"] == {"role": "worker", "service": "dchub-worker", "replica": None}
    assert (lf["trigger"], lf["provider"], lf["unsent"]) == ("scheduler", "stub", 31)
    assert lf["age_hours"] == 20.0 and lf["ran_at"].startswith("2026-09-20T11:19")
    assert lf["slot_status"] == "stalled: destination_not_configured"
    assert lf["config"]["config_gap"].startswith("CRM_PROVIDER='stub' and no credential")
    assert j["this_process"] == {"role": "web", "service": "dchub-backend", "replica": None}
    assert j["flusher_config_mismatch"] is True
    assert j["flusher_config_diff"]["provider"] == {"this_process": "hubspot", "flusher": "stub"}
    assert j["stalled"] is True, j
    for part in ("31 lead(s) queued", "dchub-worker[worker]", "20.0h ago",
                 "CRM_PROVIDER='stub' and no credential", "dchub-backend[web]"):
        assert part in j["stalled_reason"], (part, j["stalled_reason"])
    assert hs.calls == []


def test_MUST_FAIL_CONTROL_fixing_the_key_on_web_alone_is_not_green(m, monkeypatch):
    """The trap the task names: web's own env becomes fine, the worker's does not."""
    _wire(m, monkeypatch, _incident_rows())
    _worker(m, monkeypatch)
    m.flush_outbound_queue(trigger="scheduler")
    _web(m, monkeypatch, HUBSPOT_API_KEY="pat-na1-fixed")
    j = _health(m)
    assert j["destination_configured"] is True, "web's own env should now pass"
    assert j["stalled"] is True, "health went green while the flusher still skips"
    assert set(j["flusher_config_diff"]) == {"provider", "destination_configured",
                                             "config_gap"}, j["flusher_config_diff"]
    assert "destination_configured=True does not describe the flusher" in j["stalled_reason"]


def test_both_services_fixed_and_a_delivering_flush_clears_it(m, monkeypatch):
    db, hs = _wire(m, monkeypatch, [_row(1), _row(2)])
    fixed = {"CRM_PROVIDER": "hubspot", "HUBSPOT_API_KEY": "pat-na1-fixed"}
    _worker(m, monkeypatch, **fixed)
    out = m.flush_outbound_queue(trigger="scheduler")
    assert (out["pushed"], out["unsent"]) == (2, 0), out
    _web(m, monkeypatch, **fixed)
    j = _health(m)
    assert j["last_flush"]["slot_status"] == "success"
    assert (j["flusher_config_mismatch"], j["flusher_config_diff"]) == (False, {})
    assert (j["stalled"], j["stalled_reason"]) == (False, None), j


def test_a_flush_on_web_does_not_stand_in_for_the_worker(m, monkeypatch):
    """Web CAN deliver through the admin route; the schedule still runs on the
    worker, so the worker's row stays the verdict and the mismatch stays visible
    even with nothing queued."""
    _wire(m, monkeypatch, [_row(1)])
    _worker(m, monkeypatch)
    m.flush_outbound_queue(trigger="scheduler")
    _web(m, monkeypatch, HUBSPOT_API_KEY="pat-na1-fixed")
    r = _client(m).post("/api/v1/admin/crm/flush", headers=ADMIN).get_json()
    assert r["pushed"] == 1, r
    j = _health(m)
    assert set(j["last_flush_by_role"]) == {"worker", "web"}
    assert j["last_flush_by_role"]["web"]["trigger"] == "admin"
    assert j["last_flush"]["host"]["role"] == "worker"
    assert j["flusher_config_mismatch"] is True
    assert j["stalled"] is False, "nothing is queued any more"


def test_an_unset_role_process_is_the_flusher_when_no_worker_recorded(m, monkeypatch):
    _wire(m, monkeypatch, [_row(1)])
    m.flush_outbound_queue(trigger="scheduler")      # DCHUB_ROLE unset → 'all'
    _web(m, monkeypatch, HUBSPOT_API_KEY="pat-na1-test")   # same env as that process
    j = _health(m)
    assert j["last_flush"]["host"]["role"] == "all"
    assert j["flusher_config_mismatch"] is False


def test_with_no_flusher_row_health_falls_back_to_its_own_env(m, monkeypatch):
    _wire(m, monkeypatch, [_row(1)])
    _web(m, monkeypatch)
    j = _health(m)
    assert (j["last_flush"], j["flusher_config_mismatch"]) == (None, None)
    assert j["stalled"] is True and "pat-" in j["stalled_reason"], j


def test_a_healthy_flusher_is_not_stalled_by_a_web_without_the_key(m, monkeypatch):
    """The other direction: web's env reaches nothing, but web never flushes on
    schedule — the worker delivers, so rows queued since are not stalled."""
    db, _ = _wire(m, monkeypatch, [_row(1)])
    _worker(m, monkeypatch, CRM_PROVIDER="hubspot", HUBSPOT_API_KEY="pat-na1-ok")
    m.flush_outbound_queue(trigger="scheduler")
    db.rows.append(_row(2))                           # arrived after the flush
    _web(m, monkeypatch)
    j = _health(m)
    assert j["destination_configured"] is False and j["flusher_config_mismatch"] is True
    assert j["stalled"] is False, j["stalled_reason"]


# ── 2. the dead-man status, from real flushes ────────────────────────

def _slot(m, monkeypatch, rows, hubspot=None, **env):
    _wire(m, monkeypatch, rows, hubspot)
    _worker(m, monkeypatch, **env)
    return m.flush_slot_status(m.flush_outbound_queue(trigger="scheduler"))


HS = {"CRM_PROVIDER": "hubspot", "HUBSPOT_API_KEY": "pat-na1-test"}


@pytest.mark.parametrize("case, expect_ok", [
    ("stub default, leads waiting", False),
    ("stub default, nothing unsent", True),
    ("kill switch", True),
    ("destination refuses every push (401)", False),
    ("every push transient (503)", False),
    ("one transient, one delivered", True),
    ("delivered", True),
])
def test_each_outcome_lands_on_the_right_side_of_the_board(m, monkeypatch, case, expect_ok):
    from routes.ingest_runs import _OK_STATUS
    two = [_row(1), _row(2)]
    status = {
        "stub default, leads waiting": lambda: _slot(m, monkeypatch, two),
        "stub default, nothing unsent": lambda: _slot(m, monkeypatch, [_row(1, status="pushed")]),
        "kill switch": lambda: _slot(m, monkeypatch, two, DISABLE=True),
        "destination refuses every push (401)":
            lambda: _slot(m, monkeypatch, two, FakeHubSpot(default=HS_401), **HS),
        "every push transient (503)":
            lambda: _slot(m, monkeypatch, two, FakeHubSpot(default=HS_503), **HS),
        "one transient, one delivered":
            lambda: _slot(m, monkeypatch, two, FakeHubSpot(
                default=HS_201, answers={"lead1@example.com": HS_503}), **HS),
        "delivered": lambda: _slot(m, monkeypatch, two, **HS),
    }[case]()
    assert len(status) <= 40, status
    assert (status.lower() in _OK_STATUS) is expect_ok, (case, status)


def test_no_db_is_an_error_status(m, monkeypatch):
    monkeypatch.setattr(m, "_conn", lambda: None)
    _worker(m, monkeypatch, **HS)
    out = m.flush_outbound_queue(trigger="scheduler")
    assert out["recorded"] is False and m.flush_slot_status(out) == "error: no_db"


def test_an_unrecordable_skip_is_a_stall_not_an_ok(m, monkeypatch):
    """No count, no proof the queue is empty: the stub-default carve-out needs
    unsent == 0, not a missing number."""
    db, _ = _wire(m, monkeypatch, [_row(1, status="pushed")])
    monkeypatch.setattr(m, "_SCHEMA_SQL", re.sub(
        r"CREATE TABLE IF NOT EXISTS crm_flush_last \(.*?\);", "", m._SCHEMA_SQL, flags=re.S))
    _worker(m, monkeypatch)
    out = m.flush_outbound_queue(trigger="scheduler")
    assert out["recorded"] is False and "unsent" not in out, out
    assert m.flush_slot_status(out) == "stalled: destination_not_configured"


# ── 3. every exit is recorded, and recording never fails the flush ───

def test_an_exception_mid_flush_is_recorded_then_raised(m, monkeypatch):
    db, _ = _wire(m, monkeypatch, [_row(1)])
    _worker(m, monkeypatch, **HS)

    def boom(lead):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(m, "_dispatch_push", boom)
    with pytest.raises(RuntimeError, match="kaboom"):
        m.flush_outbound_queue(trigger="scheduler")
    summary = db.flush_last["worker"]["summary"]
    assert summary["error"] == "exception: kaboom", summary
    assert m.flush_slot_status(summary) == "error: exception: kaboom"


def test_the_kill_switch_exit_is_recorded(m, monkeypatch):
    db, _ = _wire(m, monkeypatch, [_row(1)])
    _worker(m, monkeypatch, DISABLE=True)
    m.flush_outbound_queue(trigger="scheduler")
    summary = db.flush_last["worker"]["summary"]
    assert (summary["skipped"], summary["config"]["disable"]) == ("disabled", True)


def test_a_record_that_cannot_be_written_never_fails_the_flush(m, monkeypatch):
    db, hs = _wire(m, monkeypatch, [_row(1)])
    monkeypatch.setattr(m, "_SCHEMA_SQL", re.sub(
        r"CREATE TABLE IF NOT EXISTS crm_flush_last \(.*?\);", "", m._SCHEMA_SQL, flags=re.S))
    _worker(m, monkeypatch, **HS)
    out = m.flush_outbound_queue(trigger="scheduler")
    assert (out["pushed"], out["recorded"]) == (1, False), out
    assert db.flush_last == {}


def test_the_row_keeps_nothing_secret(m, monkeypatch):
    db, _ = _wire(m, monkeypatch, [_row(1)])
    _worker(m, monkeypatch, CRM_PROVIDER="hubspot", HUBSPOT_API_KEY="pat-na1-SECRET-VALUE")
    m.flush_outbound_queue(trigger="scheduler")
    assert "SECRET-VALUE" not in repr(db.flush_last)


# ── 4. the scheduled slot beats what the flush did ───────────────────

def test_the_scheduled_slot_beats_what_the_flush_did(m, monkeypatch):
    """Real _run_crm_outbound_flush inside the real _run_with_guard."""
    from tests.test_worker_deadman_beats import _exec_fn, _guard
    db, hs = _wire(m, monkeypatch, _incident_rows())
    _worker(m, monkeypatch)
    sink = []
    guard, ns = _guard(monkeypatch, sink)
    job = _exec_fn("crawler_scheduler.py", "_run_crm_outbound_flush",
                   {"logger": logging.getLogger("t")})
    guard("crm_outbound_flush", job)
    assert [(f, kw["status"]) for f, kw in sink] == [
        ("worker:crm_outbound_flush", "stalled: destination_not_configured")], sink
    assert db.flush_last["worker"]["summary"]["trigger"] == "scheduler"

    sink.clear()
    _worker(m, monkeypatch, CRM_PROVIDER="hubspot", HUBSPOT_API_KEY="pat-na1-fixed")
    guard("crm_outbound_flush", job)
    assert [(f, kw["status"]) for f, kw in sink] == [
        ("worker:crm_outbound_flush", "success")], sink
    assert len(hs.calls) == 31


# ── the fake itself ──────────────────────────────────────────────────

def test_the_fake_holds_the_upsert_to_the_ddl(m):
    db = FakeDB([])
    with pytest.raises(AssertionError, match="does not exist"):
        db.run("SELECT role FROM crm_flush_last", ())
    db.run(m._SCHEMA_SQL, None)
    assert (db.flush_cols, db.flush_pk) == (["role", "ran_at", "summary"], ["role"])
    with pytest.raises(AssertionError, match="ON CONFLICT"):
        db.run(m._FLUSH_LAST_UPSERT.replace("ON CONFLICT (role)", "ON CONFLICT (ran_at)"),
               ("worker", "{}"))
    with pytest.raises(AssertionError, match="no column"):
        db.run("SELECT nonsense FROM crm_flush_last", ())
