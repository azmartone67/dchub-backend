"""Guard: reconcile paid-for entitlements against what the customer can ACTUALLY call.

Implements the check Brain L6 specced and never got built
(routes/_proposed_founder_entitlement_provisioning_repair.py, shipped as a 501
scaffold). The complaint behind it — a founding member saying "bought the
founder licence and I dont think it is working" — sat HIGH and unresolved from
2026-07-09 for a month.

★★ THE TRAP THIS FILE EXISTS TO FENCE.

The obvious reconciler asks "does mcp_dev_keys have a row for this payer?" and
calls a miss unprovisioned. That is wrong, and it gave a wrong answer about the
largest research licensee on the first pass — twice, in a written report:

    NLR ($3,000/yr, 4 seats) has ZERO mcp_dev_keys rows.
    NLR also has FOUR ACTIVE enterprise REST keys and 960 calls made
    (ian.christie 865, gabriel.zuckerman 71, galen.maclaurin 24), last on
    2026-06-15.

Read off one surface they look abandoned at signup. The truth is they had
access, used it hard for three weeks, went quiet 57 days ago, and were never
turned on for the MCP surface at all. Three different problems, three different
owners — and only one of them is provisioning.

Minting a key for a `dormant` customer does nothing except make the dashboard
look busy, which is the failure mode this whole codebase keeps re-learning.

Live class distribution when this shipped (20 payers):
    healthy 6 · never_started 9 · dormant 1 · mcp_missing 2 · no_access 2
Only the last two — 4 customers, $299/mo of real money — are provisioning bugs.

No DB and no network: _classify is pure.

Run locally:
    python3 -m pytest tests/test_entitlement_reconcile.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _row(mcp_keys=0, rest_keys=0, rest_calls=0, mcp_last=None, rest_last=None):
    return {"mcp_keys": mcp_keys, "rest_keys": rest_keys,
            "rest_calls": rest_calls, "mcp_last_used": mcp_last,
            "rest_last_used": rest_last}


@pytest.fixture()
def er():
    import routes.entitlement_reconcile as m
    return m


# ── the NLR case, pinned ─────────────────────────────────────────────────────

def test_rest_access_is_not_no_access(er):
    """THE bug. NLR: no MCP key, 4 active enterprise REST keys, 960 calls.
    Classifying that as 'never got access' is how a $3,000/yr licensee ends up
    in an apology email for a problem they do not have."""
    nlr = _row(mcp_keys=0, rest_keys=4, rest_calls=960,
               rest_last="2026-06-15T00:00:00+00:00")
    assert er._classify(nlr) == "mcp_missing"
    assert er._classify(nlr) != "no_access"


def test_no_access_requires_both_surfaces_empty(er):
    assert er._classify(_row()) == "no_access"
    assert er._classify(_row(rest_keys=1)) != "no_access"
    assert er._classify(_row(mcp_keys=1)) != "no_access"


# ── only provisioning failures get written to ────────────────────────────────

def test_only_provisioning_classes_are_fixable(er):
    assert set(er._FIXABLE) == {"no_access", "mcp_missing"}
    for klass in ("dormant", "never_started", "healthy"):
        assert klass not in er._FIXABLE, (
            f"{klass} is activation or relationship work — minting another key "
            f"does not address it and would inflate the provisioned count")


def test_dormant_is_not_a_provisioning_failure(er):
    """Had access, used it, stopped. A new key changes nothing."""
    r = _row(mcp_keys=0, rest_keys=2, rest_calls=500,
             rest_last="2026-06-15T00:00:00+00:00")
    # no MCP key at all ⇒ mcp_missing takes precedence (that IS provisionable)
    assert er._classify(r) == "mcp_missing"
    r2 = _row(mcp_keys=1, rest_keys=2, rest_calls=500,
              rest_last="2026-06-15T00:00:00+00:00")
    assert er._classify(r2) == "dormant"
    assert er._classify(r2) not in er._FIXABLE


def test_never_started_is_not_a_provisioning_failure(er):
    r = _row(mcp_keys=1, rest_keys=1, rest_calls=0)
    assert er._classify(r) == "never_started"
    assert er._classify(r) not in er._FIXABLE


def test_healthy_customer_is_left_alone(er):
    r = _row(mcp_keys=1, rest_keys=1, rest_calls=40,
             mcp_last="2026-08-10T00:00:00+00:00")
    assert er._classify(r) == "healthy"


# ── safety ───────────────────────────────────────────────────────────────────

def test_writes_require_an_explicit_confirm(er):
    src = open(er.__file__).read()
    assert 'request.args.get("confirm") == "1"' in src
    assert 'confirm and request.method == "POST"' in src, \
        "provisioning must require BOTH an explicit confirm and a POST"


def test_admin_gate_fails_closed(er):
    src = open(er.__file__).read()
    fn = src[src.index("def _admin_ok"):src.index("def _conn")]
    assert "return bool(exp) and got == exp" in fn, \
        "an unset DCHUB_ADMIN_KEY must let nobody in, not everybody"


def test_emailing_is_gated_separately_from_minting(er):
    """Minting a key and telling a customer about it are different decisions."""
    src = open(er.__file__).read()
    assert "ENTITLEMENT_RECONCILE_EMAIL" in src


def test_the_query_reads_both_surfaces(er):
    """If the REST half is ever dropped, the NLR misclassification returns."""
    assert "api_keys" in er._PAYERS_SQL and "mcp_dev_keys" in er._PAYERS_SQL
    assert "is_test" in er._PAYERS_SQL and "refunded_at" in er._PAYERS_SQL, \
        "test and refunded conversions must not count as payers"
