"""The MCP validate hop must follow the ENTITLEMENT authority, not its own
copy of the rule.

flask_mcp_endpoints._tier_cross_check resolves the users.plan leg behind
/api/v1/keys/validate — the tier every MCP tool call is served at. Its
users SELECT used to carry a hand-written status allowlist, the THIRD copy
of a rule api_tier_gating.resolve_effective_plan owns. #4877 removed one
(routes/keys_recover.py), #4903 the second (routes/mcp_key_email_
verification.py); this was the last one left, on the busiest hop.

★ WHAT THE COPY GOT WRONG. main.handle_payment_failed stamps the failure
status on failure #1, but leaves a prior payer's users.plan AND their
api_keys.rate_limit_tier alone until failure #4 — its docstring grants
them "~21 days of full paid access" ON PURPOSE. resolve_effective_plan
honours that window (it demotes only once demoted_at is ALSO stamped).
The allowlist did not: this leg answered None from the first failed retry.

That is invisible while api_keys.rate_limit_tier still reads paid. It
bites the cohort this cross-check EXISTS for — a customer whose paid
signal lives ONLY in users.plan: a web checkout driving an MCP-minted
dch_live_ key, which has an mcp_dev_keys row and no api_keys row at all.
Every leg then read non-paid and the MCP served FREE while the website
served pro — the inversion api_tier_gating's "THE WEB PATH MUST NOT
OUTRANK THE API PATH" was written to prevent, running backwards.

★ THE FAKE READS THE SQL IT IS GIVEN. It applies the status allowlist only
when the statement actually carries it, and returns the column shape the
statement actually asks for. So restoring the old predicate really does
change these outcomes; a fake dispatching on the test's own notion of
"active" would pass either way.

★ NO DATABASE. flask_mcp_endpoints opens connections per request, not at
import, so these run in CI rather than skipping — a skipped test would
prove nothing about the gate in front of a paying customer.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# flask_mcp_endpoints refuses to import without a DB URL, and nothing connects
# until a request runs — so a dummy satisfies it.
#
# ★ CONFINED TO THE IMPORT, AND PUT BACK. pytest collects the whole suite in ONE
# process, and several tests SKIP on `not (DATABASE_URL or NEON_DATABASE_URL)`
# precisely because CI runs with neither (see tests/test_top_caller_share_
# coherence.py, whose header says so outright). Leaving this set turns those
# skips into connection failures against port 1 — measured: that file is
# `13 passed, 2 skipped` alone and `17 failed` when this module is collected
# first. A module-level setdefault here is a suite-wide side effect.
_injected = not (os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL"))
if _injected:
    os.environ["NEON_DATABASE_URL"] = "postgresql://u:p@127.0.0.1:1/db"
try:
    import flask_mcp_endpoints as fme
finally:
    if _injected:
        os.environ.pop("NEON_DATABASE_URL", None)

from api_tier_gating import resolve_effective_plan

EMAIL = "payer@example.com"
KEY = "dch_live_ONLY_AN_MCP_ROW"


class _Cur:
    """One users row, one optional api_keys row, served off the real SQL."""

    def __init__(self, user=None, api_key_tier=None):
        self.user, self.api_key_tier = user, api_key_tier
        self._row = None
        self.statements = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.statements.append(s)

        if "FROM users" in s:
            u = self.user
            if u is None:
                self._row = None
                return
            # The OLD predicate, applied only when the SQL carries it.
            if "'active'" in s and "'trialing'" in s:
                if (u["status"] or "") not in ("active", "trialing"):
                    self._row = None
                    return
            if "demoted_at" in s:
                self._row = (u["plan"], u["status"], u["role"], u["demoted_at"])
            else:
                self._row = (u["plan"],)
            return

        if "FROM api_keys" in s:
            self._row = (self.api_key_tier,) if self.api_key_tier else None
            return

        if "metered_billing_decisions" in s:
            self._row = None
            return

        raise AssertionError("unmodelled SQL: %r" % s)

    def fetchone(self):
        return self._row


def _user(plan="pro", status="active", role=None, demoted_at=None):
    return {"plan": plan, "status": status, "role": role, "demoted_at": demoted_at}


# ── the predicate ────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,demoted,paid,why", [
    ("active",         None, True,  "ordinary paid"),
    ("payment_failed", None, True,  "DUNNING WINDOW — the regression"),
    ("payment_failed", "2026-09-01T00:00:00Z", False, "demoted after repeated failures"),
    ("canceled",       None, False, "canceled"),
    ("unpaid",         None, False, "unpaid"),
    ("past_due",       None, True,  "unknown status is not a demotion"),
    ("trialing",       None, True,  "trialing"),
    (None,             None, True,  "status never set — the plan stands"),
    ("",               None, True,  "empty status — the plan stands"),
])
def test_users_leg_follows_the_authority(status, demoted, paid, why):
    cur = _Cur(_user(status=status, demoted_at=demoted))
    plan_tier, _api_tier, _metered = fme._tier_cross_check(cur, KEY, EMAIL)

    # It must AGREE with the authority rather than reimplement it.
    expected = resolve_effective_plan("pro", status or "", "", demoted)
    assert plan_tier == expected.lower(), why
    assert (plan_tier == "pro") is paid, why


def test_the_dunning_customer_is_served_paid_at_the_node_gate():
    """The outcome that matters: an mcp_dev_keys row lagging at 'free', NO
    api_keys row, and a users.plan of 'pro' inside the dunning window."""
    cur = _Cur(_user(status="payment_failed"), api_key_tier=None)
    plan_tier, api_key_tier, _ = fme._tier_cross_check(cur, KEY, EMAIL)

    assert api_key_tier is None, "this cohort has no api_keys row — that is the point"
    assert fme._node_tier_max(["free", plan_tier, api_key_tier]) == "paid"


def test_a_canceled_account_is_not_widened_to_paid():
    cur = _Cur(_user(status="canceled"), api_key_tier=None)
    plan_tier, api_key_tier, _ = fme._tier_cross_check(cur, KEY, EMAIL)
    assert fme._node_tier_max(["free", plan_tier, api_key_tier]) == "free"


def test_a_demote_is_legible_rather_than_looking_like_a_missing_row():
    """tier_detail.users_plan is surfaced in the validate response. 'free'
    (this account resolves to free) must not read the same as None (no
    such user) — that distinction is the only demote signal the MCP has."""
    demoted, _a, _m = fme._tier_cross_check(
        _Cur(_user(status="payment_failed", demoted_at="2026-09-01T00:00:00Z")),
        KEY, EMAIL)
    missing, _a, _m = fme._tier_cross_check(_Cur(None), KEY, EMAIL)
    assert demoted == "free"
    assert missing is None


def test_an_admin_is_not_dropped_by_the_authority():
    """resolve_effective_plan answers 'admin', which _node_tier_max maps to
    enterprise. Carrying the raw plan instead would silently demote them."""
    cur = _Cur(_user(plan="pro", status="canceled", role="admin"))
    plan_tier, _a, _m = fme._tier_cross_check(cur, KEY, EMAIL)
    assert plan_tier == "admin"
    assert fme._node_tier_max([plan_tier]) == "enterprise"


def test_the_users_select_no_longer_carries_its_own_allowlist():
    cur = _Cur(_user())
    fme._tier_cross_check(cur, KEY, EMAIL)
    users_sql = [s for s in cur.statements if "FROM users" in s]
    assert len(users_sql) == 1
    s = users_sql[0]
    assert "'trialing'" not in s, "the rule is resolve_effective_plan's, not this SQL's"
    assert "demoted_at" in s, "the authority needs the stamp to decide"


# ── the other legs still work ────────────────────────────────────────────

def test_the_api_keys_leg_is_untouched():
    cur = _Cur(user=None, api_key_tier="enterprise")
    plan_tier, api_key_tier, _ = fme._tier_cross_check(cur, KEY, EMAIL)
    assert plan_tier is None
    assert api_key_tier == "enterprise"


def test_no_email_skips_the_users_leg_entirely():
    cur = _Cur(_user(), api_key_tier="founding")
    plan_tier, api_key_tier, _ = fme._tier_cross_check(cur, KEY, None)
    assert plan_tier is None
    assert not [s for s in cur.statements if "FROM users" in s]
    assert api_key_tier == "founding"


# ── fail-soft ────────────────────────────────────────────────────────────

def test_a_broken_authority_falls_back_to_the_old_allowlist(monkeypatch):
    """Fail CLOSED to the previous behaviour — under-grant, never over-grant."""
    import builtins
    real = builtins.__import__

    def no_auth(name, *a, **k):
        if name == "api_tier_gating":
            raise ImportError("unavailable")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_auth)

    ok, _a, _m = fme._tier_cross_check(_Cur(_user(status="active")), KEY, EMAIL)
    assert ok == "pro", "an ordinary paid customer must survive the fallback"

    dunning, _a, _m = fme._tier_cross_check(
        _Cur(_user(status="payment_failed")), KEY, EMAIL)
    assert dunning is None, "a broken import must not WIDEN the grant"
