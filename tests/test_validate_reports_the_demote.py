"""/api/v1/keys/validate must SAY when a good key is served below what it bought.

A presented credential has three outcomes. The MCP gateway could see two:

    REFUSED       200 + valid:false, key_rejected:true  -> credential_refused
    UNVERIFIABLE  backend 5xx/timeout                   -> credential_unverified
    DEMOTED       200 + valid:true, tier below entitlement -> NOTHING

The third is the one that costs money, and the gateway cannot derive it: it
sees ONE tier and has nothing to compare it against. This hop is the only place
in the system that holds both sides — users.plan next to what the account
actually resolves to. dchub-mcp-server #480 is the consumer (_authDemoted).

★ THE REASON IS ASKED OF THE AUTHORITY, NOT RESTATED. _tier_cross_check
re-resolves WITHOUT the demote stamp to learn which branch fired. The tests
below pin that it AGREES with resolve_effective_plan rather than reimplementing
it — a hand-written copy of those branches would be the fourth restatement of
the rule #4877/#4903/#4950 removed three of.

★ NO DATABASE. The pool is a module-level shim, so the real handler runs
against a fake that reads the SQL it is given.
"""
import os
import sys
from contextlib import contextmanager

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# flask_mcp_endpoints refuses to import without a DB URL, and nothing connects
# until a request runs — so a dummy satisfies it.
#
# ★ CONFINED TO THE IMPORT, AND PUT BACK. pytest collects the whole suite in ONE
# process, and several tests SKIP on `not (DATABASE_URL or NEON_DATABASE_URL)`
# because CI runs with neither (tests/test_top_caller_share_coherence.py says so
# in its header). Leaving it set turns those skips into connection failures
# against port 1 — measured: that file is `13 passed, 2 skipped` alone and
# `17 failed` when a module holding it at scope is collected first.
_injected = not (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL"))
if _injected:
    os.environ["NEON_DATABASE_URL"] = "postgresql://u:p@127.0.0.1:1/db"
try:
    import flask_mcp_endpoints as fme
finally:
    if _injected:
        os.environ.pop("NEON_DATABASE_URL", None)

from api_tier_gating import resolve_effective_plan

# Same rule for the internal key: set per test via monkeypatch (function-scoped,
# restored automatically) rather than at module scope, where it would silently
# hand every other test in the process a valid X-Internal-Key.
INTERNAL_KEY = "test-internal-key"

EMAIL = "payer@example.com"
KEY = "dch_live_ONLY_AN_MCP_ROW"


class _Cur:
    def __init__(self, user=None, api_key_tier=None, mcp_tier="free"):
        self.user, self.api_key_tier, self.mcp_tier = user, api_key_tier, mcp_tier
        self._row = None
        self.statements = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.statements.append(s)
        if "FROM mcp_dev_keys" in s:
            self._row = ("dev-1", EMAIL, self.mcp_tier, "active")
        elif s.startswith("UPDATE mcp_dev_keys"):
            self._row = None
        elif "FROM users" in s:
            u = self.user
            self._row = None if u is None else (
                u["plan"], u["status"], u["role"], u["demoted_at"])
        elif "FROM api_keys" in s:
            self._row = (self.api_key_tier,) if self.api_key_tier else None
        elif "metered_billing_decisions" in s:
            self._row = None
        else:
            raise AssertionError("unmodelled SQL: %r" % s)

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Pool:
    def __init__(self, cur):
        self._cur = cur

    @contextmanager
    def connection(self):
        yield _Conn(self._cur)


def _user(plan="pro", status="active", role=None, demoted_at=None):
    return {"plan": plan, "status": status, "role": role, "demoted_at": demoted_at}


def _validate(monkeypatch, cur):
    """Drive the REAL handler through Flask with the pool swapped out."""
    from flask import Flask
    monkeypatch.setenv("DCHUB_INTERNAL_KEY", INTERNAL_KEY)
    monkeypatch.setattr(fme, "_pool", _Pool(cur))
    app = Flask(__name__)
    app.register_blueprint(fme.mcp_bp)
    client = app.test_client()
    r = client.post("/api/v1/keys/validate",
                    json={"api_key": KEY},
                    headers={"X-Internal-Key": INTERNAL_KEY})
    assert r.status_code == 200, r.data
    return r.get_json()


# ── the reason, straight off _tier_cross_check ───────────────────────────

@pytest.mark.parametrize("status,demoted,expected", [
    ("active",         None, None),
    ("trialing",       None, None),
    ("past_due",       None, None),
    ("payment_failed", None, None),                 # dunning GRACE — still paid
    ("payment_failed", "2026-09-01", "dunning_demote"),
    ("canceled",       None, "canceled"),
    ("unpaid",         None, "canceled"),
])
def test_the_reason_names_the_branch_that_fired(status, demoted, expected):
    cur = _Cur(_user(status=status, demoted_at=demoted))
    _plan_tier, _ak, _m, reason = fme._tier_cross_check(cur, KEY, EMAIL)
    assert reason == expected
    # and it must AGREE with the authority rather than restate it
    eff = resolve_effective_plan("pro", status or "", "", demoted)
    assert (reason is not None) is (eff == "free")


def test_an_account_with_no_plan_of_record_is_not_demoted():
    """free -> free is entitlement, not a demote. Reporting it would tell a
    caller who never paid that they lost something."""
    cur = _Cur(_user(plan="free", status="canceled"))
    *_rest, reason = fme._tier_cross_check(cur, KEY, EMAIL)
    assert reason is None


def test_no_user_row_is_not_a_demote():
    *_rest, reason = fme._tier_cross_check(_Cur(None), KEY, EMAIL)
    assert reason is None


@contextmanager
def _authority_unavailable(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_auth(name, *a, **k):
        if name == "api_tier_gating":
            raise ImportError("unavailable")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_auth)
    yield


@pytest.mark.parametrize("status", [
    "active", "trialing", "canceled", "unpaid", "payment_failed", "past_due", "", None])
def test_it_is_silent_when_the_authority_is_unavailable(monkeypatch, status):
    """★ AND THE SILENCE IS STRUCTURAL, WHICH IS WHY THIS ASSERTS BOTH THINGS.

    An earlier version of this test asserted only `reason is None` and was
    VACUOUS: two mutations to the fallback survived it. The fallback yields the
    PLAN for an active/trialing account and '' otherwise — never the literal
    'free' — so the demote block is unreachable without the authority, and any
    handling inside it is dead. Pinning plan_tier too is what proves this test
    actually took the fallback path rather than passing by accident.
    """
    with _authority_unavailable(monkeypatch):
        cur = _Cur(_user(status=status))
        plan_tier, _a, _m, reason = fme._tier_cross_check(cur, KEY, EMAIL)

    expected = "pro" if status in ("active", "trialing") else None
    assert plan_tier == expected, "this must be the FALLBACK, not the authority"
    assert reason is None


# ── the emission rule, through the REAL handler ──────────────────────────

def test_a_demoted_customer_is_told_so(monkeypatch):
    body = _validate(monkeypatch, _Cur(_user(status="canceled"), mcp_tier="free"))
    assert body["tier"] == "free"
    assert body["demoted"] is True
    assert body["demote_reason"] == "canceled"


def test_the_dunning_stamp_is_named_distinctly(monkeypatch):
    body = _validate(monkeypatch, _Cur(
        _user(status="payment_failed", demoted_at="2026-09-01"), mcp_tier="free"))
    assert body["demoted"] is True
    assert body["demote_reason"] == "dunning_demote"


def test_a_paid_customer_is_not_told_they_were_demoted(monkeypatch):
    body = _validate(monkeypatch, _Cur(_user(status="active"), mcp_tier="free"))
    assert body["tier"] == "paid"
    assert body["demoted"] is False
    assert body["demote_reason"] is None


def test_no_demote_is_announced_while_another_leg_still_serves_them_paid(monkeypatch):
    """★ THE CONJUNCTION. users.plan resolved to free, but api_keys still
    carries this customer, so effective_tier is paid and NOTHING was lost.
    Announcing a demote on a paid response is a wrong label on a working call
    — and the gateway branches on this field."""
    cur = _Cur(_user(status="canceled"), api_key_tier="enterprise", mcp_tier="free")
    _pt, ak, _m, reason = fme._tier_cross_check(cur, KEY, EMAIL)
    assert reason == "canceled", "the users leg really did resolve to free"
    assert ak == "enterprise"

    body = _validate(monkeypatch, _Cur(
        _user(status="canceled"), api_key_tier="enterprise", mcp_tier="free"))
    assert body["tier"] == "enterprise"
    assert body["demoted"] is False, "served paid — nothing to report"
    assert body["demote_reason"] is None


def test_the_dunning_grace_window_is_not_a_demote(monkeypatch):
    """Failure #1: still entitled, still served paid, nothing to announce."""
    body = _validate(monkeypatch, _Cur(
        _user(status="payment_failed"), mcp_tier="free"))
    assert body["tier"] == "paid"
    assert body["demoted"] is False


def test_the_fields_are_always_present_so_the_gateway_can_branch(monkeypatch):
    body = _validate(monkeypatch, _Cur(_user(status="active"), mcp_tier="free"))
    assert "demoted" in body and "demote_reason" in body
    assert isinstance(body["demoted"], bool)
