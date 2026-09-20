"""Confirming a key binding must follow the ENTITLEMENT authority, not the
subscription_status column.

Both queries in routes/mcp_key_email_verification.py used to require
subscription_status = 'active'. That is STRICTER than the rule deciding what
the account may actually use: api_tier_gating.resolve_effective_plan keeps a
customer on their plan while status is 'payment_failed' and demoted_at is still
NULL — the dunning window, deliberately ~21 days of full paid access. During it
the platform serves the customer as paid while this module refused to let them
apply that same paid plan to a key. Other half of the routes/keys_recover.py
fix (#4877).

★ THE FAKE READS THE SQL. The users filter is applied only when the statement
actually carries it, so restoring the old predicate really does change the
outcome here. A fake using its own notion of "active" would pass either way.
"""
import pytest

import routes.mcp_key_email_verification as v
from api_tier_gating import resolve_effective_plan

EMAIL = "payer@example.com"
KEY = "dch_live_KEY"


# ── the predicate ────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,demoted,entitled,why", [
    ("active",         None, True,  "ordinary paid"),
    ("payment_failed", None, True,  "DUNNING WINDOW — the regression"),
    ("payment_failed", "2026-09-01T00:00:00Z", False, "demoted after repeated failures"),
    ("canceled",       None, False, "canceled"),
    ("unpaid",         None, False, "unpaid"),
    (None,             None, True,  "status never set — the plan stands"),
    ("",               None, True,  "empty status — the plan stands"),
    ("trialing",       None, True,  "unknown status is not a demotion"),
])
def test_entitlement_follows_the_authority(status, demoted, entitled, why):
    got = v._still_entitled("pro", status, None, demoted)
    assert got is entitled, why
    # and it must AGREE with the authority rather than reimplement it
    assert got is (resolve_effective_plan("pro", status, None, demoted) != "free")


def test_an_admin_holding_a_paid_plan_is_not_excluded():
    """resolve_effective_plan returns 'admin', which is NOT in _PAID_PLANS.
    Testing membership instead of 'not free' would newly exclude them."""
    assert "admin" not in v._PAID_PLANS
    assert v._still_entitled("pro", "active", "admin", None) is True


def test_it_fails_closed_to_the_old_predicate_without_the_authority(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_auth(name, *a, **k):
        if name == "api_tier_gating":
            raise ImportError("unavailable")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_auth)
    assert v._still_entitled("pro", "active", None, None) is True
    assert v._still_entitled("pro", "payment_failed", None, None) is False, (
        "a broken import must not WIDEN the grant"
    )


# ── the queries ──────────────────────────────────────────────────────────

class _Cur:
    """Serves one account. Applies the users filter only when the SQL has it."""

    def __init__(self, account):
        self.a, self._rows = account, []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        assert "FROM mcp_dev_keys" in s, "unmodelled SQL: %r" % s
        a = self.a
        if "u.subscription_status,'') = 'active'" in s:
            if (a.get("status") or "") != "active":
                self._rows = []
                return
        if "SELECT u.plan" in s:
            self._rows = [(a.get("plan"), a.get("status"), a.get("role"), a.get("demoted"))]
        else:
            self._rows = [(KEY, "dev-1", a.get("plan"), a.get("status"),
                           a.get("role"), a.get("demoted"))]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    monkeypatch.setattr(v, "confirm_url", lambda *a, **k: "https://dchub.cloud/x")
    monkeypatch.setattr(v, "_may_send", lambda e: True)
    monkeypatch.setattr(v, "_dispatch", lambda *a, **k: None)


DUNNING = {"plan": "pro", "status": "payment_failed", "role": None, "demoted": None}
DEMOTED = {"plan": "pro", "status": "payment_failed", "role": None,
           "demoted": "2026-09-01T00:00:00Z"}
CANCELED = {"plan": "pro", "status": "canceled", "role": None, "demoted": None}
ACTIVE = {"plan": "pro", "status": "active", "role": None, "demoted": None}


def test_a_customer_in_the_dunning_window_can_confirm():
    """★ THE REGRESSION."""
    assert v.confirmation_is_pending(_Cur(DUNNING), KEY, EMAIL) is True


def test_an_active_customer_still_can():
    assert v.confirmation_is_pending(_Cur(ACTIVE), KEY, EMAIL) is True


@pytest.mark.parametrize("account,label", [(DEMOTED, "demoted"), (CANCELED, "canceled")])
def test_a_demoted_or_canceled_account_cannot(account, label):
    assert v.confirmation_is_pending(_Cur(account), KEY, EMAIL) is False, label


def test_the_offer_sweep_includes_the_dunning_window():
    assert v._offer_for_address(_Cur(DUNNING), EMAIL, 5) == 1


@pytest.mark.parametrize("account,label", [(DEMOTED, "demoted"), (CANCELED, "canceled")])
def test_the_offer_sweep_excludes_a_demoted_account(account, label):
    assert v._offer_for_address(_Cur(account), EMAIL, 5) == 0, label


def test_the_offer_sweep_still_serves_an_active_account():
    assert v._offer_for_address(_Cur(ACTIVE), EMAIL, 5) == 1
