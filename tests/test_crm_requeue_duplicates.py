#!/usr/bin/env python3
"""A 409 'contact already exists' must be re-sendable, not silently final.

★ MEASURED 2026-09-21 09:52Z, the first real flush: 32 rows delivered, and 15
of them (14 paid_conversion) were HubSpot 409s against contacts from the 09-20
CSV import. push_to_hubspot counts a 409 as delivered and writes nothing onto
the existing contact, so those contacts never got dchub_event_type /
dchub_intent_score / dchub_attribution or their lifecycle stage — and every one
of their rows says 'pushed', so no flush looks at them again.

`POST /api/v1/admin/crm/requeue-config-failures?kind=duplicates` names those
rows (dry run by default) and, with ?apply=1, puts exactly them back in the
queue. Drives the real functions through the SQL-executing FakeDB from
test_crm_config_failure_never_burns_a_lead — no network.

MUST-FAIL CONTROLS included.
"""
from __future__ import annotations

import json

import pytest

from tests.test_crm_config_failure_never_burns_a_lead import (
    ADMIN, HS_201, FakeHubSpot, _client, _row, _wire,
)

URL = "/api/v1/admin/crm/requeue-config-failures"


def _hs_409(existing_id):
    """HubSpot's real 409 body for a create on an email it already has."""
    return json.dumps({"status": "error",
                       "message": f"Contact already exists. Existing ID: {existing_id}",
                       "correlationId": "c0ffee", "category": "CONFLICT"})


def _dup(existing_id, updated=False):
    """crm_response as the flush stores it for a 409 (updated=True is the
    shape of a 409 whose existing contact WAS patched)."""
    r = {"ok": True, "external_id": None, "dup": True,
         "raw": _hs_409(existing_id), "status_code": 409}
    if updated:
        r.update(updated=True, external_id=str(existing_id))
    return r


@pytest.fixture()
def m(monkeypatch):
    import routes.crm_reverse_etl as mod
    for k, v in dict(CRM_PROVIDER="hubspot", HUBSPOT_API_KEY="pat-na1-test",
                     SF_INSTANCE_URL="", SF_ACCESS_TOKEN="", DRY_RUN=False,
                     DISABLE=False, DCHUB_ADMIN_KEY="adm-test",
                     _SCHEMA_READY=False).items():
        monkeypatch.setattr(mod, k, v)
    return mod


def _rows():
    return [
        _row(1, status="pushed", attempts=1, crm_response=_dup(901)),
        _row(2, status="pushed", attempts=1, event="trial_key_activated",
             crm_response=_dup(902)),
        _row(3, status="pushed", attempts=1, crm_response=_dup(903, updated=True)),
        _row(4, status="pushed", attempts=1,
             crm_response={"ok": True, "external_id": "777", "status_code": 201}),
        _row(5, status="queued", attempts=0),
        _row(6, status="failed", attempts=5, last_error="hs 400"),
    ]


def test_dry_run_names_exactly_the_duplicates_nobody_updated(m, monkeypatch):
    db, hs = _wire(m, monkeypatch, _rows())
    before = db.snapshot()
    r = _client(m).post(URL + "?kind=duplicates", headers=ADMIN)
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert (j["kind"], j["dry_run"]) == ("duplicates", True), j
    assert [x["id"] for x in j["would_requeue"]] == [1, 2], (
        "only pushed 409s that were never updated — not the patched one (3), "
        "not a real create (4), nothing unsent or failed")
    assert [x["existing_contact_id"] for x in j["would_requeue"]] == ["901", "902"]
    assert j["would_requeue"][0]["lead_email"] == "lead1@example.com"
    assert db.rows == before, "a dry run wrote to the queue"
    assert not [s for s in db.statements if s.startswith("UPDATE")]
    assert hs.calls == []


def test_apply_puts_exactly_them_back_and_the_next_flush_resends_them(m, monkeypatch):
    db, hs = _wire(m, monkeypatch, _rows(), FakeHubSpot(default=HS_201))
    j = _client(m).post(URL + "?kind=duplicates&apply=1", headers=ADMIN).get_json()
    assert (j["dry_run"], j["requeued_count"]) == (False, 2), j
    by = {r["id"]: r for r in db.rows}
    for i in (1, 2):
        assert (by[i]["status"], by[i]["push_attempts"]) == ("queued", 0), by[i]
    assert by[3]["status"] == by[4]["status"] == "pushed"
    assert by[6]["status"] == "failed"
    out = m.flush_outbound_queue()
    assert out["pushed"] == 3, out            # rows 1, 2 and the untouched unsent 5
    assert sorted(hs.calls) == ["lead1@example.com", "lead2@example.com",
                                "lead5@example.com"], hs.calls


def test_kind_defaults_to_config_and_an_unknown_kind_touches_nothing(m, monkeypatch):
    db, _ = _wire(m, monkeypatch, _rows())
    c = _client(m)
    assert c.post(URL, headers=ADMIN).get_json()["kind"] == "config"
    n = len(db.statements)
    r = c.post(URL + "?kind=dups&apply=1", headers=ADMIN)
    assert r.status_code == 400 and r.get_json()["kinds"] == ["config", "duplicates"]
    assert len(db.statements) == n, "an unknown kind reached the database"


def test_MUST_FAIL_CONTROL_it_is_admin_gated(m, monkeypatch):
    db, _ = _wire(m, monkeypatch, _rows())
    r = _client(m).post(URL + "?kind=duplicates&apply=1")
    assert r.status_code == 401 and db.statements == []
