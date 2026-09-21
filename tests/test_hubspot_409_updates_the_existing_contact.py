#!/usr/bin/env python3
"""A contact that already exists must still receive the payload.

★ THE DEFECT (2026-09-20). push_to_hubspot answered HubSpot's 409 (contact
already exists) with {"ok": True, "dup": True}. The flusher marks an ok row
'pushed', so the lifecycle stage, lead status and attribution were never
written to the existing contact, and nothing retried because nothing had
failed. Any address already in HubSpot (a manual import, an earlier row for
the same person) silently lost its payload.

Now a 409 reads the contact and PATCHes it, and only a PATCH that lands is ok.

MUST-FAIL CONTROL: with the 409 branch reverted to the old `ok: True`, every
test here that expects a PATCH fails.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

EMAIL = "a.b+tag@example.com"
CONTACT = "https://api.hubapi.com/crm/v3/objects/contacts"
BY_EMAIL = CONTACT + "/a.b%2Btag%40example.com?idProperty=email"


@pytest.fixture()
def m():
    import routes.crm_reverse_etl as mod
    return mod


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _hubspot(m, monkeypatch, post=409, stored=None, get=200, patch=200,
             patch_body=None):
    """Stub `requests`; returns the ordered list of (method, url, body)."""
    calls = []

    class _Req:
        @staticmethod
        def post(url, headers=None, data=None, timeout=None):
            calls.append(("POST", url, json.loads(data)))
            if post == 201:
                return _Resp(201, {"id": "501"})
            return _Resp(post, {"message": "Contact already exists. "
                                           "Existing ID: 901"})

        @staticmethod
        def get(url, headers=None, timeout=None):
            calls.append(("GET", url, None))
            return _Resp(get, {"id": "901", "properties": stored or {}})

        @staticmethod
        def patch(url, headers=None, data=None, timeout=None):
            calls.append(("PATCH", url, json.loads(data)))
            return _Resp(patch, {"id": "901"} if patch == 200 else
                         patch_body or {"message": "Property values were not valid"})

    monkeypatch.setattr(m, "requests", _Req)
    monkeypatch.setattr(m, "HUBSPOT_API_KEY", "pat-test")
    return calls


def _lead(event_type, **kw):
    lead = {"lead_email": EMAIL, "event_type": event_type, "intent_score": 87,
            "attribution_chain": {"first_touch": "mcp"}}
    lead.update(kw)
    return lead


def _patched(calls):
    sent = [c for c in calls if c[0] == "PATCH"]
    assert len(sent) == 1, "expected exactly one PATCH, got %r" % (
        [c[0] for c in calls],)
    return sent[0][2]["properties"]


def test_409_patches_the_existing_contact_with_the_full_payload(m, monkeypatch):
    calls = _hubspot(m, monkeypatch)
    res = m.push_to_hubspot(_lead(
        "trial_key_activated", lead_first_name="Ada", lead_last_name="L",
        lead_company="Acme", lead_title="CTO"))
    created = calls[0][2]["properties"]
    assert [c[0] for c in calls] == ["POST", "GET", "PATCH"], calls
    assert calls[2][1] == BY_EMAIL, "PATCH must key on the URL-encoded email"
    props = _patched(calls)
    assert props == created, (
        "the existing contact must get the same property set the create "
        "carried; missing=%s" % sorted(set(created) - set(props)))
    assert props["lifecyclestage"] == "salesqualifiedlead"
    assert props["hs_lead_status"] == "NEW"
    assert props["dchub_event_type"] == "trial_key_activated"
    assert props["dchub_intent_score"] == "87"
    assert json.loads(props["dchub_attribution"]) == {"first_touch": "mcp"}
    assert res["ok"] is True and res["status_code"] == 200
    assert res["external_id"] == "901", "external_id comes from the PATCH"


@pytest.mark.parametrize("status", [400, 404, 429, 500])
def test_a_failed_patch_is_not_reported_as_delivered(m, monkeypatch, status):
    calls = _hubspot(m, monkeypatch, patch=status)
    res = m.push_to_hubspot(_lead("paid_conversion"))
    assert "PATCH" in [c[0] for c in calls]
    assert res["ok"] is False, (
        "a %s on the update was reported as success — the row would be "
        "marked pushed and never retried" % status)
    assert res["status_code"] == status and "patch" in res["error"]


def test_a_failed_read_is_not_reported_as_delivered(m, monkeypatch):
    calls = _hubspot(m, monkeypatch, get=404)
    res = m.push_to_hubspot(_lead("paid_conversion"))
    assert res["ok"] is False and res["status_code"] == 404
    assert "PATCH" not in [c[0] for c in calls]


def test_the_201_path_is_unchanged(m, monkeypatch):
    calls = _hubspot(m, monkeypatch, post=201)
    res = m.push_to_hubspot(_lead("paid_conversion"))
    assert [(c[0], c[1]) for c in calls] == [("POST", CONTACT)]
    assert res == {"ok": True, "external_id": "501", "raw": {"id": "501"},
                   "status_code": 201}


def test_customer_wins_over_a_stored_lead(m, monkeypatch):
    calls = _hubspot(m, monkeypatch, stored={"lifecyclestage": "lead"})
    m.push_to_hubspot(_lead("paid_conversion"))
    assert _patched(calls)["lifecyclestage"] == "customer"


def test_a_customer_is_never_moved_backwards(m, monkeypatch):
    calls = _hubspot(m, monkeypatch, stored={"lifecyclestage": "customer"})
    res = m.push_to_hubspot(_lead("mcp_high_intent"))
    props = _patched(calls)
    assert "lifecyclestage" not in props, (
        "customer -> %r is a backward move" % props["lifecyclestage"])
    assert "hs_lead_status" not in props, "NEW on a customer re-queues them"
    assert props["dchub_event_type"] == "mcp_high_intent", (
        "holding the stage must not drop the rest of the payload")
    assert res["ok"] is True and res["kept"] == {"lifecyclestage": "customer"}


@pytest.mark.parametrize("stage", ["other", "1234567"])
def test_a_stage_outside_the_known_order_is_left_alone(m, monkeypatch, stage):
    calls = _hubspot(m, monkeypatch, stored={"lifecyclestage": stage})
    m.push_to_hubspot(_lead("trial_key_activated"))
    props = _patched(calls)
    assert "lifecyclestage" not in props and "hs_lead_status" not in props


def test_a_reps_lead_status_is_not_reset(m, monkeypatch):
    calls = _hubspot(m, monkeypatch, stored={"lifecyclestage": "lead",
                                             "hs_lead_status": "IN_PROGRESS"})
    m.push_to_hubspot(_lead("trial_key_activated"))
    props = _patched(calls)
    assert props["lifecyclestage"] == "salesqualifiedlead"
    assert "hs_lead_status" not in props


def test_blank_fields_do_not_erase_the_existing_contact(m, monkeypatch):
    """HubSpot treats "" as CLEAR. The create sends blanks harmlessly; an
    update would wipe the name and company an import put there."""
    calls = _hubspot(m, monkeypatch)
    m.push_to_hubspot(_lead("paid_conversion"))
    props = _patched(calls)
    blank = sorted(k for k in ("firstname", "lastname", "company", "jobtitle")
                   if k in props)
    assert not blank, "PATCH would clear %s on the existing contact" % blank


# ── how the flush charges a failed update (#5041's classifier) ───────

@pytest.mark.parametrize("status,klass", [
    (401, "config"), (403, "config"), (429, "transient"), (503, "transient"),
    (400, "lead"), (404, "lead"),
])
def test_a_failed_update_is_charged_to_the_right_party(m, monkeypatch,
                                                       status, klass):
    """A 404 here is the by-email lookup missing THIS contact, so it is the
    row's own answer. As config it would never spend an attempt, retry every
    day forever, and make /crm/health report the whole portal as refusing."""
    _hubspot(m, monkeypatch, patch=status)
    res = m.push_to_hubspot(_lead("paid_conversion"))
    assert res["ok"] is False and res["status_code"] == status, res
    assert m._push_failure_class(res) == klass, (status, res)


def test_a_404_on_the_CREATE_is_still_config(m, monkeypatch):
    """Control: the create POSTs to one fixed URL, so its 404 is the endpoint."""
    _hubspot(m, monkeypatch, post=404)
    res = m.push_to_hubspot(_lead("paid_conversion"))
    assert "dup" not in res
    assert m._push_failure_class(res) == m.PUSH_FAIL_CONFIG


def test_a_missing_property_past_the_raw_cut_is_still_config(m, monkeypatch):
    """HubSpot lists one error per bad property. `raw` keeps 500 chars, so the
    update result must carry the codes from the FULL body, as the create does."""
    body = {"message": "Property values were not valid",
            "errors": [{"code": "INVALID_EMAIL", "message": "x" * 700},
                       {"code": "PROPERTY_DOESNT_EXIST",
                        "message": "dchub_attribution does not exist"}]}
    _hubspot(m, monkeypatch, patch=400, patch_body=body)
    res = m.push_to_hubspot(_lead("paid_conversion"))
    assert "PROPERTY_DOESNT_EXIST" not in res["raw"], "fixture must pass the cut"
    assert "PROPERTY_DOESNT_EXIST" in res["codes"]
    assert m._push_failure_class(res) == m.PUSH_FAIL_CONFIG
