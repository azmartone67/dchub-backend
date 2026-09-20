#!/usr/bin/env python3
"""A credential that is present is not a credential that works.

★ THE INCIDENT (2026-09-20). The first real push to HubSpot returned:

    hs 401 · category INVALID_AUTHENTICATION
    "Authentication credentials not found."

The request reached HubSpot; the Authorization header did not parse.
`hs_configured` had been True throughout, because it only asked whether the
string was non-empty. Meanwhile, two lines above it in the same module:

    CRM_PROVIDER    = (os.environ.get("CRM_PROVIDER") or "stub").strip().lower()
    HUBSPOT_API_KEY =  os.environ.get("HUBSPOT_API_KEY") or ""

CRM_PROVIDER stripped. The credentials never did — and a token pasted into a
dashboard carries a trailing newline or space more often than not, straight
into f"Bearer {HUBSPOT_API_KEY}".

★ And diagnosing it needed a hand-written SQL query against Neon, because
`crm_response` — which holds the provider's own explanation — was written on
every failure and SELECTed by nothing. `last_error` keeps "hs 401" and no more.

MUST-FAIL CONTROLS included.
"""
from __future__ import annotations

import importlib
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import routes.crm_reverse_etl as mod
    return importlib.reload(mod)


def test_a_token_pasted_with_whitespace_still_works(monkeypatch):
    """The actual failure mode. A trailing newline must not reach the header."""
    m = _reload(monkeypatch, HUBSPOT_API_KEY="pat-na1-abc123\n",
                CRM_PROVIDER="hubspot")
    assert m.HUBSPOT_API_KEY == "pat-na1-abc123", repr(m.HUBSPOT_API_KEY)
    assert "\n" not in m.HUBSPOT_API_KEY
    assert m._destination_state()[0] is True


def test_every_credential_is_stripped_not_just_the_provider(monkeypatch):
    m = _reload(monkeypatch,
                HUBSPOT_API_KEY="  pat-na1-x  ",
                SALESFORCE_ACCESS_TOKEN=" tok\t",
                HUNTER_API_KEY="\nhk ")
    assert m.HUBSPOT_API_KEY == "pat-na1-x"
    assert m.SF_ACCESS_TOKEN == "tok"
    assert m.HUNTER_API_KEY == "hk"


def test_a_wrong_shaped_token_is_named_BEFORE_it_burns_an_attempt(monkeypatch):
    """The queue gives up on a row after 5 attempts. Saying 'this will 401' up
    front is worth more than discovering it one attempt at a time."""
    m = _reload(monkeypatch, HUBSPOT_API_KEY="eu1-1234-legacy-api-key",
                CRM_PROVIDER="hubspot")
    configured, gap = m._destination_state()
    assert configured is False, "a non-pat token reported as a live destination"
    assert "pat-" in gap, gap
    assert "INVALID_AUTHENTICATION" in gap or "401" in gap, gap


def test_a_quoted_paste_is_caught_too(monkeypatch):
    """Copying a value with its surrounding quotes is the other common paste."""
    m = _reload(monkeypatch, HUBSPOT_API_KEY='"pat-na1-abc"',
                CRM_PROVIDER="hubspot")
    assert m._destination_state()[0] is False


def test_a_real_pat_is_accepted(monkeypatch):
    """The control. A guard that rejects everything is not a guard."""
    m = _reload(monkeypatch, HUBSPOT_API_KEY="pat-na1-0000", CRM_PROVIDER="hubspot")
    configured, gap = m._destination_state()
    assert configured is True and gap == ""


def test_the_shape_check_never_leaks_the_token(monkeypatch):
    """A diagnostic that prints the secret is worse than no diagnostic."""
    secret = "pat_this_is_the_actual_secret_value"
    m = _reload(monkeypatch, HUBSPOT_API_KEY=secret, CRM_PROVIDER="hubspot")
    _, gap = m._destination_state()
    assert secret not in gap, "config_gap leaked the credential"
    assert secret[5:] not in gap


def test_the_queue_endpoint_returns_crm_response(monkeypatch):
    """★ The field that says WHY. Written on every failure, previously SELECTed
    by nothing — diagnosing the first push needed raw SQL against Neon."""
    import routes.crm_reverse_etl as m
    src = __import__("inspect").getsource(m)
    i = src.index("def admin_queue")
    blk = src[i:i + 3000]
    sel = re.search(r"SELECT(.*?)FROM crm_outbound_queue", blk, re.S).group(1)
    cols = [c.strip() for c in sel.replace("\n", " ").split(",") if c.strip()]
    assert "crm_response" in cols, "crm_response is not selected"
    # and it is returned under its own name, at the RIGHT index
    idx = cols.index("crm_response")
    assert re.search(r'"crm_response":\s*r\[%d\]' % idx, blk), (
        f"crm_response is selected at [{idx}] but not returned from that index "
        f"— an off-by-one here silently serves a different column")


def test_every_returned_index_matches_its_selected_column():
    """The general form. One shifted column would mislabel real data."""
    import inspect
    import routes.crm_reverse_etl as m
    src = inspect.getsource(m)
    i = src.index("def admin_queue")
    blk = src[i:i + 3000]
    sel = re.search(r"SELECT(.*?)FROM crm_outbound_queue", blk, re.S).group(1)
    cols = [c.strip() for c in sel.replace("\n", " ").split(",") if c.strip()]
    pairs = re.findall(r'"(\w+)":\s*r\[(\d+)\]', blk)
    assert pairs, "no row-dict mapping found — the check would pass vacuously"
    for name, n in pairs:
        n = int(n)
        assert n < len(cols), f"{name} reads r[{n}], past the {len(cols)}-col SELECT"
        assert cols[n] == name, f"{name} reads r[{n}] which is {cols[n]!r}"
