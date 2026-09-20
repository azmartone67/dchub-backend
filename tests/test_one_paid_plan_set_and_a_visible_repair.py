"""One paid-plan set, and an admin repair that reports what it achieved.

★ THREE TYPED SETS, THREE POPULATIONS. Measured on origin/main 2026-09-20:

  activation_nudge.PAID_PLANS      starter developer pro paid enterprise founding
  keys_recover.py (inline SQL)     starter developer pro     enterprise founding
  mcp_key_email_verification       .       developer pro     enterprise founding

Every one omits `team` and `research_seed`, which tier_registry.TIERS marks
paid. That is not cosmetic: keys_recover's paid branch routes a customer AWAY
from the mail that sends a key in the clear under the free-tier template, so a
`team` customer missed their own branch — the same defect #4877 fixed for the
dunning window, still live for them. And a `starter` customer could recover a
key (in keys_recover's set) yet could NOT confirm a binding to apply the plan
they pay for (absent from the other), falling between two hand-typed lists.

★ THE REPAIR THAT DID NOT REPAIR. /api/v1/admin/entitlements/repair wrote
`plan` alone, while resolve_effective_plan reads plan AND subscription_status
AND demoted_at. After #4877/#4903 both self-serve restores follow the
authority, so a repaired account could still resolve to 'free' and stay locked
out, with nothing in the response saying so.
"""
import json

import pytest

import tier_registry
from tier_registry import TIERS, paid_plan_names
import routes.keys_recover as kr
import routes.mcp_key_email_verification as v


# ── the derived set ──────────────────────────────────────────────────────

def test_it_is_exactly_the_registry_paid_tiers_minus_admin():
    """Stated over TIERS, so a tier added there is covered without editing
    this test — the failure mode that produced three divergent lists."""
    expected = tuple(sorted(
        n for n, spec in TIERS.items() if spec.get('paid') and n != 'admin'))
    assert paid_plan_names() == expected


@pytest.mark.parametrize("plan", ["team", "research_seed", "starter"])
def test_the_plans_every_hand_typed_list_missed_are_present(plan):
    assert TIERS[plan]["paid"] is True
    assert plan in paid_plan_names()


def test_admin_is_excluded_because_it_is_a_role_not_a_purchased_plan():
    assert TIERS["admin"]["paid"] is True
    assert "admin" not in paid_plan_names()


@pytest.mark.parametrize("plan", ["free", "anon", "anonymous", "identified"])
def test_unpaid_tiers_stay_out(plan):
    assert plan not in paid_plan_names()


def test_it_is_sorted_so_the_sql_it_feeds_is_stable():
    got = paid_plan_names()
    assert list(got) == sorted(got)


# ── both consumers read it ───────────────────────────────────────────────

def test_keys_recover_uses_the_derived_set():
    assert kr._paid_plans() == paid_plan_names()


def test_confirm_binding_uses_the_derived_set():
    assert tuple(v._PAID_PLANS) == paid_plan_names()


@pytest.mark.parametrize("plan", ["team", "research_seed", "starter"])
def test_a_previously_stranded_plan_now_takes_the_paid_branch(plan, monkeypatch):
    """keys_recover: the paid branch is what withholds the free-tier template."""
    from tests.test_recovery_never_mails_a_paid_key import _Conn, EMAIL, PAID_SUBJECTS
    sent = []
    monkeypatch.setattr(kr, "_send", lambda to, s_, h: sent.append((to, s_, h)) or True)
    got = kr._lookup_and_send(
        EMAIL,
        connect=lambda: _Conn({"plan": plan, "status": "active",
                               "mcp_key": "dch_live_K"}))
    assert got == "sent"
    assert sent and sent[0][1] in PAID_SUBJECTS, sent


@pytest.mark.parametrize("plan", ["team", "research_seed", "starter"])
def test_a_previously_stranded_plan_can_now_confirm(plan):
    from tests.test_confirm_binding_follows_entitlement import _Cur, KEY, EMAIL
    assert v.confirmation_is_pending(
        _Cur({"plan": plan, "status": "active", "role": None, "demoted": None}),
        KEY, EMAIL) is True


@pytest.mark.parametrize("mod,fallback", [
    (kr, ('pro', 'founding', 'enterprise', 'starter', 'developer')),
    (v, ("developer", "pro", "founding", "enterprise")),
])
def test_each_consumer_fails_closed_to_its_own_old_tuple(mod, fallback, monkeypatch):
    """A broken registry import must protect who it protected before, not nobody."""
    import builtins
    real = builtins.__import__

    def no_registry(name, *a, **k):
        if name == "tier_registry":
            raise ImportError("unavailable")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_registry)
    assert mod._paid_plans() == fallback


def test_an_empty_registry_answer_does_not_empty_the_set(monkeypatch):
    """`() ` would silently unprotect EVERY paid account — the worst direction."""
    monkeypatch.setattr(tier_registry, "paid_plan_names", lambda: ())
    assert kr._paid_plans(), "empty set must fall back, not publish"
    assert v._paid_plans(), "empty set must fall back, not publish"


# ── the repair reports what it achieved ──────────────────────────────────

import routes.account_entitlements as ae   # noqa: E402

ADMIN = "test-admin-key"


class _RepairCur:
    """Records every statement; serves the users row from `account`."""

    def __init__(self, account, statements):
        self.a, self.st, self._row = account, statements, None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.st.append((s, params))
        if s.startswith("CREATE TABLE") or s.startswith("INSERT INTO"):
            return
        if "SELECT id, plan FROM users" in s:
            self._row = (7, self.a["plan"]); return
        if s.startswith("UPDATE users"):
            # apply what the statement actually says, so the read-back is real
            if "demoted_at=NULL" in s.replace(" ", ""):
                self.a["demoted"] = None
            self.a["plan"] = params[0]
            return
        if s.startswith("UPDATE api_keys"):
            self.rowcount = 2; return
        if "SELECT plan, subscription_status, role, demoted_at" in s:
            self._row = (self.a["plan"], self.a["status"],
                         self.a.get("role"), self.a.get("demoted")); return
        raise AssertionError("unmodelled SQL: %r" % s)

    rowcount = 0

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RepairConn:
    def __init__(self, account, statements):
        self.a, self.st = account, statements

    def cursor(self):
        return _RepairCur(self.a, self.st)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def repair_client(monkeypatch):
    from flask import Flask
    monkeypatch.setenv("DCHUB_ADMIN_KEY", ADMIN)
    monkeypatch.setattr(ae, "_DDL_DONE", [True])
    app = Flask(__name__)
    app.register_blueprint(ae.account_entitlements_bp)

    def run(account):
        statements = []
        monkeypatch.setattr(ae, "_conn", lambda: _RepairConn(account, statements))
        with app.test_client() as c:
            r = c.post("/api/v1/admin/entitlements/repair",
                       headers={"X-Admin-Key": ADMIN},
                       json={"email": "payer@example.com", "plan": "pro"})
        return r, json.loads(r.data), statements
    return run


def test_repair_clears_demoted_at(repair_client):
    acct = {"plan": "free", "status": "payment_failed",
            "role": None, "demoted": "2026-09-01T00:00:00Z"}
    r, body, st = repair_client(acct)
    assert r.status_code == 200, body
    upd = [s for s, _p in st if s.startswith("UPDATE users")]
    assert upd and "demoted_at=NULL" in upd[0].replace(" ", ""), upd
    assert body["effective_plan"] == "pro"
    assert body["took_effect"] is True
    assert "warning" not in body


def test_repair_never_writes_billing_state(repair_client):
    """subscription_status is owned by the Stripe webhooks. Writing it here
    would be this service asserting a payment it knows nothing about."""
    _r, _body, st = repair_client(
        {"plan": "free", "status": "canceled", "role": None, "demoted": None})
    for s, _p in st:
        if s.startswith("UPDATE users"):
            assert "subscription_status" not in s, s


def test_a_repair_that_does_not_take_effect_says_so(repair_client):
    """The whole point: a canceled account still resolves to free, and an admin
    must SEE that rather than believe the repair worked."""
    r, body, _st = repair_client(
        {"plan": "free", "status": "canceled", "role": None, "demoted": None})
    assert r.status_code == 200
    assert body["plan"] == "pro"
    assert body["effective_plan"] == "free"
    assert body["took_effect"] is False
    assert "canceled" in body["warning"]
    assert "remain closed" in body["warning"]


def test_effective_plan_is_read_back_not_assumed(repair_client):
    """It must come from the row after the write. If it were echoed from the
    request it could never disagree, and the warning could never fire."""
    _r, body, st = repair_client(
        {"plan": "free", "status": "unpaid", "role": None, "demoted": None})
    assert any("SELECT plan, subscription_status, role, demoted_at" in s
               for s, _p in st), st
    assert body["effective_plan"] == "free" != body["plan"]
