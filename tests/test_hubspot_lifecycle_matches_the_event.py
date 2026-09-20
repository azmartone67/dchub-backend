#!/usr/bin/env python3
"""A paying customer must not be filed as a lead.

★ THE DEFECT (2026-09-20, caught before the first push). push_to_hubspot sent

    "lifecyclestage": "lead"
    "hs_lead_status": "NEW"

on EVERY row. 24 of the 31 rows waiting to go were `paid_conversion` — people
who had already paid, some 105 days earlier. Pushing them as `lead` misreports
the funnel at its most load-bearing point, tells a rep to go prospect a
customer, and HubSpot deliberately resists moving a contact backwards once a
stage is set, so it is far cheaper to fix before the push than after.

MUST-FAIL CONTROLS included.
"""
from __future__ import annotations

import inspect
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture()
def m():
    import routes.crm_reverse_etl as mod
    return mod


def _payload(m, event_type, monkeypatch, email="x@example.com"):
    """Build the real hs_payload by intercepting the POST."""
    captured = {}

    class _Resp:
        status_code = 201
        def json(self):
            return {"id": "1"}

    class _Req:
        @staticmethod
        def post(url, headers=None, data=None, timeout=None):
            import json as _j
            captured["url"] = url
            captured["body"] = _j.loads(data)
            return _Resp()

    monkeypatch.setattr(m, "requests", _Req)
    monkeypatch.setattr(m, "HUBSPOT_API_KEY", "pat-test")
    m.push_to_hubspot({"lead_email": email, "event_type": event_type,
                       "intent_score": 100, "attribution_chain": {"a": 1}})
    return captured["body"]["properties"]


def test_a_paid_conversion_is_a_CUSTOMER(m, monkeypatch):
    p = _payload(m, "paid_conversion", monkeypatch)
    assert p["lifecyclestage"] == "customer", (
        "a paid_conversion is being filed as %r — this is the defect"
        % p["lifecyclestage"])


def test_a_customer_is_not_handed_to_a_rep_as_a_NEW_lead(m, monkeypatch):
    p = _payload(m, "paid_conversion", monkeypatch)
    assert "hs_lead_status" not in p, (
        "hs_lead_status=%r set on a customer — that is a sales prospecting "
        "field and it queues someone who already paid" % p.get("hs_lead_status"))


def test_lead_status_is_still_set_for_real_leads(m, monkeypatch):
    """The other half. Dropping it everywhere would be a different bug."""
    p = _payload(m, "newsletter_signup", monkeypatch)
    assert p.get("hs_lead_status") == "NEW", p


def test_each_event_type_gets_its_own_stage(m, monkeypatch):
    got = {e: _payload(m, e, monkeypatch)["lifecyclestage"]
           for e in m.CAPTURED_EVENT_TYPES}
    assert got["paid_conversion"] == "customer"
    assert got["newsletter_signup"] == "subscriber"
    assert got["trial_key_activated"] == "salesqualifiedlead"
    assert len(set(got.values())) >= 3, (
        "every event resolved to the same stage — the mapping is not being "
        "read: %s" % got)


def test_lifecycle_covers_every_event_type_EXACTLY(m):
    """★ The vocabulary is closed (capture_event rejects anything else), so the
    mapping can be exhaustive — and must be. A default would silently re-file a
    NEW event type as 'lead', which is precisely the bug."""
    assert set(m._LIFECYCLE_BY_EVENT) == set(m.CAPTURED_EVENT_TYPES), (
        "mapping and accepted events disagree: "
        f"unmapped={sorted(set(m.CAPTURED_EVENT_TYPES) - set(m._LIFECYCLE_BY_EVENT))} "
        f"extra={sorted(set(m._LIFECYCLE_BY_EVENT) - set(m.CAPTURED_EVENT_TYPES))}")


def test_capture_event_reads_the_shared_vocabulary(m):
    """One list. The accepted set and the mapped set cannot drift if there is
    only one of them."""
    src = inspect.getsource(m.capture_event)
    assert "CAPTURED_EVENT_TYPES" in src, (
        "capture_event re-typed its own event list")


def test_the_stages_are_real_hubspot_values(m):
    """Internal names, lowercase, no spaces. HubSpot 400s on an unknown
    lifecyclestage the same way it does on an unknown property."""
    valid = {"subscriber", "lead", "marketingqualifiedlead",
             "salesqualifiedlead", "opportunity", "customer",
             "evangelist", "other"}
    for evt, stage in m._LIFECYCLE_BY_EVENT.items():
        assert stage in valid, f"{evt} -> {stage!r} is not a HubSpot stage"


def test_MUST_FAIL_CONTROL_no_hardcoded_lead_in_the_payload(m):
    src = inspect.getsource(m.push_to_hubspot)
    code = "\n".join(l for l in src.splitlines()
                     if not l.lstrip().startswith("#"))
    assert not re.search(r'"lifecyclestage"\s*:\s*"lead"', code), (
        "the hardcoded 'lead' is back in the payload")
