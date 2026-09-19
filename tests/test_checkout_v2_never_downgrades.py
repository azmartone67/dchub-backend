"""The /api/v2/stripe/webhook checkout writer may raise a plan, never lower one.

WHY THIS EXISTS — the sibling of tests/test_checkout_never_downgrades.py.
That test pins the floor on main.py's handle_checkout_completed, added by
#4724 after a real $10.88 credit-pack purchase moved admin001 pro -> starter
on 2026-09-17 18:35:34 and took 24 api_keys rows down with it.

`users.plan` is written on a checkout by TWO live routes, and only one was
floored:

    /api/v1/stripe/webhook, /api/stripe/webhook -> main.py
        handle_checkout_completed                  (floored, #4724)
    /api/v2/stripe/webhook                      -> api_tier_gating.py
        _handle_checkout_v2                        (unfloored until now)

Both are registered in the running app: init_tier_gating (main.py) calls
register_stripe_v2_routes (api_tier_gating.py), and both answered 405 to GET
in production on 2026-09-19 — routed, not dead code.

The v2 vector is WIDER than the one that fired. _map_stripe_plan_to_tier
returns 'pro' for any unrecognised plan key, so a checkout whose
metadata.plan is absent or renamed demotes an enterprise account with no bad
amount band involved. The last test pins that the subscription handlers stay
UNfloored, so this guard can never be widened into one that strands a churned
account on a paid tier.
"""
import pytest

import api_tier_gating as atg
from plan_write_floor import keep_higher_plan, plan_rank, would_downgrade


# ── the pure floor ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("held,incoming,expected", [
    ("pro",        "starter",    "pro"),         # the incident's shape
    ("pro",        "developer",  "pro"),
    ("enterprise", "pro",        "enterprise"),  # the v2-only vector
    ("founding",   "starter",    "founding"),
    ("pro",        "founding",   "founding"),    # equal rank -> incoming wins
    ("free",       "starter",    "starter"),     # a real upgrade still lands
    ("identified", "pro",        "pro"),
    ("",           "starter",    "starter"),     # nothing held -> incoming
    (None,         "pro",        "pro"),
])
def test_keep_higher_plan(held, incoming, expected):
    assert keep_higher_plan(held, incoming) == expected


def test_unknown_incoming_plan_cannot_outrank_a_held_plan():
    """An unrecognised plan ranks -1, so it never wins against a real plan."""
    assert plan_rank("nonsense-tier") == -1
    assert keep_higher_plan("pro", "nonsense-tier") == "pro"
    assert would_downgrade("pro", "nonsense-tier") is True


# ── the handler, against a cursor that answers the SQL it is given ──────────

class _Cur:
    """A cursor that reads the SQL, not a cursor that returns a canned row.

    A fake that answered every fetchone() with the plan row would pass this
    test with the floor deleted, because the UPDATE would still be recorded.
    This one only answers the SELECT, and records executed statements so the
    assertion can read the plan actually written.
    """

    def __init__(self, held_plan, row_style):
        self._held = held_plan
        self._row_style = row_style
        self._last = ""
        self.executed = []

    def execute(self, sql, params=()):
        self._last = " ".join(sql.split())
        self.executed.append((self._last, params))

    def fetchone(self):
        if self._last.startswith("SELECT plan FROM users"):
            if self._held is None:
                return None
            return ({"plan": self._held} if self._row_style == "dict"
                    else (self._held,))
        return None

    def close(self):
        pass


class _Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def close(self):
        pass


def _run_checkout(monkeypatch, held_plan, plan_key, row_style="tuple"):
    """Run _handle_checkout_v2 and return the plan its UPDATE wrote."""
    cur = _Cur(held_plan, row_style)
    monkeypatch.setattr(atg, "get_db", lambda: _Conn(cur))
    provisioned = []
    monkeypatch.setattr(atg, "_auto_provision_api_key",
                        lambda email, plan: provisioned.append(plan))

    atg._handle_checkout_v2({
        "customer_email": "Holder@Example.com",
        "customer": "cus_test123",
        "metadata": {"plan": plan_key},
    })

    writes = [(s, p) for s, p in cur.executed
              if s.startswith("UPDATE users SET plan")]
    assert len(writes) == 1, f"expected exactly one plan write, got {writes}"
    return writes[0][1][0], provisioned


@pytest.mark.parametrize("row_style", ["tuple", "dict"])
def test_checkout_v2_does_not_demote_pro_to_developer(monkeypatch, row_style):
    """developer_monthly (rank 3) must not overwrite a held pro (rank 4)."""
    written, provisioned = _run_checkout(
        monkeypatch, "pro", "developer_monthly", row_style)
    assert written == "pro"
    # the key is provisioned at the KEPT tier, not the demoted one
    assert provisioned == ["pro"]


def test_checkout_v2_unknown_plan_key_does_not_demote_enterprise(monkeypatch):
    """The v2-only vector: unknown key -> 'pro' would demote enterprise."""
    assert atg._map_stripe_plan_to_tier("some_renamed_sku") == "pro"
    written, _ = _run_checkout(monkeypatch, "enterprise", "some_renamed_sku")
    assert written == "enterprise"


def test_checkout_v2_still_upgrades(monkeypatch):
    """The floor must not block a genuine upgrade."""
    written, provisioned = _run_checkout(
        monkeypatch, "free", "enterprise_monthly")
    assert written == "enterprise"
    assert provisioned == ["enterprise"]


def test_checkout_v2_writes_incoming_when_no_user_row(monkeypatch):
    """No row to compare against -> fail-soft, provisioning still works."""
    written, _ = _run_checkout(monkeypatch, None, "pro_monthly")
    assert written == "pro"


def test_checkout_v2_reads_the_held_plan_before_writing(monkeypatch):
    """The SELECT must actually happen — a floor that never reads is inert."""
    cur = _Cur("pro", "tuple")
    monkeypatch.setattr(atg, "get_db", lambda: _Conn(cur))
    monkeypatch.setattr(atg, "_auto_provision_api_key", lambda *a: None)
    atg._handle_checkout_v2({
        "customer_email": "holder@example.com",
        "customer": "cus_x",
        "metadata": {"plan": "developer_monthly"},
    })
    kinds = [s.split()[0] for s, _ in cur.executed]
    assert kinds[0] == "SELECT", f"SELECT must precede the write, got {kinds}"
    assert "UPDATE" in kinds


# ── the counter-guard: subscription downgrades must still demote ───────────

def test_subscription_handlers_are_not_floored():
    """A churned account must still fall. If someone 'fixes' the sub handlers
    by importing the floor into them, this fails — on purpose."""
    import inspect
    for fn_name in ("_handle_sub_updated_v2", "_handle_sub_deleted_v2"):
        src = inspect.getsource(getattr(atg, fn_name))
        assert "keep_higher_plan" not in src, (
            f"{fn_name} must NOT be floored — a real paid downgrade arrives "
            f"here and has to land, or a churned account is stranded on a "
            f"paid tier.")
