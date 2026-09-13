#!/usr/bin/env python3
"""tests/test_spec_debt_finding_evidence_endpoint.py — the evidence read is
admin-only, validates its input, and never turns a failed read into "quiet".

NO NETWORK, NO DATABASE: read_evidence is replaced. A read failure must be a 503
with state UNMEASURED; an empty 200 would read as "nothing is firing" to the
closer that consumes it.
"""
import pytest
from flask import Flask

PATH = "/api/v1/brain/spec-debt/finding-evidence"
KEY = "test-admin-key"
GOOD = {"findings": [{"issue": "operator_profile_gap:Equinix", "url": "/operators/equinix"}]}


@pytest.fixture
def app(monkeypatch):
    from routes import brain_spec_debt as m
    for name in ("DCHUB_INTERNAL_KEY", "INTERNAL_KEY", "SPEC_DEBT_QUEUE_DISABLE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DCHUB_ADMIN_KEY", KEY)
    a = Flask("spec-debt-evidence-test")
    a.register_blueprint(m.brain_spec_debt_bp)
    return a


def _fake_read(monkeypatch, result=None, raises=None):
    calls = []

    def fake(targets):
        calls.append(targets)
        if raises:
            raise raises
        return result

    from routes import brain_detector_ledger
    monkeypatch.setattr(brain_detector_ledger, "read_evidence", fake)
    return calls


def test_no_key_is_401(app, monkeypatch):
    calls = _fake_read(monkeypatch, {"state": "MEASURED", "findings": []})
    r = app.test_client().post(PATH, json=GOOD)
    assert r.status_code == 401 and not calls


def test_no_configured_key_is_401_even_with_a_header(app, monkeypatch):
    monkeypatch.delenv("DCHUB_ADMIN_KEY")
    calls = _fake_read(monkeypatch, {"state": "MEASURED", "findings": []})
    r = app.test_client().post(PATH, json=GOOD, headers={"X-Admin-Key": ""})
    assert r.status_code == 401 and not calls


@pytest.mark.parametrize("body", [
    None, {}, {"findings": []}, {"findings": "x"}, {"findings": [{"url": "u"}]},
    {"findings": [{"issue": "i"}] * 401},
], ids=["no-body", "no-findings", "empty", "not-a-list", "no-issue", "too-many"])
def test_a_bad_body_is_400_and_reads_nothing(app, monkeypatch, body):
    calls = _fake_read(monkeypatch, {"state": "MEASURED", "findings": []})
    kw = {"json": body} if body is not None else {}
    r = app.test_client().post(PATH, headers={"X-Admin-Key": KEY}, **kw)
    assert r.status_code == 400 and not calls


def test_a_measured_read_passes_through_with_clean_targets(app, monkeypatch):
    result = {"state": "MEASURED", "findings": [{"verdict": "firing"}], "ledger": {"sweeps": 3}}
    calls = _fake_read(monkeypatch, result)
    body = {"findings": [{"issue": "i" * 300, "url": "u", "url_prefix": 1, "extra": "x"}]}
    r = app.test_client().post(PATH, json=body, headers={"X-Admin-Key": KEY})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["findings"] == [{"verdict": "firing"}]
    assert r.headers["Cache-Control"].startswith("no-store")
    assert calls == [[{"issue": "i" * 200, "url": "u", "url_prefix": True}]]


@pytest.mark.parametrize("result,raises", [
    ({"state": "UNMEASURED", "reason": "no DATABASE_URL"}, None),
    (None, RuntimeError("pool exhausted")),
], ids=["unmeasured", "raised"])
def test_a_failed_read_is_503_unmeasured_never_an_empty_200(app, monkeypatch, result, raises):
    _fake_read(monkeypatch, result, raises)
    r = app.test_client().post(PATH, json=GOOD, headers={"X-Admin-Key": KEY})
    body = r.get_json()
    assert r.status_code == 503 and body["ok"] is False and body["state"] == "UNMEASURED"
