"""Gate liveness ledger — predicate fences.

scripts/gate_runs_selftest.py is the must-fail CONTROL (it proves the predicate
can still go red and runs in CI as its own step). This file fences the parts a
control cannot: the registry's shape, the wire-level verdict vocabulary, and the
inverted semantic that `verdict='fail'` is HEALTHY.
"""
import datetime

import pytest

from routes.gate_runs import (
    GATE_REGISTRY,
    _OK_VERDICT,
    _VERDICTS,
    _SELFTEST,
    evaluate_gate,
)

NOW = datetime.datetime(2026, 8, 31, 12, 0, tzinfo=datetime.timezone.utc)


def healthy(**over):
    rec = {
        "last_run": NOW - datetime.timedelta(hours=1),
        "last_verdict": "pass",
        "last_refusal": NOW - datetime.timedelta(days=7),
        "refusals_total": 3,
        "last_checked_n": 412,
        "consecutive_vacuous": 0,
        "selftest": "pass",
        "cadence_hours": 48,
        "first_seen": NOW - datetime.timedelta(days=200),
    }
    rec.update(over)
    return rec


def test_healthy_row_is_quiet():
    assert evaluate_gate(healthy(), NOW) == ([], [])


@pytest.mark.parametrize("mutation,fragment", [
    ({"last_run": None}, "never ran"),
    ({"last_run": NOW - datetime.timedelta(hours=200)}, "cadence"),
    ({"last_verdict": "unmeasured"}, "unmeasured"),
    ({"last_verdict": "greenish"}, "unknown"),
    ({"last_checked_n": 0}, "vacuous"),
    ({"consecutive_vacuous": 3}, "examined nothing"),
])
def test_each_alarm_class_fires(mutation, fragment):
    alarms, _ = evaluate_gate(healthy(**mutation), NOW)
    assert any(fragment in a.lower() for a in alarms), alarms


def test_fail_verdict_is_healthy_not_an_alarm():
    """★★★ The inverted semantic. `fail` means the gate REFUSED something and
    did its job. If this test ever 'fixes' itself into expecting an alarm, the
    whole board inverts and every working gate reads broken."""
    alarms, _ = evaluate_gate(healthy(last_verdict="fail", last_refusal=NOW), NOW)
    assert alarms == []
    assert "fail" in _OK_VERDICT


def test_no_scope_zero_is_affirmative_not_vacuous():
    """The gate analogue of the feed ledger's no_new_data. A delta gate on a PR
    that touched no Python examined zero items and is healthy."""
    alarms, _ = evaluate_gate(healthy(last_verdict="no_scope", last_checked_n=0), NOW)
    assert alarms == []


@pytest.mark.parametrize("selftest,fragment", [
    ("absent", "unproven"),
    ("fail", "must-fail control is fail"),
])
def test_g5_advises_and_does_not_alarm(selftest, fragment):
    """Advisory until DCHUB_GATE_G5_BLOCKS=1 (phase 4), so shipping the ledger
    cannot turn 11 uncontrolled gates into 11 red alarms on day one."""
    alarms, advisories = evaluate_gate(healthy(selftest=selftest), NOW)
    assert any(fragment in a.lower() for a in advisories), advisories
    assert not any(fragment in a.lower() for a in alarms), alarms


def test_g6_advises_only_after_the_window_and_never_alarms():
    old = healthy(refusals_total=0, last_refusal=None)
    young = healthy(refusals_total=0, last_refusal=None,
                    first_seen=NOW - datetime.timedelta(days=10))
    alarms_o, adv_o = evaluate_gate(old, NOW)
    alarms_y, adv_y = evaluate_gate(young, NOW)
    assert any("never refused" in a.lower() for a in adv_o)
    assert alarms_o == []
    assert adv_y == [] and alarms_y == []


def test_registry_is_job_granular_and_declares_blocking():
    assert len(GATE_REGISTRY) >= 8
    for gate, (repo, cad, blocking) in GATE_REGISTRY.items():
        assert ":" in gate, "registry key must be workflow:job, got %r" % gate
        assert repo and cad > 0
        assert isinstance(blocking, bool)
    # pre-merge holds four blocking gates plus one that cannot fail a PR —
    # the reason the registry is job-granular rather than workflow-granular.
    pm = {g: v for g, v in GATE_REGISTRY.items() if g.startswith("pre-merge:")}
    assert len(pm) >= 5
    assert pm["pre-merge:smoke-probe"][2] is False
    assert pm["pre-merge:unit-tests"][2] is True
    # ★ 2026-09-05: flipped to blocking when the gate became a ratchet. It was
    # False because job-level continue-on-error made it unable to fail a PR —
    # the registry recorded that honestly. The continue-on-error is gone.
    assert GATE_REGISTRY["check-route-tables:check"][2] is True


def test_verdict_and_selftest_vocabularies_are_closed():
    assert _OK_VERDICT < _VERDICTS
    assert "unmeasured" in _VERDICTS and "unmeasured" not in _OK_VERDICT
    assert _SELFTEST == {"pass", "fail", "absent"}


def test_missing_cadence_falls_back_and_does_not_crash():
    rec = healthy()
    rec.pop("cadence_hours")
    alarms, _ = evaluate_gate(rec, NOW)
    assert alarms == []


def _client(monkeypatch):
    from flask import Flask
    from routes.gate_runs import register_gate_runs
    for v in ("DATABASE_URL", "NEON_DATABASE_URL", "POSTGRES_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", "k")
    app = Flask(__name__)
    register_gate_runs(app)
    return app.test_client()


def test_blueprint_is_routable_not_merely_registered(monkeypatch):
    """Registered is not routable. A blueprint that imports fine and serves
    nothing is the failure mode this whole ledger exists to make visible."""
    from flask import Flask
    from routes.gate_runs import register_gate_runs
    app = Flask(__name__)
    register_gate_runs(app)
    urls = {str(r): sorted(r.methods - {"HEAD", "OPTIONS"}) for r in app.url_map.iter_rules()}
    assert urls.get("/api/v1/ops/gates") == ["GET"]
    assert urls.get("/api/v1/admin/gates/beat") == ["POST"]


def test_beat_fails_closed_without_an_admin_key(monkeypatch):
    c = _client(monkeypatch)
    assert c.post("/api/v1/admin/gates/beat", json={"gate": "a:b"}).status_code == 401
    assert c.post("/api/v1/admin/gates/beat", json={"gate": "a:b"},
                  headers={"X-Admin-Key": "wrong"}).status_code == 401


@pytest.mark.parametrize("payload,fragment", [
    ({"verdict": "pass"}, "gate required"),
    ({"gate": "a:b", "verdict": "greenish"}, "verdict must be one of"),
    ({"gate": "a:b", "selftest": "maybe"}, "selftest must be one of"),
])
def test_bad_arguments_are_400_not_503(monkeypatch, payload, fragment):
    """★ Argument validation must run BEFORE the DSN check. With the order
    reversed a bad verdict returns 503 'no DATABASE_URL' — an infrastructure
    error standing in for a caller error, which a sender retries forever.
    Same family as an MCP tool answering 200 to a bad argument."""
    r = _client(monkeypatch).post("/api/v1/admin/gates/beat", json=payload,
                                  headers={"X-Admin-Key": "k"})
    assert r.status_code == 400, r.get_json()
    assert fragment in r.get_json()["error"]


def test_valid_payload_without_a_database_is_503(monkeypatch):
    r = _client(monkeypatch).post("/api/v1/admin/gates/beat",
                                  json={"gate": "a:b", "verdict": "pass"},
                                  headers={"X-Admin-Key": "k"})
    assert r.status_code == 503
