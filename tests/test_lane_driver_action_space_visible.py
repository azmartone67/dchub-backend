"""tests/test_lane_driver_action_space_visible.py — the driver's action space is
readable from outside the process (2026-08-29).

#3322 gated registry effectors behind BRAIN_LANE_DRIVER_EFFECTORS. The only way
to confirm the gate was to read the source or trust the unit tests:
/api/v1/admin/brain/lane-driver/state returned `decisions` and `ok` and nothing
else. "No effector has been dispatched" was the closest available evidence, and
that is absence of evidence — the driver mostly chooses `stop` anyway, so the
observation is identical whether the gate works or not.

A shell whose whole subject is that decisions must be legible from outside
cannot leave its own action space unreadable.

★ THESE TESTS DRIVE THE ENDPOINT, NOT available_actions().

That is not a stylistic choice. The same vacuity has now bitten this codebase
FOUR times: the `brain_findings_DISABLED` prefix match, lane 3's tests that
exercised decision_schema() while the call site went unguarded, lane 6 shipping
a module with NO CALL SITE while its tests called the function directly (#3323),
and the scan-floor meta-guard. A helper that is correct but unreached is the
same defect as a registry that is built but unread — this repo's recurring one.

House rules: no DB, never import main, nothing at module scope.

Run:  python3 -m pytest tests/test_lane_driver_action_space_visible.py -v
"""
from __future__ import annotations

import json

import pytest


def _client(monkeypatch, key="test-admin-key"):
    """A Flask test client with ONLY this blueprint mounted — never main."""
    from flask import Flask
    from routes.brain_lane_driver import brain_lane_driver_bp

    monkeypatch.setenv("DCHUB_ADMIN_KEY", key)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NEON_DATABASE_URL", raising=False)
    app = Flask(__name__)
    app.register_blueprint(brain_lane_driver_bp)
    return app.test_client(), key


def _state(monkeypatch, **env):
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    client, key = _client(monkeypatch)
    r = client.get("/api/v1/admin/brain/lane-driver/state",
                   headers={"X-Admin-Key": key})
    assert r.status_code == 200, r.status_code
    return json.loads(r.data)


# ── ★ the endpoint actually publishes it ─────────────────────────────────

def test_the_endpoint_publishes_the_action_space(monkeypatch):
    """★ THE GATE. Without this field the flag is unverifiable from outside,
    which is what prompted the change."""
    body = _state(monkeypatch, BRAIN_LANE_DRIVER_EFFECTORS=None)
    assert "action_space" in body, \
        "the endpoint still returns only decisions — the space is unreadable"
    sp = body["action_space"]
    assert sp["count"] == len(sp["verbs"])
    assert "stop" in sp["verbs"]


def test_the_decisions_list_is_still_returned(monkeypatch):
    """THE PAIRED CONTROL. The decisions list is what an operator came for;
    adding a field must not cost them the endpoint."""
    body = _state(monkeypatch, BRAIN_LANE_DRIVER_EFFECTORS=None)
    assert body["ok"] is True
    assert "decisions" in body


def test_the_endpoint_is_still_admin_gated(monkeypatch):
    """The action space names which mutating verbs are reachable. It is not
    public."""
    client, _key = _client(monkeypatch)
    r = client.get("/api/v1/admin/brain/lane-driver/state")
    assert r.status_code == 401


# ── the flag is now observable, both ways ────────────────────────────────

def test_opted_out_is_visible_as_eight_static_verbs(monkeypatch):
    body = _state(monkeypatch, BRAIN_LANE_DRIVER_EFFECTORS=None)
    sp = body["action_space"]
    assert sp["effectors_opted_in"] is False
    assert sp["registry"] == []
    assert not [v for v in sp["verbs"] if v.startswith("effector:")]
    assert "BRAIN_LANE_DRIVER_EFFECTORS" in sp["registry_state"]


def test_opted_in_is_visible_too(monkeypatch):
    """THE PAIRED CONTROL. A field that only ever reports 'off' proves
    nothing about the flag — it would read the same if it were hardcoded."""
    from routes import squasher_action_classes as ac

    class Conn:
        def __enter__(self): return self

        def __exit__(self, *a): return False

        def cursor(self): return Cur()

    class Cur:
        def __enter__(self): return self

        def __exit__(self, *a): return False

    monkeypatch.setattr(ac, "enabled", lambda: True)
    monkeypatch.setattr(ac, "_conn", lambda: Conn())
    monkeypatch.setattr(ac, "class_rows", lambda cur:
                        [{"class": "facility_dedup_apply", "granted": True,
                          "breaker_tripped": False}])
    monkeypatch.setattr(ac, "eligible", lambda r: (True, "ok"))
    body = _state(monkeypatch, BRAIN_LANE_DRIVER_EFFECTORS="1")
    sp = body["action_space"]
    assert sp["effectors_opted_in"] is True
    assert "effector:facility_dedup_apply" in sp["verbs"]
    assert sp["registry"] == ["effector:facility_dedup_apply"]


def test_the_actuation_kill_switch_is_visible(monkeypatch):
    """BRAIN_LANE_DRIVER_ACT_DISABLED decides whether ANY verb is dispatched.
    An operator reading the space needs to know if the whole driver is parked."""
    body = _state(monkeypatch, BRAIN_LANE_DRIVER_ACT_DISABLED="1",
                  BRAIN_LANE_DRIVER_EFFECTORS=None)
    assert body["action_space"]["act_disabled"] is True
    body2 = _state(monkeypatch, BRAIN_LANE_DRIVER_ACT_DISABLED=None,
                   BRAIN_LANE_DRIVER_EFFECTORS=None)
    assert body2["action_space"]["act_disabled"] is False


# ── the three registry reasons stay distinct ON THE WIRE ─────────────────

def test_the_registry_reason_is_specific_on_the_wire(monkeypatch):
    """opted-out / globally-killed / unreadable are three different facts.
    They were kept distinct in-process; this proves they survive to the
    caller, which is the only place the distinction is any use."""
    from routes import squasher_action_classes as ac

    out = _state(monkeypatch, BRAIN_LANE_DRIVER_EFFECTORS=None)
    opted = out["action_space"]["registry_state"]

    monkeypatch.setattr(ac, "enabled", lambda: False)
    killed = _state(monkeypatch,
                    BRAIN_LANE_DRIVER_EFFECTORS="1")["action_space"]["registry_state"]

    monkeypatch.setattr(ac, "enabled", lambda: True)

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ac, "_conn", boom)
    unread = _state(monkeypatch,
                    BRAIN_LANE_DRIVER_EFFECTORS="1")["action_space"]["registry_state"]

    assert len({opted, killed, unread}) == 3, "two reasons collapsed on the wire"
    assert "connection refused" in unread


# ── a broken field must not cost the endpoint ────────────────────────────

def test_a_failure_building_the_space_degrades_the_field_not_the_endpoint(monkeypatch):
    """An operator hitting /state during an outage still needs the decisions."""
    from routes import brain_lane_driver as d

    def boom():
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(d, "available_actions", boom)
    body = _state(monkeypatch, BRAIN_LANE_DRIVER_EFFECTORS=None)
    assert body["ok"] is True
    assert "decisions" in body
    assert body["action_space"]["known"] is False
    assert "registry exploded" in body["action_space"]["error"]
