#!/usr/bin/env python3
"""A CRM destination is the provider AND its credential — never either alone.

★ THE DEFECT, observed live 2026-09-20 within minutes of the owner adding the
key. `destination_configured` was:

    configured = bool((SF_INSTANCE_URL and SF_ACCESS_TOKEN) or HUBSPOT_API_KEY)

which never looked at CRM_PROVIDER, while `_dispatch_push` switches on nothing
else. Setting HUBSPOT_API_KEY alone produced:

    hs_configured: True · destination_configured: True · stalled: False
    provider: 'stub'    · status_counts: {'queued_export': 31}

Alarm off, 31 leads — 24 of them paid conversions, oldest 105 days — still
routed to push_to_stub. A half-configured destination is the one state where
going quiet is worse than never having alarmed, and the old stalled_reason
actively set the trap: "nothing will ever be pushed until HUBSPOT_API_KEY or
the Salesforce pair is set". Necessary, not sufficient.

MUST-FAIL CONTROLS included.
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture()
def m():
    import routes.crm_reverse_etl as mod
    return mod


def _set(m, provider, hs="", sf_url="", sf_tok="", dry=False):
    m.CRM_PROVIDER = provider
    m.HUBSPOT_API_KEY = hs
    m.SF_INSTANCE_URL = sf_url
    m.SF_ACCESS_TOKEN = sf_tok
    m.DRY_RUN = dry


def test_THE_INCIDENT_key_without_provider_is_not_configured(m):
    """The exact live state: key set, provider still stub."""
    _set(m, "stub", hs="pat-na1-xxxx")
    configured, gap = m._destination_state()
    assert configured is False, (
        "a HubSpot key with provider='stub' reported CONFIGURED — this is the "
        "false-green that silenced a 105-day stall")
    assert "CRM_PROVIDER" in gap and "stub" in gap, gap


def test_provider_without_key_is_not_configured(m):
    _set(m, "hubspot", hs="")
    configured, gap = m._destination_state()
    assert configured is False
    assert "HUBSPOT_API_KEY" in gap, gap


def test_both_together_IS_configured(m):
    _set(m, "hubspot", hs="pat-na1-xxxx")
    configured, gap = m._destination_state()
    assert configured is True and gap == ""


def test_salesforce_needs_both_halves_of_the_pair(m):
    _set(m, "salesforce", sf_url="https://x.my.salesforce.com")
    assert m._destination_state()[0] is False
    _set(m, "salesforce", sf_url="https://x.my.salesforce.com", sf_tok="tok")
    assert m._destination_state()[0] is True


def test_dry_run_is_never_a_destination(m):
    """DRY_RUN short-circuits _dispatch_push before any provider branch, so a
    fully-credentialled dry run still reaches no CRM."""
    _set(m, "hubspot", hs="pat-na1-xxxx", dry=True)
    configured, gap = m._destination_state()
    assert configured is False
    assert "DRY_RUN" in gap, gap


def test_it_mirrors_dispatch_exactly(m):
    """★ The two must agree or the report is about a different system than the
    one that runs. For every combination, 'configured' must equal 'dispatch
    reaches something other than the stub'."""
    seen_true = seen_false = False
    for provider in ("stub", "hubspot", "salesforce", "garbage"):
        for hs in ("", "k"):
            for sf in ((), ("u", "t")):
                _set(m, provider, hs=hs,
                     sf_url=(sf[0] if sf else ""), sf_tok=(sf[1] if sf else ""))
                configured, _ = m._destination_state()
                # what _dispatch_push would actually call
                if provider == "hubspot":
                    reaches = bool(hs)
                elif provider == "salesforce":
                    reaches = bool(sf)
                else:
                    reaches = False
                assert configured == reaches, (
                    f"provider={provider} hs={bool(hs)} sf={bool(sf)}: "
                    f"health says {configured}, dispatch reaches {reaches}")
                seen_true |= configured
                seen_false |= not configured
    assert seen_true and seen_false, "the sweep never exercised both verdicts"


def test_the_gap_message_never_claims_the_key_alone_is_enough(m):
    """The old wording is what set the trap. It must not come back."""
    _set(m, "stub", hs="pat-na1-xxxx")
    _, gap = m._destination_state()
    assert "until HUBSPOT_API_KEY or the" not in gap, (
        "the message that says the key alone suffices is back")
    assert "push_to_stub" in gap or "CRM_PROVIDER=hubspot" in gap, (
        "the message must tell the reader the one thing still missing")
