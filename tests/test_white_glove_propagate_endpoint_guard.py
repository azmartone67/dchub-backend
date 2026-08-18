"""Guard: POST /white-glove/propagate must not re-run a day that already ran.

★ WHY THIS EXISTS. The ran_today() guard lived ONLY in
crawler_scheduler._run_white_glove_propagate. The HTTP endpoint went straight
to run_white_glove_propagation(), so any caller was one POST away from a
duplicate day of REAL outbound registry submissions.

That was survivable while the only caller was the in-process slot. It stopped
being survivable the moment an off-worker cron started POSTing here as a
backstop: the lane already runs on ~60% of days, so an unguarded backstop
would re-submit every listing on most of them.

These tests pin the three answers ran_today() can give, and what each must do:

    True  -> skip   (already ran; a second pass is duplicate submissions)
    None  -> skip   (DB unreadable; ran_today answers None rather than False
                     precisely so a caller cannot read an outage as permission)
    False -> RUN    (the slot genuinely missed; this is the whole point)
"""
import json
import sys
import types

import pytest
from flask import Flask


@pytest.fixture
def client(monkeypatch):
    from routes.white_glove_propagation import white_glove_propagation_bp
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(white_glove_propagation_bp)
    return app.test_client()


@pytest.fixture(autouse=True)
def _admin_and_switch(monkeypatch):
    """Authenticate, and keep the kill switch off, so tests exercise the
    ran_today() branch rather than short-circuiting before it."""
    import routes.white_glove_propagation as wgp
    monkeypatch.setattr(wgp, "_admin_ok", lambda: True)
    monkeypatch.setattr(wgp, "_disabled", lambda: False)
    return wgp


def _spy_run(monkeypatch, wgp):
    """Replace the real propagation with a call-recording stub."""
    calls = []

    def _fake(dry_run=True):
        calls.append({"dry_run": dry_run})
        return {"ok": True, "checked": 16, "drifted": 2,
                "auto_path": 2, "human_gated": 0}

    monkeypatch.setattr(wgp, "run_white_glove_propagation", _fake)
    return calls


def test_already_ran_today_does_not_resubmit(monkeypatch, client, _admin_and_switch):
    """The ~60%-of-days case. A healthy day must be a no-op, not a second
    pass of real submissions against every registry."""
    wgp = _admin_and_switch
    monkeypatch.setattr(wgp, "ran_today", lambda: True)
    calls = _spy_run(monkeypatch, wgp)

    r = client.post("/api/v1/admin/white-glove/propagate?dry_run=0")
    body = json.loads(r.data)

    assert r.status_code == 200
    assert body["skipped"] is True
    assert body["reason"] == "already_ran_today"
    assert calls == [], "propagation RAN on a day it had already run — every "\
                        "registry listing would be re-submitted"


def test_unknown_ran_today_skips_rather_than_gambling(monkeypatch, client,
                                                      _admin_and_switch):
    """★ None is NOT False. ran_today() answers None on a DB outage so that a
    caller cannot read 'cannot tell' as 'safe to re-run'."""
    wgp = _admin_and_switch
    monkeypatch.setattr(wgp, "ran_today", lambda: None)
    calls = _spy_run(monkeypatch, wgp)

    body = json.loads(client.post(
        "/api/v1/admin/white-glove/propagate?dry_run=0").data)

    assert body["skipped"] is True
    assert body["reason"] == "ran_today_unknown_db_unreadable"
    assert calls == [], "an unreadable DB was treated as permission to re-run"


def test_missed_day_actually_propagates(monkeypatch, client, _admin_and_switch):
    """The guard must not become a lock. When the slot genuinely missed, the
    backstop has to fire — otherwise this change just breaks the lane in a
    quieter way."""
    wgp = _admin_and_switch
    monkeypatch.setattr(wgp, "ran_today", lambda: False)
    calls = _spy_run(monkeypatch, wgp)

    body = json.loads(client.post(
        "/api/v1/admin/white-glove/propagate?dry_run=0").data)

    assert not body.get("skipped")
    assert body["checked"] == 16
    assert calls == [{"dry_run": False}], "backstop did not run on a missed day"


def test_force_overrides_the_guard(monkeypatch, client, _admin_and_switch):
    """Manual dispatch must retain an escape hatch."""
    wgp = _admin_and_switch
    monkeypatch.setattr(wgp, "ran_today", lambda: True)
    calls = _spy_run(monkeypatch, wgp)

    body = json.loads(client.post(
        "/api/v1/admin/white-glove/propagate?dry_run=0&force=1").data)

    assert not body.get("skipped")
    assert calls == [{"dry_run": False}]


def test_dry_run_is_never_gated(monkeypatch, client, _admin_and_switch):
    """A dry run submits nothing and ran_today() ignores dry rows, so gating
    it would only break the probe path."""
    wgp = _admin_and_switch
    monkeypatch.setattr(wgp, "ran_today", lambda: True)
    calls = _spy_run(monkeypatch, wgp)

    body = json.loads(client.post(
        "/api/v1/admin/white-glove/propagate?dry_run=1").data)

    assert not body.get("skipped")
    assert calls == [{"dry_run": True}]


def test_kill_switch_still_wins(monkeypatch, client, _admin_and_switch):
    """WHITE_GLOVE_PROPAGATE_DISABLE=1 must short-circuit ahead of the guard,
    and must not need a DB read to do it."""
    wgp = _admin_and_switch
    monkeypatch.setattr(wgp, "_disabled", lambda: True)

    def _boom():
        raise AssertionError("ran_today() consulted while kill-switched")

    monkeypatch.setattr(wgp, "ran_today", _boom)
    calls = _spy_run(monkeypatch, wgp)

    body = json.loads(client.post(
        "/api/v1/admin/white-glove/propagate?dry_run=0").data)

    assert body["skipped"] is True
    assert calls == []
